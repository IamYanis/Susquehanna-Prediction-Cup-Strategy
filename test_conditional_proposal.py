"""GET-only proposal checks, using fake books, accounts and credentials."""
import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import requests

import order_preview as preview
import price_reader as scanner
from test_account_reader import holdings, isolate_quarantine, order, orders_page, position, tournament
from test_api_audit import (
    OTHER_TOURNAMENT_ID, QUOTE_TIME, TOURNAMENT_ID, election_tree,
    exchange_book, page, pair_relationship, party_market,
)


class ConditionalProposalTests(unittest.TestCase):
    def setUp(self):
        isolate_quarantine(self)
        for obj, name, value in ((preview.time, "monotonic", 100),
                                 (preview.time, "time", QUOTE_TIME)):
            patcher = patch.object(obj, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("_last_request_started", None), ("_read_cooldown_until", 0),
                            ("READ_REQUEST_SPACING", 0)):
            patcher = patch.object(scanner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def payloads(self):
        return [tournament(), holdings([]), orders_page([]),
                party_market("Democratic", 1, 11), party_market("Republican", 2, 12),
                page([]), election_tree("Democratic", 1), election_tree("Republican", 2),
                exchange_book(1, 11, bid=.875, ask=.88), exchange_book(2, 12, bid=.155, ask=.16)]

    def session(self, payloads):
        session = MagicMock()
        responses = []
        for payload in payloads:
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            responses.append(response)
        session.get.side_effect = responses
        session.__enter__.return_value = session
        return session

    def run_cli(self, session, flags=None):
        output = io.StringIO()
        args = ["order_preview.py", "--dem-market", "1", "--rep-market", "2"]
        args += ["--conditional-proposal"] if flags is None else flags
        with patch("sys.argv", args), patch.object(preview, "load_dotenv"), \
                patch.object(preview.os, "getenv", return_value="fake-proposal-secret"), \
                patch.object(preview.requests, "Session", return_value=session), \
                contextlib.redirect_stdout(output):
            code = preview.main()
        return code, output.getvalue()

    def assert_no_writes(self, session):
        for method in ("post", "delete", "put", "patch"):
            getattr(session, method).assert_not_called()

    def test_missing_relationship_produces_analysis_without_executable_material(self):
        session = self.session(self.payloads())
        account = preview.read_account(session)
        proposal = preview.conditional_proposal(session, account, "1", "2", 100)
        self.assertFalse(proposal["execution_approved"])
        self.assertFalse(proposal["submission_enabled"])
        self.assertFalse(proposal["relationship_verified"])
        self.assertEqual(proposal["quantity"], 1)
        self.assertEqual([leg["price"] for leg in proposal["legs"]], [.125, .845])
        self.assertEqual(proposal["max_new_spend"], .97)
        self.assertEqual([gain for _, gain in proposal["scenarios"]],
                         [.03, 1.03, -.97, 0, -.845, -.125, -.125, -.845])
        for field in ("request", "idempotencyKey", "approval", "market_context"):
            self.assertNotIn(field, proposal)
        self.assert_no_writes(session)

    def test_cli_makes_only_scoped_gets_and_never_opens_state_files(self):
        session = self.session(self.payloads())
        with patch("builtins.open", side_effect=AssertionError("Proposal opened a file")), \
                patch.object(io, "open", side_effect=AssertionError("Proposal opened a file")):
            code, output = self.run_cli(session)
        self.assertEqual(code, 0, output)
        self.assertIn("EXECUTION NOT APPROVED", output)
        self.assertIn("strict execution remains blocked", output)
        self.assertIn("+0.030", output)
        self.assertIn("-0.845", output)
        self.assertNotIn("fake-proposal-secret", output)
        self.assertNotIn("idempotencyKey", output)
        self.assertEqual(session.get.call_count, 10)
        for call in session.get.call_args_list:
            self.assertTrue(call.args[0].startswith(scanner.API_BASE_URL + "/"))
            self.assertFalse(call.kwargs["allow_redirects"])
            if "/tournaments/" not in call.args[0]:
                self.assertEqual(call.kwargs["params"]["tournamentId"], TOURNAMENT_ID)
        self.assert_no_writes(session)

    def test_default_preview_keeps_strict_gate(self):
        session = self.session(self.payloads())
        code, output = self.run_cli(session, flags=[])
        self.assertEqual(code, 1)
        self.assertIn("settlement approval is required", output)
        self.assertEqual(session.get.call_count, 6)
        self.assertNotIn("CONDITIONAL PROPOSAL |", output)
        self.assert_no_writes(session)

    def test_relationship_evidence_does_not_turn_analysis_into_execution_approval(self):
        payloads = self.payloads()
        payloads[5] = page([pair_relationship()])
        session = self.session(payloads)
        account = preview.read_account(session)
        proposal = preview.conditional_proposal(session, account, "1", "2", 100)
        self.assertTrue(proposal["relationship_verified"])
        self.assertFalse(proposal["execution_approved"])
        self.assertNotIn("request", proposal)
        self.assertEqual(session.get.call_count, 10)
        self.assert_no_writes(session)

    def test_slow_account_reads_cannot_produce_a_current_proposal(self):
        session = self.session(self.payloads())
        account = preview.read_account(session)
        with self.assertRaisesRegex(preview.PreviewBlocked, "Account observations are too old"):
            preview.conditional_proposal(session, account, "1", "2", 84)
        self.assert_no_writes(session)

    def test_larger_quantity_and_yes_pair_block_before_secret_or_account_reads(self):
        for flags in (["--quantity", "2"], ["--quantity", "0"], ["--position", "YES-PAIR"]):
            with self.subTest(flags=flags), patch.object(preview, "load_dotenv") as load:
                session = MagicMock()
                code, output = self.run_cli(session, flags=["--conditional-proposal"] + flags)
                self.assertEqual(code, 1)
                self.assertIn("exactly one share", output)
                session.get.assert_not_called()
                load.assert_not_called()

    def test_mismatched_structured_race_stage_settlement_or_scope_blocks(self):
        for mutation in ("raceId", "stageId", "winnerName", "settlement_date", "scope"):
            payloads = self.payloads()
            if mutation == "scope":
                payloads[7]["contexts"][0]["tournament"]["id"] = OTHER_TOURNAMENT_ID
            elif mutation == "settlement_date":
                payloads[7]["root"][mutation] = "2026-11-05T17:00:00Z"
            else:
                payloads[7]["root"]["contract_details"][mutation] = "999"
            with self.subTest(mutation=mutation):
                session = self.session(payloads)
                code, output = self.run_cli(session)
                self.assertEqual(code, 1)
                self.assertNotIn("CONDITIONAL PROPOSAL |", output)
                self.assertEqual(session.get.call_count, 8)
                self.assert_no_writes(session)

    def test_inadequate_liquidity_stale_book_and_rounding_removed_gap_block(self):
        for mutation in ("depth", "missing_bid", "stale", "rounded_gap"):
            payloads = self.payloads()
            if mutation == "depth":
                payloads[8]["bids"][0]["quantity"] = 49
            elif mutation == "missing_bid":
                payloads[8]["bids"] = []
            elif mutation == "stale":
                payloads[8]["asOf"]["at"] = "2026-10-07T11:59:54Z"
            else:
                payloads[8] = exchange_book(1, 11, bid=.509, ask=.51)
                payloads[9] = exchange_book(2, 12, bid=.512, ask=.515)
            with self.subTest(mutation=mutation):
                session = self.session(payloads)
                code, output = self.run_cli(session)
                self.assertEqual(code, 1)
                self.assertNotIn("CONDITIONAL PROPOSAL |", output)
                self.assert_no_writes(session)

    def test_cash_pending_reserves_race_limit_and_existing_selected_exposure_block(self):
        for mutation in ("cash", "reserves", "race_limit", "selected_holding", "selected_order"):
            payloads = self.payloads()
            if mutation == "cash":
                payloads[0]["myBalance"] = .96
            elif mutation == "reserves":
                payloads[0]["myBalance"] = 2.96
                payloads[2] = orders_page([order(exchange_id=90)])
            elif mutation == "race_limit":
                row = position(exchange_id=90)
                row["costBasis"] = 149.04
                payloads[1] = holdings([row])
            elif mutation == "selected_holding":
                payloads[1] = holdings([position()])
            else:
                payloads[2] = orders_page([order()])
            with self.subTest(mutation=mutation):
                session = self.session(payloads)
                code, output = self.run_cli(session)
                self.assertEqual(code, 1)
                self.assertNotIn("CONDITIONAL PROPOSAL |", output)
                self.assert_no_writes(session)

    def test_malformed_relationship_is_not_treated_as_missing_evidence(self):
        payloads = self.payloads()
        payloads[5] = page([{"type": "mutually_exclusive", "status": "active", "id": "bad"}])
        session = self.session(payloads)
        code, output = self.run_cli(session)
        self.assertEqual(code, 1)
        self.assertEqual(session.get.call_count, 6)
        self.assertNotIn("CONDITIONAL PROPOSAL |", output)
        self.assert_no_writes(session)

    def test_network_error_cannot_leak_secret_or_create_a_proposal(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = requests.ConnectionError("Authorization: Bearer fake-proposal-secret")
        code, output = self.run_cli(session)
        self.assertEqual(code, 1)
        self.assertNotIn("fake-proposal-secret", output)
        self.assertNotIn("Authorization", output)
        self.assertNotIn("CONDITIONAL PROPOSAL |", output)
        self.assert_no_writes(session)


if __name__ == "__main__":
    unittest.main()
