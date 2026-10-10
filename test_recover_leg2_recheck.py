"""A consumed first recovery cannot replay; a new verified permission is one-use."""
import copy
import unittest
from decimal import Decimal
from unittest.mock import patch

import autonomous_pilot as auto
import live_pilot as pilot
import recover_filled_leg1 as filled
import recover_leg2_recheck as recovery
import test_recover_alaska_leg1 as fixtures
from test_autonomous_pilot import InjectedCrash


class Leg2RecheckRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.first = fixtures.AlaskaLegRecoveryTests()
        self.addCleanup(self.first.doCleanups)
        self.first.setUp()
        self.fixture, self.session, self.path, self.key = self.first.fixture, self.first.session, self.first.path, self.first.key
        rows = self.fixture.fixture.transactions
        rows[0]["currentPrice"] = .710
        filled.recover(self.session, self.key, apply=True)
        rows[0]["currentPrice"] = .705
        # Reproduce the former raw-dictionary guard, without any real APIs.
        with patch.object(auto, "account_unchanged_after_leg1", return_value=False):
            report = auto.resume_recovered_leg1(self.session, self.key)
        self.assertEqual(report["reason"], recovery.HALT_REASON)
        self.cp = pilot._read_checkpoint_locked(self.path.resolve())
        cash = Decimal(str(self.cp["last_reconciled_account_cash"]))
        patcher = patch.object(recovery, "EXPECTED_CASH", cash)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session.post.reset_mock()
        self.session.delete.reset_mock()

    def test_dry_run_does_not_write_or_submit(self):
        raw = self.path.read_bytes()
        result = recovery.recover(self.session, self.key)
        self.assertEqual(result["result"], "DRY_RUN_PASS")
        self.assertEqual(result["after"]["state"], pilot.LEG2_RECHECK)
        self.assertEqual(result["after"]["revision"], self.cp["revision"] + 1)
        self.assertIsNone(result["leg_two_intent"])
        self.assertIsNone(result["continuation_consumed_at"])
        self.assertEqual(self.path.read_bytes(), raw)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_apply_preserves_original_consumed_permission_and_colorado(self):
        result = recovery.recover(self.session, self.key, apply=True)
        saved = pilot._read_checkpoint_locked(self.path.resolve())
        original = auto.active_attempt(self.cp)
        current = auto.active_attempt(saved)
        self.assertEqual(current["filled_leg1_recovery"], original["filled_leg1_recovery"])
        self.assertEqual(current["legs"], original["legs"])
        self.assertEqual(current["after"], original["after"])
        self.assertEqual(current["reads"][:len(original["reads"])], original["reads"])
        self.assertEqual(current["stages"][:len(original["stages"])], original["stages"])
        self.assertEqual(saved["confirmed_cumulative_debits"], self.cp["confirmed_cumulative_debits"])
        self.assertEqual(saved["quarantine_reserve"], self.cp["quarantine_reserve"])
        other = self.first.completed_key
        for field in ("autonomous_positions", "live_exposures", "accounted_pair_costs"):
            self.assertEqual(saved[field][other], self.cp[field][other])
        self.assertEqual(saved["autonomous_execution"]["attempts"][other], self.cp["autonomous_execution"]["attempts"][other])
        with self.assertRaises(pilot.PilotBlocked):
            filled.require_resume(saved, self.key)
        recovery.require_resume(saved, self.key, result["continuation_permission"])
        self.session.post.assert_not_called()

    def test_new_permission_submits_only_leg_two_once_after_fresh_quote(self):
        report = recovery.recover(self.session, self.key, apply=True)
        self.session.post.side_effect = self.first.rounded_post
        self.session.get.reset_mock()
        permission = report["continuation_permission"]
        result = auto.resume_recovered_leg1(self.session, self.key, permission)
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(self.session.post.call_count, 1)
        body = self.session.post.call_args.kwargs["json"]
        self.assertEqual(body["exchangeId"], "14")
        self.assertNotEqual(body["idempotencyKey"], auto.active_attempt(self.cp)["legs"][0]["intent"]["request"]["idempotencyKey"])
        self.assertTrue(any(c.args[0].endswith('/exchanges/14/orderbook') for c in self.session.get.call_args_list))
        saved = self.fixture.checkpoint()
        attempt = saved["autonomous_execution"]["attempts"][self.key]
        self.assertEqual(Decimal(attempt["completion"]["actual_leg1_notional"]), Decimal(".295"))
        self.assertIsNotNone(attempt["leg2_recheck_recovery"]["consumed_at"])
        self.assertEqual(attempt["filled_leg1_recovery"], auto.active_attempt(self.cp)["filled_leg1_recovery"])
        with self.assertRaises(pilot.PilotBlocked):
            auto.resume_recovered_leg1(self.session, self.key, permission)
        self.assertEqual(self.session.post.call_count, 1)

    def test_wrong_permission_and_repeated_recovery_do_not_write(self):
        report = recovery.recover(self.session, self.key, apply=True)
        raw = self.path.read_bytes()
        for action in (lambda: auto.resume_recovered_leg1(self.session, self.key, "wrong"),
                       lambda: auto.resume_recovered_leg1(self.session, self.key),
                       lambda: recovery.recover(self.session, self.key, apply=True)):
            with self.assertRaises(pilot.PilotBlocked):
                action()
            self.assertEqual(self.path.read_bytes(), raw)
        self.session.post.assert_not_called()

    def test_cash_execution_or_other_position_changes_prevent_recovery(self):
        fixture = self.fixture.fixture
        baseline = self.path.read_bytes()
        positions = fixture.current["positions"]
        alaska = next(p for p in positions if p["exchangeId"] == "13")
        colorado = next(p for p in positions if p["exchangeId"] == "11")
        for container, field, value in ((fixture.current["tournament"], "myBalance", 19998),
                (alaska, "quantity", -2), (colorado, "avgCost", .5),
                (fixture.transactions[0], "price", .300), (fixture.transactions[0], "orderId", 999),
                (fixture.fills[0], "quantity", -.5), (fixture.receipts[1003][0], "priceLimit", .300)):
            old = container.get(field)
            present = field in container
            try:
                container[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), baseline)
            finally:
                if present:
                    container[field] = old
                else:
                    del container[field]
        self.session.post.assert_not_called()

    def test_added_removed_duplicate_transactions_or_leg2_fill_prevent_recovery(self):
        fixture = self.fixture.fixture
        raw = self.path.read_bytes()
        original = copy.deepcopy(fixture.transactions)
        for rows in (original + [dict(original[0], event_id="new")], original[1:], original + [original[0]]):
            fixture.transactions[:] = copy.deepcopy(rows)
            with self.assertRaises(ValueError):
                recovery.recover(self.session, self.key, apply=True)
            self.assertEqual(self.path.read_bytes(), raw)
        fixture.transactions[:] = original
        fixture.fills.insert(0, dict(fixture.fills[0], id=2004, orderId=1004, marketId="4", exchangeId="14"))
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key, apply=True)
        self.assertEqual(self.path.read_bytes(), raw)
        self.session.post.assert_not_called()

    def test_restart_preserves_audit_and_halts_without_automatic_resume(self):
        report = recovery.recover(self.session, self.key, apply=True)
        restored = pilot.load_checkpoint()
        self.assertEqual(restored["state"], pilot.HALTED)
        self.assertEqual(auto.active_attempt(restored)["filled_leg1_recovery"], auto.active_attempt(self.cp)["filled_leg1_recovery"])
        self.assertIsNone(auto.active_attempt(restored)["leg2_recheck_recovery"]["consumed_at"])
        with self.assertRaises(pilot.PilotBlocked):
            auto.resume_recovered_leg1(self.session, self.key, report["continuation_permission"])
        self.session.post.assert_not_called()

    def test_second_holding_open_order_or_new_unfilled_order_prevents_recovery(self):
        from test_account_reader import holdings, position
        fixture = self.fixture.fixture
        raw = self.path.read_bytes()
        current = copy.deepcopy(fixture.current)
        row = position("14", "4", -1)
        row.update(avgCost=.69, costBasis=.69, marketValue=.69, unrealizedPnl=0)
        fixture.current.update(holdings(fixture.current["positions"] + [row]))
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key, apply=True)
        fixture.current.clear()
        fixture.current.update(copy.deepcopy(current))
        new_order = dict(fixture.receipts[1003][0], id=1004, exchangeId="14", quantityFilled=0)
        fixture.receipts[1004] = new_order, {}
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key, apply=True)
        del fixture.receipts[1004]
        fixture.current["orders"] = [dict(new_order, open=True)]
        with self.assertRaises(ValueError):
            recovery.recover(self.session, self.key, apply=True)
        self.assertEqual(self.path.read_bytes(), raw)
        self.session.post.assert_not_called()

    def test_crash_consumes_new_permission_before_any_order_and_cannot_replay(self):
        report = recovery.recover(self.session, self.key, apply=True)
        with patch.object(auto, "prepare_second_leg", side_effect=InjectedCrash()):
            with self.assertRaises(InjectedCrash):
                auto.resume_recovered_leg1(self.session, self.key, report["continuation_permission"])
        saved = self.fixture.checkpoint()
        self.assertEqual(saved["state"], pilot.HALTED)
        self.assertIsNotNone(auto.active_attempt(saved)["leg2_recheck_recovery"]["consumed_at"])
        with self.assertRaises(pilot.PilotBlocked):
            auto.resume_recovered_leg1(self.session, self.key, report["continuation_permission"])
        self.session.post.assert_not_called()

    def test_worse_fresh_second_quote_stops_without_post(self):
        report = recovery.recover(self.session, self.key, apply=True)
        from test_api_audit import exchange_book
        self.fixture.books["14"] = exchange_book(4, 14, bid=.20, ask=.21)
        result = auto.resume_recovered_leg1(self.session, self.key, report["continuation_permission"])
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("Leg two quote deteriorated", result["reason"])
        saved = self.fixture.checkpoint()
        self.assertIsNone(auto.active_attempt(saved)["legs"][1]["intent"])
        self.session.post.assert_not_called()

    def test_ordinary_save_cannot_clear_halt_or_reset_consumed_permission(self):
        raw = self.path.read_bytes()
        proposed = copy.deepcopy(self.cp)
        proposed.update(state=pilot.LEG2_RECHECK, manual_review_required=False, review_reason="")
        auto.active_attempt(proposed).update(state=pilot.LEG2_RECHECK, halt_reason="", halted_from=None)
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(proposed)
        self.assertEqual(self.path.read_bytes(), raw)
        recovery.recover(self.session, self.key, apply=True)
        current = self.fixture.checkpoint()
        current["autonomous_execution"]["attempts"][self.key]["filled_leg1_recovery"]["resume_consumed_at"] = None
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(current)
        self.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
