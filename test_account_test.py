"""Offline checks for the supervised one-share test; every HTTP reply is fake."""
import contextlib
import copy
import io
import json
import sys
import tempfile
import stat
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

import account_test as supervised
import paper_trader as paper
import price_reader as scanner
from test_account_reader import holdings, isolate_quarantine, order, orders_page, position, tournament
from test_api_audit import (
    OTHER_TOURNAMENT_ID, QUOTE_TIME, TOURNAMENT_ID, exchange_book, party_market,
)


class AccountTestTests(unittest.TestCase):
    def setUp(self):
        isolate_quarantine(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "one_share_test.json"
        self.session = MagicMock()
        self.session.headers = {}
        self.session.__enter__.return_value = self.session
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        # Freeze freshness and read pacing without sleeping or accessing an API.
        for target, name, value in (
            (supervised, "STATE_PATH", self.path),
            (scanner, "_last_request_started", None),
            (scanner, "_read_cooldown_until", 0),
            (supervised.time, "monotonic", Mock(return_value=100)),
            (supervised.time, "time", Mock(return_value=QUOTE_TIME)),
            (supervised.time, "sleep", Mock()),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def response(self, payload=None, status=200):
        response = requests.Response()
        response.status_code = status
        response.url = scanner.API_BASE_URL + "/orders"
        response._content = json.dumps({} if payload is None else payload).encode()
        return response

    def empty_account(self, cash=5000):
        metadata = tournament()
        metadata["myBalance"] = cash
        portfolio = holdings([])
        return {"tournament": metadata, "positions": [],
                "summary": portfolio["summary"], "orders": []}

    def input_reads(self, cash=5000, positions=None, orders=None, market=None, book=None):
        metadata = tournament()
        metadata["myBalance"] = cash
        payloads = [metadata, holdings([] if positions is None else positions),
                    orders_page([] if orders is None else orders),
                    party_market("Democratic", 1, 11) if market is None else market,
                    exchange_book() if book is None else book]
        self.session.get.side_effect = [self.response(payload) for payload in payloads]

    def inputs(self, price=.65, account=None):
        account = self.empty_account() if account is None else account
        return account, "11", price, supervised.account_risk(account, ["11"])

    def make_intent(self, side="yes", save=True):
        # Most state-machine tests isolate the write path from independent GET
        # validation. Other tests below exercise all five real parsing paths.
        with patch.object(supervised, "read_test_inputs",
                          return_value=self.inputs(.65 if side == "yes" else .4)):
            intent = supervised.prepare_test(self.session, "1", side)
        if save:
            supervised.save_intent(intent, self.path, new=not self.path.exists())
        return intent

    def receipt(self, intent, order_id=101, opened=True, filled=0, cost=0, remaining=1):
        body = intent["request"]
        return {"orderId": order_id, "exchangeId": body["exchangeId"],
                "action": "buy", "side": body["side"], "quantity": 1,
                "price": body["price"], "open": opened,
                "quantityTraded": filled, "totalCost": cost,
                "remainingQuantity": remaining,
                "fillPrice": None if filled == 0 else cost / filled}

    def submit(self, intent, receipt=None, current_price=None):
        self.session.post.return_value = self.response(
            self.receipt(intent) if receipt is None else receipt)
        price = intent["request"]["price"] if current_price is None else current_price
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs(price)):
            return supervised.submit_test(self.session, intent, self.path, intent["approval"])

    def accepted(self, side="yes", filled=0, cost=0, remaining=1):
        intent = self.make_intent(side)
        self.submit(intent, self.receipt(intent, opened=remaining > 0,
                                         filled=filled, cost=cost, remaining=remaining))
        self.session.reset_mock()
        return intent

    def detail(self, intent, opened=True, remaining=1, filled=0):
        body = intent["request"]
        return {"id": intent["order_id"], "exchangeId": body["exchangeId"],
                "side": body["side"], "action": "buy", "quantity": remaining,
                "priceLimit": body["price"], "open": opened,
                "tournamentId": body["tournamentId"],
                "expirationDate": body["expirationDate"], "quantityFilled": filled,
                "terminalReasonCode": None if opened else "cancelled"}

    def test_expiry_comparison_discards_only_submillisecond_precision(self):
        client = "2026-10-10T17:16:27.154575+00:00"
        for reported in ("2026-10-10T17:16:27.154Z", "2026-10-10T17:16:27.154+00:00",
                         "2026-10-10T18:16:27.154+01:00"):
            with self.subTest(reported=reported):
                self.assertTrue(supervised.same_order_expiry(client, reported))
        self.assertFalse(supervised.same_order_expiry(client, "2026-10-10T17:16:27.155Z"))
        for invalid in (None, "invalid", "2026-10-10T17:16:27.154"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                supervised.same_order_expiry(client, invalid)

    def test_normalized_expiry_does_not_relax_other_order_parameters(self):
        intent = self.accepted()
        intent["request"]["expirationDate"] = "2026-10-10T17:16:27.154575+00:00"
        intent["approval"] = supervised.approval_hash(intent)
        order = self.detail(intent)
        order["expirationDate"] = "2026-10-10T17:16:27.154Z"
        self.session.get.side_effect = None
        self.session.get.return_value = self.response(order)
        self.assertEqual(supervised.read_order(self.session, intent), order)
        for field, value in (("id", 102), ("exchangeId", "99"), ("tournamentId", OTHER_TOURNAMENT_ID),
                             ("action", "sell"), ("side", "no"), ("priceLimit", .6), ("quantity", 2),
                             ("expirationDate", "2026-10-10T17:16:27.155Z")):
            bad = dict(order, **{field: value})
            self.session.get.return_value = self.response(bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                supervised.read_order(self.session, intent)
        self.session.post.assert_not_called()

    def fills(self, intent, filled=0, price=.35, rows=None, more=False, cursor=None):
        side = intent["request"]["side"]
        signed = filled if side == "yes" else -filled
        if rows is None:
            rows = [] if filled == 0 else [{"id": 501, "side": side, "quantity": signed,
                                           "price": price, "filledAt": "2026-10-07T12:00:00Z"}]
        return {"orderId": intent["order_id"], "exchangeId": intent["request"]["exchangeId"],
                "tournamentId": intent["request"]["tournamentId"], "data": rows,
                "coverage": {"complete": True}, "totalQuantityFilled": signed,
                "avgFillPrice": None if filled == 0 else price,
                "pagination": {"hasMore": more, "nextCursor": cursor}}

    def check_reads(self, intent, detail=None, pages=None):
        payloads = [self.detail(intent) if detail is None else detail]
        payloads.extend([self.fills(intent)] if pages is None else pages)
        self.session.get.side_effect = [self.response(payload) for payload in payloads]

    def test_prepare_uses_scoped_gets_and_explicit_one_share_limit(self):
        book = exchange_book(ask=.651)
        self.input_reads(book=book)
        intent = supervised.prepare_test(self.session, "1", "yes")
        body = intent["request"]
        self.assertEqual(body["quantity"], 1)
        self.assertEqual(body["price"], .655)
        self.assertEqual(body["action"], "buy")
        self.assertEqual(body["tournamentId"], TOURNAMENT_ID)
        self.assertEqual(body["exchangeId"], "11")
        self.assertEqual(intent["state"], "PREPARED")
        expiry = supervised.parse_api_timestamp(body["expirationDate"])
        created = supervised.parse_api_timestamp(intent["created_at"])
        self.assertEqual(expiry - created, timedelta(minutes=15))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.session.get.call_count, 5)
        urls = [call.args[0] for call in self.session.get.call_args_list]
        self.assertEqual(urls[-2:], [scanner.API_BASE_URL + "/markets/1",
                                    scanner.API_BASE_URL + "/exchanges/11/orderbook"])
        for call in self.session.get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(self.session.get.call_args_list[-2].kwargs["params"],
                         {"tournamentId": TOURNAMENT_ID})
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_prepare_no_buys_complement_of_yes_bid_rounded_up(self):
        self.input_reads(book=exchange_book(bid=.609))
        intent = supervised.prepare_test(self.session, "1", "no")
        self.assertEqual(intent["request"]["price"], .395)
        self.assertEqual(intent["request"]["side"], "no")
        self.session.post.assert_not_called()

    def test_prepare_rejects_wrong_market_scope_closed_market_and_low_liquidity(self):
        wrong_market = party_market("Democratic", 9, 11)
        wrong_scope = party_market("Democratic", 1, 11)
        wrong_scope["contexts"][0]["tournament"]["id"] = OTHER_TOURNAMENT_ID
        closed = party_market("Democratic", 1, 11)
        closed["status"] = "closed"
        small = exchange_book()
        small["asks"][0]["quantity"] = 49
        for market, book in ((wrong_market, None), (wrong_scope, None),
                             (closed, None), (None, small), (None, exchange_book(ask=None))):
            with self.subTest(market=market, book=book):
                self.input_reads(market=market, book=book)
                with self.assertRaises(ValueError):
                    supervised.prepare_test(self.session, "1", "yes")
                self.assertFalse(self.path.exists())
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_prepare_invalid_side_fails_before_any_get(self):
        with self.assertRaises(supervised.TestError):
            supervised.prepare_test(self.session, "1", "both")
        self.session.get.assert_not_called()

    def test_prepare_rejects_stale_or_invalid_book(self):
        for field, value in (("at", "2026-10-07T11:59:54Z"), ("at", "invalid")):
            with self.subTest(value=value):
                book = exchange_book()
                book["asOf"][field] = value
                self.input_reads(book=book)
                with self.assertRaises(ValueError):
                    supervised.prepare_test(self.session, "1", "yes")
        self.session.post.assert_not_called()

    def test_prepare_counts_all_pending_order_reserves_and_signed_holdings(self):
        pending = order(exchange_id=21)
        pending.update(action="sell", side="no", priceLimit=None, quantity=2)
        self.input_reads(cash=2.5, orders=[pending])
        with self.assertRaises(supervised.TestError):
            supervised.prepare_test(self.session, "1", "yes")
        no_holding = position(21, 2, -1)
        no_holding.update(costBasis=149.5, marketValue=.4, unrealizedPnl=-149.1)
        self.input_reads(positions=[no_holding])
        with self.assertRaises(supervised.TestError):
            supervised.prepare_test(self.session, "1", "yes")
        self.session.post.assert_not_called()

    def test_prepare_blocks_overlapping_holding_or_order(self):
        for positions, orders in (([position(11, 1, -1)], []), ([], [order(exchange_id=11)])):
            with self.subTest(positions=positions):
                self.input_reads(positions=positions, orders=orders)
                with self.assertRaises(ValueError):
                    supervised.prepare_test(self.session, "1", "yes")
        self.session.post.assert_not_called()

    def test_unique_keys_and_hashes_round_trip_with_exclusive_creation(self):
        first = self.make_intent()
        second = self.make_intent(save=False)
        self.assertNotEqual(first["request"]["idempotencyKey"], second["request"]["idempotencyKey"])
        self.assertNotEqual(first["approval"], second["approval"])
        self.assertEqual(supervised.load_intent(self.path), first)
        before = self.path.read_bytes()
        with self.assertRaises(supervised.TestError):
            supervised.save_intent(second, self.path, new=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_missing_or_tampered_journal_never_becomes_a_new_test(self):
        with self.assertRaises(supervised.TestError):
            supervised.load_intent(self.path)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(supervised.TestError):
            supervised.load_intent(self.path)
        intent = self.make_intent(save=False)
        intent["request"]["price"] = .7
        self.path.write_text(json.dumps(intent), encoding="utf-8")
        with self.assertRaises(supervised.TestError):
            supervised.load_intent(self.path)

    def test_invalid_quantity_price_scope_and_state_are_rejected_even_with_new_hash(self):
        original = self.make_intent(save=False)
        mutations = [("quantity", 2), ("quantity", True), ("price", .651),
                     ("price", float("inf")), ("tournamentId", "missing"),
                     ("exchangeId", True), ("action", "sell")]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(original)
                bad["request"][field] = value
                if field != "price" or value != float("inf"):
                    bad["approval"] = supervised.approval_hash(bad)
                with self.assertRaises(ValueError):
                    supervised.validate_intent(bad)
        bad = copy.deepcopy(original)
        bad.update(state="ACCEPTED", order_id=None)
        with self.assertRaises(supervised.TestError):
            supervised.validate_intent(bad)

    def test_wrong_approval_stops_before_preflight_journal_and_post(self):
        intent = self.make_intent()
        before = self.path.read_bytes()
        with patch.object(supervised, "read_test_inputs") as preflight:
            with self.assertRaises(supervised.TestError):
                supervised.submit_test(self.session, intent, self.path, "wrong-token")
        preflight.assert_not_called()
        self.session.post.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_fresh_submit_rechecks_all_gets_and_keeps_exact_approved_body(self):
        intent = self.make_intent()
        approved = copy.deepcopy(intent["request"])
        self.input_reads(book=exchange_book(ask=.6))
        self.session.post.return_value = self.response(self.receipt(intent))
        supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(self.session.get.call_count, 5)
        self.assertEqual(self.session.post.call_args.kwargs["json"], approved)
        self.assertEqual(intent["request"], approved)
        self.assertEqual(intent["state"], "ACCEPTED")

    def test_worse_quote_or_exhausted_cash_leaves_prepared_journal_untouched(self):
        intent = self.make_intent()
        before = self.path.read_bytes()
        for inputs in (self.inputs(.655), self.inputs(.65, self.empty_account(.64))):
            with self.subTest(inputs=inputs):
                with patch.object(supervised, "read_test_inputs", return_value=inputs):
                    with self.assertRaises(supervised.TestError):
                        supervised.submit_test(self.session, intent, self.path, intent["approval"])
                self.assertEqual(self.path.read_bytes(), before)
        self.session.post.assert_not_called()

    def test_expired_preparation_stops_before_preflight_or_post(self):
        intent = self.make_intent()
        intent["request"]["expirationDate"] = "2026-10-07T11:59:00Z"
        intent["approval"] = supervised.approval_hash(intent)
        supervised.save_intent(intent, self.path)
        with patch.object(supervised, "read_test_inputs") as preflight:
            with self.assertRaises(supervised.TestError):
                supervised.submit_test(self.session, intent, self.path, intent["approval"])
        preflight.assert_not_called()
        self.session.post.assert_not_called()

    def test_submitting_is_durable_before_the_only_post(self):
        intent = self.make_intent()
        body = copy.deepcopy(intent["request"])
        def placement(url, **kwargs):
            self.assertEqual(supervised.load_intent(self.path)["state"], "SUBMITTING")
            self.assertEqual(url, scanner.API_BASE_URL + "/orders")
            self.assertEqual(kwargs["json"], body)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], scanner.REQUEST_TIMEOUT)
            return self.response(self.receipt(intent))
        self.session.post.side_effect = placement
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()):
            supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(self.session.post.call_count, 1)
        self.session.delete.assert_not_called()
        self.assertEqual(supervised.load_intent(self.path)["order_id"], 101)

    def test_cannot_post_if_submitting_journal_save_fails(self):
        intent = self.make_intent()
        before = self.path.read_bytes()
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()), \
                patch.object(supervised, "save_intent", side_effect=supervised.TestError("Cannot save test journal")):
            with self.assertRaises(supervised.TestError):
                supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(intent["state"], "PREPARED")
        self.session.post.assert_not_called()

    def test_accepted_save_failure_preserves_submitting_and_blocks_replacement(self):
        intent = self.make_intent()
        save = supervised.save_intent
        def save_until_receipt(proposed, path, **kwargs):
            if proposed["state"] == "ACCEPTED":
                raise supervised.TestError("Cannot save test journal")
            return save(proposed, path, **kwargs)
        self.session.post.return_value = self.response(self.receipt(intent))
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()), \
                patch.object(supervised, "save_intent", side_effect=save_until_receipt):
            with self.assertRaises(supervised.TestError):
                supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(supervised.load_intent(self.path)["state"], "SUBMITTING")
        self.assertEqual(self.session.post.call_count, 1)
        self.session.delete.assert_not_called()
        restored = supervised.load_intent(self.path)
        with self.assertRaises(supervised.TestError):
            supervised.submit_test(self.session, restored, self.path, restored["approval"])
        self.assertEqual(self.session.post.call_count, 1)

    def test_timeout_records_unknown_and_blocks_replay_or_replacement(self):
        intent = self.make_intent()
        approved = copy.deepcopy(intent["request"])
        self.session.post.side_effect = requests.Timeout("synthetic network failure")
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()):
            with self.assertRaisesRegex(supervised.TestError, "UNKNOWN"):
                supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(supervised.load_intent(self.path)["state"], "UNKNOWN")
        with self.assertRaises(supervised.TestError):
            supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(self.session.post.call_count, 1)
        restored = supervised.load_intent(self.path)
        self.session.post.side_effect = None
        self.session.post.return_value = self.response(self.receipt(restored))
        with patch.object(supervised, "read_test_inputs") as preflight:
            with self.assertRaisesRegex(supervised.TestError, "replay is disabled"):
                supervised.submit_test(self.session, restored, self.path, restored["approval"], recover=True)
        preflight.assert_not_called()
        self.assertEqual(self.session.post.call_count, 1)
        for call in self.session.post.call_args_list:
            self.assertEqual(call.kwargs["json"], approved)
        self.assertEqual(restored["state"], "UNKNOWN")
        self.session.delete.assert_not_called()


    def test_documented_http_error_is_saved_without_raw_details_or_secret(self):
        intent = self.make_intent()
        self.session.post.return_value = self.response({"error": {
            "code": "TERMS_NOT_ACKNOWLEDGED", "message": "never-save-fake-secret",
            "details": {"Authorization": "never-save-fake-secret"}}}, status=403)
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()), self.assertRaises(supervised.TestError):
            supervised.submit_test(self.session, intent, self.path, intent["approval"])
        restored = supervised.load_intent(self.path)
        self.assertEqual(restored["placement_diagnostic"], {
            "kind": "http-response", "http_status": 403, "api_code": "TERMS_NOT_ACKNOWLEDGED"})
        supervised.print_intent(restored)
        self.assertNotIn("never-save-fake-secret", self.path.read_text())
        self.assertNotIn("never-save-fake-secret", self.output.getvalue())
        with self.assertRaises(supervised.TestError):
            supervised.submit_test(self.session, restored, self.path, restored["approval"])
        self.assertEqual(self.session.post.call_count, 1)

    def test_unrecognized_error_code_and_network_text_are_never_saved(self):
        response = self.response({"error": {"code": "never-save-fake-secret"}}, status=503)
        diagnostic = supervised.safe_placement_diagnostic(response, ValueError("never-save-fake-secret"))
        self.assertEqual(diagnostic, {"kind": "http-response", "http_status": 503, "api_code": None})
        for error, kind in ((requests.Timeout("never-save-fake-secret"), "timeout"),
                            (requests.ConnectionError("never-save-fake-secret"), "connection-error")):
            diagnostic = supervised.safe_placement_diagnostic(None, error)
            self.assertEqual(diagnostic, {"kind": kind, "http_status": None, "api_code": None})

    def test_invalid_json_receipt_saves_status_without_unsafe_response_content(self):
        intent = self.make_intent()
        response = self.response()
        response._content = b"never-save-fake-secret"
        self.session.post.return_value = response
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()), self.assertRaises(supervised.TestError):
            supervised.submit_test(self.session, intent, self.path, intent["approval"])
        diagnostic = supervised.load_intent(self.path)["placement_diagnostic"]
        self.assertEqual(diagnostic, {"kind": "invalid-receipt", "http_status": 200, "api_code": None})
        self.assertNotIn("never-save-fake-secret", self.path.read_text())

    def test_tampered_diagnostic_and_diagnostic_on_prepared_state_are_rejected(self):
        intent = self.make_intent()
        intent["placement_diagnostic"] = {"kind": "timeout", "http_status": None, "api_code": None}
        with self.assertRaises(supervised.TestError):
            supervised.validate_intent(intent)
        intent["state"] = "UNKNOWN"
        supervised.validate_intent(intent)
        intent["placement_diagnostic"]["api_code"] = "unsafe-code"
        with self.assertRaises(supervised.TestError):
            supervised.validate_intent(intent)

    def test_unknown_outcome_cannot_be_disproved_with_missing_order_gets(self):
        intent = self.make_intent()
        supervised.commit_state(intent, self.path, "UNKNOWN")
        with self.assertRaisesRegex(supervised.TestError, "GET reads cannot recover"):
            supervised.check_test(self.session, intent, self.path)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()
        self.assertEqual(supervised.load_intent(self.path)["state"], "UNKNOWN")

    def test_untrusted_placement_receipts_record_unknown_without_retry(self):
        for field, value in (("exchangeId", "99"), ("side", "no"), ("quantity", True),
                             ("price", .6), ("quantityTraded", float("nan")),
                             ("totalCost", .7), ("remainingQuantity", 2), ("orderId", None)):
            with self.subTest(field=field):
                intent = self.make_intent()
                receipt = self.receipt(intent)
                receipt[field] = value
                self.session.post.reset_mock()
                self.session.post.side_effect = None
                with self.assertRaises(supervised.TestError):
                    self.submit(intent, receipt)
                self.assertEqual(self.session.post.call_count, 1)
                self.assertEqual(supervised.load_intent(self.path)["state"], "UNKNOWN")
                self.assertIsNone(intent["order_id"])
        self.session.delete.assert_not_called()

    def test_redirect_or_non_200_placement_is_unknown(self):
        for status in (302, 400, 429, 500):
            with self.subTest(status=status):
                intent = self.make_intent()
                self.session.post.reset_mock()
                self.session.post.return_value = self.response(self.receipt(intent), status)
                with patch.object(supervised, "read_test_inputs", return_value=self.inputs()):
                    with self.assertRaises(supervised.TestError):
                        supervised.submit_test(self.session, intent, self.path, intent["approval"])
                self.assertEqual(intent["state"], "UNKNOWN")
                self.assertEqual(self.session.post.call_count, 1)

    def test_noop_requires_no_identifier_no_fill_no_open_remainder(self):
        intent = self.make_intent()
        self.submit(intent, self.receipt(intent, order_id=None, opened=False, remaining=0))
        self.assertEqual(intent["state"], "NOOP")
        self.assertIsNone(intent["order_id"])
        with self.assertRaises(supervised.TestError):
            supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.session.delete.assert_not_called()

    def test_check_no_fills_use_signed_quantity_and_actual_side_price(self):
        intent = self.accepted("no")
        closed = self.detail(intent, opened=False, remaining=0, filled=1)
        history = self.fills(intent, filled=1, price=.37)
        self.check_reads(intent, closed, [history])
        supervised.check_test(self.session, intent, self.path)
        self.assertEqual(intent["state"], "OBSERVED_TERMINAL")
        self.assertEqual(intent["observation"]["filled_quantity"], 1)
        self.assertAlmostEqual(intent["observation"]["filled_cost"], .37)
        self.assertEqual(self.session.get.call_args_list[1].kwargs["params"], {"limit": 200})
        # A repeat observation neither buys again nor double-counts a held share.
        self.check_reads(intent, closed, [history])
        supervised.check_test(self.session, intent, self.path)
        self.assertAlmostEqual(intent["observation"]["filled_cost"], .37)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_complete_fill_pagination_including_empty_first_page(self):
        intent = self.accepted()
        first = self.fills(intent, filled=1, price=.5, rows=[], more=True, cursor="next")
        final = self.fills(intent, filled=1, price=.5)
        self.check_reads(intent, self.detail(intent, opened=False, remaining=0, filled=1), [first, final])
        supervised.check_test(self.session, intent, self.path)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.session.get.call_args_list[-1].kwargs["params"],
                         {"limit": 200, "cursor": "next"})
        self.assertAlmostEqual(intent["observation"]["filled_cost"], .5)

    def test_invalid_fill_scope_coverage_sign_price_and_totals_do_not_change_journal(self):
        intent = self.accepted("no")
        baseline = self.path.read_bytes()
        mutations = [lambda row: row.update(tournamentId=OTHER_TOURNAMENT_ID),
                     lambda row: row["coverage"].update(complete=False),
                     lambda row: row["data"][0].update(quantity=1),
                     lambda row: row["data"][0].update(price=.6),
                     lambda row: row.update(totalQuantityFilled=-.5),
                     lambda row: row["pagination"].update(hasMore="false")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = self.fills(intent, filled=1, price=.37)
                mutate(bad)
                self.check_reads(intent, self.detail(intent, opened=False, remaining=0, filled=1), [bad])
                with self.assertRaises(ValueError):
                    supervised.check_test(self.session, intent, self.path)
                self.assertEqual(self.path.read_bytes(), baseline)

    def test_duplicate_fills_and_changing_lifecycle_during_pagination_are_blocked(self):
        intent = self.accepted()
        first = self.fills(intent, filled=1, rows=[{"id": 501, "side": "yes", "quantity": .5,
                                                 "price": .35, "filledAt": "2026-10-07T12:00:00Z"}],
                           more=True, cursor="next")
        duplicate = copy.deepcopy(first)
        duplicate["pagination"] = {"hasMore": False, "nextCursor": None}
        changed = copy.deepcopy(duplicate)
        changed["totalQuantityFilled"] = .5
        for final in (duplicate, changed):
            with self.subTest(final=final):
                self.check_reads(intent, self.detail(intent, opened=False, remaining=0, filled=1), [first, final])
                with self.assertRaises(supervised.TestError):
                    supervised.check_test(self.session, intent, self.path)

    def test_reads_cannot_erase_previously_confirmed_placement_fills(self):
        intent = self.accepted(filled=1, cost=.5, remaining=0)
        before = self.path.read_bytes()
        self.check_reads(intent, self.detail(intent, opened=False, remaining=0, filled=0))
        with self.assertRaisesRegex(supervised.TestError, "not caught up"):
            supervised.check_test(self.session, intent, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(intent["observation"]["placement_cost"], .5)

    def test_closed_order_requires_matching_fills_and_cannot_reopen(self):
        intent = self.accepted()
        self.check_reads(intent, self.detail(intent, opened=False, remaining=0, filled=1))
        with self.assertRaises(supervised.TestError):
            supervised.check_test(self.session, intent, self.path)
        self.check_reads(intent, self.detail(intent, opened=False, remaining=0, filled=0))
        supervised.check_test(self.session, intent, self.path)
        self.check_reads(intent)
        with self.assertRaisesRegex(supervised.TestError, "reopened"):
            supervised.check_test(self.session, intent, self.path)

    def test_wrong_cancel_approval_stops_before_reads_and_writes(self):
        intent = self.accepted()
        before = self.path.read_bytes()
        with self.assertRaises(supervised.TestError):
            supervised.cancel_test(self.session, intent, self.path, "wrong-token")
        self.session.get.assert_not_called()
        self.session.delete.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_order_scope_side_price_or_expiry_prevents_cancellation(self):
        intent = self.accepted()
        before = self.path.read_bytes()
        for field, value in (("tournamentId", OTHER_TOURNAMENT_ID), ("exchangeId", "99"),
                             ("side", "no"), ("priceLimit", .6),
                             ("expirationDate", "2026-10-07T12:01:00Z")):
            with self.subTest(field=field):
                bad = self.detail(intent)
                bad[field] = value
                self.session.get.side_effect = [self.response(bad)]
                with self.assertRaises(ValueError):
                    supervised.cancel_test(self.session, intent, self.path, intent["approval"])
                self.assertEqual(self.path.read_bytes(), before)
        self.session.delete.assert_not_called()
        self.session.post.assert_not_called()

    def test_cancel_records_intent_before_deleting_specific_order_without_body(self):
        intent = self.accepted()
        self.session.get.side_effect = [self.response(self.detail(intent)),
                                       self.response(self.detail(intent, opened=False, remaining=0)),
                                       self.response(self.fills(intent))]
        def cancellation(url, **kwargs):
            self.assertEqual(supervised.load_intent(self.path)["state"], "CANCEL_REQUESTED")
            self.assertEqual(url, scanner.API_BASE_URL + "/orders/101")
            self.assertNotIn("json", kwargs)
            self.assertNotIn("data", kwargs)
            self.assertFalse(kwargs["allow_redirects"])
            return self.response({"orderId": 101, "tournamentId": TOURNAMENT_ID})
        self.session.delete.side_effect = cancellation
        supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(self.session.delete.call_count, 1)
        self.assertEqual(intent["state"], "OBSERVED_TERMINAL")
        self.assertEqual(intent["observation"]["filled_quantity"], 0)
        self.session.post.assert_not_called()

    def test_cancel_409_race_preserves_filled_no_share_and_cost(self):
        intent = self.accepted("no")
        self.session.get.side_effect = [self.response(self.detail(intent)),
                                       self.response(self.detail(intent, opened=False, remaining=0, filled=1)),
                                       self.response(self.fills(intent, filled=1, price=.37))]
        self.session.delete.return_value = self.response(status=409)
        supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(intent["state"], "OBSERVED_TERMINAL")
        self.assertEqual(intent["observation"]["filled_quantity"], 1)
        self.assertAlmostEqual(intent["observation"]["filled_cost"], .37)
        self.session.post.assert_not_called()

    def test_cancel_timeout_stays_unknown_until_a_closed_get_and_never_resubmits(self):
        intent = self.accepted()
        self.session.get.side_effect = [self.response(self.detail(intent))]
        self.session.delete.side_effect = requests.Timeout("synthetic timeout")
        with self.assertRaisesRegex(supervised.TestError, "UNKNOWN"):
            supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(supervised.load_intent(self.path)["state"], "CANCEL_UNKNOWN")
        self.check_reads(intent)
        supervised.check_test(self.session, intent, self.path)
        self.assertEqual(intent["state"], "CANCEL_UNKNOWN")
        self.check_reads(intent, self.detail(intent, opened=False, remaining=0))
        supervised.check_test(self.session, intent, self.path)
        self.assertEqual(intent["state"], "OBSERVED_TERMINAL")
        self.assertEqual(self.session.delete.call_count, 1)
        self.session.post.assert_not_called()

    def test_untrusted_cancel_receipt_is_unknown_and_does_not_check_or_place_again(self):
        intent = self.accepted()
        self.session.get.side_effect = [self.response(self.detail(intent))]
        self.session.delete.return_value = self.response({"orderId": 101, "tournamentId": OTHER_TOURNAMENT_ID})
        with self.assertRaises(supervised.TestError):
            supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(intent["state"], "CANCEL_UNKNOWN")
        self.assertEqual(self.session.get.call_count, 1)
        self.session.post.assert_not_called()

    def test_cancel_already_closed_order_uses_only_gets(self):
        intent = self.accepted()
        closed = self.detail(intent, opened=False, remaining=0)
        self.session.get.side_effect = [self.response(closed), self.response(closed),
                                       self.response(self.fills(intent))]
        supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(intent["state"], "OBSERVED_TERMINAL")
        self.session.delete.assert_not_called()
        self.session.post.assert_not_called()

    def test_cli_prepare_with_fake_key_is_get_only_and_leaves_paper_files_unchanged(self):
        self.input_reads()
        paper_state = Path(self.temporary.name) / "paper_state.json"
        paper_trades = Path(self.temporary.name) / "paper_trades.csv"
        paper_state.write_text("synthetic paper state", encoding="utf-8")
        paper_trades.write_text("synthetic paper trades", encoding="utf-8")
        before = (paper_state.read_bytes(), paper_trades.read_bytes())
        sentinel_key = "FAKE-TEST-KEY-NOT-A-CREDENTIAL"
        with patch.object(supervised, "load_dotenv") as dotenv, \
                patch.object(supervised, "STATE_PATH", self.path), \
                patch.object(supervised.os, "getenv", return_value=sentinel_key), \
                patch.object(supervised.requests, "Session", return_value=self.session), \
                patch.object(paper, "PORTFOLIO_PATH", paper_state), \
                patch.object(paper, "TRADE_LOG_PATH", paper_trades), \
                patch.object(sys, "argv", ["account_test.py", "prepare", "--market", "1", "--side", "yes",
                                          "--state", str(self.path)]):
            self.assertEqual(supervised.main(), 0)
        dotenv.assert_called_once()
        self.assertEqual(self.session.get.call_count, 5)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()
        self.assertEqual(supervised.load_intent(self.path)["state"], "PREPARED")
        self.assertEqual((paper_state.read_bytes(), paper_trades.read_bytes()), before)
        self.assertNotIn(sentinel_key, self.output.getvalue())

    def test_cli_existing_journal_wrong_approval_and_show_never_open_network(self):
        intent = self.make_intent()
        before = self.path.read_bytes()
        for arguments, expected in ((["prepare", "--market", "1", "--side", "yes"], 1),
                                    (["submit", "--approve", "wrong-token"], 1), (["show"], 0)):
            with self.subTest(arguments=arguments), \
                    patch.object(supervised, "load_dotenv") as dotenv, \
                    patch.object(supervised.requests, "Session") as network, \
                    patch.object(sys, "argv", ["account_test.py", *arguments, "--state", str(self.path)]):
                self.assertEqual(supervised.main(), expected)
                dotenv.assert_not_called()
                network.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
        self.assertIn(intent["approval"], self.output.getvalue())

    def test_cli_request_failure_prints_safe_reason_not_raw_secret(self):
        sentinel = "FAKE-SECRET-FOR-REDACTION-CHECK"
        self.session.get.side_effect = requests.ConnectionError("Authorization Bearer " + sentinel)
        with patch.object(supervised, "load_dotenv"), \
                patch.object(supervised.os, "getenv", return_value=sentinel), \
                patch.object(supervised.requests, "Session", return_value=self.session), \
                patch.object(sys, "argv", ["account_test.py", "prepare", "--market", "1", "--side", "yes",
                                          "--state", str(self.path)]):
            self.assertEqual(supervised.main(), 1)
        self.assertNotIn(sentinel, self.output.getvalue())
        self.assertFalse(self.path.exists())
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_submit_exception_uses_fixed_reason_and_preserves_unknown_journal(self):
        intent = self.make_intent()
        sentinel = "FAKE-SECRET-FOR-ERROR-CHECK"
        self.session.post.side_effect = requests.ConnectionError("Authorization Bearer " + sentinel)
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()):
            with self.assertRaises(supervised.TestError) as caught:
                supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.assertNotIn(sentinel, str(caught.exception))
        self.assertNotIn(sentinel, self.path.read_text(encoding="utf-8"))
        self.assertEqual(supervised.load_intent(self.path)["state"], "UNKNOWN")

    def test_loaded_observations_reject_signed_boolean_and_inconsistent_costs(self):
        intent = self.accepted("no")
        malformed = [
            {"placement_quantity": -1, "placement_cost": .3},
            {"placement_quantity": True, "placement_cost": .3},
            {"placement_quantity": 1, "placement_cost": True},
            {"placement_quantity": .5, "placement_cost": .21},
            {"placement_quantity": 0},
            {"placement_quantity": 0, "placement_cost": 0,
             "filled_quantity": -1, "filled_cost": .3, "open": False},
            {"placement_quantity": 0, "placement_cost": 0,
             "filled_quantity": .5, "filled_cost": .1, "open": "false"},
            {"placement_quantity": 1, "placement_cost": .3,
             "filled_quantity": .5, "filled_cost": .2, "open": False},
        ]
        for observation in malformed:
            with self.subTest(observation=observation):
                bad = copy.deepcopy(intent)
                bad["observation"] = observation
                self.path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(supervised.TestError):
                    supervised.load_intent(self.path)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_loaded_unconfirmed_and_terminal_observations_have_state_consistency(self):
        intent = self.make_intent()
        intent["observation"] = {"placement_quantity": 0, "placement_cost": 0}
        self.path.write_text(json.dumps(intent), encoding="utf-8")
        with self.assertRaises(supervised.TestError):
            supervised.load_intent(self.path)
        intent = self.accepted()
        intent.update(state="OBSERVED_TERMINAL", observation={
            "placement_quantity": 0, "placement_cost": 0,
            "filled_quantity": 0, "filled_cost": 0, "open": True,
        })
        self.path.write_text(json.dumps(intent), encoding="utf-8")
        with self.assertRaises(supervised.TestError):
            supervised.load_intent(self.path)

    def test_fill_cost_cannot_increase_without_new_shares(self):
        intent = self.accepted()
        partial = self.detail(intent, remaining=.5)
        self.check_reads(intent, partial, [self.fills(intent, filled=.5, price=.35)])
        supervised.check_test(self.session, intent, self.path)
        before = self.path.read_bytes()
        self.check_reads(intent, partial, [self.fills(intent, filled=.5, price=.4)])
        with self.assertRaisesRegex(supervised.TestError, "cost changed"):
            supervised.check_test(self.session, intent, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertAlmostEqual(intent["observation"]["filled_cost"], .175)

    def test_terminal_history_cannot_gain_more_shares_on_later_checks(self):
        intent = self.accepted()
        self.check_reads(intent, self.detail(intent, opened=False, remaining=0, filled=.4),
                         [self.fills(intent, filled=.4, price=.35)])
        supervised.check_test(self.session, intent, self.path)
        before = self.path.read_bytes()
        self.check_reads(intent, self.detail(intent, opened=False, remaining=0, filled=.5),
                         [self.fills(intent, filled=.5, price=.35)])
        with self.assertRaisesRegex(supervised.TestError, "Closed reporting history changed"):
            supervised.check_test(self.session, intent, self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_terminal_order_reopening_prevents_delete_before_any_journal_change(self):
        intent = self.accepted()
        self.check_reads(intent, self.detail(intent, opened=False, remaining=0))
        supervised.check_test(self.session, intent, self.path)
        before = self.path.read_bytes()
        self.session.get.side_effect = [self.response(self.detail(intent))]
        with self.assertRaisesRegex(supervised.TestError, "terminal order reopened"):
            supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(self.path.read_bytes(), before)
        self.session.delete.assert_not_called()

    def test_placement_open_remainder_and_fill_price_must_be_consistent(self):
        cases = [dict(open=True, remainingQuantity=0),
                 dict(open=False, remainingQuantity=1),
                 dict(open=True, quantityTraded=.1, remainingQuantity=.5, totalCost=.04, fillPrice=.4),
                 dict(open=False, quantityTraded=1, remainingQuantity=0, totalCost=.5, fillPrice=.7),
                 dict(open=False, quantityTraded=1, remainingQuantity=0, totalCost=.5, fillPrice=True)]
        for changes in cases:
            with self.subTest(changes=changes):
                intent = self.make_intent()
                receipt = self.receipt(intent)
                receipt.update(changes)
                self.session.post.reset_mock()
                with self.assertRaises(supervised.TestError):
                    self.submit(intent, receipt)
                self.assertEqual(intent["state"], "UNKNOWN")
                self.assertEqual(self.session.post.call_count, 1)
        self.session.delete.assert_not_called()

    def test_fill_order_id_cannot_be_boolean_even_when_known_id_is_one(self):
        intent = self.accepted()
        intent["order_id"] = 1
        supervised.save_intent(intent, self.path)
        bad = self.fills(intent)
        bad["orderId"] = True
        self.check_reads(intent, pages=[bad])
        with self.assertRaisesRegex(supervised.TestError, "invalid scope or coverage"):
            supervised.check_test(self.session, intent, self.path)
        self.assertEqual(intent["state"], "ACCEPTED")

    def test_cancel_receipt_order_id_cannot_be_boolean_when_known_id_is_one(self):
        intent = self.accepted()
        intent["order_id"] = 1
        supervised.save_intent(intent, self.path)
        self.session.get.side_effect = [self.response(self.detail(intent))]
        self.session.delete.return_value = self.response({"orderId": True, "tournamentId": TOURNAMENT_ID})
        with self.assertRaises(supervised.TestError):
            supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(intent["state"], "CANCEL_UNKNOWN")
        self.assertEqual(self.session.delete.call_count, 1)
        self.assertEqual(self.session.get.call_count, 1)

    def test_order_detail_boolean_id_prevents_cancellation(self):
        intent = self.accepted()
        intent["order_id"] = 1
        supervised.save_intent(intent, self.path)
        bad = self.detail(intent)
        bad["id"] = True
        self.session.get.side_effect = [self.response(bad)]
        with self.assertRaises(supervised.TestError):
            supervised.cancel_test(self.session, intent, self.path, intent["approval"])
        self.session.delete.assert_not_called()

    def test_expiration_is_checked_again_after_preflight_before_any_post(self):
        intent = self.make_intent()
        expiry = supervised.parse_api_timestamp(intent["request"]["expirationDate"]).timestamp()
        before = self.path.read_bytes()
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()) as preflight, \
                patch.object(supervised.time, "time", side_effect=[expiry - 1, expiry]):
            with self.assertRaisesRegex(supervised.TestError, "expired"):
                supervised.submit_test(self.session, intent, self.path, intent["approval"])
        preflight.assert_called_once()
        self.session.post.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_prepared_copy_cannot_overwrite_attempted_or_partially_filled_journal(self):
        original = self.make_intent()
        for state in ("SUBMITTING", "UNKNOWN", "ACCEPTED"):
            saved = copy.deepcopy(original)
            saved["state"] = state
            if state == "ACCEPTED":
                saved.update(order_id=101, observation={"placement_quantity": .5, "placement_cost": .325})
            supervised.save_intent(saved, self.path)
            before = self.path.read_bytes()
            with self.subTest(state=state), patch.object(supervised, "read_test_inputs") as reads, \
                    self.assertRaisesRegex(supervised.TestError, "Saved journal changed"):
                supervised.submit_test(self.session, copy.deepcopy(original), self.path, original["approval"])
            reads.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)
        self.session.post.assert_not_called()

    def test_submission_lock_blocks_another_caller_before_any_api_reads(self):
        intent = self.make_intent()
        with supervised.submission_lock(self.path), patch.object(supervised, "read_test_inputs") as reads:
            with self.assertRaisesRegex(supervised.TestError, "locked"):
                supervised.submit_test(self.session, intent, self.path, intent["approval"])
        reads.assert_not_called()
        self.session.post.assert_not_called()
        self.assertEqual(supervised.load_intent(self.path)["state"], "PREPARED")

    def test_submission_through_symlink_updates_the_locked_original_journal(self):
        intent = self.make_intent()
        alias = self.path.with_name('alias.json')
        alias.symlink_to(self.path)
        self.session.post.return_value = self.response(self.receipt(intent))
        with patch.object(supervised, "read_test_inputs", return_value=self.inputs()):
            supervised.submit_test(self.session, intent, alias, intent["approval"])
        self.assertTrue(alias.is_symlink())
        self.assertEqual(supervised.load_intent(self.path)["state"], "ACCEPTED")
        self.assertEqual(self.session.post.call_count, 1)

    def test_alternate_cli_path_cannot_prepare_or_submit_around_an_unknown_default(self):
        intent = self.make_intent()
        supervised.commit_state(intent, self.path, "UNKNOWN")
        alternate = self.path.with_name('replacement.json')
        before = self.path.read_bytes()
        for action, extra in (("prepare", ["--market", "1", "--side", "no"]),
                              ("submit", ["--approve", intent["approval"]])):
            with self.subTest(action=action), patch.object(sys, "argv", [
                    "account_test.py", action, "--state", str(alternate)] + extra), \
                    patch.object(supervised, "load_dotenv") as dotenv, \
                    patch.object(supervised.requests, "Session") as factory:
                self.assertEqual(supervised.main(), 1)
            dotenv.assert_not_called()
            factory.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(alternate.exists())

    def test_single_cli_submission_cannot_bypass_existing_pair_journals(self):
        intent = self.make_intent()
        self.path.with_name('paired_account_test').mkdir()
        with patch.object(sys, "argv", ['account_test.py', 'submit', '--approve', intent['approval']]), \
                patch.object(supervised, "load_dotenv") as dotenv, \
                patch.object(supervised.requests, "Session") as factory:
            self.assertEqual(supervised.main(), 1)
        dotenv.assert_not_called()
        factory.assert_not_called()

    def test_directory_sync_completes_before_post_and_failure_blocks_submission(self):
        intent = self.make_intent()
        original_fsync = supervised.os.fsync
        directory_flushes = []
        def fsync(descriptor):
            if stat.S_ISDIR(supervised.os.fstat(descriptor).st_mode):
                directory_flushes.append(supervised.load_intent(self.path)["state"])
            return original_fsync(descriptor)
        def post(url, **kwargs):
            self.assertIn("SUBMITTING", directory_flushes)
            return self.response(self.receipt(intent))
        self.session.post.side_effect = post
        with patch.object(supervised.os, "fsync", side_effect=fsync), \
                patch.object(supervised, "read_test_inputs", return_value=self.inputs()):
            supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.assertEqual(directory_flushes, ['SUBMITTING', 'ACCEPTED'])

    def test_failed_directory_sync_of_submitting_intent_prevents_post_and_stale_retry(self):
        intent = self.make_intent()
        with patch.object(supervised, "sync_directory", side_effect=OSError('synthetic directory failure')), \
                patch.object(supervised, "read_test_inputs", return_value=self.inputs()), self.assertRaises(supervised.TestError):
            supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.session.post.assert_not_called()
        self.assertEqual(supervised.load_intent(self.path)["state"], "SUBMITTING")
        with self.assertRaisesRegex(supervised.TestError, "Saved journal changed"):
            supervised.submit_test(self.session, intent, self.path, intent["approval"])
        self.session.post.assert_not_called()

    def test_exclusive_new_journal_creation_flushes_its_parent_directory(self):
        intent = self.make_intent(save=False)
        with patch.object(supervised, "sync_directory") as flush:
            supervised.save_intent(intent, self.path, new=True)
        flush.assert_called_once_with(self.path.parent)


if __name__ == "__main__":
    unittest.main()
