"""Fake orders and temporary checkpoints only; no real recovery or submission."""
import copy
import unittest
from decimal import Decimal
from urllib.parse import urlparse
from unittest.mock import patch

import autonomous_pilot as auto
import live_pilot as pilot
import pilot_account
import recover_completed_addon as recovery
import test_autonomous_lifecycle as lifecycle
from test_account_reader import holdings, orders_page
from test_api_audit import exchange_book


def stale_after_second(original):
    def reconcile(reads, state, index):
        snapshot = original(reads, state, index)
        if index == 1:
            # Model the final GET delay after the individual leg has passed.
            snapshot = copy.deepcopy(snapshot)
            snapshot["freshness"]["started_monotonic"] -= 20
            snapshot["freshness"]["completed_monotonic"] -= 20
        return snapshot
    return reconcile


class AddonFixture(unittest.TestCase):
    def setUp(self):
        self.life = lifecycle.AutonomousLifecycleTests()
        self.addCleanup(self.life.doCleanups)
        self.life.setUp()
        self.fixture, self.session = self.life.fixture, self.life.session
        self.path = self.fixture.path
        self.books(1)
        result = self.life.cycle()
        self.assertEqual(result["state"], pilot.READY)
        self.root = result["entry"]["execution_id"]
        self.original = copy.deepcopy(self.fixture.checkpoint())
        self.books(2)

    def books(self, sequence):
        for mid, eid, price in ((1, 11, ".295"), (2, 12, ".690")):
            book = exchange_book(mid, eid, bid=float(Decimal(1) - Decimal(price)), ask=.995)
            book["asOf"]["sequence"] = sequence
            self.fixture.books[str(eid)] = book


class FinalSnapshotRefreshTests(AddonFixture):
    def finish(self, mutate=None):
        original = auto.reconcile_leg
        def delayed(reads, state, index):
            snapshot = stale_after_second(original)(reads, state, index)
            if index == 1 and mutate:
                mutate()
            return snapshot
        with patch.object(auto, "reconcile_leg", side_effect=delayed):
            return self.life.cycle()

    def test_stale_final_snapshot_refreshes_and_merges_unchanged_account(self):
        with patch.object(pilot_account, "read_snapshot", wraps=pilot_account.read_snapshot) as snapshots:
            result = self.finish()
        self.assertEqual(result["state"], pilot.READY, result.get("reason"))
        self.assertEqual(snapshots.call_args_list[-2].kwargs.get("order_ids"), [])
        cp = self.fixture.checkpoint()
        p = cp["autonomous_positions"][self.root]
        self.assertEqual(p["quantity"], 2)
        self.assertEqual(p["remaining_quantities"], ["2", "2"])
        self.assertEqual(Decimal(p["total_entry_cost"]), Decimal("1.970"))
        self.assertEqual(list(map(Decimal, p["average_entry_prices"])), [Decimal(".295"), Decimal(".690")])
        pilot_account.check_fresh(p["last_snapshot"]["freshness"]["started_monotonic"])
        self.assertEqual(len(self.life.posts), 4)  # Two original + two add-on POSTs only.
        self.assertEqual(cp["autonomous_execution"]["attempts"][self.root], self.original["autonomous_execution"]["attempts"][self.root])

    def test_refreshed_snapshot_new_financial_activity_halts(self):
        def fee():
            ledger = self.fixture.fixture.transactions
            ledger.insert(0, {"event_id": "new-fee", "event_type": "fee", "createdAt": ledger[0]["createdAt"],
                "tournamentId": ledger[0]["tournamentId"], "quantity": 0, "amount": -.01, "transactionType": "FEE"})
        result = self.finish(fee)
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("Account changed during final reconciliation", result["reason"])
        self.assertEqual(self.fixture.checkpoint()["autonomous_positions"][self.root]["quantity"], 1)
        self.assertEqual(len(self.life.posts), 4)

    def test_refreshed_holdings_mismatch_halts(self):
        def change():
            account = self.fixture.fixture.current
            account["positions"][0]["quantity"] = -1
            account.update(holdings(account["positions"]))
        result = self.finish(change)
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("Account changed during final reconciliation", result["reason"])
        self.assertEqual(len(self.life.posts), 4)

    def test_fresh_final_snapshot_does_not_fetch_an_extra_snapshot(self):
        with patch.object(auto, "require_unchanged_final_account") as comparison:
            result = self.life.cycle()
        self.assertEqual(result["state"], pilot.READY)
        comparison.assert_not_called()
        self.assertEqual(len(self.life.posts), 4)


class CompletedAddonRecoveryTests(AddonFixture):
    def setUp(self):
        super().setUp()
        # Reproduce the historical age-only halt after both reviews passed.
        with patch.object(auto, "reconcile_leg", side_effect=stale_after_second(auto.reconcile_leg)), \
             patch.object(auto, "require_unchanged_final_account", side_effect=pilot_account.AccountReadinessBlocked(
                 pilot_account.STALE, "Account read exceeded the existing 15-second freshness window")):
            result = self.life.cycle()
        self.assertEqual(result["reason"], recovery.HALT_REASON)
        self.cp = self.fixture.checkpoint()
        self.key = self.cp["autonomous_execution"]["active_attempt"]
        a = self.cp["autonomous_execution"]["attempts"][self.key]
        target = {"attempt": self.key, "parent": self.root, "revision": self.cp["revision"],
                  "markets": ["1", "2"], "exchanges": ["11", "12"], "orders": [1003, 1004],
                  "fills": [2003, 2004], "transactions": ["trade-2003", "trade-2004"],
                  "prices": [Decimal(".295"), Decimal(".690")], "cash": Decimal(self.cp["last_reconciled_account_cash"])}
        patcher = patch.object(recovery, "TARGET", target)
        patcher.start()
        self.addCleanup(patcher.stop)
        get = self.fixture.get
        def all_orders(url, **kwargs):
            if urlparse(url).path.endswith("/orders") and kwargs.get("params", {}).get("status") == "all":
                return self.fixture.base.response(orders_page([r[0] for r in self.fixture.fixture.receipts.values()], coverage={"complete": True}))
            return get(url, **kwargs)
        self.session.get.side_effect = all_orders
        self.session.post.reset_mock()
        self.session.post.side_effect = AssertionError("Recovery cannot submit or retry")

    def test_dry_run_proves_merge_and_writes_nothing(self):
        before = self.path.read_bytes()
        result = recovery.recover(self.session, self.key)
        self.assertEqual(result["result"], "DRY_RUN_PASS")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(result["after"]["state"], pilot.READY)
        self.assertEqual(result["after"]["revision"], self.cp["revision"] + 1)
        self.assertEqual(result["position"]["quantity"], 2)
        self.assertEqual(Decimal(result["position"]["total_entry_cost"]), Decimal("1.970"))
        self.assertEqual(result["after"]["confirmed_cumulative_debits"], self.cp["confirmed_cumulative_debits"])
        self.assertEqual(result["after"]["quarantine_reserve"], self.cp["quarantine_reserve"])
        self.assertEqual(Decimal(result["after"]["reserved_unconfirmed_capital"]), 0)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_apply_to_temporary_state_preserves_full_history_and_weighted_costs(self):
        recovery.recover(self.session, self.key, apply=True)
        cp = pilot.load_checkpoint()
        old = self.cp["autonomous_execution"]["attempts"][self.key]
        current = cp["autonomous_execution"]["attempts"][self.key]
        self.assertEqual(current["legs"], old["legs"])
        self.assertEqual(current["before"], old["before"])
        self.assertEqual(current["after"], old["after"])
        self.assertEqual(current["reads"][:len(old["reads"])], old["reads"])
        self.assertEqual(current["stages"][:len(old["stages"])], old["stages"])
        self.assertEqual(cp["autonomous_execution"]["attempts"][self.root], self.cp["autonomous_execution"]["attempts"][self.root])
        self.assertEqual(cp["accounted_pair_costs"], self.cp["accounted_pair_costs"])
        self.assertEqual(cp["confirmed_cumulative_debits"], self.cp["confirmed_cumulative_debits"])
        p = cp["autonomous_positions"][self.root]
        self.assertEqual(p["execution_ids"], [self.root, self.key])
        self.assertEqual(p["per_leg_quantities"], ["2", "2"])
        self.assertEqual(list(map(Decimal, p["per_leg_costs"])), [Decimal(".590"), Decimal("1.380")])
        self.assertEqual(Decimal(p["total_entry_cost"]), Decimal("1.970"))
        self.assertEqual(Decimal(p["average_pair_cost"]), Decimal(".985"))
        self.assertEqual(p["actual_entry_prices"], self.cp["autonomous_positions"][self.root]["actual_entry_prices"])
        self.assertIsNone(cp["autonomous_execution"]["active_attempt"])
        self.assertEqual(cp["state"], pilot.READY)
        self.assertFalse(cp["manual_review_required"])
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key, apply=True)

    def test_partial_leg_refuses_without_writes(self):
        before = self.path.read_bytes()
        order, fills = self.fixture.fixture.receipts[1004]
        order["quantityFilled"] = .5
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key, apply=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.session.post.assert_not_called()

    def test_only_one_confirmed_leg_refuses(self):
        bad = copy.deepcopy(self.cp)
        bad["autonomous_execution"]["attempts"][self.key]["legs"][1]["intent"] = None
        with self.assertRaises(ValueError):
            recovery.eligible_attempt(bad, self.key)
        self.session.post.assert_not_called()

    def test_duplicate_or_additional_fill_refuses(self):
        before = self.path.read_bytes()
        for fid in (2004, 9999):
            rows = self.fixture.fixture.fills
            rows.insert(0, dict(rows[0], id=fid))
            try:
                with self.subTest(fill=fid), self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), before)
            finally:
                rows.pop(0)
        self.session.post.assert_not_called()

    def test_cash_holdings_and_transaction_changes_refuse(self):
        account = self.fixture.fixture
        cases = [(account.current["tournament"], "myBalance", 1),
                 (account.current["positions"][0], "quantity", -1),
                 (account.transactions[0], "price", .695)]
        before = self.path.read_bytes()
        for row, key, value in cases:
            old = row[key]
            try:
                row[key] = value
                with self.subTest(field=key), self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), before)
            finally:
                row[key] = old
        self.session.post.assert_not_called()

    def test_added_transaction_refuses(self):
        rows = self.fixture.fixture.transactions
        rows.insert(0, dict(rows[0], event_id="extra"))
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key, apply=True)
        self.session.post.assert_not_called()

    def test_mutable_marks_are_allowed(self):
        self.fixture.fixture.current["positions"][0]["currentPrice"] = .42
        self.fixture.fixture.transactions[0]["currentPrice"] = .123
        result = recovery.recover(self.session, self.key)
        self.assertEqual(result["result"], "DRY_RUN_PASS")

    def test_changed_authorization_refuses(self):
        self.fixture.relationships = []
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key, apply=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.session.post.assert_not_called()

    def test_ordinary_save_cannot_clear_this_halt(self):
        with pilot.pilot_lock() as path:
            cp = pilot._read_checkpoint_locked(path)
            cp.update(state=pilot.READY, manual_review_required=False, review_reason="")
            with self.assertRaises(ValueError):
                pilot.save_checkpoint(cp)


if __name__ == "__main__":
    unittest.main()
