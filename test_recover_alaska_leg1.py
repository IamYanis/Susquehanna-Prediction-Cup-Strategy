"""Mocked rounded Alaska fill with an existing completed pair, in temp state."""
import unittest
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse
from unittest.mock import patch

import autonomous_pilot as auto
import live_pilot as pilot
import live_settlement as live
import recover_filled_leg1 as recovery
import supervised_accounting_probe as probe
import test_autonomous_lifecycle as fixtures
from test_account_reader import holdings, orders_page
from test_api_audit import election_tree, exchange_book, pair_relationship, party_market
from test_supervised_accounting_probe import zero_collateral


class AlaskaLegRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = fixtures.AutonomousLifecycleTests()
        self.addCleanup(self.lifecycle.doCleanups)
        self.lifecycle.setUp()
        self.fixture = self.lifecycle.fixture
        self.session, self.path = self.fixture.session, self.fixture.path
        self.assertEqual(self.lifecycle.cycle()["state"], pilot.READY)
        self.old_checkpoint = self.fixture.checkpoint()
        self.completed_key = next(iter(self.old_checkpoint["autonomous_positions"]))
        # Add a second exact fake authorization, without changing the first.
        for party, mid, eid in (("Democratic", "3", "13"), ("Republican", "4", "14")):
            self.fixture.markets[mid] = party_market(party, mid, eid)
            self.fixture.nodes[mid] = election_tree(party, mid)
        relationship = pair_relationship()
        relationship["id"] = "33333333-3333-3333-3333-333333333333"
        relationship["nodes"] = [{"exchangeId": "13", "marketId": "3", "outcome": "YES", "role": "member"},
                                 {"exchangeId": "14", "marketId": "4", "outcome": "YES", "role": "member"}]
        self.fixture.relationships.append(relationship)
        approval = dict(self.fixture.approval, market_ids=["3", "4"], exchange_ids=["13", "14"])
        _, context = auto.scanner.get_pair_rules(self.session, self.fixture.markets["3"], self.fixture.markets["4"],
                                                approval["tournament_id"])
        approval.update(evidence_hash=context["settlement_fingerprint"], source_refs=live.evidence_sources(approval))
        self.fixture.write_authorizations([self.fixture.approval, approval])
        self.fixture.books.update({"13": exchange_book(3, 13, bid=.705, ask=.71),
                                   "14": exchange_book(4, 14, bid=.31, ask=.315)})
        self.session.post.side_effect = self.rounded_post
        original_review = probe.review_observation
        def old_review(*args):
            result = original_review(*args)
            self.assertTrue(result["observations"]["isolated_execution_reconciled"])
            result["issues"].insert(0, "POSITION_COST_SEMANTICS_UNVERIFIED")
            result["observations"]["isolated_execution_reconciled"] = False
            return result
        with patch.object(probe, "review_observation", side_effect=old_review):
            result = auto.execute_candidate(self.session, ["3", "4"])
        self.assertEqual(result["reason"], recovery.ALASKA_HALT_REASON)
        self.cp = self.fixture.checkpoint()
        self.key = self.cp["autonomous_execution"]["active_attempt"]
        target = {"attempt": self.key, "markets": ["3", "4"], "exchanges": ["13", "14"],
                  "order": 1003, "fill": 2003, "price": Decimal(".295")}
        patcher = patch.object(recovery, "ALASKA_TARGET", target)
        patcher.start()
        self.addCleanup(patcher.stop)
        def get(url, **kwargs):
            if urlparse(url).path.endswith("/orders") and kwargs.get("params", {}).get("status") == "all":
                rows = [r[0] for r in self.fixture.fixture.receipts.values()]
                return self.fixture.base.response(orders_page(rows, coverage={"complete": True}))
            return self.fixture.get(url, **kwargs)
        self.session.get.side_effect = get
        self.session.post.reset_mock()
        self.session.post.side_effect = AssertionError("Recovery must not POST")

    def rounded_post(self, url, **kwargs):
        response = self.lifecycle.post(url, **kwargs)
        receipt = response.json()
        price = Decimal(str(receipt["fillPrice"]))
        row = next(p for p in self.fixture.fixture.current["positions"] if p["exchangeId"] == receipt["exchangeId"])
        row["costBasis"] = float(price.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
        account = self.fixture.fixture.current
        account.update(holdings(account["positions"]))
        account["tournament"]["myBalance"] = float(Decimal(str(account["tournament"]["myBalance"])).quantize(
            Decimal(".01"), rounding=ROUND_HALF_UP))
        receipt["all"] = zero_collateral(float(price))
        return self.fixture.base.response(receipt)

    def test_dry_run_keeps_state_and_existing_pair_unchanged(self):
        before = self.path.read_bytes()
        report = recovery.recover(self.session, self.key)
        self.assertEqual(report["result"], "DRY_RUN_PASS")
        self.assertEqual(report["after"]["state"], pilot.LEG2_RECHECK)
        self.assertEqual(report["after"]["revision"], self.cp["revision"] + 1)
        self.assertEqual(Decimal(report["accounting"]["fill_notional"]), Decimal(".295"))
        self.assertEqual(Decimal(report["accounting"]["reported_balance_debit"]), Decimal(".29"))
        self.assertEqual(Decimal(report["accounting"]["conservative_allocation_charge"]), Decimal(".31"))
        self.assertEqual(report["exposure_after"]["confirmed_quantities"], ["1", "0"])
        self.assertEqual(self.path.read_bytes(), before)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_apply_archives_original_review_and_preserves_other_completed_position(self):
        recovery.recover(self.session, self.key, apply=True)
        saved = pilot._read_checkpoint_locked(self.path.resolve())
        before = self.cp["autonomous_execution"]["attempts"][self.key]
        after = saved["autonomous_execution"]["attempts"][self.key]
        for field in ("request", "approval", "order_id"):
            self.assertEqual(after["legs"][0]["intent"][field], before["legs"][0]["intent"][field])
        self.assertEqual(after["legs"][0]["receipt"], before["legs"][0]["receipt"])
        audit = after["filled_leg1_recovery"]
        self.assertEqual(audit["original_leg1_evidence"], {"activity": before["legs"][0]["activity"],
                          "review": before["legs"][0]["review"], "after": before["after"][0]})
        self.assertEqual(audit["submission_status"], "RETIRED_NEVER_RETRY")
        self.assertIsNone(after["legs"][1]["intent"])
        self.assertEqual(saved["quarantine_reserve"], self.cp["quarantine_reserve"])
        for field in ("autonomous_positions", "live_exposures", "accounted_pair_costs"):
            self.assertEqual(saved[field][self.completed_key], self.cp[field][self.completed_key])
        self.assertEqual(saved["autonomous_execution"]["attempts"][self.completed_key],
                         self.cp["autonomous_execution"]["attempts"][self.completed_key])
        self.assertEqual(after["stages"][:len(before["stages"])], before["stages"])
        self.assertEqual(after["reads"][:len(before["reads"])], before["reads"])
        recovery.require_resume(saved, self.key)
        self.session.post.assert_not_called()

    def test_restart_remains_halted_with_one_sided_exposure(self):
        recovery.recover(self.session, self.key, apply=True)
        saved = pilot.load_checkpoint()
        self.assertEqual(saved["state"], pilot.HALTED)
        self.assertEqual(saved["live_exposures"][self.key]["confirmed_quantities"], ["1", "0"])
        self.session.post.assert_not_called()

    def test_explicit_resume_can_only_submit_a_new_second_leg_once(self):
        recovery.recover(self.session, self.key, apply=True)
        self.session.post.side_effect = self.rounded_post
        result = auto.resume_recovered_leg1(self.session, self.key)
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["exchangeId"], "14")
        self.assertNotEqual(self.lifecycle.posts[-1]["idempotencyKey"], self.lifecycle.posts[-2]["idempotencyKey"])
        with self.assertRaises(pilot.PilotBlocked):
            auto.resume_recovered_leg1(self.session, self.key)
        self.assertEqual(self.session.post.call_count, 1)

    def test_changed_execution_basis_or_cash_blocks_without_writes(self):
        account = self.fixture.fixture
        position = next(p for p in account.current["positions"] if p["exchangeId"] == "13")
        before = self.path.read_bytes()
        for container, field, value in ((position, "costBasis", .31), (position, "avgCost", .300),
                (position, "quantity", -2), (account.receipts[1003][0], "priceLimit", .300),
                (account.receipts[1003][1]["data"][0], "id", 999),
                (account.current["tournament"], "myBalance", 19998.8)):
            old = container[field]
            try:
                container[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), before)
            finally:
                container[field] = old
        self.session.post.assert_not_called()

    def test_fee_or_second_leg_execution_blocks_without_writes(self):
        account = self.fixture.fixture
        before = self.path.read_bytes()
        for rows, extra in ((account.transactions, {"event_id": "extra-fee", "event_type": "fee", "quantity": 0,
                "createdAt": account.transactions[0]["createdAt"], "tournamentId": self.cp["tournament_id"],
                "amount": -.005, "transactionType": "FEE"}),
                (account.fills, dict(account.fills[0], id=2004, orderId=1004, exchangeId="14", marketId="4"))):
            rows.append(extra)
            try:
                with self.assertRaises(ValueError):
                    recovery.recover(self.session, self.key, apply=True)
                self.assertEqual(self.path.read_bytes(), before)
            finally:
                rows.pop()
        self.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
