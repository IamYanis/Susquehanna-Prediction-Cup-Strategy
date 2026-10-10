"""Policy-halt recovery uses fake GETs and temporary checkpoints only."""
import copy
import json
import unittest
from unittest.mock import patch

import requests
import live_pilot as pilot
import pilot_account
import recover_settlement_halt as recovery
import test_manual_autonomous_settlement as fixtures
from test_account_reader import holdings, order, position


class SettlementHaltRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManualAutonomousSettlementTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.session = self.fixture.session
        self.path = self.fixture.fixture.path
        for name, value in (("EXPECTED_PAIRS", (("1", "2"),)),
                            ("POLICY_HASH", self.fixture.approval["policy_content_sha256"])):
            patcher = patch.object(recovery, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Build the same zero-debit pre-entry halt in a disposable test file.
        # A real recovery never creates a baseline or edits state this way.
        cp = self.fixture.fixture.checkpoint()
        snapshot = pilot_account.read_snapshot(self.session, checkpoint=cp, order_ids=[])
        cp.update(baseline_snapshot=snapshot, baseline_snapshot_hash=pilot.snapshot_hash(snapshot),
                  state=pilot.HALTED, manual_review_required=True, review_reason=recovery.HALT_REASON)
        pilot.validate_checkpoint(cp)
        self.path.write_text(json.dumps(cp))
        self.before = self.path.read_bytes()
        self.original = copy.deepcopy(cp)
        self.session.reset_mock()
        self.session.post.side_effect = AssertionError("Recovery must never POST")

    def checkpoint(self):
        return pilot._read_checkpoint_locked(self.path.resolve())

    def recover(self, apply=False):
        result = recovery.recover(self.session, apply=apply)
        self.fixture.assert_no_post()
        return result

    def test_dry_run_changes_no_runtime_bytes_or_allocation(self):
        result = self.recover()
        self.assertEqual(result["result"], "DRY_RUN_PASS")
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(result["before"]["state"], pilot.HALTED)
        self.assertEqual(result["after"]["state"], pilot.READY)
        for key in ("confirmed_cumulative_debits", "quarantine_reserve", "reserved_unconfirmed_capital",
                    "calculated_remaining_allocation"):
            self.assertEqual(result["before"][key], result["after"][key])

    def test_explicit_apply_preserves_halt_audit_baseline_allocation_and_restart(self):
        result = self.recover(apply=True)
        cp = pilot.load_checkpoint()
        self.assertEqual(result["result"], "RECOVERED")
        self.assertEqual(cp["state"], pilot.READY)
        self.assertFalse(cp["manual_review_required"])
        for key in self.original:
            if key not in {"state", "manual_review_required", "review_reason", "revision", "updated_at"}:
                self.assertEqual(cp[key], self.original[key], key)
        record = cp["settlement_halt_recoveries"][0]
        self.assertEqual(record["prior_halt_reason"], recovery.HALT_REASON)
        self.assertEqual(record["prior_checkpoint_hash"], pilot.snapshot_hash(self.original))
        self.assertTrue(record["reads"])
        self.assertEqual(record["approvals"], [self.fixture.approval])
        self.assertEqual(pilot.load_checkpoint(), cp)
        self.fixture.assert_no_post()

    def test_ordinary_save_cannot_clear_halt_or_append_recovery_audit(self):
        captured = []
        original = recovery.proposed_checkpoint
        def capture(*args):
            value = original(*args)
            captured.append(copy.deepcopy(value))
            return value
        with patch.object(recovery, "proposed_checkpoint", side_effect=capture):
            self.recover()
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(captured[0])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_applied_recovery_cannot_repeat_or_erase_audit(self):
        self.recover(apply=True)
        before = self.path.read_bytes()
        with self.assertRaises(pilot.PilotBlocked):
            self.recover(apply=True)
        cp = self.checkpoint()
        del cp["settlement_halt_recoveries"]
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(cp)
        self.assertEqual(self.path.read_bytes(), before)

    def test_changed_policy_or_market_root_keeps_halt(self):
        self.fixture.policy = self.fixture.policy.replace(b"refund policy.", b"new payout rule.")
        with self.assertRaises(ValueError):
            self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.fixture.assert_no_post()

    def test_changed_account_cash_positions_orders_or_history_keeps_halt(self):
        account = self.fixture.fixture.fixture
        original = copy.deepcopy(account.current)
        for case in ("cash", "position", "order", "fills", "transactions"):
            account.current = copy.deepcopy(original)
            account.fills, account.transactions = [], []
            if case == "cash":
                account.current["tournament"]["myBalance"] += 1
            elif case == "position":
                account.current.update(holdings([position("11", "1", -1)]))
            elif case == "order":
                account.current["orders"] = [order(301, "11")]
            elif case == "fills":
                account.fills = [{"id": 501, "orderId": 301, "exchangeId": "11", "marketId": "1",
                    "side": "no", "quantity": -1, "price": .4, "filledAt": account.stamp}]
            else:
                account.transactions = [{"event_id": "new-transaction", "event_type": "trade", "createdAt": account.stamp,
                    "tournamentId": self.original["tournament_id"], "exchangeId": "11", "marketId": "1",
                    "price": .4, "quantity": -1, "amount": None, "transactionType": None}]
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.recover(apply=True)
            self.assertEqual(self.path.read_bytes(), self.before)
        self.fixture.assert_no_post()

    def test_other_halt_or_any_unconfirmed_reservation_cannot_use_recovery(self):
        for change in ({"review_reason": "Unknown submission"}, {"reserved_unconfirmed_capital": "0.115"},
                       {"confirmed_cumulative_debits": "0.01"}, {"autonomous_execution": {"active_attempt": "unknown"}}):
            cp = copy.deepcopy(self.original)
            cp.update(change)
            with self.subTest(change=change), self.assertRaises(pilot.PilotBlocked):
                recovery.eligible_checkpoint(cp)

    def test_get_timeout_leaves_halt_unchanged(self):
        self.session.get.side_effect = requests.Timeout("Synthetic GET failure")
        with self.assertRaises(ValueError):
            self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.fixture.assert_no_post()


if __name__ == "__main__":
    unittest.main()
