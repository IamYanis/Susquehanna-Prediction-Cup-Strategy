"""Only fake exchange GETs and temporary state; no recovery touches real files."""
import copy
import unittest
from decimal import Decimal
from urllib.parse import urlparse
from unittest.mock import patch

import account_test as single
import autonomous_pilot as auto
import live_pilot as pilot
import recover_filled_leg1 as recovery
import test_autonomous_pilot as fixtures
from test_account_reader import holdings, orders_page
from test_api_audit import exchange_book


class FilledLegRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AutonomousPilotTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.session, self.path = self.fixture.session, self.fixture.path
        self.fixture.books = {"11": exchange_book(1, 11, bid=.925, ask=.93),
                              "12": exchange_book(2, 12, bid=.1, ask=.105)}
        # Reproduce the old client's microseconds and the server's truncation.
        make_intent, post = auto.new_intent, self.fixture.post
        def old_intent(*args):
            intent = make_intent(*args)
            expiry = single.parse_api_timestamp(intent["request"]["expirationDate"])
            expiry = expiry.replace(microsecond=expiry.microsecond + 575)
            intent["request"]["expirationDate"] = expiry.isoformat()
            intent["approval"] = single.approval_hash(intent)
            return intent
        def normalized_post(url, **kwargs):
            response = post(url, **kwargs)
            account = self.fixture.fixture
            order = account.receipts[301][0]
            order["expirationDate"] = single.parse_api_timestamp(order["expirationDate"]).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            row = account.current["positions"][0]
            row.update(avgCost=.075, costBasis=.08, marketValue=.08, unrealizedPnl=0)
            account.current.update(holdings([row]))
            account.current["tournament"]["myBalance"] = 19999.92
            return response
        self.session.post.side_effect = normalized_post
        with patch.object(auto, "new_intent", side_effect=old_intent), patch.object(single, "same_order_expiry", return_value=False):
            result = self.fixture.execute()
        self.assertEqual(result["reason"], recovery.HALT_REASON)
        self.cp = pilot._read_checkpoint_locked(self.path.resolve())
        self.key = self.cp["autonomous_execution"]["active_attempt"]
        target = {"attempt": self.key, "markets": ["1", "2"], "exchanges": ["11", "12"],
                  "order": 301, "fill": 501, "price": Decimal(".075")}
        target_patch = patch.object(recovery, "TARGET", target)
        target_patch.start()
        self.addCleanup(target_patch.stop)
        def get(url, **kwargs):
            if urlparse(url).path.endswith("/orders") and kwargs.get("params", {}).get("status") == "all":
                return self.fixture.base.response(orders_page([r[0] for r in self.fixture.fixture.receipts.values()], coverage={"complete": True}))
            return self.fixture.get(url, **kwargs)
        self.session.get.side_effect = get
        self.session.post.reset_mock()
        self.session.post.side_effect = AssertionError("Recovery must never submit an order")

    def test_dry_run_is_read_only_and_preserves_one_sided_capital(self):
        before = self.path.read_bytes()
        report = recovery.recover(self.session, self.key)
        self.assertEqual(report["result"], "DRY_RUN_PASS")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(report["after"]["state"], pilot.LEG2_RECHECK)
        self.assertEqual(Decimal(report["accounting"]["fill_notional"]), Decimal(".075"))
        self.assertEqual(Decimal(report["accounting"]["reported_balance_debit"]), Decimal(".08"))
        self.assertEqual(Decimal(report["after"]["confirmed_cumulative_debits"]), Decimal(".10"))
        self.assertEqual(Decimal(report["after"]["reserved_unconfirmed_capital"]), Decimal(".945"))
        self.assertEqual(report["after"]["quarantine_reserve"], report["before"]["quarantine_reserve"])
        self.assertIsNone(report["leg_two_intent"])
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_apply_records_fill_and_retires_key_without_submitting_second_leg(self):
        report = recovery.recover(self.session, self.key, apply=True)
        cp = pilot._read_checkpoint_locked(self.path.resolve())
        attempt = cp["autonomous_execution"]["attempts"][self.key]
        old = self.cp["autonomous_execution"]["attempts"][self.key]
        self.assertEqual(attempt["legs"][0]["intent"]["request"], old["legs"][0]["intent"]["request"])
        self.assertEqual(attempt["legs"][0]["receipt"], old["legs"][0]["receipt"])
        self.assertEqual(attempt["filled_leg1_recovery"]["original_intent"], old["legs"][0]["intent"])
        self.assertEqual(attempt["filled_leg1_recovery"]["submission_status"], "RETIRED_NEVER_RETRY")
        self.assertEqual(cp["live_exposures"][self.key]["confirmed_quantities"], ["1", "0"])
        self.assertEqual(cp["live_exposures"][self.key]["confirmed_costs"], ["0.075", "0"])
        self.assertEqual(cp.get("baseline_snapshot"), self.cp.get("baseline_snapshot"))
        self.assertEqual(cp["state"], pilot.LEG2_RECHECK)
        self.assertIsNone(attempt["legs"][1]["intent"])
        self.assertEqual(report["orders_submitted"], 0)
        with self.assertRaises(pilot.PilotBlocked):
            recovery.recover(self.session, self.key, apply=True)
        self.session.post.assert_not_called()

    def test_ordinary_restart_still_halts_instead_of_submitting(self):
        recovery.recover(self.session, self.key, apply=True)
        restored = pilot.load_checkpoint()
        self.assertEqual(restored["state"], pilot.HALTED)
        self.assertEqual(restored["live_exposures"][self.key]["confirmed_quantities"], ["1", "0"])
        self.session.post.assert_not_called()
        with self.assertRaises(pilot.PilotBlocked):
            auto.resume_recovered_leg1(self.session, self.key)

    def test_explicit_resume_submits_only_new_second_leg_key_once(self):
        recovery.recover(self.session, self.key, apply=True)
        saved_reads = pilot._read_checkpoint_locked(self.path.resolve())["autonomous_execution"]["attempts"][self.key]["reads"]
        self.session.post.side_effect = self.fixture.post
        result = auto.resume_recovered_leg1(self.session, self.key)
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(len(self.fixture.posts), 2)  # Original leg one plus only leg two.
        self.assertNotEqual(self.fixture.posts[0]["idempotencyKey"], self.fixture.posts[1]["idempotencyKey"])
        self.assertEqual(self.fixture.posts[1]["exchangeId"], "12")
        final = pilot._read_checkpoint_locked(self.path.resolve())["autonomous_execution"]["attempts"][self.key]
        self.assertEqual(final["reads"][:len(saved_reads)], saved_reads)
        self.session.post.reset_mock()
        with self.assertRaises(pilot.PilotBlocked):
            auto.resume_recovered_leg1(self.session, self.key)
        self.session.post.assert_not_called()

    def test_changed_evidence_keeps_halt_and_does_not_write(self):
        account = self.fixture.fixture
        cases = [
            (account.receipts[301][0], "expirationDate", "2026-10-10T17:16:27.155Z"),
            (account.receipts[301][0], "action", "sell"),
            (account.receipts[301][0], "priceLimit", .08),
            (account.receipts[301][1]["data"][0], "id", 999),
            (account.receipts[301][1]["data"][0], "quantity", -.5),
            (account.current["positions"][0], "quantity", -2),
            (account.transactions[0], "orderType", "SELL"),
            (account.current["tournament"], "myBalance", 19999.8),
        ]
        before = self.path.read_bytes()
        for container, field, bad in cases:
            previous = container[field]
            try:
                container[field] = bad
                with self.subTest(field=field), self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), before)
            finally:
                container[field] = previous
        self.session.post.assert_not_called()

    def test_extra_fee_or_second_leg_fill_blocks_recovery(self):
        account = self.fixture.fixture
        before = self.path.read_bytes()
        for rows, extra in ((account.transactions, {
                "event_id": "extra-fee", "event_type": "fee", "createdAt": account.transactions[0]["createdAt"],
                "tournamentId": self.cp["tournament_id"], "amount": -.005, "quantity": 0, "transactionType": "FEE"}),
                (account.fills, dict(account.fills[0], id=502, orderId=302, marketId="2", exchangeId="12"))):
            rows.append(extra)
            try:
                with self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), before)
            finally:
                rows.pop()
        self.session.post.assert_not_called()

    def test_resume_rechecks_recorded_basis_and_does_not_relax_cost_matching(self):
        recovery.recover(self.session, self.key, apply=True)
        row = self.fixture.fixture.current["positions"][0]
        row.update(costBasis=.085, marketValue=.085)
        self.fixture.fixture.current.update(holdings([row]))
        self.session.post.side_effect = self.fixture.post
        result = auto.resume_recovered_leg1(self.session, self.key)
        self.assertEqual(result["state"], pilot.HALTED)
        self.session.post.assert_not_called()

    def test_crash_before_second_leg_post_does_not_allow_resume_replay(self):
        recovery.recover(self.session, self.key, apply=True)
        with patch.object(auto, "prepare_second_leg", side_effect=fixtures.InjectedCrash()):
            with self.assertRaises(fixtures.InjectedCrash):
                auto.resume_recovered_leg1(self.session, self.key)
        cp = pilot._read_checkpoint_locked(self.path.resolve())
        self.assertEqual(cp["state"], pilot.HALTED)
        self.assertIsNotNone(cp["autonomous_execution"]["attempts"][self.key]["filled_leg1_recovery"]["resume_consumed_at"])
        with self.assertRaises(pilot.PilotBlocked):
            auto.resume_recovered_leg1(self.session, self.key)
        self.session.post.assert_not_called()

    def test_additional_order_blocks_recovery(self):
        duplicate = copy.deepcopy(self.fixture.fixture.receipts[301])
        duplicate[0].update(id=302, exchangeId="12")
        self.fixture.fixture.receipts[302] = duplicate
        before = self.path.read_bytes()
        with self.assertRaises(pilot.PilotBlocked):
            recovery.recover(self.session, self.key, apply=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
