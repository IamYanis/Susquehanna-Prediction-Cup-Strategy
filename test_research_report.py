"""Research with invented books: no real API, credential or portfolio access."""
import contextlib
import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import paper_trader
import price_reader as scanner
import research_report as research
from test_api_audit import (
    TOURNAMENT_ID, QUOTE_TIME, election_tree, exchange_book, page,
    pair_relationship, party_market,
)


class ResearchReportTests(unittest.TestCase):
    def setUp(self):
        self.markets = (party_market("Democratic", 1, 11), party_market("Republican", 2, 12))
        for target, name, value in ((scanner, "_last_request_started", None),
                                    (scanner, "_read_cooldown_until", 0),
                                    (scanner.time, "monotonic", 100), (scanner.time, "time", QUOTE_TIME)):
            patcher = patch.object(target, name, return_value=value) if name in ("time", "monotonic") else patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(scanner.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, payload, status=200):
        response = requests.Response()
        response.status_code = status
        response.url = scanner.MARKETS_URL
        response._content = json.dumps(payload).encode()
        return response

    def session(self, payloads):
        session = Mock()
        session.get.side_effect = [self.response(payload) for payload in payloads]
        return session

    def rows(self, bid=.6, ask=.65, relationships=None, extras=(), quantities=None):
        books = [exchange_book(1, 11, bid, ask), exchange_book(2, 12, bid, ask)]
        if quantities is not None:
            for book, quantity in zip(books, quantities):
                for side in ("bids", "asks"):
                    for level in book[side]:
                        level["quantity"] = quantity
        session = self.session(books + list(extras))
        result = research.pair_rows(session, "Test Senate race", self.markets, TOURNAMENT_ID,
                                    [] if relationships is None else relationships)
        session.post.assert_not_called()
        session.delete.assert_not_called()
        return result, session

    def test_empty_graph_still_reads_books_and_never_approves_apparent_gap(self):
        rows, session = self.rows()
        self.assertEqual(session.get.call_count, 2)
        yes, no = rows
        self.assertAlmostEqual(yes["combined_limit_cost"], 1.3)
        self.assertAlmostEqual(no["dem_buy_price"], .4)
        self.assertAlmostEqual(no["combined_limit_cost"], .8)
        self.assertAlmostEqual(no["apparent_price_gap"], .2)
        self.assertTrue(no["meets_price_and_depth_thresholds"])
        self.assertEqual(no["settlement_status"], "UNVERIFIED")
        self.assertIn("No active relationship", no["settlement_note"])
        self.assertTrue(all(row["execution_approved"] is False for row in rows))
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs["params"], {"tournamentId": TOURNAMENT_ID, "depth": 1})
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_tick_rounding_can_remove_a_gap_that_raw_prices_suggest(self):
        books = [exchange_book(1, 11, .4, .4901), exchange_book(2, 12, .4, .4899)]
        rows = research.pair_rows(self.session(books), "Race", self.markets, TOURNAMENT_ID, [])
        yes = rows[0]
        self.assertAlmostEqual(yes["raw_combined_cost"], .98)
        self.assertAlmostEqual(yes["combined_limit_cost"], .985)
        self.assertFalse(yes["meets_price_and_depth_thresholds"])

    def test_exact_threshold_and_fractional_depth_quantity_cap(self):
        rows, _ = self.rows(bid=.51, ask=.55, quantities=(120.8, 50.9))
        self.assertAlmostEqual(rows[1]["apparent_price_gap"], .02)
        self.assertTrue(rows[1]["meets_price_and_depth_thresholds"])
        self.assertEqual(rows[1]["research_quantity"], 50)
        rows, _ = self.rows(quantities=(101, 200))
        self.assertEqual(rows[1]["research_quantity"], 100)
        rows, _ = self.rows(quantities=(49.99, 100))
        self.assertFalse(rows[1]["meets_price_and_depth_thresholds"])

    def test_one_sided_book_preserves_available_leg_without_inventing_cost(self):
        books = [exchange_book(1, 11, .6, None), exchange_book(2, 12, .6, .65)]
        rows = research.pair_rows(self.session(books), "Race", self.markets, TOURNAMENT_ID, [])
        self.assertEqual(rows[0]["quote_status"], "MISSING_BOOK_SIDE")
        self.assertEqual(rows[0]["rep_buy_price"], .65)
        self.assertNotIn("dem_buy_price", rows[0])
        self.assertNotIn("apparent_price_gap", rows[0])
        self.assertEqual(rows[1]["quote_status"], "OBSERVED")

    def test_empty_books_and_market_prices_do_not_become_limit_candidates(self):
        rows, _ = self.rows(bid=None, ask=None)
        self.assertTrue(all(row["quote_status"] == "MISSING_BOOK_SIDE" for row in rows))
        rows, _ = self.rows(bid=.999, ask=1)
        self.assertEqual(rows[0]["quote_status"], "NO_SUPPORTED_LIMIT_PRICE")
        self.assertFalse(rows[0]["meets_price_and_depth_thresholds"])

    def test_closed_market_does_not_fetch_books(self):
        markets = copy.deepcopy(self.markets)
        markets[0]["status"] = "closed"
        session = Mock()
        rows = research.pair_rows(session, "Race", markets, TOURNAMENT_ID, [])
        session.get.assert_not_called()
        self.assertTrue(all(row["quote_status"] == "MARKET_CLOSED" for row in rows))

    def test_stale_wrong_scope_and_network_reads_are_not_rankable(self):
        stale = exchange_book()
        stale["asOf"]["at"] = "2026-10-07T11:59:00Z"
        wrong = exchange_book(2, 12)
        for first in (stale, wrong):
            rows = research.pair_rows(self.session([first]), "Race", self.markets, TOURNAMENT_ID, [])
            self.assertTrue(all("apparent_price_gap" not in row for row in rows))
            self.assertTrue(all(not row["meets_price_and_depth_thresholds"] for row in rows))
        session = Mock()
        session.get.side_effect = requests.Timeout("fake-secret-key-in-error")
        rows = research.pair_rows(session, "Race", self.markets, TOURNAMENT_ID, [])
        self.assertNotIn("fake-secret-key", str(rows))
        self.assertEqual(rows[0]["quote_status"], "API request failed")

    def test_first_book_ages_out_during_second_read(self):
        old = {"received_at": 94, "quoted_at": QUOTE_TIME, "version": {"at": "2026-10-07T12:00:00Z"}}
        new = dict(old, received_at=100)
        with patch.object(research, "get_best_prices", side_effect=[old, new]):
            rows = research.pair_rows(Mock(), "Race", self.markets, TOURNAMENT_ID, [])
        self.assertEqual(rows[0]["quote_status"], "Pair books became stale during reads")

    def test_relationship_and_rules_verify_only_the_supported_pair_type(self):
        extras = (election_tree("Democratic", 1), election_tree("Republican", 2))
        rows, _ = self.rows(relationships=[pair_relationship()], extras=extras)
        self.assertEqual(rows[0]["settlement_status"], "UNVERIFIED")
        self.assertEqual(rows[1]["settlement_status"], "VERIFIED_NORMAL_SETTLEMENT")
        self.assertFalse(rows[1]["execution_approved"])
        rows, _ = self.rows(relationships=[pair_relationship(exhaustive=True)], extras=extras)
        self.assertTrue(all(row["settlement_status"] == "VERIFIED_NORMAL_SETTLEMENT" for row in rows))

    def test_larger_exhaustive_group_and_mismatched_rules_do_not_approve_yes(self):
        extras = (election_tree("Democratic", 1), election_tree("Republican", 2))
        rows, _ = self.rows(relationships=[pair_relationship(exhaustive=True, extra_member=True)], extras=extras)
        self.assertEqual(rows[0]["settlement_status"], "UNVERIFIED")
        bad = copy.deepcopy(extras[1])
        bad["root"]["contract_details"]["raceId"] = "999"
        rows, _ = self.rows(relationships=[pair_relationship(exhaustive=True)], extras=(extras[0], bad))
        self.assertTrue(all(row["settlement_status"] == "UNVERIFIED" for row in rows))

    def test_threshold_candidates_rank_above_larger_illiquid_gaps(self):
        rows, _ = self.rows()
        illiquid = dict(rows[1], race="Illiquid", apparent_price_gap=.5,
                        available_pairs=1, meets_price_and_depth_thresholds=False)
        unavailable = dict(rows[0], race="Missing", quote_status="MISSING_BOOK_SIDE")
        unavailable.pop("apparent_price_gap")
        ordered = research.ranked_rows([unavailable, illiquid] + rows)
        self.assertEqual(ordered[0]["race"], "Test Senate race")
        self.assertEqual(ordered[0]["position_type"], "NO-PAIR")
        self.assertEqual(ordered[-1]["race"], "Missing")

    def test_inspection_reports_matching_identity_without_payout_proof(self):
        session = self.session([election_tree("Democratic", 1), election_tree("Republican", 2)])
        review = research.inspect_candidate(session, self.markets, TOURNAMENT_ID)
        self.assertIn("MATCHING_IDENTITY", review)
        self.assertIn("third-party coverage, fusion tickets and refund", review)

    def test_inspection_rejects_different_races_and_freeform_rules(self):
        dem, rep = election_tree("Democratic", 1), election_tree("Republican", 2)
        rep["root"]["contract_details"]["raceId"] = "999"
        review = research.inspect_candidate(self.session([dem, rep]), self.markets, TOURNAMENT_ID)
        self.assertTrue(review.startswith("RULE_MISMATCH"))
        dem["root"]["contract_type"] = "Freeform"
        review = research.inspect_candidate(self.session([dem]), self.markets, TOURNAMENT_ID)
        self.assertTrue(review.startswith("UNREVIEWED"))

    def test_rate_limit_aborts_full_report_and_retains_existing_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.csv"
            path.write_text("previous report")
            session = self.session([{"id": TOURNAMENT_ID, "slug": "midterm-elections", "status": "active",
                                     "currencyName": "SUSQies"}, page(list(self.markets)), page([])])
            session.get.side_effect = list(session.get.side_effect) + [scanner.RateLimitError()]
            output = io.StringIO()
            with patch("sys.argv", ["research_report.py", "--output", str(path)]), \
                    patch.object(research, "load_dotenv"), patch.dict(research.os.environ, {"SIG_API_KEY": "fake-key"}), \
                    patch.object(research.requests, "Session") as factory, contextlib.redirect_stdout(output):
                factory.return_value.__enter__.return_value = session
                self.assertEqual(research.main(), 1)
            self.assertEqual(path.read_text(), "previous report")
            self.assertIn("No completed report saved", output.getvalue())
            session.post.assert_not_called()

    def test_complete_cli_gets_all_prices_inspects_top_pair_and_preserves_paper_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portfolio, trades = root / "paper_portfolio.json", root / "paper_trades.csv"
            portfolio.write_text("portfolio sentinel")
            trades.write_text("trade sentinel")
            report = root / "research.csv"
            session = self.session([
                {"id": TOURNAMENT_ID, "slug": "midterm-elections", "status": "active", "currencyName": "SUSQies"},
                page([self.markets[0]], True, "next"), page([self.markets[1]]), page([]),
                exchange_book(1, 11), exchange_book(2, 12),
                election_tree("Democratic", 1), election_tree("Republican", 2),
            ])
            output = io.StringIO()
            with patch("sys.argv", ["research_report.py", "--output", str(report)]), \
                    patch.object(research, "load_dotenv"), patch.dict(research.os.environ, {"SIG_API_KEY": "fake-key"}), \
                    patch.object(research.requests, "Session") as factory, \
                    patch.object(paper_trader, "PORTFOLIO_PATH", portfolio), \
                    patch.object(paper_trader, "TRADE_LOG_PATH", trades), \
                    patch.object(scanner, "execute_paper_trade") as trade, contextlib.redirect_stdout(output):
                factory.return_value.__enter__.return_value = session
                self.assertEqual(research.main(), 0)
            trade.assert_not_called()
            session.post.assert_not_called()
            session.delete.assert_not_called()
            self.assertEqual(session.get.call_count, 8)
            self.assertTrue(all("/orders" not in call.args[0] for call in session.get.call_args_list))
            with report.open() as stream:
                saved = list(csv.DictReader(stream))
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0]["position_type"], "NO-PAIR")
            self.assertEqual(saved[0]["execution_approved"], "False")
            self.assertIn("MATCHING_IDENTITY", saved[0]["rules_review"])
            self.assertEqual(portfolio.read_text(), "portfolio sentinel")
            self.assertEqual(trades.read_text(), "trade sentinel")
            self.assertNotIn("fake-key", output.getvalue())

    def test_relationship_failure_does_not_turn_into_empty_graph_approval(self):
        with patch.object(research, "get_races", return_value=({"Race": self.markets}, {"1", "2"})), \
                patch.object(research, "fetch_pages", side_effect=requests.Timeout("secret")), \
                contextlib.redirect_stdout(io.StringIO()):
            rows = research.build_report(self.session([exchange_book(1, 11), exchange_book(2, 12),
                                                       election_tree("Democratic", 1), election_tree("Republican", 2)]), TOURNAMENT_ID)
        self.assertTrue(all(row["settlement_status"] == "UNVERIFIED" for row in rows))
        self.assertIn("Relationship evidence unavailable", rows[0]["settlement_note"])
        self.assertNotIn("secret", str(rows))

    def test_csv_escapes_formula_titles_and_atomic_failure_preserves_old_file(self):
        rows, _ = self.rows()
        rows[0]["race"] = " =1+1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.csv"
            research.save_report(rows, path)
            with path.open() as stream:
                saved = list(csv.DictReader(stream))
            self.assertEqual(saved[0]["race"], "' =1+1")
            original = path.read_bytes()
            with patch.object(research.os, "replace", side_effect=OSError("secret")):
                with self.assertRaises(OSError):
                    research.save_report(rows, path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_output_cannot_overwrite_paper_log_or_credentials(self):
        for filename in ("paper_trades.csv", "paper_trades.CSV", ".env", "paper_portfolio.json"):
            with self.subTest(filename=filename), patch("sys.argv", ["research_report.py", "--output", filename]), \
                    patch.object(research, "load_dotenv") as dotenv, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    research.main()
                dotenv.assert_not_called()

    def test_focused_report_only_reads_and_inspects_the_exact_requested_pair(self):
        other = (party_market("Democratic", 3, 13), party_market("Republican", 4, 14))
        other[0]["title"] = "Will the Democratic Party win the Other Senate race?"
        other[1]["title"] = "Will the Republican Party win the Other Senate race?"
        session = self.session([page(list(self.markets) + list(other)), page([]),
                                exchange_book(1, 11), exchange_book(2, 12),
                                election_tree("Democratic", 1), election_tree("Republican", 2)])
        with contextlib.redirect_stdout(io.StringIO()):
            rows = research.build_report(session, TOURNAMENT_ID, market_ids=("1", "2"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["dem_market_id"] == "1" and row["rep_market_id"] == "2" for row in rows))
        self.assertTrue(all("/exchanges/13/" not in call.args[0] and "/exchanges/14/" not in call.args[0]
                            for call in session.get.call_args_list))
        session.post.assert_not_called()
        session.delete.assert_not_called()

    def test_focused_report_rejects_invalid_reversed_missing_or_unmatched_ids(self):
        for ids in (("1", "1"), ("1/evil", "2"), ("1",), ("2", "1"), ("1", "999")):
            with self.subTest(ids=ids), patch.object(research, "get_races", return_value=({"Race": self.markets}, {"1", "2"})), \
                    patch.object(research, "fetch_pages") as fetch, patch.object(research, "get_best_prices") as book:
                with self.assertRaises(scanner.DataValidationError):
                    research.build_report(Mock(), TOURNAMENT_ID, market_ids=ids)
                fetch.assert_not_called()
                book.assert_not_called()

    def test_focused_cli_requires_both_ids_before_loading_credentials(self):
        with patch("sys.argv", ["research_report.py", "--dem-market", "1"]), \
                patch.object(research, "load_dotenv") as dotenv, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                research.main()
            dotenv.assert_not_called()

    def test_focused_cli_default_cannot_overwrite_the_full_report(self):
        session = Mock()
        output = io.StringIO()
        with patch("sys.argv", ["research_report.py", "--dem-market", "1", "--rep-market", "2"]), \
                patch.object(research, "load_dotenv"), patch.dict(research.os.environ, {"SIG_API_KEY": "fake-key"}), \
                patch.object(research.requests, "Session") as factory, patch.object(research, "get_tournament", return_value=TOURNAMENT_ID), \
                patch.object(research, "build_report", return_value=[]) as build, patch.object(research, "save_report") as save, \
                contextlib.redirect_stdout(output):
            factory.return_value.__enter__.return_value = session
            self.assertEqual(research.main(), 0)
        build.assert_called_once_with(session, TOURNAMENT_ID, market_ids=("1", "2"))
        save.assert_called_once_with([], research.PAIR_REPORT_PATH)
        self.assertNotEqual(research.PAIR_REPORT_PATH, research.REPORT_PATH)


if __name__ == "__main__":
    unittest.main()
