"""Read-only account checks using fake responses, credentials, and paper files."""
import contextlib
import copy
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

import account_reader as account
import execution_quarantine as quarantine
import paper_trader as paper
import price_reader as scanner


TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440001"


def isolate_quarantine(test):
    """Account tests must never load the user's real execution/quarantine files."""
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    for name, path in (("STATE_PATH", root / "quarantine.json"),
                       ("SOURCE_DIR", root / "source"), ("ACTIVE_DIR", root / "active")):
        patcher = patch.object(quarantine, name, path)
        patcher.start()
        test.addCleanup(patcher.stop)


def tournament():
    return {
        "id": TOURNAMENT_ID, "slug": "midterm-elections", "name": "Test Cup",
        "status": "active", "currencyName": "SUSQies", "myBalance": 99990.8,
        "initialBalance": 100000, "isPendingEnrolment": False,
    }


def position(exchange_id=11, market_id=1, quantity=7):
    # Signed quantity is the exchange convention. Keep a NO holding negative.
    return {
        "exchangeId": exchange_id, "marketId": market_id,
        "marketTitle": "Synthetic election market", "settled": False,
        "quantity": quantity, "currentPrice": .6, "marketValue": 4.2,
        "costBasis": 3.5, "unrealizedPnl": .7,
    }


def holdings(positions=None):
    if positions is None:
        long_yes = position()
        long_no = position(12, 2, -3)
        long_no.update(currentPrice=.4, marketValue=1.2, costBasis=.9, unrealizedPnl=.3)
        positions = [long_yes, long_no]
    return {
        "positions": positions,
        "summary": {
            "totalMarketValue": sum(row["marketValue"] for row in positions),
            "totalCostBasis": sum(row["costBasis"] for row in positions),
            "totalUnrealizedPnl": sum(row["unrealizedPnl"] for row in positions),
        },
    }


def order(order_id=101, exchange_id=11):
    return {
        "id": order_id, "exchangeId": exchange_id, "side": "yes", "action": "buy",
        "quantity": 2, "priceLimit": .55, "open": True,
        "tournamentId": TOURNAMENT_ID, "createdAt": "2026-10-07T10:00:00+00:00",
        "expirationDate": None,
    }


def orders_page(rows=None, more=False, cursor=None, coverage=None):
    result = {
        "data": [order()] if rows is None else rows,
        "pagination": {"hasMore": more, "nextCursor": cursor},
    }
    if coverage is not None:
        result["coverage"] = coverage
    return result


class AccountReaderTests(unittest.TestCase):
    def setUp(self):
        isolate_quarantine(self)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.session = Mock()

    def read_with(self, metadata=None, portfolio=None, pages=None):
        responses = [tournament() if metadata is None else metadata,
                     holdings() if portfolio is None else portfolio]
        responses.extend([orders_page()] if pages is None else pages)
        with patch.object(account, "fetch_json", side_effect=responses) as fetch:
            result = account.read_account(self.session)
        return result, fetch

    def test_three_scoped_gets_and_reported_totals(self):
        result, fetch = self.read_with()
        self.assertEqual(result["tournament"]["id"], TOURNAMENT_ID)
        self.assertEqual(result["tournament"]["myBalance"], 99990.8)
        self.assertEqual(result["tournament"]["initialBalance"], 100000)
        self.assertEqual(result["positions"][1]["quantity"], -3)
        self.assertAlmostEqual(result["summary"]["totalMarketValue"], 5.4)
        self.assertAlmostEqual(result["summary"]["totalCostBasis"], 4.4)
        self.assertAlmostEqual(result["summary"]["totalUnrealizedPnl"], 1)
        self.assertEqual([row["id"] for row in result["orders"]], [101])
        self.assertEqual(fetch.call_count, 3)
        urls = [call.args[1] for call in fetch.call_args_list]
        self.assertEqual(urls, [f"{scanner.API_BASE_URL}/tournaments/midterm-elections",
                                f"{scanner.API_BASE_URL}/tournaments/midterm-elections/portfolio/positions",
                                f"{scanner.API_BASE_URL}/orders"])
        # Positions use the tournament slug in their path; orders use its UUID.
        params = fetch.call_args_list[-1].kwargs["params"]
        self.assertEqual(params["tournamentId"], TOURNAMENT_ID)
        self.assertEqual(params["status"], "open")
        self.assertIs(fetch.call_args_list[0].args[0], self.session)

    def test_empty_account_is_valid_and_zero_balance_is_preserved(self):
        metadata = tournament()
        metadata["myBalance"] = 0
        result, _ = self.read_with(metadata, holdings([]), [orders_page([])])
        self.assertEqual(result["tournament"]["myBalance"], 0)
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["summary"]["totalMarketValue"], 0)

    def test_pending_enrolment_rejects_before_holdings_or_orders(self):
        metadata = tournament()
        metadata["isPendingEnrolment"] = True
        with patch.object(account, "fetch_json", return_value=metadata) as fetch:
            with self.assertRaises(ValueError):
                account.read_account(self.session)
        self.assertEqual(fetch.call_count, 1)

    def test_invalid_metadata_rejects_before_positions(self):
        cases = [("id", None), ("id", "wrong"), ("slug", "other-cup"),
                 ("name", None), ("status", "closed"), ("currencyName", "USD"),
                 ("isPendingEnrolment", "false"), ("myBalance", True),
                 ("myBalance", float("nan")), ("myBalance", -1),
                 ("initialBalance", False), ("initialBalance", float("inf")),
                 ("initialBalance", -1)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                metadata = tournament()
                metadata[field] = value
                with patch.object(account, "fetch_json", return_value=metadata) as fetch:
                    with self.assertRaises(ValueError):
                        account.read_account(self.session)
                self.assertEqual(fetch.call_count, 1)

    def test_invalid_position_data_is_rejected(self):
        cases = [("exchangeId", True), ("exchangeId", 0), ("marketId", False),
                 ("marketId", -1), ("marketTitle", None), ("settled", "false"),
                 ("quantity", True), ("quantity", float("nan")),
                 ("currentPrice", False), ("currentPrice", -0.01),
                 ("currentPrice", 1.01), ("marketValue", float("inf")),
                 ("marketValue", -1), ("costBasis", True), ("costBasis", -1),
                 ("unrealizedPnl", float("nan"))]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                portfolio = holdings()
                portfolio["positions"][0][field] = value
                with self.assertRaises(ValueError):
                    self.read_with(portfolio=portfolio)

    def test_duplicate_position_exchange_id_is_rejected(self):
        portfolio = holdings()
        portfolio["positions"][1]["exchangeId"] = 11
        with self.assertRaises(ValueError):
            self.read_with(portfolio=portfolio)

    def test_negative_reported_pnl_is_valid(self):
        row = position()
        row.update(costBasis=5, unrealizedPnl=-.8)
        result, _ = self.read_with(portfolio=holdings([row]))
        self.assertEqual(result["positions"][0]["unrealizedPnl"], -.8)

    def test_invalid_or_inconsistent_summary_is_rejected(self):
        cases = [("totalMarketValue", True), ("totalMarketValue", float("nan")),
                 ("totalCostBasis", -1), ("totalUnrealizedPnl", float("inf")),
                 ("totalMarketValue", 999), ("totalCostBasis", 999),
                 ("totalUnrealizedPnl", 999)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                portfolio = holdings()
                portfolio["summary"][field] = value
                with self.assertRaises(ValueError):
                    self.read_with(portfolio=portfolio)

    def test_positions_and_summary_are_required(self):
        for portfolio in ({}, {"positions": None, "summary": {}},
                          {"positions": [], "summary": {}},
                          {"positions": [], "summary": None}):
            with self.subTest(portfolio=portfolio):
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    self.read_with(portfolio=portfolio)

    def test_invalid_order_values_and_wrong_scope_are_rejected(self):
        cases = [("id", True), ("id", 0), ("exchangeId", False),
                 ("exchangeId", -1), ("side", "other"), ("action", "cancel"),
                 ("quantity", True), ("quantity", 0), ("quantity", -1),
                 ("quantity", float("nan")), ("priceLimit", True),
                 ("priceLimit", -0.01), ("priceLimit", 1.01),
                 ("priceLimit", float("inf")), ("open", False), ("open", 1),
                 ("tournamentId", OTHER_TOURNAMENT_ID), ("tournamentId", None),
                 ("createdAt", None), ("createdAt", "2026-10-07T10:00:00"),
                 ("expirationDate", "2026-10-08T10:00:00")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                row = order()
                row[field] = value
                with self.assertRaises(ValueError):
                    self.read_with(pages=[orders_page([row])])

    def test_no_sell_order_market_price_and_expiry_are_preserved(self):
        row = order()
        row.update(side="no", action="sell", priceLimit=None,
                   expirationDate="2026-10-08T10:00:00+01:00")
        result, _ = self.read_with(pages=[orders_page([row])])
        self.assertEqual(result["orders"][0]["side"], "no")
        self.assertEqual(result["orders"][0]["action"], "sell")
        self.assertIsNone(result["orders"][0]["priceLimit"])
        self.assertEqual(result["orders"][0]["expirationDate"], row["expirationDate"])

    def test_all_open_order_pages_are_read(self):
        pages = [orders_page([order()], more=True, cursor="next-1"),
                 orders_page([order(102, 12)])]
        result, fetch = self.read_with(pages=pages)
        self.assertEqual([row["id"] for row in result["orders"]], [101, 102])
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(fetch.call_args_list[-1].kwargs["params"]["cursor"], "next-1")

    def test_duplicate_order_ids_across_pages_are_rejected(self):
        pages = [orders_page([order()], more=True, cursor="next-1"), orders_page([order()])]
        with self.assertRaises(ValueError):
            self.read_with(pages=pages)

    def test_missing_and_repeated_cursors_are_rejected(self):
        for cursor in (None, ""):
            with self.subTest(cursor=cursor):
                with self.assertRaises(ValueError):
                    self.read_with(pages=[orders_page([], more=True, cursor=cursor)])
        pages = [orders_page([], more=True, cursor="same"),
                 orders_page([], more=True, cursor="same")]
        with self.assertRaises(ValueError):
            self.read_with(pages=pages)

    def test_invalid_order_pagination_is_not_an_empty_order_account(self):
        for page in ({"data": []}, {"data": None, "pagination": {"hasMore": False}},
                     {"data": [], "pagination": {"hasMore": 0}}):
            with self.subTest(page=page):
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    self.read_with(pages=[page])

    def test_later_page_network_failure_never_returns_partial_orders(self):
        responses = [tournament(), holdings(), orders_page([], more=True, cursor="next-1"),
                     requests.Timeout("synthetic request failure")]
        with patch.object(account, "fetch_json", side_effect=responses) as fetch:
            with self.assertRaises(requests.RequestException):
                account.read_account(self.session)
        self.assertEqual(fetch.call_count, 4)

    def test_finite_order_page_cap_does_not_return_partial_results(self):
        pages = [orders_page([], more=True, cursor="next-1"),
                 orders_page([], more=True, cursor="next-2")]
        with patch.object(account, "MAX_ORDER_PAGES", 2):
            with self.assertRaises(ValueError):
                self.read_with(pages=pages)

    def test_optional_coverage_accepts_complete_and_rejects_incomplete(self):
        for sequence in (None, 0, 100):
            with self.subTest(valid_sequence=sequence):
                result, _ = self.read_with(pages=[orders_page([], coverage={
                    "complete": True, "projectedThroughSequence": sequence})])
                self.assertEqual(result["orders"], [])
        for coverage in ({"complete": False, "projectedThroughSequence": 1},
                         {"complete": "true", "projectedThroughSequence": 1},
                         {"complete": True, "projectedThroughSequence": True},
                         {"complete": True, "projectedThroughSequence": -1},
                         {"complete": True, "projectedThroughSequence": float("nan")}):
            with self.subTest(coverage=coverage):
                with self.assertRaises(ValueError):
                    self.read_with(pages=[orders_page([], coverage=coverage)])

    def test_cli_uses_gets_and_keeps_paper_files_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            portfolio_path = Path(temporary_directory) / "paper_portfolio.json"
            log_path = Path(temporary_directory) / "paper_trades.csv"
            portfolio_path.write_bytes(b"synthetic portfolio sentinel\n")
            log_path.write_bytes(b"synthetic trade log sentinel\n")
            before = {path: path.read_bytes() for path in (portfolio_path, log_path)}
            session = MagicMock()
            session.headers = {}
            responses = []
            for payload in (tournament(), holdings(), orders_page()):
                response = Mock(status_code=200)
                response.json.return_value = copy.deepcopy(payload)
                responses.append(response)
            session.get.side_effect = responses
            manager = MagicMock()
            manager.__enter__.return_value = session
            with patch("sys.argv", ["account_reader.py", "--details"]), \
                    patch.object(account, "load_dotenv") as load_env, \
                    patch.dict(account.os.environ, {"SIG_API_KEY": "test-only"}), \
                    patch.object(account.requests, "Session", return_value=manager), \
                    patch.object(account, "fetch_json", scanner.fetch_json), \
                    patch.object(scanner, "_last_request_started", None), \
                    patch.object(scanner, "_read_cooldown_until", 0), \
                    patch.object(scanner.time, "monotonic", return_value=100), \
                    patch.object(scanner.time, "sleep"), \
                    patch.object(paper, "PORTFOLIO_PATH", portfolio_path), \
                    patch.object(paper, "TRADE_LOG_PATH", log_path), \
                    patch.object(paper, "load_portfolio", side_effect=AssertionError("paper load called")), \
                    patch.object(paper, "save_portfolio", side_effect=AssertionError("paper save called")), \
                    patch.object(paper, "sync_trade_log", side_effect=AssertionError("paper log called")):
                self.assertEqual(account.main(), 0)
            self.assertEqual(load_env.call_args.args[0], Path(account.__file__).with_name(".env"))
            self.assertEqual(session.get.call_count, 3)
            for call in session.get.call_args_list:
                self.assertTrue(call.args[0].startswith(scanner.API_BASE_URL + "/"))
                self.assertFalse(call.kwargs["allow_redirects"])
            for method in (session.post, session.put, session.patch, session.delete):
                method.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertEqual(set(Path(temporary_directory).iterdir()), set(before))

    def test_cli_failure_hides_raw_exception_and_fake_key(self):
        fake_key = "test-only-account-secret"
        error = requests.RequestException(f"Authorization: Bearer {fake_key}")
        with patch("sys.argv", ["account_reader.py"]), \
                patch.object(account, "load_dotenv"), \
                patch.dict(account.os.environ, {"SIG_API_KEY": fake_key}), \
                patch.object(account, "fetch_json", side_effect=error):
            self.assertEqual(account.main(), 1)
        self.assertNotIn(fake_key, self.output.getvalue())
        self.assertNotIn("Authorization", self.output.getvalue())

    def test_missing_key_stops_cli_before_requests(self):
        with patch("sys.argv", ["account_reader.py"]), \
                patch.object(account, "load_dotenv"), \
                patch.dict(account.os.environ, {}, clear=True), \
                patch.object(account, "fetch_json") as fetch:
            self.assertEqual(account.main(), 1)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
