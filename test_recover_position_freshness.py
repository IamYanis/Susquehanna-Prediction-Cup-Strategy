"""Freshness recovery: synthetic GETs and disposable state/approvals only."""
import copy
import json
import unittest
from unittest.mock import patch

import requests

import autonomous_pilot as auto
import live_pilot as pilot
import recover_position_freshness as recovery
import test_manual_autonomous_settlement as fixtures
from test_account_reader import holdings, orders_page, order


class PositionFreshnessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManualAutonomousSettlementTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.session = self.fixture.session
        self.path = self.fixture.fixture.path
        result = auto.execute_candidate(self.session, ["1", "2"])
        self.assertEqual(result["state"], pilot.READY, result)
        cp = self.fixture.fixture.checkpoint()
        cp = pilot.halted_result(cp, recovery.HALT_REASON)["checkpoint"]
        cp["revision"] = recovery.TARGET_REVISION
        pilot.validate_checkpoint(cp)
        self.path.write_text(json.dumps(cp))  # Disposable fixture, never real state.
        self.original, self.before = copy.deepcopy(cp), self.path.read_bytes()
        target = patch.object(recovery, "TARGET_CHECKPOINT_HASH", pilot.snapshot_hash(cp))
        target.start()
        self.addCleanup(target.stop)
        self.account = self.fixture.fixture.fixture
        self.session.get.side_effect = self.get
        self.session.reset_mock()
        self.session.post.side_effect = AssertionError("Recovery cannot submit an order")

    def get(self, url, **kwargs):
        if url.endswith("/orders") and kwargs.get("params", {}).get("status") == "all":
            rows = [r[0] for r in self.account.receipts.values()]
            return self.fixture.fixture.base.response(orders_page(rows, coverage={"complete": True}))
        return self.fixture.get(url, **kwargs)

    def recover(self, apply=False):
        result = recovery.recover(self.session, apply=apply)
        self.fixture.assert_no_post()
        return result

    def test_dry_run_proves_receipts_and_positions_without_writing(self):
        result = self.recover()
        self.assertEqual(result["result"], "DRY_RUN_PASS")
        self.assertEqual(result["after"]["state"], pilot.READY)
        self.assertEqual(result["after"]["revision"], 222)
        self.assertFalse(result["state_written"])
        self.assertEqual(result["orders_verified"], 2)
        self.assertEqual(result["authorizations_verified"], 1)
        self.assertEqual(self.path.read_bytes(), self.before)
        for key in ("confirmed_cumulative_debits", "quarantine_reserve", "reserved_unconfirmed_capital",
                    "calculated_remaining_allocation"):
            self.assertEqual(result["before"][key], result["after"][key])

    def test_apply_only_clears_exact_halt_preserves_positions_and_survives_restart(self):
        self.assertEqual(self.recover(apply=True)["result"], "RECOVERED")
        cp = pilot.load_checkpoint()
        self.assertEqual(cp["state"], pilot.READY)
        self.assertFalse(cp["manual_review_required"])
        for key in self.original:
            if key not in {"state", "manual_review_required", "review_reason", "revision", "updated_at"}:
                self.assertEqual(cp[key], self.original[key], key)
        record = cp["position_freshness_recoveries"][0]
        self.assertEqual(record["prior_halt_reason"], recovery.HALT_REASON)
        self.assertEqual(record["prior_checkpoint_hash"], pilot.snapshot_hash(self.original))
        self.assertEqual(pilot.load_checkpoint(), cp)
        # Archived recovery evidence is stable during normal later writes.
        self.assertEqual(pilot.save_checkpoint(cp)["position_freshness_recoveries"], [record])

    def test_ordinary_save_cannot_clear_halt_or_add_recovery_audit(self):
        captured = []
        original = recovery.proposed_checkpoint
        def capture(*args):
            cp = original(*args)
            captured.append(copy.deepcopy(cp))
            return cp
        with patch.object(recovery, "proposed_checkpoint", side_effect=capture):
            self.recover()
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(captured[0])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_recovery_cannot_repeat_and_audit_cannot_be_deleted_or_modified(self):
        self.recover(apply=True)
        before = self.path.read_bytes()
        with self.assertRaises(pilot.PilotBlocked):
            self.recover(apply=True)
        for change in ("delete", "modify"):
            cp = pilot.load_checkpoint()
            if change == "delete":
                del cp["position_freshness_recoveries"]
            else:
                cp["position_freshness_recoveries"][0]["recovered_at"] = "2026-10-09T12:00:00+00:00"
            with self.subTest(change=change), self.assertRaises(pilot.PilotBlocked):
                pilot.save_checkpoint(cp)
            self.assertEqual(self.path.read_bytes(), before)

    def test_other_halt_revision_unfinished_attempt_or_reserve_is_blocked(self):
        cases = ({"review_reason": "Unknown execution"}, {"revision": 222},
                 {"quarantine_reserve": ".125"}, {"reserved_unconfirmed_capital": ".04"})
        for fields in cases:
            cp = copy.deepcopy(self.original)
            cp.update(fields)
            with self.subTest(fields=fields), self.assertRaises(pilot.PilotBlocked):
                recovery.eligible_checkpoint(cp)
        cp = copy.deepcopy(self.original)
        cp["autonomous_execution"]["active_attempt"] = next(iter(cp["autonomous_execution"]["attempts"]))
        with self.assertRaises(pilot.PilotBlocked):
            recovery.eligible_checkpoint(cp)

    def test_changed_cash_holdings_orders_fills_or_transactions_keep_halt(self):
        original = copy.deepcopy((self.account.current, self.account.fills, self.account.transactions))
        for case in ("cash", "holding", "open_order", "fill", "added_transaction", "removed_transaction", "financial_transaction"):
            self.account.current, self.account.fills, self.account.transactions = copy.deepcopy(original)
            if case == "cash":
                self.account.current["tournament"]["myBalance"] -= .1
            elif case == "holding":
                self.account.current.update(holdings(self.account.current["positions"][:1]))
            elif case == "open_order":
                self.account.current["orders"] = [order(999, "11")]
            elif case == "fill":
                self.account.fills[0]["price"] += .005
            elif case == "added_transaction":
                row = copy.deepcopy(self.account.transactions[0])
                row["event_id"] = "unexplained-new-event"
                self.account.transactions.insert(0, row)
            elif case == "removed_transaction":
                self.account.transactions.pop()
            else:
                self.account.transactions[0]["price"] += .005
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.recover(apply=True)
            self.assertEqual(self.path.read_bytes(), self.before)
            self.fixture.assert_no_post()

    def test_mark_only_refresh_does_not_block_recovery(self):
        self.account.current["positions"][0]["currentPrice"] += .01
        self.account.transactions[0]["currentPrice"] = .123
        self.assertEqual(self.recover()["result"], "DRY_RUN_PASS")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_changed_terminal_order_or_missing_saved_fill_keeps_halt(self):
        original = copy.deepcopy(self.account.receipts)
        oid = next(iter(original))
        for case in ("order_price", "expiry", "missing_fill", "new_closed_order"):
            self.account.receipts = copy.deepcopy(original)
            if case == "order_price":
                self.account.receipts[oid][0]["priceLimit"] += .005
            elif case == "expiry":
                self.account.receipts[oid][0]["expirationDate"] = "2026-10-11T12:00:00+00:00"
            elif case == "missing_fill":
                self.account.receipts[oid][1]["data"] = []
            else:
                row = copy.deepcopy(self.account.receipts[oid][0])
                row["id"] = 999
                self.account.receipts[999] = row, {}
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.recover(apply=True)
            self.assertEqual(self.path.read_bytes(), self.before)

    def test_changed_settlement_evidence_keeps_halt(self):
        self.fixture.policy = self.fixture.policy.replace(b"refund policy.", b"changed settlement rule.")
        with self.assertRaises(ValueError):
            self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_stale_account_capture_or_get_failure_keeps_halt(self):
        with patch.object(recovery.pilot_account, "check_fresh", side_effect=
                         recovery.pilot_account.AccountReadinessBlocked(recovery.pilot_account.STALE, "Slow capture")):
            with self.assertRaises(ValueError):
                self.recover(apply=True)
        self.session.get.side_effect = requests.Timeout("Synthetic timeout")
        with self.assertRaises(ValueError):
            self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.fixture.assert_no_post()


if __name__ == "__main__":
    unittest.main()
