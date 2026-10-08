"""Offline regression tests: no credentials or network requests needed."""
import contextlib
import copy
import csv
import io
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests
import paper_trader as trader
import price_reader as scanner
from test_account_reader import isolate_quarantine


class ScannerTests(unittest.TestCase):
    tournament_id = "550e8400-e29b-41d4-a716-446655440000"

    def race_markets(self):
        return ({"Race": ({"id": "1", "status": "open"}, {"id": "2", "status": "open"})}, {"1", "2"})

    def setUp(self):
        isolate_quarantine(self)
        # Every test gets a separate portfolio; never read or write the real one.
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.portfolio_path = Path(temporary_directory.name) / "paper_portfolio.json"
        self.trade_log_path = Path(temporary_directory.name) / "paper_trades.csv"
        for name, value in (("PORTFOLIO_PATH", self.portfolio_path),
                            ("TRADE_LOG_PATH", self.trade_log_path),
                            ("open_positions", []),
                            ("paper_balance", trader.STARTING_BALANCE)):
            patcher = patch.object(trader, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("_last_request_started", None), ("_read_cooldown_until", 0),
                            ("get_tournament", Mock(return_value=self.tournament_id))):
            patcher = patch.object(scanner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def opportunity(self, edge=0.1, quantity=100):
        return {"YES-PAIR": {"cost_per_pair": 1 - edge,
                             "profit_per_pair": edge, "quantity": quantity,
                             "market_context": self.trade_context("Race", 1 - edge)}}

    def trade_context(self, race, cost):
        # Portfolio/log tests use verified synthetic evidence. Distinct fake
        # races also get distinct instruments, so duplicate checks stay real.
        context = self.recorded_context()
        if race != "Race":
            base = int(hashlib.sha256(race.encode()).hexdigest()[:8], 16) + 10000
            context["market_ids"] = [str(base), str(base + 1)]
            context["exchange_ids"] = [str(base + 10), str(base + 11)]
            context["relationships"][0]["members"] = list(zip(context["exchange_ids"], context["market_ids"]))
        context["leg_prices"] = [cost / 2, cost / 2]
        return context

    def paper_trade(self, race, position, cost, profit, quantity, context=None):
        return trader.execute_paper_trade(race, position, cost, profit, quantity,
                                          self.trade_context(race, cost) if context is None else context)

    def recorded_context(self):
        """Public provenance for a verified two-party fixture, never credentials."""
        return {"tournament_id": self.tournament_id, "market_ids": ["1", "2"],
                "exchange_ids": ["11", "12"], "race_key": ["1", "2", "2026-11-03", "General", "Party Winner"],
                "settlement_fingerprint": "a" * 64,
                "relationships": [{"id": "22222222-2222-2222-2222-222222222222", "version": 1,
                                   "isExhaustive": True, "members": [["11", "1"], ["12", "2"]]}],
                "payout_condition": "ordinary_binary_settlement; refunds are separate",
                "leg_prices": [.45, .45], "book_versions": [
                    {"sequence": 1, "at": "2026-10-07T12:00:00.1234567+00:00"},
                    {"sequence": 2, "at": "2026-10-07T12:00:01.1234567+00:00"}]}

    def test_transitions_and_duplicate_after_reappearance(self):
        previous = {}
        scanner.report_race("Race", self.opportunity(), previous)
        scanner.report_race("Race", self.opportunity(), previous)
        scanner.report_race("Race", self.opportunity(0.105), previous)
        self.assertNotIn("CHANGED", self.output.getvalue())
        scanner.report_race("Race", self.opportunity(0.11), previous)
        scanner.report_race("Race", {}, previous)
        scanner.report_race("Race", self.opportunity(), previous)
        self.assertEqual(len(trader.open_positions), 1)
        self.assertEqual(trader.paper_balance, 4910)
        self.assertEqual(sum(line.startswith("APPEARED |") for line in self.output.getvalue().splitlines()), 2)
        self.assertIn("CHANGED |", self.output.getvalue())
        self.assertIn("DISAPPEARED |", self.output.getvalue())

    def test_quantity_change(self):
        previous = {}
        scanner.report_race("Race", self.opportunity(quantity=60), previous)
        scanner.report_race("Race", self.opportunity(quantity=70), previous)
        self.assertIn("CHANGED |", self.output.getvalue())

    def test_risk_limits_and_invalid_numbers(self):
        for cost, quantity in ((0.9, 300), (0.9, 200), (float('nan'), 100)):
            self.assertFalse(self.paper_trade("Race", "YES-PAIR", cost, .1, quantity))
        trader.paper_balance = 10
        self.assertFalse(self.paper_trade("Race", "YES-PAIR", .9, .1, 100))
        self.assertEqual(trader.open_positions, [])
        self.assertEqual(trader.paper_balance, 10)
        trader.paper_balance = 5000
        self.assertTrue(self.paper_trade("Race", "YES-PAIR", .9, .1, 100))
        self.assertFalse(self.paper_trade("Race", "NO-PAIR", .9, .1, 100))
        self.assertEqual(len(trader.open_positions), 1)

    def test_calculations_and_thresholds(self):
        book = {"bid": .45, "ask": .49, "bid_quantity": 200, "ask_quantity": 200}
        result = scanner.find_opportunities(book, book, ("YES-PAIR", "NO-PAIR"))
        self.assertEqual(result["YES-PAIR"]["quantity"], 100)
        book["ask_quantity"] = 49
        self.assertEqual(scanner.find_opportunities(book, book, ("YES-PAIR", "NO-PAIR")), {})
        book.update(bid=.6, ask=.65, bid_quantity=70)
        self.assertAlmostEqual(scanner.find_opportunities(book, book, ("YES-PAIR", "NO-PAIR"))["NO-PAIR"]["cost_per_pair"], .8)
        self.assertEqual(scanner.find_opportunities(None, book), {})

    def test_four_levels_include_exact_boundaries(self):
        for edge, expected in ((-.01, "IGNORE"), (.004999, "IGNORE"), (.005, "WATCH"),
                               (.009999, "WATCH"), (.010, "PAPER TRADE"),
                               (.019999, "PAPER TRADE"), (.020, "STRONG PAPER TRADE")):
            with self.subTest(edge=edge):
                self.assertEqual(scanner.classify_edge(edge), expected)
        for bad in (True, float("nan"), float("inf"), "0.01"):
            with self.subTest(bad=bad), self.assertRaises(scanner.DataValidationError):
                scanner.classify_edge(bad)

    def test_tick_rounded_one_percent_senate_prices_are_paper_trade(self):
        books = [{"bid": bid, "ask": bid + .005, "bid_quantity": 50, "ask_quantity": 50}
                 for bid in (.64, .37)]
        opportunity = scanner.find_opportunities(*books, {"NO-PAIR"})["NO-PAIR"]
        self.assertEqual(opportunity["classification"], "PAPER TRADE")
        self.assertEqual(opportunity["cost_per_pair"], .990)
        self.assertEqual(opportunity["profit_per_pair"], .010)
        self.assertAlmostEqual(opportunity["profit_per_pair"] / opportunity["cost_per_pair"] * 100, 1.010101)
        self.assertEqual(scanner.find_opportunities(*books), {})  # No settlement permission.
        books[0]["bid_quantity"] = 49
        self.assertEqual(scanner.find_opportunities(*books, {"NO-PAIR"}), {})

    def test_watch_does_not_trade_and_upgrade_triggers_trade_below_material_change(self):
        previous = {}
        scanner.report_race("Race", self.opportunity(.009, quantity=1), previous)
        self.assertEqual(trader.open_positions, [])
        self.assertFalse(self.portfolio_path.exists())
        scanner.report_race("Race", self.opportunity(.010, quantity=1), previous)
        self.assertEqual(len(trader.open_positions), 1)
        self.assertIn("WATCH", self.output.getvalue())
        self.assertIn("CHANGED | Race | YES-PAIR | PAPER TRADE", self.output.getvalue())

    def test_paper_trader_requires_evidence_and_rejects_watch_even_when_called_directly(self):
        self.assertFalse(trader.execute_paper_trade("Race", "YES-PAIR", .99, .01, 1))
        context = self.trade_context("Race", .995)
        self.assertFalse(trader.execute_paper_trade("Race", "YES-PAIR", .995, .005, 1, context))
        context["relationships"] = []
        context["leg_prices"] = [.495, .495]
        self.assertFalse(trader.execute_paper_trade("Race", "YES-PAIR", .99, .01, 1, context))
        self.assertEqual(trader.open_positions, [])
        self.assertEqual(trader.paper_balance, 5000)
        self.assertFalse(self.portfolio_path.exists())

    def test_tick_rounding_does_not_promote_sub_one_percent_edge_to_paper_trade(self):
        left = {"ask": .4901, "ask_quantity": 50, "bid": .48, "bid_quantity": 50}
        right = dict(left, ask=.4998)
        found = scanner.find_opportunities(left, right, {"YES-PAIR"})["YES-PAIR"]
        self.assertEqual(found["cost_per_pair"], .995)
        self.assertEqual(found["classification"], "WATCH")

    def manual_senate_fixture(self):
        import paired_account_test as paired
        from test_api_audit import election_tree, party_market

        patcher = patch.object(paired, "MANUAL_TOURNAMENT", self.tournament_id)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(scanner.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        markets, trees = [], []
        for party, market_id, exchange_id in (("Democratic", 153, 842), ("Republican", 154, 843)):
            market = party_market(party, market_id, exchange_id)
            market["title"] = f"Will the {party} Party win the U.S. Senate?"
            markets.append(market)
            tree = election_tree(party, market_id)
            tree["root"].update(contract_type="Freeform", contract_details={
                "description": paired.manual_rule_description(party), "contractType": "Freeform"})
            trees.append(tree)
        _, record = paired.read_manual_evidence(self.manual_session(markets + trees), self.tournament_id, "153", "154")
        versions = self.recorded_context()["book_versions"]
        books = [{"bid": bid, "ask": bid + .005, "bid_quantity": 50, "ask_quantity": 50,
                  "version": version} for bid, version in zip((.64, .37), versions)]
        return markets, trees, books, record["evidence_hash"]

    def manual_session(self, payloads):
        session = Mock()
        responses = []
        for payload in copy.deepcopy(payloads):
            response = Mock(status_code=200)
            response.json.return_value = payload
            responses.append(response)
        session.get.side_effect = responses
        return session

    def test_manual_one_percent_paper_scan_uses_explicit_evidence_and_survives_restart(self):
        markets, trees, books, approval = self.manual_senate_fixture()
        with patch.object(scanner, "get_races", return_value=({"U.S. Senate": markets}, {"153", "154"})), \
                patch.object(scanner, "fetch_pages", return_value=[]), \
                patch.object(scanner, "get_best_prices", side_effect=books):
            session = self.manual_session(markets + trees)
            scanner.scan_once(session, {}, self.tournament_id, manual_approval=approval)
        session.post.assert_not_called()
        session.delete.assert_not_called()
        self.assertEqual(len(trader.open_positions), 1)
        position = trader.open_positions[0]
        self.assertEqual(position["quantity"], 1)
        self.assertEqual(position["classification"], "PAPER TRADE")
        self.assertEqual(position["capital_used"], .99)
        self.assertEqual(position["market_context"]["manual_approval"]["evidence_hash"], approval)
        self.assertIn("PAPER TRADE | edge 1.00%", self.output.getvalue())
        trader.open_positions.clear()
        trader.load_portfolio()
        self.assertEqual(trader.open_positions[0]["classification"], "PAPER TRADE")
        # Paper evidence must never satisfy the machine/live validator.
        with self.assertRaises(ValueError):
            trader.validate_market_context(trader.open_positions[0])
        self.assertFalse(trader.execute_paper_trade("U.S. Senate", "NO-PAIR", .99, .01, 2,
                                                   position["market_context"]))

    def test_manual_paper_pair_is_not_approved_by_default_or_by_wrong_hash(self):
        markets, trees, books, approval = self.manual_senate_fixture()
        for explicit in (None, "a" * 64):
            with self.subTest(explicit=explicit), \
                    patch.object(scanner, "get_races", return_value=({"U.S. Senate": markets}, {"153", "154"})), \
                    patch.object(scanner, "fetch_pages", return_value=[]), \
                    patch.object(scanner, "get_best_prices", side_effect=books), \
                    patch.object(scanner, "execute_paper_trade") as trade:
                scanner.scan_once(self.manual_session(markets + trees), {}, self.tournament_id, manual_approval=explicit)
                trade.assert_not_called()
        self.assertEqual(trader.open_positions, [])

    def test_manual_paper_approval_cannot_enable_repeated_scanning(self):
        with patch("sys.argv", ["price_reader.py", "--manual-settlement-approval", "a" * 64]), \
                contextlib.redirect_stderr(self.output), self.assertRaises(SystemExit), \
                patch.object(scanner.requests, "Session") as session:
            scanner.main()
        session.assert_not_called()

    def test_new_paper_trade_keeps_the_quarantine_cash_reserve(self):
        import execution_quarantine as quarantine

        trader.paper_balance = 1
        with patch.object(quarantine, "reserved_cost", return_value=.125):
            self.assertFalse(self.paper_trade("Race", "YES-PAIR", .99, .01, 1))
        self.assertEqual(trader.paper_balance, 1)
        self.assertEqual(trader.open_positions, [])

    def test_failed_fetch_preserves_state(self):
        previous = {("Race", "YES-PAIR"): self.opportunity()["YES-PAIR"]}
        original = previous.copy()
        with patch.object(scanner, "get_races", side_effect=requests.Timeout):
            scanner.scan_once(Mock(), previous, self.tournament_id)
        with patch.object(scanner, "get_races", return_value=self.race_markets()), \
                patch.object(scanner, "fetch_pages", return_value=[]), \
                patch.object(scanner, "get_pair_rules", return_value=({"YES-PAIR"}, {})), \
                patch.object(scanner, "get_best_prices", side_effect=ValueError):
            scanner.scan_once(Mock(), previous, self.tournament_id)
        self.assertEqual(previous, original)
        self.assertNotIn("DISAPPEARED", self.output.getvalue())

    def test_http_checks_and_bad_book(self):
        session = Mock()
        session.get.return_value.status_code = 500
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError
        with self.assertRaises(requests.HTTPError):
            scanner.fetch_json(session, scanner.MARKETS_URL)
        session.get.assert_called_once_with(scanner.MARKETS_URL, params=None, timeout=10, allow_redirects=False)
        session.get.return_value.json.assert_not_called()
        market = {"id": "1", "isComposite": False, "isMultiOutcome": False,
                  "exchanges": [{"id": "10", "option": "YES"}]}
        with patch.object(scanner, "fetch_json", return_value={
                "exchangeId": "10", "marketId": "1",
                "asOf": {"sequence": 1, "at": datetime.now(timezone.utc).isoformat()},
                "bids": [{"price": "nan", "quantity": 100}],
                "asks": [{"price": .5, "quantity": 100}]}):
            with self.assertRaises(ValueError):
                scanner.get_best_prices(session, market, self.tournament_id)

    def test_removed_race_disappears(self):
        opportunity = self.opportunity()["YES-PAIR"]
        opportunity["market_context"] = {"market_ids": ["1", "2"]}
        previous = {("Race", "YES-PAIR"): opportunity}
        with patch.object(scanner, "get_races", return_value=({}, set())), \
                patch.object(scanner, "fetch_pages", return_value=[]):
            scanner.scan_once(Mock(), previous, self.tournament_id)
        self.assertEqual(previous, {})

    def test_loop_keeps_observations_until_interrupt(self):
        with patch("sys.argv", ["price_reader.py"]), \
                patch.object(scanner, "load_dotenv"), \
                patch.dict(scanner.os.environ, {"SIG_API_KEY": "test-only"}), \
                patch.object(scanner, "scan_once") as scan, \
                patch.object(scanner.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            self.assertEqual(scanner.main(), 0)
        self.assertEqual(scan.call_count, 2)
        self.assertIs(scan.call_args_list[0].args[1], scan.call_args_list[1].args[1])

    def test_missing_file_starts_fresh_without_creating_a_file(self):
        self.assertFalse(trader.load_portfolio())
        self.assertEqual(trader.paper_balance, 5000)
        self.assertEqual(trader.open_positions, [])
        self.assertFalse(self.portfolio_path.exists())

    def test_restore_cash_positions_and_duplicate_protection(self):
        self.assertTrue(self.paper_trade("Race", "YES-PAIR", .9, .1, 100))
        saved = self.portfolio_path.read_text()
        trader.paper_balance = 5000
        trader.open_positions.clear()
        self.assertTrue(trader.load_portfolio())
        self.assertEqual(trader.paper_balance, 4910)
        self.assertEqual(len(trader.open_positions), 1)
        self.assertFalse(self.paper_trade("Race", "YES-PAIR", .9, .1, 100))
        self.assertEqual(self.portfolio_path.read_text(), saved)
        # Loaded holdings still count toward exposure for the other position type.
        self.assertFalse(self.paper_trade("Race", "NO-PAIR", .9, .1, 100))
        self.assertEqual(self.portfolio_path.read_text(), saved)

    def test_recorded_provenance_survives_restart_and_title_change(self):
        context = self.recorded_context()
        self.assertTrue(self.paper_trade("Race", "YES-PAIR", .9, .1, 100, context))
        saved = self.portfolio_path.read_bytes()
        trader.open_positions.clear()
        trader.paper_balance = 5000
        self.assertTrue(trader.load_portfolio())
        restored = trader.open_positions[0]["market_context"]
        self.assertEqual(restored, context)
        self.assertFalse(self.paper_trade("Renamed race", "YES-PAIR", .9, .1, 100, context))
        # The other pair type still shares the original race's capital limit.
        self.assertFalse(self.paper_trade("Renamed race", "NO-PAIR", .9, .1, 100, context))
        self.assertEqual(self.portfolio_path.read_bytes(), saved)
        self.assertEqual(trader.paper_balance, 4910)

    def test_invalid_saved_provenance_stops_without_rewriting_holdings(self):
        self.paper_trade("Race", "YES-PAIR", .9, .1, 100, self.recorded_context())
        valid = json.loads(self.portfolio_path.read_text())
        for changes in ({"tournament_id": None}, {"exchange_ids": ["11", "11"]},
                        {"leg_prices": [.2, .2]}, {"settlement_fingerprint": "wrong"},
                        {"book_versions": [{"sequence": 1, "at": None}]},
                        {"relationships": ["invalid"]}):
            with self.subTest(changes=changes):
                data = copy.deepcopy(valid)
                data["open_positions"][0]["market_context"].update(changes)
                self.portfolio_path.write_text(json.dumps(data))
                original = self.portfolio_path.read_bytes()
                with self.assertRaises(trader.PortfolioError):
                    trader.load_portfolio()
                self.assertEqual(self.portfolio_path.read_bytes(), original)
                self.assertEqual(trader.paper_balance, 4910)
    def test_corrupt_file_stops_startup_before_network_access(self):
        self.portfolio_path.write_text("{unfinished JSON")
        with patch("sys.argv", ["price_reader.py", "--once"]), \
                patch.object(scanner, "load_dotenv"), \
                patch.dict(scanner.os.environ, {"SIG_API_KEY": "test-only"}), \
                patch.object(scanner.requests, "Session") as session:
            self.assertEqual(scanner.main(), 1)
        session.assert_not_called()
        self.assertEqual(self.portfolio_path.read_text(), "{unfinished JSON")
        self.assertEqual(trader.paper_balance, 5000)
        self.assertEqual(trader.open_positions, [])

    def test_invalid_snapshots_leave_memory_and_file_untouched(self):
        self.paper_trade("Race", "YES-PAIR", .9, .1, 100)
        valid = json.loads(self.portfolio_path.read_text())
        cases = []
        # A syntactically valid file can still contain unsafe or inconsistent data.
        for field, value in (("version", 99), ("version", True),
                             ("starting_balance", 6000), ("paper_balance", -1),
                             ("paper_balance", float("nan")), ("paper_balance", 5000),
                             ("open_positions", "wrong type")):
            data = copy.deepcopy(valid)
            data[field] = value
            cases.append(data)
        for field, value in (("race", ""), ("position_type", "UNKNOWN"),
                             ("quantity", False), ("quantity", -1),
                             ("capital_used", 91), ("minimum_profit", 99),
                             ("cost_per_pair", float("inf"))):
            data = copy.deepcopy(valid)
            data["open_positions"][0][field] = value
            cases.append(data)
        duplicate = copy.deepcopy(valid)
        duplicate["open_positions"].append(copy.deepcopy(valid["open_positions"][0]))
        duplicate["paper_balance"] -= 90
        cases.extend([duplicate, [], None, {}])

        for data in cases:
            with self.subTest(data=data):
                self.portfolio_path.write_text(json.dumps(data))
                original = self.portfolio_path.read_bytes()
                with self.assertRaises(trader.PortfolioError):
                    trader.load_portfolio()
                self.assertEqual(trader.paper_balance, 4910)
                self.assertEqual(len(trader.open_positions), 1)
                self.assertEqual(self.portfolio_path.read_bytes(), original)

    def test_unreadable_file_does_not_reset_existing_memory(self):
        self.paper_trade("Race", "YES-PAIR", .9, .1, 100)
        with patch.object(Path, "open", side_effect=PermissionError):
            with self.assertRaises(trader.PortfolioError):
                trader.load_portfolio()
        self.assertEqual(trader.paper_balance, 4910)
        self.assertEqual(len(trader.open_positions), 1)

    def test_failed_save_preserves_last_snapshot_and_cash(self):
        self.paper_trade("First race", "YES-PAIR", .9, .1, 100)
        original = self.portfolio_path.read_bytes()
        for target in ("replace", "dump"):
            with self.subTest(failure=target):
                module = trader.os if target == "replace" else trader.json
                with patch.object(module, target, side_effect=OSError("test write failure")):
                    with self.assertRaises(trader.PortfolioError):
                        self.paper_trade("Second race", "YES-PAIR", .9, .1, 100)
                self.assertEqual(trader.paper_balance, 4910)
                self.assertEqual(len(trader.open_positions), 1)
                self.assertEqual(self.portfolio_path.read_bytes(), original)
                self.assertEqual(list(self.portfolio_path.parent.glob("*.tmp")), [])

    def test_failed_first_save_stops_scanner_without_accepting_trade(self):
        book = {"bid": .4, "ask": .45, "bid_quantity": 100, "ask_quantity": 100,
                "version": {"sequence": 1, "at": "2026-10-07T12:00:00Z"}}
        with patch("sys.argv", ["price_reader.py"]), \
                patch.object(scanner, "load_dotenv"), \
                patch.dict(scanner.os.environ, {"SIG_API_KEY": "test-only"}), \
                patch.object(scanner.requests, "Session"), \
                patch.object(scanner, "get_races", return_value=self.race_markets()), \
                patch.object(scanner, "fetch_pages", return_value=[]), \
                patch.object(scanner, "get_best_prices", return_value=book), \
                patch.object(scanner, "get_pair_rules", return_value=({"YES-PAIR"}, self.recorded_context())), \
                patch.object(trader.os, "replace", side_effect=OSError), \
                patch.object(scanner.time, "sleep") as sleep:
            self.assertEqual(scanner.main(), 1)
        sleep.assert_not_called()
        self.assertEqual(trader.paper_balance, 5000)
        self.assertEqual(trader.open_positions, [])
        self.assertFalse(self.portfolio_path.exists())
        self.assertIn("paper trade was not accepted", self.output.getvalue())

    def test_actual_process_restart_restores_portfolio(self):
        # Two separate Python processes prove the state lives on disk, not just
        # in the test's imported module. Both use fake trades and a temporary file.
        script = """
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
import paper_trader as trader
trader.PORTFOLIO_PATH = Path(sys.argv[1])
trader.TRADE_LOG_PATH = trader.PORTFOLIO_PATH.with_name("paper_trades.csv")
restored = trader.load_portfolio()
accepted = trader.execute_paper_trade("Race", "YES-PAIR", .9, .1, 100, json.loads(sys.argv[2]))
print(json.dumps([restored, accepted, trader.paper_balance, len(trader.open_positions)]))
"""
        results = []
        for _ in range(2):
            process = subprocess.run(
                [sys.executable, "-c", script, str(self.portfolio_path), json.dumps(self.recorded_context())],
                cwd=Path(trader.__file__).parent, capture_output=True, text=True,
                check=True, timeout=10,
            )
            results.append(json.loads(process.stdout.splitlines()[-1]))
        self.assertEqual(results, [[False, True, 4910, 1], [True, False, 4910, 1]])
        self.assertEqual(len(self.read_trade_log()), 1)

    def read_trade_log(self):
        with self.trade_log_path.open(encoding="utf-8", newline="") as log_file:
            return list(csv.DictReader(log_file))

    def test_csv_records_both_trade_types_and_preserves_quoted_races(self):
        race = 'Example, "North" race'
        self.assertTrue(self.paper_trade(race, "YES-PAIR", .9, .1, 60))
        self.assertTrue(self.paper_trade(race, "NO-PAIR", .8, .2, 60))
        rows = self.read_trade_log()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["race"], race)
        self.assertEqual([row["position_type"] for row in rows], ["YES-PAIR", "NO-PAIR"])
        self.assertEqual([float(row["cash_remaining"]) for row in rows], [4946, 4898])
        self.assertAlmostEqual(float(rows[0]["capital_used"]), 54)
        self.assertAlmostEqual(float(rows[1]["minimum_expected_profit"]), 12)
        self.assertEqual(len({row["trade_id"] for row in rows}), 2)
        self.assertEqual(rows[0]["timestamp_utc"], trader.open_positions[0]["timestamp_utc"])
        self.assertTrue(rows[0]["timestamp_utc"].endswith("+00:00"))
        self.assertEqual(tuple(rows[0]), trader.TRADE_LOG_FIELDS)

    def test_duplicate_and_rejected_trades_do_not_add_log_rows(self):
        self.paper_trade("Race", "YES-PAIR", .9, .1, 100)
        original = self.trade_log_path.read_bytes()
        self.assertFalse(self.paper_trade("Race", "YES-PAIR", .9, .1, 100))
        self.assertFalse(self.paper_trade("Race", "NO-PAIR", .9, .1, 100))
        self.assertFalse(self.paper_trade("Other", "YES-PAIR", .9, .1, 300))
        self.assertEqual(self.trade_log_path.read_bytes(), original)

    def test_csv_failure_keeps_trade_saved_and_startup_recovers_once(self):
        self.paper_trade("First", "YES-PAIR", .9, .1, 100)
        original = self.trade_log_path.read_bytes()
        original_replace = trader.os.replace

        def fail_csv_replace(source, destination):
            if destination == self.trade_log_path:
                raise OSError("simulated CSV write failure")
            return original_replace(source, destination)

        with patch.object(trader.os, "replace", side_effect=fail_csv_replace):
            with self.assertRaises(trader.PortfolioError):
                self.paper_trade("Second", "NO-PAIR", .8, .2, 100)
        # The JSON accepted the trade before logging failed; do not roll it back.
        self.assertEqual(trader.paper_balance, 4830)
        self.assertEqual(len(trader.open_positions), 2)
        self.assertEqual(self.trade_log_path.read_bytes(), original)
        self.assertEqual(len(json.loads(self.portfolio_path.read_text())["open_positions"]), 2)
        self.assertEqual(list(self.trade_log_path.parent.glob("*.tmp")), [])
        trade = trader.open_positions[1].copy()
        trader.open_positions.clear()
        trader.paper_balance = 5000
        self.assertTrue(trader.load_portfolio())
        self.assertEqual(len(self.read_trade_log()), 2)
        row = self.read_trade_log()[1]
        self.assertEqual(row["trade_id"], trade["trade_id"])
        self.assertEqual(row["timestamp_utc"], trade["timestamp_utc"])
        self.assertEqual(float(row["cash_remaining"]), 4830)
        self.assertTrue(trader.load_portfolio())
        self.assertEqual(len(self.read_trade_log()), 2)
        self.assertFalse(self.paper_trade("Second", "NO-PAIR", .8, .2, 100))

    def test_failed_portfolio_save_does_not_log_unaccepted_trade(self):
        with patch.object(trader.os, "replace", side_effect=OSError):
            with self.assertRaises(trader.PortfolioError):
                self.paper_trade("Race", "YES-PAIR", .9, .1, 100)
        self.assertFalse(self.trade_log_path.exists())
        self.assertEqual(trader.open_positions, [])

    def test_missing_csv_is_rebuilt_with_original_trade_values(self):
        self.paper_trade("First", "YES-PAIR", .9, .1, 100)
        self.paper_trade("Second", "YES-PAIR", .9, .1, 100)
        original_rows = self.read_trade_log()
        self.trade_log_path.unlink()
        self.assertTrue(trader.load_portfolio())
        self.assertEqual(self.read_trade_log(), original_rows)

    def test_old_portfolio_loads_without_inventing_trade_timestamps(self):
        legacy = {"race": "Old race", "position_type": "YES-PAIR", "quantity": 100,
                  "cost_per_pair": .9, "capital_used": 90, "minimum_profit": 10}
        trader.save_portfolio(4910, [legacy])
        self.assertTrue(trader.load_portfolio())
        self.assertEqual(trader.open_positions, [legacy])
        self.assertFalse(self.trade_log_path.exists())
        self.paper_trade("New race", "YES-PAIR", .9, .1, 100)
        self.assertEqual([row["race"] for row in self.read_trade_log()], ["New race"])

    def test_csv_history_survives_a_fresh_paper_portfolio(self):
        self.paper_trade("Race", "YES-PAIR", .9, .1, 100)
        first_id = self.read_trade_log()[0]["trade_id"]
        self.portfolio_path.unlink()
        self.assertFalse(trader.load_portfolio())
        self.paper_trade("Race", "YES-PAIR", .9, .1, 100)
        rows = self.read_trade_log()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[1]["trade_id"], first_id)

    def test_corrupt_csv_is_left_untouched(self):
        self.paper_trade("Race", "YES-PAIR", .9, .1, 100)
        valid_log = self.trade_log_path.read_text()
        cases = ["wrong,header\n", valid_log + "truncated,row\n",
                 valid_log + valid_log.splitlines()[1] + "\n",
                 valid_log.replace("4910.0", "1234.0")]
        for contents in cases:
            with self.subTest(contents=contents):
                self.trade_log_path.write_text(contents)
                original = self.trade_log_path.read_bytes()
                with self.assertRaises(trader.PortfolioError):
                    trader.load_portfolio()
                self.assertEqual(self.trade_log_path.read_bytes(), original)
                self.assertEqual(trader.paper_balance, 4910)
                self.assertEqual(len(trader.open_positions), 1)

    def test_incomplete_trade_metadata_is_rejected_before_logging(self):
        self.paper_trade("Race", "YES-PAIR", .9, .1, 100)
        data = json.loads(self.portfolio_path.read_text())
        del data["open_positions"][0]["timestamp_utc"]
        self.portfolio_path.write_text(json.dumps(data))
        original_log = self.trade_log_path.read_bytes()
        with self.assertRaises(trader.PortfolioError):
            trader.load_portfolio()
        self.assertEqual(self.trade_log_path.read_bytes(), original_log)


if __name__ == "__main__":
    unittest.main()
