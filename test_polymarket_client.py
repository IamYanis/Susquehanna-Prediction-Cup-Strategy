"""Public-client safety and pricing tests; no network requests."""
import json
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

import requests

import polymarket_client as client


def response(status=200, payload=None, headers=None):
    return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=payload or {}))


class PublicClientTests(unittest.TestCase):
    def test_only_documented_get_routes_are_reachable(self):
        with client.ReadOnlySession("POLYMARKET") as session, patch.object(requests.Session, "request") as send:
            for method, url in [("POST", client.CLOB_URL + "/orders"), ("DELETE", client.CLOB_URL + "/order"),
                                ("GET", client.CLOB_URL + "/orders"), ("GET", "https://other.example/book"),
                                ("GET", client.GAMMA_URL + "/markets/1")]:
                if method == "GET" and url == client.GAMMA_URL + "/markets/1":
                    session.request(method, url, allow_redirects=False)
                else:
                    with self.assertRaises(client.ExternalDataError):
                        session.request(method, url, allow_redirects=False)
            self.assertEqual(send.call_count, 1)

    def test_sig_session_also_refuses_order_routes_and_writes(self):
        with client.ReadOnlySession("SIG") as session, patch.object(requests.Session, "request") as send:
            for method, url in [("POST", "https://sig.thesuper.market/api/v1/orders"),
                                ("GET", "https://sig.thesuper.market/api/v1/orders"),
                                ("GET", client.GAMMA_URL + "/markets/1")]:
                with self.assertRaises(client.ExternalDataError):
                    session.request(method, url)
            send.assert_not_called()

    def test_public_session_rejects_sig_key_auth_and_redirects(self):
        with client.ReadOnlySession("POLYMARKET") as session, patch.object(requests.Session, "request") as send:
            session.headers["Authorization"] = "Bearer FAKE_TEST_KEY"
            with self.assertRaises(client.ExternalDataError):
                client.fetch_json(session, client.GAMMA_URL + "/markets/1")
            del session.headers["Authorization"]
            with self.assertRaises(client.ExternalDataError):
                session.get(client.GAMMA_URL + "/markets/1", allow_redirects=True)
            send.assert_not_called()

    def test_anonymous_server_cookies_are_discarded_between_gets(self):
        with client.ReadOnlySession("POLYMARKET") as session:
            def server_reply(*args, **kwargs):
                session.cookies.set("__cf_bm", "FAKE_ANONYMOUS_TEST_COOKIE")
                return response(payload={"id": "1"})
            with patch.object(requests.Session, "request", side_effect=server_reply) as send:
                for _ in range(2):
                    session.get(client.GAMMA_URL + "/markets/1", allow_redirects=False)
                    self.assertFalse(session.cookies)
                self.assertEqual(send.call_count, 2)

    def test_retry_get_on_transient_error(self):
        session = Mock(last_read_started=None)
        session.get.side_effect = [requests.Timeout(), response(503), response(payload={"id": "1"})]
        with patch.object(client.time, "sleep"):
            self.assertEqual(client.fetch_json(session, client.GAMMA_URL + "/markets/1"), {"id": "1"})
        self.assertEqual(session.get.call_count, 3)
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in session.get.call_args_list))

    def test_rate_limit_retry_and_long_wait_stops(self):
        session = Mock(last_read_started=None)
        session.get.side_effect = [response(429, headers={"Retry-After": "2"}), response(payload={"ok": True})]
        with patch.object(client.time, "sleep") as sleep:
            self.assertEqual(client.fetch_json(session, client.GAMMA_URL + "/markets/1"), {"ok": True})
        sleep.assert_any_call(2)
        session = Mock(last_read_started=None, get=Mock(return_value=response(429, headers={"Retry-After": "60"})))
        with self.assertRaisesRegex(client.ExternalDataError, "EXTERNAL_RATE_LIMITED"):
            client.fetch_json(session, client.GAMMA_URL + "/markets/1")
        self.assertEqual(session.get.call_count, 1)

    def test_retries_are_bounded(self):
        session = Mock(last_read_started=None, get=Mock(side_effect=requests.ConnectionError()))
        with patch.object(client.time, "sleep"), self.assertRaises(client.ExternalDataError):
            client.fetch_json(session, client.GAMMA_URL + "/markets/1")
        self.assertEqual(session.get.call_count, 3)

    def test_redirect_or_bad_json_is_not_retried(self):
        for reply in [response(302), Mock(status_code=200, json=Mock(side_effect=ValueError()))]:
            session = Mock(last_read_started=None, get=Mock(return_value=reply))
            with self.assertRaises(client.ExternalDataError):
                client.fetch_json(session, client.GAMMA_URL + "/markets/1")
            self.assertEqual(session.get.call_count, 1)

    def test_metadata_token_order_follows_outcome_labels(self):
        raw = {"id": "1", "conditionId": "0x" + "a" * 64, "outcomes": json.dumps(["No", "Yes"]),
               "clobTokenIds": json.dumps(["2", "1"]), "outcomePrices": '["0.2", "0.8"]',
               "liquidity": "50", "volume": "100", "active": True, "closed": False,
               "enableOrderBook": True, "acceptingOrders": True}
        with patch.object(client, "fetch_json", return_value=raw):
            metadata = client.read_market(Mock(), "1")
        self.assertEqual(metadata["tokens"], {"No": "2", "Yes": "1"})
        self.assertEqual(metadata["headline"]["Yes"], Decimal("0.8"))
        self.assertTrue(metadata["open"])
        for field, value in [("id", "9"), ("outcomes", '["Candidate", "Other"]'), ("version", "v2")]:
            with patch.object(client, "fetch_json", return_value=dict(raw, **{field: value})), self.assertRaises(client.ExternalDataError):
                client.read_market(Mock(), "1")

    def book(self, bids=None, asks=None, **changes):
        raw = {"asset_id": "1", "market": "condition", "timestamp": "1000000",
               "bids": bids if bids is not None else [{"price": ".10", "size": "999"}, {"price": ".40", "size": "60"}],
               "asks": asks if asks is not None else [{"price": ".99", "size": "999"}, {"price": ".60", "size": "70"}]}
        raw.update(changes)
        return client.normalize_book(raw, "1", "condition", 1000)

    def test_true_best_prices_midpoint_and_top_depth(self):
        book = self.book()
        self.assertEqual(book["bid"], Decimal(".40"))
        self.assertEqual(book["ask"], Decimal(".60"))
        self.assertEqual(book["midpoint"], Decimal(".50"))
        self.assertEqual(book["bid_depth"], Decimal("60"))
        self.assertEqual(book["ask_depth"], Decimal("70"))
        self.assertEqual(client.reference(book, Decimal(".95")), (Decimal(".50"), "BOOK_MIDPOINT", "HIGH"))

    def test_best_depth_sums_equal_levels(self):
        book = self.book(bids=[{"price": ".40", "size": "25"}, {"price": ".40", "size": "30"}])
        self.assertEqual(book["bid_depth"], Decimal("55"))

    def test_missing_sides_lower_confidence(self):
        for book, expected in [(self.book(bids=[]), "ONE_SIDED_ASK"), (self.book(asks=[]), "ONE_SIDED_BID"),
                               (self.book(bids=[], asks=[]), "HEADLINE_REFERENCE_ONLY"), (None, "HEADLINE_REFERENCE_ONLY")]:
            _, source, confidence = client.reference(book, Decimal(".95"))
            self.assertEqual(source, expected)
            self.assertEqual(confidence, "LOW")

    def test_stale_missing_future_timestamps_are_not_fresh(self):
        self.assertTrue(client.is_fresh(self.book(), 1000))
        self.assertFalse(client.is_fresh(self.book(timestamp=None), 1000))
        self.assertFalse(client.is_fresh(self.book(timestamp="939000"), 1000))
        self.assertFalse(client.is_fresh(self.book(timestamp="1006000"), 1000))
        self.assertFalse(client.is_fresh(self.book(), 1006))

    def test_wrong_identity_crossed_and_invalid_numbers_rejected(self):
        for changes in [{"asset_id": "wrong"}, {"market": "wrong"},
                        {"bids": [{"price": ".90", "size": "1"}]},
                        {"asks": [{"price": "NaN", "size": "10"}]},
                        {"bids": [{"price": ".40", "size": "-1"}]}]:
            with self.assertRaises(client.ExternalDataError):
                self.book(**changes)


if __name__ == "__main__":
    unittest.main()
