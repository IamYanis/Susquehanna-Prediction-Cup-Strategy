"""Recovery uses fake GETs and temporary state; no real account writes/orders."""
import copy
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import autonomous_pilot as auto
import live_pilot as pilot
import pilot_account
import recover_rejected_attempt as recovery
import test_autonomous_pilot as fixtures
from test_account_reader import holdings, order, orders_page, position
from test_api_audit import exchange_book


class RejectedRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.AutonomousPilotTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.account = fixture.fixture
        self.session, self.path = fixture.session, fixture.path
        # Match the actual failed attempt's .075 + .040 reservation. The
        # quarantine itself is isolated; only its conservative cost is mocked.
        with patch.object(pilot.quarantine, "reserved_cost", return_value=.125):
            cp = pilot.load_checkpoint()
            cp["quarantine_reserve"] = "0.125"
            pilot.refresh_totals(cp)
            pilot.save_checkpoint(cp)
        self.reserve_patch = patch.object(pilot.quarantine, "reserved_cost", return_value=.125)
        self.reserve_patch.start()
        self.addCleanup(self.reserve_patch.stop)
        fixture.books = {"11": exchange_book(1, 11, bid=.925, ask=.93),
                         "12": exchange_book(2, 12, bid=.1, ask=.105)}
        self.error_payload = {"error": {"code": "INSUFFICIENT_SCOPES", "message": "Insufficient scopes. Required: trade",
                              "details": {"required": ["trade"], "missing": ["trade"]}}}
        def rejected_post(url, **kwargs):
            response = fixture.base.response(self.error_payload)
            response.status_code = 403
            return response
        self.session.post.side_effect = rejected_post
        result = fixture.execute()
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("HTTP 403 INSUFFICIENT_SCOPES", result["reason"])
        self.assertIn("trade scope", result["reason"])
        self.session.post.assert_called_once()
        self.key = self.checkpoint()["autonomous_execution"]["active_attempt"]
        self.before = self.path.read_bytes()
        self.session.reset_mock()
        # Subsequent recovery must have no possible submission call.
        self.session.post.side_effect = AssertionError("Recovery must never POST")

    def checkpoint(self):
        return pilot._read_checkpoint_locked(self.path.resolve())

    def assert_no_writes(self):
        for method in ("post", "delete", "put", "patch"):
            getattr(self.session, method).assert_not_called()

    def recover(self, apply=False):
        result = recovery.recover(self.session, self.key, apply=apply)
        self.assert_no_writes()
        return result

    def test_dry_run_preserves_all_runtime_bytes_and_predicts_exact_release(self):
        result = self.recover()
        self.assertEqual(result["result"], "DRY_RUN_PASS")
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(Decimal(result["released_reservation"]), Decimal(".115"))
        self.assertEqual(result["before"]["state"], pilot.HALTED)
        self.assertEqual(result["after"]["state"], pilot.READY)
        self.assertEqual(Decimal(result["after"]["quarantine_reserve"]), Decimal(".125"))
        self.assertEqual(Decimal(result["after"]["calculated_remaining_allocation"]), Decimal("4999.875"))

    def test_apply_preserves_original_evidence_retires_key_and_survives_restart(self):
        original = self.checkpoint()
        result = self.recover(apply=True)
        cp = pilot.load_checkpoint()
        self.assertEqual(result["result"], "RECOVERED")
        self.assertEqual(cp["state"], pilot.READY)
        self.assertFalse(cp["manual_review_required"])
        self.assertIsNone(cp["autonomous_execution"]["active_attempt"])
        old = original["autonomous_execution"]["attempts"][self.key]
        saved = cp["autonomous_execution"]["attempts"][self.key]
        for field in ("legs", "before", "reads", "halt_reason", "halted_from", "authorization", "initial_prices"):
            self.assertEqual(saved[field], old[field])
        self.assertEqual(saved["state"], pilot.REJECTED_RETIRED)
        record = saved["rejection_recovery"]
        self.assertEqual(record["intent_status"], "RETIRED_NEVER_RETRY")
        self.assertEqual(record["retired_idempotency_key"], old["legs"][0]["intent"]["request"]["idempotencyKey"])
        self.assertEqual(record["reserved_exposure_before"], original["live_exposures"][self.key])
        self.assertTrue(record["snapshot"]["data_complete"])
        self.assertTrue(record["reads"])
        self.assertEqual(cp["confirmed_cumulative_debits"], "0")
        self.assertEqual(Decimal(cp["quarantine_reserve"]), Decimal(".125"))
        self.assertEqual(Decimal(cp["reserved_unconfirmed_capital"]), 0)
        self.assertEqual(Decimal(cp["configured_allocation"]), 5000)
        pilot.require_execution_clear(cp)
        # Retirement includes uniqueness checks for all subsequent intents.
        old_hex = record["retired_idempotency_key"].removeprefix("account-test-")
        with patch.object(auto, "uuid4", return_value=SimpleNamespace(hex=old_hex)):
            with self.assertRaisesRegex(pilot.PilotBlocked, "Duplicate idempotency"):
                auto.new_intent(cp, self.fixture.approval, 0, .075)
        self.assert_no_writes()

    def test_normal_save_cannot_clear_halt_even_with_valid_recovery_preview(self):
        proposed = []
        real = recovery.proposed_checkpoint
        def capture(*args):
            value = real(*args)
            proposed.append(copy.deepcopy(value))
            return value
        with patch.object(recovery, "proposed_checkpoint", side_effect=capture):
            self.recover()
        with self.assertRaisesRegex(pilot.PilotBlocked, "cannot be cleared"):
            pilot.save_checkpoint(proposed[0])
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_applied_recovery_cannot_run_twice_or_delete_retired_history(self):
        self.recover(apply=True)
        saved = self.path.read_bytes()
        with self.assertRaises(pilot.PilotBlocked):
            self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), saved)
        cp = self.checkpoint()
        cp["autonomous_execution"]["attempts"] = {}
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(cp)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_unexpected_order_on_either_pair_instrument_blocks(self):
        original_get = self.session.get.side_effect
        for eid in ("11", "12"):
            with self.subTest(exchange=eid):
                found = order(999, eid)
                found.update(open=False, createdAt=datetime.now(timezone.utc).isoformat())
                def get(url, **kwargs):
                    if url.endswith("/orders") and kwargs.get("params", {}).get("status") == "all":
                        return self.fixture.base.response(orders_page([found], coverage={"complete": True}))
                    return original_get(url, **kwargs)
                with patch.object(self.session, "get", side_effect=get):
                    with self.assertRaisesRegex(pilot.PilotBlocked, "order exists"):
                        self.recover(apply=True)
                self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_writes()

    def test_balance_position_fill_or_transaction_change_blocks(self):
        original_current = copy.deepcopy(self.account.current)
        changes = (
            ("balance", lambda: self.account.current["tournament"].update(myBalance=19999.99)),
            ("position", lambda: self.account.current.update(holdings([position("11", "1", -1)]))),
            ("fill", lambda: self.account.fills.append({"id": 1001, "orderId": 999, "exchangeId": "11", "marketId": "1",
                    "side": "no", "quantity": -1, "price": .075, "filledAt": datetime.now(timezone.utc).isoformat()})),
            ("transaction", lambda: self.account.transactions.append({"event_id": "fee-999", "event_type": "fee",
                    "createdAt": datetime.now(timezone.utc).isoformat(), "tournamentId": self.checkpoint()["tournament_id"],
                    "quantity": 0, "amount": -.01})),
        )
        for name, change in changes:
            with self.subTest(condition=name):
                self.account.current = copy.deepcopy(original_current)
                self.account.fills, self.account.transactions = [], []
                change()
                with self.assertRaises(pilot.PilotBlocked):
                    self.recover(apply=True)
                self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_writes()

    def test_incomplete_history_unavailable_api_and_stale_data_keep_halt(self):
        original_get = self.session.get.side_effect
        def incomplete(url, **kwargs):
            if url.endswith("/orders") and kwargs.get("params", {}).get("status") == "all":
                return self.fixture.base.response(orders_page([], coverage={"complete": False}))
            return original_get(url, **kwargs)
        with patch.object(self.session, "get", side_effect=incomplete):
            with self.assertRaisesRegex(pilot.PilotBlocked, "coverage"):
                self.recover(apply=True)
        self.account.failed_path = "/tournaments/midterm-elections/portfolio/transactions"
        with self.assertRaises(pilot_account.AccountReadinessBlocked):
            self.recover(apply=True)
        self.account.failed_path = None
        with patch.object(pilot_account, "check_fresh", side_effect=pilot_account.AccountReadinessBlocked("STALE", "stale")):
            with self.assertRaises(pilot_account.AccountReadinessBlocked):
                self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_writes()

    def test_generic_403_timeout_receipt_and_wrong_attempt_are_not_recoverable(self):
        original = self.checkpoint()
        for condition in ("403", "timeout", "receipt", "wrong_id"):
            with self.subTest(condition=condition):
                cp = copy.deepcopy(original)
                first = auto.active_attempt(cp)["legs"][0]
                if condition == "403":
                    first["placement_response"]["body"] = '{"error":{"code":"ACCESS_DENIED"}}'
                elif condition == "timeout":
                    first["placement_response"] = None
                elif condition == "receipt":
                    first["receipt"] = {"orderId": 999}
                with self.assertRaises(pilot.PilotBlocked):
                    recovery.eligible_attempt(cp, "0" * 64 if condition == "wrong_id" else self.key)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_external_unresolved_execution_blocks_before_any_get(self):
        with patch.object(pilot, "require_external_execution_clear", side_effect=pilot.PilotBlocked("Unresolved execution")):
            with self.assertRaisesRegex(pilot.PilotBlocked, "Unresolved"):
                self.recover(apply=True)
        self.session.get.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_failed_atomic_write_keeps_halt_reservation_and_history(self):
        with patch.object(pilot.os, "replace", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaisesRegex(pilot.PilotBlocked, "state write failed"):
                self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.checkpoint()["state"], pilot.HALTED)
        self.assertEqual(list(self.path.parent.glob("." + self.path.name + ".*.tmp")), [])
        self.assert_no_writes()

    def test_retired_history_and_zero_exposure_cannot_be_altered(self):
        self.recover(apply=True)
        saved = self.path.read_bytes()
        cp = self.checkpoint()
        cp["autonomous_execution"]["attempts"][self.key]["reads"] = []
        with self.assertRaisesRegex(pilot.PilotBlocked, "retired execution evidence changed"):
            pilot.save_checkpoint(cp)
        cp = self.checkpoint()
        cp["live_exposures"][self.key]["possible_additional_quantities"] = ["1", "0"]
        pilot.refresh_totals(cp)
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(cp)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_lock_conflict_blocks_recovery_and_leaves_state_unchanged(self):
        with pilot.pilot_lock():
            with self.assertRaises(OSError):
                self.recover(apply=True)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assert_no_writes()

    def test_scope_error_is_specific_but_generic_403_stays_ambiguous(self):
        evidence = auto.active_attempt(self.checkpoint())["legs"][0]["placement_response"]
        with self.assertRaisesRegex(pilot.PilotBlocked, "HTTP 403 INSUFFICIENT_SCOPES.*trade scope"):
            auto.require_trade_scope_response(evidence)
        evidence["body"] = '{"error":{"code":"ACCESS_DENIED"}}'
        self.assertFalse(auto.missing_trade_scope(evidence))
        auto.require_trade_scope_response(evidence)


if __name__ == "__main__":
    unittest.main()
