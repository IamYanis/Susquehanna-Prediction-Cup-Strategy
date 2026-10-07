"""Offline regression tests: no credentials or network requests needed."""
import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import paper_trader as trader
import price_reader as scanner


class ScannerTests(unittest.TestCase):
    def setUp(self):
        # Every test gets a separate portfolio; never read or write the real one.
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.portfolio_path = Path(temporary_directory.name) / "paper_portfolio.json"
        for name, value in (("PORTFOLIO_PATH", self.portfolio_path),
                            ("open_positions", []),
                            ("paper_balance", trader.STARTING_BALANCE)):
            patcher = patch.object(trader, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def opportunity(self, edge=0.1, quantity=100):
        return {"YES-PAIR": {"cost_per_pair": 1 - edge,
                             "profit_per_pair": edge, "quantity": quantity}}

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
            self.assertFalse(trader.execute_paper_trade("Race", "YES-PAIR", cost, .1, quantity))
        trader.paper_balance = 10
        self.assertFalse(trader.execute_paper_trade("Race", "YES-PAIR", .9, .1, 100))
        self.assertEqual(trader.open_positions, [])
        self.assertEqual(trader.paper_balance, 10)
        trader.paper_balance = 5000
        self.assertTrue(trader.execute_paper_trade("Race", "YES-PAIR", .9, .1, 100))
        self.assertFalse(trader.execute_paper_trade("Race", "NO-PAIR", .9, .1, 100))
        self.assertEqual(len(trader.open_positions), 1)

    def test_calculations_and_thresholds(self):
        book = {"bid": .45, "ask": .49, "bid_quantity": 200, "ask_quantity": 200}
        result = scanner.find_opportunities(book, book)
        self.assertEqual(result["YES-PAIR"]["quantity"], 100)
        book["ask_quantity"] = 49
        self.assertEqual(scanner.find_opportunities(book, book), {})
        book.update(bid=.6, ask=.65, bid_quantity=70)
        self.assertAlmostEqual(scanner.find_opportunities(book, book)["NO-PAIR"]["cost_per_pair"], .8)
        self.assertEqual(scanner.find_opportunities(None, book), {})

    def test_failed_fetch_preserves_state(self):
        previous = {("Race", "YES-PAIR"): self.opportunity()["YES-PAIR"]}
        original = previous.copy()
        with patch.object(scanner, "get_races", side_effect=requests.Timeout):
            scanner.scan_once(Mock(), previous)
        with patch.object(scanner, "get_races", return_value={"Race": (1, 2)}), \
                patch.object(scanner, "get_best_prices", side_effect=ValueError):
            scanner.scan_once(Mock(), previous)
        self.assertEqual(previous, original)
        self.assertNotIn("DISAPPEARED", self.output.getvalue())

    def test_http_checks_and_bad_book(self):
        session = Mock()
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError
        with self.assertRaises(requests.HTTPError):
            scanner.fetch_json(session, "https://example.test")
        session.get.assert_called_once_with("https://example.test", timeout=10)
        session.get.return_value.json.assert_not_called()
        with patch.object(scanner, "fetch_json", return_value={"exchanges": [
                {"bids": [{"price": "nan", "quantity": 100}],
                 "asks": [{"price": .5, "quantity": 100}]}]}):
            with self.assertRaises(ValueError):
                scanner.get_best_prices(session, 1)

    def test_removed_race_disappears(self):
        previous = {("Race", "YES-PAIR"): self.opportunity()["YES-PAIR"]}
        with patch.object(scanner, "get_races", return_value={}):
            scanner.scan_once(Mock(), previous)
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
        self.assertTrue(trader.execute_paper_trade("Race", "YES-PAIR", .9, .1, 100))
        saved = self.portfolio_path.read_text()
        trader.paper_balance = 5000
        trader.open_positions.clear()
        self.assertTrue(trader.load_portfolio())
        self.assertEqual(trader.paper_balance, 4910)
        self.assertEqual(len(trader.open_positions), 1)
        self.assertFalse(trader.execute_paper_trade("Race", "YES-PAIR", .9, .1, 100))
        self.assertEqual(self.portfolio_path.read_text(), saved)
        # Loaded holdings still count toward exposure for the other position type.
        self.assertFalse(trader.execute_paper_trade("Race", "NO-PAIR", .9, .1, 100))
        self.assertEqual(self.portfolio_path.read_text(), saved)

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
        trader.execute_paper_trade("Race", "YES-PAIR", .9, .1, 100)
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
        trader.execute_paper_trade("Race", "YES-PAIR", .9, .1, 100)
        with patch.object(Path, "open", side_effect=PermissionError):
            with self.assertRaises(trader.PortfolioError):
                trader.load_portfolio()
        self.assertEqual(trader.paper_balance, 4910)
        self.assertEqual(len(trader.open_positions), 1)

    def test_failed_save_preserves_last_snapshot_and_cash(self):
        trader.execute_paper_trade("First race", "YES-PAIR", .9, .1, 100)
        original = self.portfolio_path.read_bytes()
        for target in ("replace", "dump"):
            with self.subTest(failure=target):
                module = trader.os if target == "replace" else trader.json
                with patch.object(module, target, side_effect=OSError("test write failure")):
                    with self.assertRaises(trader.PortfolioError):
                        trader.execute_paper_trade("Second race", "YES-PAIR", .9, .1, 100)
                self.assertEqual(trader.paper_balance, 4910)
                self.assertEqual(len(trader.open_positions), 1)
                self.assertEqual(self.portfolio_path.read_bytes(), original)
                self.assertEqual(list(self.portfolio_path.parent.glob("*.tmp")), [])

    def test_failed_first_save_stops_scanner_without_accepting_trade(self):
        book = {"bid": .4, "ask": .45, "bid_quantity": 100, "ask_quantity": 100}
        with patch("sys.argv", ["price_reader.py"]), \
                patch.object(scanner, "load_dotenv"), \
                patch.dict(scanner.os.environ, {"SIG_API_KEY": "test-only"}), \
                patch.object(scanner.requests, "Session"), \
                patch.object(scanner, "get_races", return_value={"Race": (1, 2)}), \
                patch.object(scanner, "get_best_prices", return_value=book), \
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
import paper_trader as trader
trader.PORTFOLIO_PATH = Path(sys.argv[1])
restored = trader.load_portfolio()
accepted = trader.execute_paper_trade("Race", "YES-PAIR", .9, .1, 100)
print(json.dumps([restored, accepted, trader.paper_balance, len(trader.open_positions)]))
"""
        results = []
        for _ in range(2):
            process = subprocess.run(
                [sys.executable, "-c", script, str(self.portfolio_path)],
                cwd=Path(trader.__file__).parent, capture_output=True, text=True,
                check=True, timeout=10,
            )
            results.append(json.loads(process.stdout.splitlines()[-1]))
        self.assertEqual(results, [[False, True, 4910, 1], [True, False, 4910, 1]])


if __name__ == "__main__":
    unittest.main()
