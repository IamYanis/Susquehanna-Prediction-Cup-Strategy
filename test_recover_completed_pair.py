"""Mocked Colorado finalization; all state and API replies are disposable."""
import copy
import unittest
from decimal import Decimal
from unittest.mock import patch

import autonomous_pilot as auto
import live_pilot as pilot
import recover_completed_pair as recovery
import recover_filled_leg1 as first_recovery
import supervised_accounting_probe as probe
import test_recover_filled_leg1 as fixtures
from test_supervised_accounting_probe import zero_collateral


class CompletedPairRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.first = fixtures.FilledLegRecoveryTests()
        self.addCleanup(self.first.doCleanups)
        self.first.setUp()
        self.fixture, self.path, self.session, self.key = self.first.fixture, self.first.path, self.first.session, self.first.key
        first_recovery.recover(self.session, self.key, apply=True)
        # Reproduce the already-filled second leg and the old collateral check.
        def second_post(url, **kwargs):
            response = self.fixture.post(url, **kwargs)
            receipt = response.json()
            receipt["all"] = zero_collateral(.9)
            account = self.fixture.fixture
            account.current["positions"][1]["avgCost"] = .9
            return self.fixture.base.response(receipt)
        self.session.post.side_effect = second_post
        with patch.object(probe, "ordinary_cash_receipt", return_value=False):
            result = auto.resume_recovered_leg1(self.session, self.key)
        self.assertEqual(result["reason"], recovery.HALT_REASON)
        self.cp = pilot._read_checkpoint_locked(self.path.resolve())
        target = {"attempt": self.key, "markets": ["1", "2"], "exchanges": ["11", "12"],
                  "orders": [301, 302], "fills": [501, 502], "prices": [Decimal(".075"), Decimal(".900")],
                  "displayed_debit": Decimal(".980")}
        patcher = patch.object(recovery, "TARGET", target)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session.post.reset_mock()
        self.session.post.side_effect = AssertionError("Final recovery must never submit/retry an order")

    def test_dry_run_keeps_runtime_bytes_and_existing_fills_unchanged(self):
        before = self.path.read_bytes()
        result = recovery.recover(self.session, self.key)
        self.assertEqual(result["result"], "DRY_RUN_PASS")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(result["after"]["state"], pilot.READY)
        self.assertEqual(result["after"]["revision"], self.cp["revision"] + 1)
        self.assertEqual(Decimal(result["accounting"]["pair_notional"]), Decimal(".975"))
        self.assertEqual(Decimal(result["accounting"]["displayed_pair_debit"]), Decimal(".98"))
        self.assertEqual(Decimal(result["accounting"]["difference"]), Decimal(".005"))
        self.assertEqual(Decimal(result["after"]["confirmed_cumulative_debits"]), 1)
        self.assertEqual(Decimal(result["after"]["reserved_unconfirmed_capital"]), 0)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_apply_preserves_history_and_registers_normal_open_position(self):
        recovery.recover(self.session, self.key, apply=True)
        restored = pilot.load_checkpoint()
        old = self.cp["autonomous_execution"]["attempts"][self.key]
        saved = restored["autonomous_execution"]["attempts"][self.key]
        for previous_leg, leg in zip(old["legs"], saved["legs"]):
            for field in ("intent", "receipt", "activity", "placement_response", "post_attempted"):
                self.assertEqual(leg[field], previous_leg[field])
        self.assertEqual(saved["filled_leg1_recovery"], old["filled_leg1_recovery"])
        self.assertEqual(saved["final_reconciliation_recovery"]["original_leg2_review"], old["legs"][1]["review"])
        self.assertEqual(saved["stages"][:len(old["stages"])], old["stages"])
        self.assertEqual(saved["reads"][:len(old["reads"])], old["reads"])
        self.assertEqual(restored["state"], pilot.READY)
        self.assertIsNone(restored["autonomous_execution"]["active_attempt"])
        self.assertEqual(restored["quarantine_reserve"], self.cp["quarantine_reserve"])
        exposure = restored["live_exposures"][self.key]
        self.assertEqual(exposure["confirmed_quantities"], self.cp["live_exposures"][self.key]["confirmed_quantities"])
        self.assertEqual(exposure["execution_status"], "RECONCILED_PAIR")
        p = restored["autonomous_positions"][self.key]
        self.assertEqual(p["status"], "OPEN")
        self.assertEqual(list(map(Decimal, p["actual_entry_prices"])), [Decimal(".075"), Decimal(".9")])
        self.assertEqual(Decimal(p["total_entry_cost"]), Decimal(".975"))
        self.assertEqual(Decimal(p["entry_edge"]), Decimal(".025"))
        self.assertEqual(p["remaining_quantities"], ["1", "1"])
        self.assertEqual(Decimal(restored["allocated_cash_remaining"]), Decimal(4999))
        self.assertEqual(Decimal(restored["last_reconciled_account_cash"]), Decimal("19999.02"))
        with self.assertRaises(pilot.PilotBlocked):
            auto.resume_recovered_leg1(self.session, self.key)
        with self.assertRaises(pilot.PilotBlocked):
            recovery.recover(self.session, self.key, apply=True)
        self.session.post.assert_not_called()

    def test_completed_pair_can_hold_and_duplicate_entry_remains_blocked(self):
        recovery.recover(self.session, self.key, apply=True)
        with pilot.pilot_lock() as path:
            cp = pilot._read_checkpoint_locked(path)
            result = auto.manage_positions_locked(self.session, cp)
            self.assertEqual(result["positions"][0]["action"], "HOLD")
            with self.assertRaises(ValueError):
                auto.fresh_candidate(self.session, ["1", "2"], pilot._read_checkpoint_locked(path))
        self.session.post.assert_not_called()

    def test_changed_execution_or_cash_evidence_blocks_without_writes(self):
        account = self.fixture.fixture
        cases = [(account.receipts[302][0], "open", True),
                 (account.receipts[302][0], "priceLimit", .905),
                 (account.receipts[302][1]["data"][0], "quantity", -.5),
                 (account.receipts[302][1]["data"][0], "id", 999),
                 (account.current["positions"][1], "quantity", -2),
                 (account.current["tournament"], "myBalance", 19999),
                 (account.transactions[0], "price", .905)]
        before = self.path.read_bytes()
        for container, field, value in cases:
            saved = container[field]
            try:
                container[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), before)
            finally:
                container[field] = saved
        self.session.post.assert_not_called()

    def test_nonzero_or_incomplete_receipt_blocks_finalization(self):
        before = self.path.read_bytes()
        attempt = copy.deepcopy(self.cp["autonomous_execution"]["attempts"][self.key])
        record = recovery.recover(self.session, self.key)
        self.assertEqual(record["result"], "DRY_RUN_PASS")
        for key in zero_collateral(.9):
            bad = copy.deepcopy(attempt)
            del bad["legs"][1]["receipt"]["all"][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                recovery.corrected_review(bad)
        bad = copy.deepcopy(attempt)
        bad["legs"][1]["receipt"]["all"]["collateralRepayment"] = .1
        with self.assertRaises(ValueError):
            recovery.corrected_review(bad)
        self.assertEqual(self.path.read_bytes(), before)

    def test_extra_fee_or_unrelated_trade_blocks(self):
        account = self.fixture.fixture
        before = self.path.read_bytes()
        for extra in ({"event_id": "fee", "event_type": "fee", "createdAt": account.transactions[0]["createdAt"],
                       "tournamentId": self.cp["tournament_id"], "quantity": 0, "amount": -.01, "transactionType": "FEE"},
                      dict(account.transactions[0], event_id="extra", exchangeId="99", marketId="99")):
            account.transactions.append(extra)
            try:
                with self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), before)
            finally:
                account.transactions.pop()
        self.session.post.assert_not_called()

    def test_mutable_display_metadata_does_not_hide_changed_accounting(self):
        self.fixture.fixture.transactions[0].update(currentPrice=.123, marketTitle="Display refresh")
        self.assertEqual(recovery.recover(self.session, self.key)["result"], "DRY_RUN_PASS")
        self.fixture.fixture.transactions[0]["quantity"] = -2
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key)
        self.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
