"""Local order drafting and scoped GET integration, with disposable account data."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import order_preview as preview
import price_reader as scanner
from test_account_reader import holdings, isolate_quarantine, order, orders_page, position, tournament
from test_api_audit import (
    OTHER_TOURNAMENT_ID, QUOTE_TIME, TOURNAMENT_ID, election_tree,
    exchange_book, page, pair_relationship, party_market,
)


def account_snapshot(positions=None, orders=None, cash=5000):
    metadata = tournament()
    metadata["myBalance"] = cash
    return {"tournament": metadata, **holdings([] if positions is None else positions),
            "orders": [] if orders is None else orders}


def context(exhaustive=True):
    return {"tournament_id": TOURNAMENT_ID, "market_ids": ["1", "2"],
            "exchange_ids": ["11", "12"],
            "race_key": ["62978", "98108", "2026-11-03", "General", "Party Winner"],
            "settlement_fingerprint": "a" * 64,
            "relationships": [{"id": "22222222-2222-2222-2222-222222222222", "version": 1,
                               "isExhaustive": exhaustive, "members": [["11", "1"], ["12", "2"]]}],
            "payout_condition": "ordinary_binary_settlement; refunds are separate"}


def parsed_book(ask=.4, bid=.35, quantity=100):
    return {"ask": ask, "bid": bid, "ask_quantity": quantity, "bid_quantity": quantity,
            "received_at": 100, "quoted_at": QUOTE_TIME,
            "version": {"sequence": 4, "at": "2026-10-07T12:00:00Z"}}


class OrderPreviewTests(unittest.TestCase):
    def setUp(self):
        isolate_quarantine(self)
        for target, value in (("time.monotonic", 100), ("time.time", QUOTE_TIME)):
            patcher = patch.object(preview.time, target.split(".")[1], return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("_last_request_started", None), ("_read_cooldown_until", 0),
                            ("READ_REQUEST_SPACING", 0)):
            patcher = patch.object(scanner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = account_snapshot()
        self.context = context()
        self.books = [parsed_book(), parsed_book(ask=.45, bid=.4)]

    def build(self, **kwargs):
        args = {"account": self.account, "position_type": "YES-PAIR", "market_context": self.context,
                "books": self.books, "account_started": 100}
        args.update(kwargs)
        return preview.build_preview(**args)

    def responses(self, relationships=None):
        return [tournament(), holdings([]), orders_page([]),
                party_market("Democratic", 1, 11), party_market("Republican", 2, 12),
                page([pair_relationship(exhaustive=True)] if relationships is None else relationships),
                election_tree("Democratic", 1), election_tree("Republican", 2),
                exchange_book(1, 11, bid=.35, ask=.4), exchange_book(2, 12, bid=.4, ask=.45)]

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

    def run_cli(self, session, output=None):
        output = io.StringIO() if output is None else output
        with patch("sys.argv", ["order_preview.py", "--dem-market", "1", "--rep-market", "2",
                                "--position", "YES-PAIR"]), \
                patch.object(preview, "load_dotenv"), \
                patch.object(preview.os, "getenv", return_value="fake-preview-secret"), \
                patch.object(preview.requests, "Session", return_value=session), \
                contextlib.redirect_stdout(output):
            result = preview.main()
        return result, output.getvalue()

    def test_yes_pair_has_exact_schema_and_real_cash(self):
        self.account["tournament"]["myBalance"] = 111
        result = self.build()
        body = result["request"]
        self.assertFalse(result["submission_enabled"])
        self.assertEqual(result["endpoint_path"], "/api/v1/orders/multi-leg")
        self.assertEqual(set(body), {"idempotencyKey", "legs"})
        self.assertTrue(body["idempotencyKey"].startswith("preview-"))
        self.assertEqual([leg["price"] for leg in body["legs"]], [.4, .45])
        self.assertEqual([leg["side"] for leg in body["legs"]], ["yes", "yes"])
        self.assertTrue(all(leg["action"] == "buy" and leg["quantity"] == 100
                            and leg["tournamentId"] == TOURNAMENT_ID for leg in body["legs"]))
        self.assertEqual(result["risk"]["reported_cash"], 111)
        self.assertEqual(result["max_new_spend"], 85)
        self.assertEqual(result["conditional_projected_profit"], 15)

    def test_no_prices_are_side_relative_complements_rounded_up(self):
        result = self.build(position_type="NO-PAIR",
                            books=[parsed_book(bid=.604, ask=.65), parsed_book(bid=.55, ask=.6)])
        self.assertEqual([leg["price"] for leg in result["request"]["legs"]], [.4, .45])
        self.assertTrue(all(leg["side"] == "no" for leg in result["request"]["legs"]))
        self.assertEqual(result["max_new_spend"], 85)

    def test_exact_ticks_remain_exact_and_off_ticks_round_up(self):
        for value, expected in ((.4, .4), (.401, .405), (.994, .995), (.005, .005)):
            with self.subTest(value=value):
                self.assertEqual(preview.ceil_buy_limit(value), expected)

    def test_invalid_limit_prices_never_become_market_orders(self):
        for value in (0, 1, -.01, .996, True, float("nan"), float("inf"), ".4"):
            with self.subTest(value=value), self.assertRaises(preview.PreviewBlocked):
                preview.ceil_buy_limit(value)

    def test_rounding_can_remove_previously_valid_edge(self):
        self.books = [parsed_book(ask=.491, bid=.48), parsed_book(ask=.488, bid=.48)]
        with self.assertRaisesRegex(preview.PreviewBlocked, "Tick-rounded"):
            self.build()

    def test_edge_exactly_two_percent_passes(self):
        self.books = [parsed_book(ask=.49, bid=.48), parsed_book(ask=.49, bid=.48)]
        self.assertEqual(self.build()["ordinary_edge"], .02)

    def test_normal_live_checker_rejects_every_lower_tick_edge(self):
        # Exhaust the permitted tick grid up to the 2% boundary. Prices with
        # the same sum have the same ordinary edge, regardless of leg split.
        first = Decimal(".360")
        for ticks in range(1, 200):
            second = ticks * preview.PRICE_TICK
            edge = Decimal("1.000") - first - second
            books = [parsed_book(bid=float(1 - price), ask=float(1 - price + preview.PRICE_TICK))
                     for price in (first, second)]
            with self.subTest(second=second, edge=edge):
                if edge < Decimal(".020"):
                    with self.assertRaises(preview.PreviewBlocked):
                        preview.observed_limits("NO-PAIR", books, 100, quantity=1)
                else:
                    result = preview.observed_limits("NO-PAIR", books, 100, quantity=1)
                    self.assertEqual(result[3], edge)
        self.assertEqual(preview.MIN_EDGE, .020)
        self.assertEqual(scanner.MIN_EDGE, .020)

    def test_automatic_quantity_floors_depth_and_caps_at_100(self):
        self.books[0]["ask_quantity"] = 75.9
        self.assertEqual(self.build()["quantity"], 75)
        self.books[0]["ask_quantity"] = 1000
        self.assertEqual(self.build()["quantity"], 100)

    def test_explicit_small_quantity_still_requires_50_share_liquidity(self):
        self.assertEqual(self.build(quantity=1)["quantity"], 1)
        self.books[0]["ask_quantity"] = 49
        with self.assertRaises(preview.PreviewBlocked):
            self.build(quantity=1)

    def test_invalid_and_oversized_quantity_are_blocked(self):
        for quantity in (0, -1, 101, True, 1.5, float("nan")):
            with self.subTest(quantity=quantity), self.assertRaises(preview.PreviewBlocked):
                self.build(quantity=quantity)
        self.books[0]["ask_quantity"] = 50
        with self.assertRaises(preview.PreviewBlocked):
            self.build(quantity=51)

    def test_every_resting_side_action_and_null_limit_reserves_one_per_share(self):
        rows = []
        for index, (action, side, limit) in enumerate(
                (("buy", "yes", .4), ("buy", "no", None), ("sell", "yes", .9), ("sell", "no", .1))):
            row = order(101 + index, 90 + index)
            row.update(action=action, side=side, priceLimit=limit, quantity=10)
            rows.append(row)
        result = self.build(account=account_snapshot(orders=rows, cash=200))
        self.assertEqual(result["risk"]["existing_order_reserve"], 40)
        self.assertEqual(result["risk"]["available_cash"], 160)
        self.assertEqual(result["risk"]["race_exposure_after_upper_bound"], 125)

    def test_expired_but_open_orders_still_reserve_cash(self):
        row = order(exchange_id=90)
        row["expirationDate"] = "2026-10-06T12:00:00Z"
        risk = preview.account_risk(account_snapshot(orders=[row]), ["11", "12"])
        self.assertEqual(risk["existing_order_reserve"], 2)

    def test_signed_no_holding_uses_reported_cost_not_valuation_or_quantity(self):
        row = position(90, 9, -7)
        row.update(currentPrice=.99, costBasis=60, unrealizedPnl=-55.8)
        result = self.build(account=account_snapshot(positions=[row]))
        self.assertEqual(result["risk"]["existing_holdings_cost_basis"], 60)
        self.assertEqual(result["risk"]["race_exposure_after_upper_bound"], 145)

    def test_all_holdings_and_pending_orders_count_towards_race_upper_bound(self):
        row = position(90, 9)
        row.update(costBasis=60, unrealizedPnl=-55.8)
        resting = order(exchange_id=91)
        resting["quantity"] = 6
        with self.assertRaisesRegex(preview.PreviewBlocked, "race exposure"):
            self.build(account=account_snapshot(positions=[row], orders=[resting]))

    def test_actual_cash_is_used_instead_of_starting_paper_balance(self):
        with self.assertRaisesRegex(preview.PreviewBlocked, "Reported cash"):
            self.build(account=account_snapshot(cash=84))
        self.assertEqual(self.build(account=account_snapshot(cash=85))["max_new_spend"], 85)

    def test_pending_reserve_reduces_spendable_cash_without_sale_credit(self):
        row = order(exchange_id=90)
        row.update(action="sell", quantity=10, priceLimit=.99)
        with self.assertRaisesRegex(preview.PreviewBlocked, "Reported cash"):
            self.build(account=account_snapshot(orders=[row], cash=94))

    def test_candidate_holdings_block_both_same_side_and_netting_orders(self):
        for signed_quantity in (7, -7):
            with self.subTest(quantity=signed_quantity), self.assertRaisesRegex(preview.PreviewBlocked, "holding"):
                self.build(account=account_snapshot(positions=[position(quantity=signed_quantity)]))

    def test_candidate_open_orders_block_every_side_and_action(self):
        for action in ("buy", "sell"):
            for side in ("yes", "no"):
                row = order()
                row.update(action=action, side=side)
                with self.subTest(action=action, side=side), self.assertRaisesRegex(preview.PreviewBlocked, "open order"):
                    self.build(account=account_snapshot(orders=[row]))

    def test_stale_account_and_books_cannot_produce_a_draft(self):
        for started in (84.9, 101, True):
            with self.subTest(started=started), self.assertRaises(preview.PreviewBlocked):
                self.build(account_started=started)
        for field, value in (("received_at", 94.9), ("quoted_at", QUOTE_TIME - 6)):
            books = copy.deepcopy(self.books)
            books[0][field] = value
            with self.subTest(field=field), self.assertRaises(preview.PreviewBlocked):
                self.build(books=books)

    def test_conflicting_quote_version_and_scope_are_rejected(self):
        books = copy.deepcopy(self.books)
        books[0]["version"]["at"] = "2026-10-07T12:00:01Z"
        with self.assertRaisesRegex(preview.PreviewBlocked, "timestamps"):
            self.build(books=books)
        other_context = context()
        other_context["tournament_id"] = OTHER_TOURNAMENT_ID
        with self.assertRaisesRegex(preview.PreviewBlocked, "competition"):
            self.build(market_context=other_context)

    def test_nonexhaustive_relationship_cannot_approve_yes_pair(self):
        with self.assertRaises(ValueError):
            self.build(market_context=context(exhaustive=False))

    def test_body_validation_rejects_extra_fields_and_market_prices(self):
        body = self.build()["request"]
        for mutation in ("extra", "price", "side", "quantity", "tournament"):
            changed = copy.deepcopy(body)
            if mutation == "extra":
                changed["dryRun"] = True
            elif mutation == "price":
                changed["legs"][0]["price"] = 1
            elif mutation == "side":
                changed["legs"][0]["side"] = "YES"
            elif mutation == "quantity":
                changed["legs"][0]["quantity"] = True
            else:
                changed["legs"][0]["tournamentId"] = OTHER_TOURNAMENT_ID
            with self.subTest(mutation=mutation), self.assertRaises(preview.PreviewBlocked):
                preview.validate_request_body(changed)

    def test_complete_cli_uses_scoped_gets_and_leaves_paper_files_unchanged(self):
        session = self.session(self.responses())
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("paper_portfolio.json", "paper_trades.csv")]
            for path in paths:
                path.write_bytes(b"preview test sentinel")
            before = [path.read_bytes() for path in paths]
            # Guard access to the real portfolio as well as the disposable files.
            real_open = io.open
            def guarded_open(file, *args, **kwargs):
                if isinstance(file, (str, Path)) and Path(file).name in {"paper_portfolio.json", "paper_trades.csv", ".env"}:
                    raise AssertionError("Preview opened a protected file")
                return real_open(file, *args, **kwargs)
            with patch.object(io, "open", side_effect=guarded_open), patch("builtins.open", side_effect=guarded_open):
                code, output = self.run_cli(session)
            self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(code, 0, output)
        self.assertIn("PREVIEW READY", output)
        self.assertIn("not an atomic executable snapshot", output)
        self.assertNotIn("fake-preview-secret", output)
        self.assertEqual(session.get.call_count, 10)
        for call in session.get.call_args_list:
            self.assertTrue(call.args[0].startswith(scanner.API_BASE_URL + "/"))
            self.assertFalse(call.kwargs["allow_redirects"])
            if "/tournaments/" not in call.args[0]:
                self.assertEqual(call.kwargs["params"]["tournamentId"], TOURNAMENT_ID)
        for method in ("post", "put", "patch", "delete"):
            getattr(session, method).assert_not_called()

    def test_empty_relationship_graph_stops_before_books_and_has_no_draft(self):
        session = self.session(self.responses(relationships=[]))
        code, output = self.run_cli(session)
        self.assertEqual(code, 1)
        self.assertIn("No active relationship", output)
        self.assertNotIn("idempotencyKey", output)
        self.assertEqual(session.get.call_count, 6)
        session.post.assert_not_called()

    def test_cli_hides_raw_network_exception_and_fake_key(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = requests.ConnectionError("Authorization: Bearer fake-preview-secret")
        code, output = self.run_cli(session)
        self.assertEqual(code, 1)
        self.assertIn("PREVIEW BLOCKED", output)
        self.assertNotIn("fake-preview-secret", output)
        self.assertNotIn("Authorization", output)
        session.post.assert_not_called()

    def test_wrong_market_identity_closed_market_and_scope_stop_preview(self):
        for mutation in ("id", "status", "scope"):
            payloads = self.responses()
            if mutation == "id":
                payloads[3]["id"] = "9"
            elif mutation == "status":
                payloads[3]["status"] = "closed"
            else:
                payloads[3]["contexts"][0]["tournament"]["id"] = OTHER_TOURNAMENT_ID
            with self.subTest(mutation=mutation):
                code, output = self.run_cli(self.session(payloads))
                self.assertEqual(code, 1)
                self.assertNotIn("idempotencyKey", output)


if __name__ == "__main__":
    unittest.main()
