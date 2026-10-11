"""Interruption recovery uses synthetic GETs and disposable state only."""
import copy
import json
import unittest
from unittest.mock import patch

import requests

import autonomous_pilot as auto
import live_pilot as pilot
import recover_position_interruption as recovery
import test_manual_autonomous_settlement as fixtures
from test_account_reader import holdings, orders_page, order


class PositionInterruptionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManualAutonomousSettlementTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.session, self.path = self.fixture.session, self.fixture.fixture.path
        self.assertEqual(auto.execute_candidate(self.session, ["1", "2"])["state"], pilot.READY)
        cp = pilot.halted_result(self.fixture.fixture.checkpoint(), recovery.HALT_REASON)["checkpoint"]
        cp["revision"] = recovery.TARGET_REVISION
        pilot.validate_checkpoint(cp)
        self.path.write_text(json.dumps(cp))
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
        self.session.delete.assert_not_called()
        return result

    def test_dry_run_checks_orders_and_authorizations_without_writing(self):
        result = self.recover()
        self.assertEqual(result["result"], "DRY_RUN_PASS")
        self.assertEqual(result["after"]["state"], pilot.READY)
        self.assertEqual(result["after"]["revision"], 291)
        self.assertEqual(result["orders_verified"], 2)
        self.assertEqual(result["authorizations_verified"], 1)
        self.assertFalse(result["state_written"])
        self.assertEqual(self.path.read_bytes(), self.before)
        for key in ("confirmed_cumulative_debits", "quarantine_reserve", "reserved_unconfirmed_capital",
                    "calculated_remaining_allocation"):
            self.assertEqual(result["before"][key], result["after"][key])

    def test_apply_archives_only_this_halt_and_preserves_positions_and_allocation(self):
        self.assertEqual(self.recover(apply=True)["result"], "RECOVERED")
        cp = pilot.load_checkpoint()
        self.assertEqual(cp["state"], pilot.READY)
        self.assertEqual(cp["revision"], 291)
        self.assertFalse(cp["manual_review_required"])
        for key in self.original:
            if key not in {"state", "manual_review_required", "review_reason", "revision", "updated_at"}:
                self.assertEqual(cp[key], self.original[key], key)
        record = cp[recovery.AUDIT_KEY][0]
        self.assertEqual(record["prior_halt_reason"], recovery.HALT_REASON)
        self.assertEqual(record["prior_checkpoint_hash"], pilot.snapshot_hash(self.original))
        self.assertEqual(pilot.load_checkpoint(), cp)
        self.assertEqual(pilot.save_checkpoint(cp)[recovery.AUDIT_KEY], [record])

    def test_ordinary_save_cannot_clear_halt_or_create_audit(self):
        captured, original = [], recovery.proposed_checkpoint
        def capture(*args):
            cp = original(*args)
            captured.append(copy.deepcopy(cp))
            return cp
        with patch.object(recovery, "proposed_checkpoint", side_effect=capture):
            self.recover()
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(captured[0])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_recovery_cannot_repeat_or_erase_its_audit(self):
        self.recover(apply=True)
        before = self.path.read_bytes()
        with self.assertRaises(pilot.PilotBlocked):
            self.recover(apply=True)
        for change in ("delete", "modify"):
            cp = pilot.load_checkpoint()
            if change == "delete":
                del cp[recovery.AUDIT_KEY]
            else:
                cp[recovery.AUDIT_KEY][0]["prior_halt_reason"] = "Other reason"
            with self.subTest(change=change), self.assertRaises(pilot.PilotBlocked):
                pilot.save_checkpoint(cp)
            self.assertEqual(self.path.read_bytes(), before)

    def test_changed_reason_revision_allocation_or_quarantine_is_rejected(self):
        for fields in ({"review_reason": "Other interruption"}, {"revision": 291},
                       {"confirmed_cumulative_debits": "4"}, {"quarantine_reserve": ".125"}):
            cp = copy.deepcopy(self.original)
            cp.update(fields)
            with self.subTest(fields=fields), self.assertRaises(pilot.PilotBlocked):
                recovery.eligible_checkpoint(cp)
        cp = copy.deepcopy(self.original)
        cp["autonomous_execution"]["active_attempt"] = next(iter(cp["autonomous_execution"]["attempts"]))
        with self.assertRaises(pilot.PilotBlocked):
            recovery.eligible_checkpoint(cp)
        cp = copy.deepcopy(self.original)
        next(iter(cp["autonomous_positions"].values()))["exit_execution"] = {"legs": []}
        with self.assertRaises(pilot.PilotBlocked):
            recovery.eligible_checkpoint(cp)

    def test_changed_cash_holdings_orders_fills_or_transactions_keep_halt(self):
        original = copy.deepcopy((self.account.current, self.account.fills, self.account.transactions))
        for case in ("cash", "holding", "open_order", "fill", "new_transaction", "removed_transaction", "financial_change"):
            self.account.current, self.account.fills, self.account.transactions = copy.deepcopy(original)
            if case == "cash":
                self.account.current["tournament"]["myBalance"] -= .1
            elif case == "holding":
                self.account.current.update(holdings(self.account.current["positions"][:1]))
            elif case == "open_order":
                self.account.current["orders"] = [order(999, "11")]
            elif case == "fill":
                self.account.fills[0]["price"] += .005
            elif case == "new_transaction":
                row = copy.deepcopy(self.account.transactions[0])
                row["event_id"] = "unexpected-event"
                self.account.transactions.insert(0, row)
            elif case == "removed_transaction":
                self.account.transactions.pop()
            else:
                self.account.transactions[0]["price"] += .005
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.recover(apply=True)
            self.assertEqual(self.path.read_bytes(), self.before)

    def test_mutable_marks_do_not_block_economically_identical_account(self):
        self.account.current["positions"][0]["currentPrice"] += .01
        self.account.transactions[0]["currentPrice"] = .123
        self.assertEqual(self.recover()["result"], "DRY_RUN_PASS")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_new_closed_order_or_changed_settlement_keeps_halt(self):
        oid = next(iter(self.account.receipts))
        row = copy.deepcopy(self.account.receipts[oid][0])
        row["id"] = 999
        self.account.receipts[999] = row, {}
        with self.assertRaises(ValueError):
            self.recover(apply=True)
        del self.account.receipts[999]
        self.fixture.policy = self.fixture.policy.replace(b"refund policy.", b"changed settlement rule.")
        with self.assertRaises(ValueError):
            self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_stale_reads_or_api_failure_do_not_clear_halt(self):
        error = auto.pilot_account.AccountReadinessBlocked(auto.pilot_account.STALE, "Slow capture")
        with patch.object(auto.pilot_account, "check_fresh", side_effect=error), self.assertRaises(ValueError):
            self.recover(apply=True)
        self.session.get.side_effect = requests.Timeout("Synthetic timeout")
        with self.assertRaises(ValueError):
            self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
