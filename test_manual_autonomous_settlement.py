"""Distinct manual autonomous permission: fake HTTP and temporary state only."""
import copy
import hashlib
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import requests
import autonomous_pilot as auto
import config
import live_pilot as pilot
import live_settlement as live
import manual_autonomous_settlement as manual
import price_reader as scanner
import execution_quarantine as quarantine
import test_autonomous_pilot as autonomous_fixtures
from order_preview import PreviewBlocked
from test_api_audit import exchange_book
from test_account_reader import holdings, position


class ManualAutonomousSettlementTests(unittest.TestCase):
    def setUp(self):
        # Reuse the complete two-leg fake exchange, accounting and allocation
        # fixtures. Their enable switches affect mocks only, never real HTTP.
        self.fixture = autonomous_fixtures.AutonomousPilotTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.session = self.fixture.session
        self.path = self.fixture.base.root / "manual_autonomous_approvals.json"
        patcher = patch.object(live, "MANUAL_AUTONOMOUS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.policy = b"<html><main>Official synthetic ordinary settlement and refund policy.</main></html>"
        self.session.get.side_effect = self.get
        self.approval = copy.deepcopy(self.fixture.approval)
        self.approval.update(verification_route=live.MANUAL_AUTONOMOUS, evidence_version=1,
            settlement_rationale="Human-reviewed interpretation: these exact two synthetic party-winner outcomes cannot both hold.",
            limitations=list(manual.REQUIRED_LIMITATIONS))
        self.approval["source_refs"] = live.evidence_sources(self.approval)
        self.fixture.write_authorizations([])
        self.write_manual([])
        self.approval["evidence_hash"] = self.evidence()["evidence_hash"]
        self.write_manual([self.approval])
        self.session.get.reset_mock()
        self.paper_before = self.fixture.base.approval_path.read_bytes()

    def get(self, url, **kwargs):
        if url == manual.POLICY_URL:
            response = requests.Response()
            response.status_code = 200
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            response._content = self.policy
            return response
        return self.fixture.get(url, **kwargs)

    def write_manual(self, entries):
        self.path.write_text(json.dumps({"version": 1, "allowed_mode": config.LIVE_PILOT,
            "authorization_tier": live.MANUAL_AUTONOMOUS, "pairs": entries}))

    def evidence(self, approval=None):
        return manual.read_evidence(self.session, list(self.fixture.markets.values()),
                                    self.approval if approval is None else approval)

    def verify(self):
        return live.verify_settlement(self.session, list(self.fixture.markets.values()),
                                      self.approval["tournament_id"], self.approval)

    def assert_no_post(self):
        for method in ("post", "delete", "put", "patch"):
            getattr(self.session, method).assert_not_called()

    def test_new_config_is_empty_and_real_submission_stays_disabled(self):
        root = Path(live.__file__).parent
        self.assertEqual(json.loads((root / "manual_autonomous_approvals.json").read_text())["pairs"], [])
        self.assertIn("AUTONOMOUS_LIVE_PILOT_ENABLED = False", (root / "config.py").read_text())
        self.assertIn("LIVE_PILOT_SUBMISSION_ENABLED = False", (root / "config.py").read_text())

    def test_explicit_manual_tier_passes_only_its_read_only_settlement_gate(self):
        before = self.fixture.path.read_bytes()
        allowed, context = self.verify()
        self.assertEqual(allowed, {"NO-PAIR"})
        self.assertEqual(context["authorization_tier"], live.MANUAL_AUTONOMOUS)
        self.assertFalse(context["machine_verified"])
        self.assertEqual(context["manual_evidence"]["settlement_policy_sha256"], hashlib.sha256(self.policy).hexdigest())
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assertEqual(self.fixture.base.approval_path.read_bytes(), self.paper_before)
        self.assert_no_post()

    def test_manual_tier_reuses_full_account_quote_risk_checks_without_staging(self):
        before = self.fixture.path.read_bytes()
        result = auto.fresh_candidate(self.session, ["1", "2"], self.fixture.checkpoint())
        self.assertEqual(result["authorization_tier"], live.MANUAL_AUTONOMOUS)
        self.assertEqual(result["risk"]["live_allocation"], 5000)
        self.assertEqual(Decimal(result["edge"]), Decimal('.200'))
        self.assertFalse(result["submission_enabled"])
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assert_no_post()

    def test_mock_pair_journal_preserves_the_manual_tier_and_reconciles(self):
        result = self.fixture.execute()
        self.assertEqual(result["state"], pilot.READY)
        self.assertEqual(len(self.fixture.posts), 2)
        cp = self.fixture.checkpoint()
        attempt = cp["autonomous_execution"]["attempts"][result["execution_id"]]
        self.assertEqual(attempt["authorization"], self.approval)
        self.assertEqual([body["quantity"] for body in self.fixture.posts], [1, 1])
        self.assertLess(Decimal(cp["allocated_cash_remaining"]), Decimal(5000))

    def test_mock_loop_uses_manual_allowlist_and_skips_completed_pair_on_next_cycle(self):
        with patch.object(auto.time, "sleep", side_effect=[None, KeyboardInterrupt]), self.assertRaises(KeyboardInterrupt):
            auto.run(self.session)
        self.assertEqual(len(self.fixture.posts), 2)
        self.assertEqual(len(self.fixture.checkpoint()["autonomous_execution"]["attempts"]), 1)

    def test_machine_tier_is_preferred_when_both_are_explicitly_present(self):
        self.fixture.write_authorizations([self.fixture.approval])
        selected = auto.autonomous_authorization(["1", "2"], self.approval["tournament_id"])
        self.assertEqual(selected, self.fixture.approval)
        self.assertEqual(len(live.autonomous_authorizations()), 1)
        self.assertEqual(live.authorization_tier(selected), live.MACHINE_AUTONOMOUS)
        self.assert_no_post()

    def test_selected_machine_failure_does_not_fall_back_to_manual(self):
        self.fixture.write_authorizations([self.fixture.approval])
        self.fixture.relationships = []
        self.fixture.assert_halted(0)
        self.assertFalse(any(call.args[0] == manual.POLICY_URL for call in self.session.get.call_args_list))

    def test_supervised_machine_permission_does_not_suppress_separate_manual_autonomous_permission(self):
        supervised = copy.deepcopy(self.fixture.approval)
        supervised["execution_mode"] = live.SUPERVISED_MODE
        self.fixture.write_authorizations([supervised])
        self.assertEqual(auto.autonomous_authorization(["1", "2"], self.approval["tournament_id"]), self.approval)
        self.assertEqual(live.configured_authorization(["1", "2"]), supervised)

    def test_paper_and_supervised_approvals_never_grant_manual_autonomy(self):
        self.write_manual([])
        supervised = copy.deepcopy(self.fixture.approval)
        supervised["execution_mode"] = live.SUPERVISED_MODE
        self.fixture.write_authorizations([supervised])
        with self.assertRaises(live.LiveSettlementBlocked):
            auto.autonomous_authorization(["1", "2"], self.approval["tournament_id"])
        self.session.get.assert_not_called()
        self.assert_no_post()

    def test_new_tier_cannot_authorize_the_supervised_probe(self):
        with self.assertRaises(live.LiveSettlementBlocked):
            live.configured_authorization(["1", "2"])
        self.assert_no_post()

    def test_authorizations_are_rejected_in_the_wrong_configuration_file(self):
        self.fixture.write_authorizations([self.approval])
        with self.assertRaises(live.LiveSettlementBlocked):
            live.load_authorizations()
        self.fixture.write_authorizations([])
        self.write_manual([self.fixture.approval])
        with self.assertRaises(live.LiveSettlementBlocked):
            live.load_manual_autonomous_authorizations()

    def test_unlisted_pair_and_wrong_uuid_slug_or_id_order_are_blocked(self):
        for ids, tid, slug in ((["3", "4"], self.approval["tournament_id"], "midterm-elections"),
                               (["2", "1"], self.approval["tournament_id"], "midterm-elections"),
                               (["1", "2"], "550e8400-e29b-41d4-a716-446655440099", "midterm-elections"),
                               (["1", "2"], self.approval["tournament_id"], "another-cup")):
            with self.subTest(ids=ids, tid=tid, slug=slug), self.assertRaises(live.LiveSettlementBlocked):
                live.configured_authorization(ids, tid, slug, live.AUTONOMOUS_MODE)
        self.session.get.assert_not_called()

    def test_malformed_missing_duplicate_or_wrong_tier_config_fails_closed(self):
        for text in ('{', '{"version":1,"version":1}', json.dumps({"version":1,"allowed_mode":"paper-only","pairs":[]}),
                     json.dumps({"version":1,"allowed_mode":"LIVE_PILOT","authorization_tier":"supervised","pairs":[]})):
            self.path.write_text(text)
            with self.assertRaises(live.LiveSettlementBlocked):
                live.autonomous_authorizations()
        self.write_manual([self.approval, self.approval])
        with self.assertRaises(live.LiveSettlementBlocked):
            live.autonomous_authorizations()
        self.path.unlink()
        with self.assertRaises(live.LiveSettlementBlocked):
            live.autonomous_authorizations()

    def test_quantity_relationship_mode_version_sources_and_limitations_are_strict(self):
        for key, value in (("max_quantity", 2), ("max_quantity", True), ("position_types", ["YES-PAIR"]),
                           ("relationship_type", "correlated"), ("execution_mode", live.SUPERVISED_MODE),
                           ("evidence_version", True), ("evidence_version", 2), ("limitations", []),
                           ("source_refs", ["https://example.org/settlement"]), ("approved_at", None)):
            changed = copy.deepcopy(self.approval)
            changed[key] = value
            with self.subTest(key=key, value=value), self.assertRaises((live.LiveSettlementBlocked, ValueError)):
                live.validate_authorization(changed)

    def test_changed_market_exchange_status_or_tournament_blocks_execution(self):
        original = copy.deepcopy(self.fixture.markets["1"])
        for key, value in (("id", 9), ("exchanges", [{"id":99,"option":"YES"}]), ("status", "closed")):
            self.fixture.markets["1"] = copy.deepcopy(original)
            self.fixture.markets["1"][key] = value
            with self.subTest(key=key), self.assertRaises(live.LiveSettlementBlocked):
                self.verify()
        self.fixture.markets["1"] = original
        self.fixture.fixture.current["tournament"]["id"] = "550e8400-e29b-41d4-a716-446655440099"
        with self.assertRaises(live.LiveSettlementBlocked):
            self.verify()
        self.assert_no_post()

    def test_changed_root_wording_date_type_or_resolved_state_requires_revalidation(self):
        original = copy.deepcopy(self.fixture.nodes["1"])
        for key, value in (("contract_details", {"description":"Changed official interpretation"}),
                           ("settlement_date", "2026-12-03T00:00:00Z"), ("contract_type", "Freeform"),
                           ("settled_with", "YES")):
            self.fixture.nodes["1"] = copy.deepcopy(original)
            self.fixture.nodes["1"]["root"][key] = value
            with self.subTest(key=key), self.assertRaises(live.LiveSettlementBlocked):
                self.verify()
        self.assert_no_post()

    def test_changed_policy_bytes_and_unchanged_url_require_revalidation(self):
        self.policy += b" Different cancellation rule."
        with self.assertRaisesRegex(live.LiveSettlementBlocked, "changed"):
            self.verify()
        self.assert_no_post()

    def test_approval_rationale_limitations_timestamp_version_and_sources_are_hash_bound(self):
        for key, value in (("settlement_rationale", "Different human assumption"), ("approval_version", 2),
                           ("approved_at", "2026-10-08T12:00:00+00:00"),
                           ("limitations", self.approval["limitations"] + ["Additional assumption"])):
            changed = copy.deepcopy(self.approval)
            changed[key] = value
            self.assertNotEqual(self.evidence(changed)["evidence_hash"], self.approval["evidence_hash"])
        changed = copy.deepcopy(self.approval)
        changed["source_refs"][1] += "&differentSource=true"
        with self.assertRaises(live.LiveSettlementBlocked):
            self.evidence(changed)

    def test_missing_ambiguous_or_redirected_policy_never_supplies_evidence(self):
        for status, content_type, body in ((302,"text/html",b"redirect"), (429,"text/html",b"rate limit"),
                                          (200,"application/json",b"{}"), (200,"text/html",b"")):
            response = requests.Response()
            response.status_code = status
            response.headers["Content-Type"] = content_type
            response._content = body
            with self.subTest(status=status, content_type=content_type, body=body), \
                    patch.object(self.session, "get", return_value=response), self.assertRaises(live.LiveSettlementBlocked):
                manual.read_policy_hash(self.session)

    def test_quote_balance_changes_do_not_change_settlement_evidence(self):
        self.fixture.books["11"] = exchange_book(1, 11, bid=.61, ask=.65)
        self.fixture.fixture.current["tournament"]["myBalance"] = 21000
        self.assertEqual(self.evidence()["evidence_hash"], self.approval["evidence_hash"])

    def test_disabled_switches_block_manual_tier_before_any_read_or_state_change(self):
        before = self.fixture.path.read_bytes()
        with patch.object(config, "AUTONOMOUS_LIVE_PILOT_ENABLED", False), self.assertRaisesRegex(pilot.PilotBlocked, "disabled"):
            self.fixture.execute()
        self.session.get.assert_not_called()
        self.assert_no_post()
        self.assertEqual(self.fixture.path.read_bytes(), before)

    def test_manual_tier_still_requires_half_percent_edge_and_depth(self):
        for bid, quantity in ((.495,100), (.6,0)):
            self.fixture.books["11"] = exchange_book(1, 11, bid=bid, ask=max(bid,.65))
            self.fixture.books["12"] = exchange_book(2, 12, bid=.505, ask=.65)
            self.fixture.books["11"]["bids"][0]["quantity"] = quantity
            with self.subTest(bid=bid, quantity=quantity), self.assertRaises(PreviewBlocked):
                auto.fresh_candidate(self.session,["1","2"],self.fixture.checkpoint())
        self.assert_no_post()

    def test_manual_tier_accepts_exact_half_percent_with_all_other_gates(self):
        self.fixture.books["12"] = exchange_book(2, 12, bid=.405, ask=.41)
        candidate = auto.fresh_candidate(self.session, ["1", "2"], self.fixture.checkpoint())
        self.assertEqual(Decimal(candidate["edge"]), Decimal(".005"))
        self.assertEqual(candidate["prices"], [.4, .595])
        self.assertEqual(candidate["authorization_tier"], live.MANUAL_AUTONOMOUS)
        self.assert_no_post()

    def test_existing_holdings_and_persistent_halt_still_block_manual_tier(self):
        self.fixture.fixture.current.update(holdings([position("11","1",-1)]))
        with self.assertRaises(ValueError):
            auto.fresh_candidate(self.session,["1","2"],self.fixture.checkpoint())
        self.assert_no_post()

    def test_quarantine_still_blocks_the_manual_settlement_gate(self):
        with patch.object(quarantine, "require_unblocked_markets", side_effect=quarantine.QuarantineError("Quarantined market")), \
                self.assertRaises(live.LiveSettlementBlocked):
            self.verify()
        self.assert_no_post()

    def test_stale_quotes_are_not_accepted_by_the_manual_tier(self):
        self.fixture.books["11"]["asOf"]["at"] = "2020-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            auto.fresh_candidate(self.session,["1","2"],self.fixture.checkpoint())
        self.assert_no_post()

    def test_manual_tier_cannot_ignore_per_race_or_total_exposure(self):
        for cost in (99.5, 500):
            holding = position("13", "3", -1)
            holding.update(costBasis=cost, marketValue=cost, unrealizedPnl=0)
            self.fixture.fixture.current.update(holdings([holding]))
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                auto.fresh_candidate(self.session,["1","2"],self.fixture.checkpoint())
        self.assert_no_post()

    def test_policy_change_after_leg_one_halts_with_one_sided_exposure(self):
        def post_then_change(url, **kwargs):
            response = self.fixture.post(url, **kwargs)
            self.policy += b" Changed after first fill."
            return response
        self.session.post.side_effect = post_then_change
        cp = self.fixture.assert_halted(1)
        exposure = next(iter(cp["live_exposures"].values()))
        self.assertEqual(exposure["confirmed_quantities"], ["1.0","0"])
        self.assertIn(live.NEEDS_REVALIDATION, cp["review_reason"])
        self.assertLess(Decimal(cp["allocated_cash_remaining"]), Decimal(5000))
        self.assertEqual(self.fixture.checkpoint()["state"], pilot.HALTED)

    def test_preflight_evidence_mismatch_latches_halt_even_if_evidence_is_restored(self):
        original = copy.deepcopy(self.fixture.nodes["1"])
        self.fixture.nodes["1"]["root"]["contract_details"]["winnerName"] = "Changed affiliation"
        cp = self.fixture.assert_halted(0)
        self.fixture.nodes["1"] = original
        self.assertEqual(self.fixture.checkpoint(), cp)
        with self.assertRaises(pilot.PilotBlocked):
            self.fixture.execute()

    def test_revocation_halts_and_cannot_clear_by_restoring_approval(self):
        def revoke_after_post(url, **kwargs):
            response = self.fixture.post(url, **kwargs)
            self.write_manual([])
            return response
        self.session.post.side_effect = revoke_after_post
        cp = self.fixture.assert_halted(1)
        self.write_manual([self.approval])
        self.assertEqual(self.fixture.checkpoint(), cp)
        with self.assertRaises(pilot.PilotBlocked):
            self.fixture.execute()

    def test_ambiguous_submission_remains_halted_and_is_never_retried(self):
        self.fixture.scripts[0]["before_fill_error"] = requests.Timeout("Synthetic ambiguous POST")
        cp = self.fixture.assert_halted(1)
        self.assertGreater(Decimal(cp["reserved_unconfirmed_capital"]), 0)


if __name__ == "__main__":
    unittest.main()
