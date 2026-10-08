"""Separate LIVE permissions: fake official evidence, temporary files, no orders."""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import live_pilot as pilot
import live_settlement as live
import paired_account_test as paired
import paper_trader as paper
import price_reader as scanner
import test_live_pilot as fixtures
import test_live_pilot_state as state_fixtures
from test_account_reader import holdings, orders_page
from test_api_audit import TOURNAMENT_ID, election_tree, exchange_book, page, pair_relationship, party_market


class LiveSettlementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LivePilotTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.session = self.fixture.session
        self.live_path = self.fixture.live_approval_path
        self.paper_path = self.fixture.approval_path
        self.paper_before = self.paper_path.read_bytes()

    def write_live(self, entries):
        self.live_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT", "pairs": entries}))

    def audit(self, payloads=None, ids=None, checkpoint=None):
        payloads = self.fixture.candidate_payloads() if payloads is None else payloads
        self.session.get.side_effect = [self.fixture.response(payload) for payload in payloads]
        return pilot.audit_candidate(self.session, ["1", "2"] if ids is None else ids,
                                     self.fixture.checkpoint if checkpoint is None else checkpoint)

    def assert_revalidation_halt(self, payloads=None, ids=None, **kwargs):
        with self.assertRaises(live.LiveSettlementBlocked) as caught:
            self.audit(payloads, ids, **kwargs)
        self.assertEqual(caught.exception.code, live.NEEDS_REVALIDATION)
        saved = pilot.load_checkpoint()
        self.assertEqual(saved["state"], pilot.HALTED)
        self.assertTrue(saved["review_reason"].startswith(live.NEEDS_REVALIDATION))
        self.assertEqual(self.paper_path.read_bytes(), self.paper_before)
        self.fixture.assert_no_writes()
        return saved

    def test_paper_approval_alone_blocks_live_before_account_or_quote_reads(self):
        self.write_live([])
        with self.assertRaises(live.LiveSettlementBlocked) as caught:
            self.audit()
        self.assertEqual(caught.exception.code, live.NOT_AUTHORIZED)
        self.session.get.assert_not_called()
        self.assertEqual(pilot.load_checkpoint(), self.fixture.checkpoint)
        self.assertEqual(self.paper_path.read_bytes(), self.paper_before)
        self.fixture.assert_no_writes()

    def test_explicit_live_authorization_passes_settlement_without_paper_approval(self):
        self.paper_path.unlink()  # Live permission must not depend on this file.
        result = self.audit()
        self.assertEqual(result["settlement_status"], live.VERIFIED)
        self.assertEqual(result["settlement_route"], live.MACHINE)
        self.assertEqual(result["live_authorization"]["evidence_hash"], fixtures.live_approval()["evidence_hash"])
        self.assertTrue(result["checks_passed"])
        self.assertFalse(result["live_eligible"])
        self.assertFalse(result["submission_enabled"])
        self.assertFalse(self.paper_path.exists())
        self.assertEqual(self.session.get.call_count, 10)
        self.fixture.assert_no_writes()

    def test_unlisted_pair_cannot_reach_live_gate(self):
        with self.assertRaises(live.LiveSettlementBlocked) as caught:
            self.audit(ids=["3", "4"])
        self.assertEqual(caught.exception.code, live.NOT_AUTHORIZED)
        self.session.get.assert_not_called()
        self.fixture.assert_no_writes()

    def test_fresh_market_id_mismatch_blocks_and_latches_review(self):
        payloads = self.fixture.candidate_payloads()
        payloads[3]["id"] = 999
        self.assert_revalidation_halt(payloads)
        self.assertEqual(self.session.get.call_count, 4)

    def test_fresh_exchange_mapping_mismatch_blocks_before_books(self):
        payloads = self.fixture.candidate_payloads()
        payloads[3]["exchanges"][0]["id"] = 999
        self.assert_revalidation_halt(payloads)
        self.assertEqual(self.session.get.call_count, 5)

    def test_missing_authorized_market_returns_revalidation_not_retry(self):
        payloads = self.fixture.candidate_payloads()
        responses = [self.fixture.response(payload) for payload in payloads[:3]]
        missing = requests.Response()
        missing.status_code = 404
        missing._content = b'{"error": "not found"}'
        self.session.get.side_effect = responses + [missing]
        with self.assertRaises(live.LiveSettlementBlocked) as caught:
            pilot.audit_candidate(self.session, ["1", "2"], self.fixture.checkpoint)
        self.assertEqual(caught.exception.code, live.NEEDS_REVALIDATION)
        self.assertEqual(pilot.load_checkpoint()["state"], pilot.HALTED)
        self.assertEqual(self.session.get.call_count, 4)
        self.fixture.assert_no_writes()

    def test_changed_official_rule_fingerprint_blocks_before_quotes(self):
        payloads = self.fixture.candidate_payloads()
        payloads[6]["root"]["settlement_date"] = "2026-12-02T00:00:00Z"
        payloads[7]["root"]["settlement_date"] = "2026-12-02T00:00:00Z"
        self.assert_revalidation_halt(payloads)
        self.assertEqual(self.session.get.call_count, 8)

    def test_changed_relationship_type_blocks_before_resolution_tree_reads(self):
        relationship = pair_relationship()
        relationship["type"] = "correlated"
        self.assert_revalidation_halt(self.fixture.candidate_payloads(relationships=[relationship]))
        self.assertEqual(self.session.get.call_count, 6)

    def test_changed_relationship_version_requires_explicit_revalidation(self):
        relationship = pair_relationship()
        relationship["version"] = 2
        self.assert_revalidation_halt(self.fixture.candidate_payloads(relationships=[relationship]))

    def test_account_tournament_mismatch_is_not_inferred_from_market_titles(self):
        payloads = self.fixture.candidate_payloads()
        payloads[0]["id"] = "550e8400-e29b-41d4-a716-446655440001"
        self.assert_revalidation_halt(payloads)

    def test_market_order_and_slug_mismatch_are_blocked(self):
        with self.assertRaises(live.LiveSettlementBlocked) as caught:
            live.configured_authorization(["2", "1"], TOURNAMENT_ID)
        self.assertEqual(caught.exception.code, live.NEEDS_REVALIDATION)
        with self.assertRaises(live.LiveSettlementBlocked) as caught:
            live.configured_authorization(["1", "2"], TOURNAMENT_ID, "another-cup")
        self.assertEqual(caught.exception.code, live.NEEDS_REVALIDATION)
        self.session.get.assert_not_called()

    def test_evidence_restoring_and_restart_do_not_clear_the_persistent_condition(self):
        before = self.live_path.read_bytes()
        saved = self.assert_revalidation_halt(self.fixture.candidate_payloads(relationships=[]))
        # A new process reloads the existing halt. Neither restoring the graph
        # nor rewriting the old approval permits another readiness attempt.
        self.live_path.write_bytes(before)
        restarted = subprocess.run([sys.executable, "-c", state_fixtures.RESTART_SCRIPT, str(pilot.ALLOCATION_PATH)],
                                   cwd=Path(pilot.__file__).parent, capture_output=True, text=True, timeout=10)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        restored = json.loads(restarted.stdout)
        self.assertEqual(restored, saved)
        self.session.get.reset_mock()
        with self.assertRaises(live.LiveSettlementBlocked) as caught:
            self.audit(checkpoint=restored)
        self.assertEqual(caught.exception.code, live.NEEDS_REVALIDATION)
        self.session.get.assert_not_called()
        self.assertEqual(pilot.load_checkpoint(), saved)

    def test_revocation_during_readiness_latches_review_even_when_now_unlisted(self):
        responses = iter(self.fixture.response(payload) for payload in self.fixture.candidate_payloads())

        def revoke_on_read(url, **kwargs):
            if url.endswith("/orders"):
                self.write_live([])
            return next(responses)

        self.session.get.side_effect = revoke_on_read
        with self.assertRaises(live.LiveSettlementBlocked) as caught:
            pilot.audit_candidate(self.session, ["1", "2"], self.fixture.checkpoint)
        self.assertEqual(caught.exception.code, live.NEEDS_REVALIDATION)
        self.assertEqual(pilot.load_checkpoint()["state"], pilot.HALTED)
        self.assertEqual(self.session.get.call_count, 3)
        self.fixture.assert_no_writes()

    def test_invalid_or_paper_scoped_config_cannot_authorize_live(self):
        invalid_entries = []
        for key, value in (("evidence_hash", "not-a-hash"), ("relationship_type", "correlated"),
                           ("max_quantity", 2), ("execution_mode", "unattended"), ("approved_at", None),
                           ("approval_version", True), ("market_ids", ["1", "1"]), ("source_refs", [])):
            changed = fixtures.live_approval()
            changed[key] = value
            invalid_entries.append(changed)
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                self.write_live([entry])
                with self.assertRaisesRegex(live.LiveSettlementBlocked, live.NEEDS_REVALIDATION):
                    live.load_authorizations()
        self.live_path.write_text(self.paper_path.read_text())
        with self.assertRaisesRegex(live.LiveSettlementBlocked, live.NEEDS_REVALIDATION):
            live.load_authorizations()
        self.write_live([fixtures.live_approval(), fixtures.live_approval()])
        with self.assertRaisesRegex(live.LiveSettlementBlocked, live.NEEDS_REVALIDATION):
            live.load_authorizations()
        self.session.get.assert_not_called()

    def test_corrupt_live_configuration_latches_halt_without_rewriting_configuration(self):
        bad = '{"version": 1, "version": 2, "allowed_mode": "LIVE_PILOT", "pairs": []}'
        self.live_path.write_text(bad)
        self.assert_revalidation_halt()
        self.assertEqual(self.live_path.read_text(), bad)
        self.session.get.assert_not_called()

    def manual_fixture(self):
        patcher = patch.object(paired, "MANUAL_TOURNAMENT", TOURNAMENT_ID)
        patcher.start()
        self.addCleanup(patcher.stop)
        markets, trees = [], []
        for party, market_id, exchange_id in (("Democratic", 153, 842), ("Republican", 154, 843)):
            market = party_market(party, market_id, exchange_id)
            market["title"] = f"Will the {party} Party win the U.S. Senate?"
            tree = election_tree(party, market_id)
            tree["root"].update(contract_type="Freeform", contract_details={
                "description": paired.manual_rule_description(party), "contractType": "Freeform"})
            markets.append(market)
            trees.append(tree)
        self.session.get.side_effect = [self.fixture.response(payload) for payload in markets + trees]
        _, record = paired.read_manual_evidence(self.session, TOURNAMENT_ID, "153", "154")
        approval = fixtures.live_approval()
        approval.update(pair_name="Synthetic supervised Senate approval", market_ids=["153", "154"],
                        exchange_ids=["842", "843"], verification_route=live.MANUAL,
                        evidence_hash=record["evidence_hash"], settlement_rationale=record["proposition"],
                        source_refs=record["source_refs"], limitations=record["limitations"])
        self.write_live([approval])
        # Manual verification rereads market identities as well as the exact rules.
        payloads = [self.fixture.account["tournament"], holdings([]), orders_page([]), *markets, *markets, *trees,
                    exchange_book(153, 842), exchange_book(154, 843)]
        self.session.get.reset_mock()
        return approval, payloads

    def test_explicit_narrow_manual_live_permission_passes_without_paper_metadata(self):
        approval, payloads = self.manual_fixture()
        with patch.object(pilot.single, "prepare_test") as single_prepare, \
                patch.object(paired, "prepare_pair") as pair_prepare:
            result = self.audit(payloads, ids=["153", "154"])
        self.assertEqual(result["settlement_route"], live.MANUAL)
        self.assertEqual(result["settlement_status"], live.VERIFIED)
        self.assertEqual(result["live_authorization"]["approved_at"], approval["approved_at"])
        self.assertFalse(result["live_eligible"])
        self.assertEqual(self.paper_path.read_bytes(), self.paper_before)
        single_prepare.assert_not_called()
        pair_prepare.assert_not_called()
        self.fixture.assert_no_writes()

    def test_manual_official_wording_change_requires_revalidation(self):
        _, payloads = self.manual_fixture()
        payloads[7]["root"]["contract_details"]["description"] += " Changed rule."
        self.assert_revalidation_halt(payloads, ids=["153", "154"])

    def test_machine_authorization_cannot_switch_to_manual_when_graph_disappears(self):
        self.assert_revalidation_halt(self.fixture.candidate_payloads(relationships=[]))
        self.assertEqual(self.live_path.read_text(), json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT",
                                                               "pairs": [fixtures.live_approval()]}))

    def test_second_leg_diagnostic_rechecks_live_authorization_and_latches_revocation(self):
        pair, legs = self.fixture.synthetic_pair((True, False))
        account = self.fixture.venue_observations(legs, (1, 0))
        with pilot.pilot_lock():
            with patch.object(paired, "read_account", return_value=account):
                reconciliation = pilot.reconcile_pilot(self.session, pair, legs, self.fixture.checkpoint)
            self.write_live([])
            self.session.get.reset_mock()
            with self.assertRaises(live.LiveSettlementBlocked) as caught:
                pilot.consider_second_leg(self.session, pair, reconciliation, manually_supervised=True, restarted=False)
            self.assertEqual(caught.exception.code, live.NEEDS_REVALIDATION)
            self.assertTrue(pilot.load_checkpoint()["review_reason"].startswith(live.NEEDS_REVALIDATION))
            self.session.get.assert_not_called()
        self.fixture.assert_no_writes()

    def test_cli_reports_and_persists_revalidation_code_without_stale_revision_error(self):
        self.session.get.side_effect = [self.fixture.response(payload) for payload in self.fixture.candidate_payloads(relationships=[])]
        output = io.StringIO()
        with patch("sys.argv", ["live_pilot.py", "--dem-market", "1", "--rep-market", "2"]), \
                patch.object(pilot, "load_dotenv"), patch.object(pilot.os, "getenv", return_value="offline-key"), \
                patch.object(pilot.requests, "Session") as session_factory, contextlib.redirect_stdout(output):
            session_factory.return_value.__enter__.return_value = self.session
            self.assertEqual(pilot.main(), 1)
        self.assertIn(live.NEEDS_REVALIDATION, output.getvalue())
        self.assertNotIn("Stale", output.getvalue())
        self.assertTrue(pilot.load_checkpoint()["review_reason"].startswith(live.NEEDS_REVALIDATION))
        self.fixture.assert_no_writes()

    def test_paper_scanner_still_trades_paper_approved_pair_despite_live_halt_and_bad_live_config(self):
        self.live_path.write_text("invalid live configuration")
        pilot.save_checkpoint(pilot.halted_result(self.fixture.checkpoint, live.NEEDS_REVALIDATION + " | offline halt")["checkpoint"])
        payloads = [page([party_market("Democratic", 1, 11), party_market("Republican", 2, 12)]),
                    page([pair_relationship()]), election_tree("Democratic", 1), election_tree("Republican", 2),
                    exchange_book(1, 11), exchange_book(2, 12)]
        previous = {}
        with patch.object(paper, "PORTFOLIO_PATH", self.fixture.root / "paper_portfolio.json"), \
                patch.object(paper, "TRADE_LOG_PATH", self.fixture.root / "paper_trades.csv"), \
                patch.object(paper, "open_positions", []), patch.object(paper, "paper_balance", 5000), \
                contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                self.session.get.side_effect = [self.fixture.response(payload) for payload in payloads]
                scanner.scan_once(self.session, previous, TOURNAMENT_ID)
            self.assertEqual(len(paper.open_positions), 1)
            self.assertEqual(paper.open_positions[0]["position_type"], "NO-PAIR")
            self.assertEqual(paper.open_positions[0]["quantity"], 100)
            self.assertEqual(paper.paper_balance, 4920)
            self.assertEqual(pilot.load_checkpoint()["state"], pilot.HALTED)
        self.assertEqual(self.paper_path.read_bytes(), self.paper_before)
        self.fixture.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
