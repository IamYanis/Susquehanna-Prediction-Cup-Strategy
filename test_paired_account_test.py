"""One-pair controller tests. Fake HTTP only; no credentials or account writes."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import account_test as single
import order_preview
import paired_account_test as paired
import paper_trader as paper
import price_reader as scanner
from test_account_reader import holdings, orders_page, tournament
from test_api_audit import QUOTE_TIME, TOURNAMENT_ID, election_tree, exchange_book, page, pair_relationship, party_market
from test_order_preview import account_snapshot, context, parsed_book


class PairedAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "paired"
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.records = {}
        self.output = io.StringIO()
        for target, name, value in ((scanner, "_last_request_started", None), (scanner, "_read_cooldown_until", 0),
                                    (single, "STATE_PATH", Path(self.temporary.name) / "unused_single.json")):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("time", QUOTE_TIME), ("monotonic", 100)):
            patcher = patch.object(paired.time, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(scanner.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.account = account_snapshot()
        self.preview = order_preview.build_preview(self.account, "NO-PAIR", context(),
                                                  [parsed_book(bid=.6), parsed_book(bid=.6)], 100, quantity=1)

    def response(self, payload, status=200):
        response = requests.Response()
        response.status_code = status
        response.url = scanner.API_BASE_URL + "/orders"
        response._content = json.dumps(payload).encode()
        return response

    def prepare(self):
        with patch.object(paired, "read_account", return_value=self.account), \
                patch.object(paired, "preview_pair", return_value=copy.deepcopy(self.preview)):
            return paired.prepare_pair(self.session, self.directory, "1", "2")

    def conditional_payloads(self):
        return [tournament(), holdings([]), orders_page([]),
                party_market("Democratic", 387, 1076), party_market("Republican", 388, 1077),
                page([]), election_tree("Democratic", 387), election_tree("Republican", 388),
                exchange_book(387, 1076, bid=.875, ask=.88),
                exchange_book(388, 1077, bid=.155, ask=.16)]

    def prepare_conditional(self):
        self.session.get.side_effect = [self.response(payload) for payload in self.conditional_payloads()]
        return paired.prepare_pair(self.session, self.directory, "387", "388", conditional=True)

    def install_venue(self, fills=(1, 1), unknown_at=None, late_full=False):
        def post(url, **kwargs):
            pair, legs = paired.load_pair(self.directory)
            index = self.session.post.call_count - 1
            self.assertEqual(pair["state"], "EXECUTING")
            self.assertEqual(legs[index]["state"], "SUBMITTING")
            body = kwargs["json"]
            self.assertEqual(body, pair["requests"][index]["request"])
            self.assertEqual(body["quantity"], 1)
            self.assertFalse(kwargs["allow_redirects"])
            order_id, quantity = 101 + index, fills[index]
            self.records[order_id] = {"body": copy.deepcopy(body), "quantity": quantity, "open": quantity < 1}
            if unknown_at == index:
                raise requests.Timeout("synthetic-secret-in-exception")
            return self.response({"orderId": order_id, "exchangeId": body["exchangeId"],
                                  "action": "buy", "side": "no", "quantity": 1, "price": body["price"],
                                  "open": quantity < 1, "quantityTraded": quantity,
                                  "totalCost": quantity * body["price"], "remainingQuantity": 1 - quantity,
                                  "fillPrice": None if quantity == 0 else body["price"]})

        def get(url, **kwargs):
            parts = url.split("/")
            order_id = int(parts[-2] if parts[-1] == "fills" else parts[-1])
            record = self.records[order_id]
            body, quantity = record["body"], record["quantity"]
            if parts[-1] == "fills":
                rows = [] if quantity == 0 else [{"id": 1000 + order_id, "side": "no", "quantity": -quantity,
                                                 "price": body["price"], "filledAt": "2026-10-07T12:00:00Z"}]
                return self.response({"orderId": order_id, "exchangeId": body["exchangeId"], "tournamentId": TOURNAMENT_ID,
                                      "coverage": {"complete": True}, "data": rows,
                                      "totalQuantityFilled": -quantity, "avgFillPrice": body["price"] if quantity else None,
                                      "pagination": {"hasMore": False, "nextCursor": None}})
            return self.response({"id": order_id, "exchangeId": body["exchangeId"], "side": "no", "action": "buy",
                                  "quantity": 1 - quantity if record["open"] else 1, "quantityFilled": quantity,
                                  "priceLimit": body["price"], "open": record["open"], "tournamentId": TOURNAMENT_ID,
                                  "expirationDate": body["expirationDate"], "terminalReasonCode": None if record["open"] else "closed"})

        def delete(url, **kwargs):
            pair, _ = paired.load_pair(self.directory)
            self.assertEqual(pair["state"], "EXECUTING" if self.session.post.call_count else "CANCELLING")
            order_id = int(url.split("/")[-1])
            record = self.records[order_id]
            record["open"] = False
            if late_full:
                record["quantity"] = 1
            return self.response({"orderId": order_id, "tournamentId": TOURNAMENT_ID})

        self.session.post.side_effect = post
        self.session.get.side_effect = get
        self.session.delete.side_effect = delete

    def execute(self, pair):
        def inputs(session, market_id, side, slug):
            exchange = "11" if market_id == "1" else "12"
            return self.account, exchange, .4, single.account_risk(self.account, [exchange])
        with patch.object(paired, "preflight_pair"), patch.object(single, "read_test_inputs", side_effect=inputs):
            return paired.submit_pair(self.session, self.directory, pair["approval"])

    def test_full_pair_uses_two_durable_distinct_requests_and_never_replays(self):
        pair, _ = self.prepare()
        self.install_venue()
        result, legs = self.execute(pair)
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual([paired.filled_quantity(leg) for leg in legs], [1, 1])
        self.assertEqual(self.session.post.call_count, 2)
        self.session.delete.assert_not_called()
        self.assertNotEqual(legs[0]["request"]["idempotencyKey"], legs[1]["request"]["idempotencyKey"])
        with self.assertRaises(single.TestError):
            self.execute(result)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertFalse((self.directory / ".operation.lock").exists())

    def test_first_partial_or_unfilled_order_cancels_remainder_without_second_post(self):
        for quantity in (0, .5):
            with self.subTest(quantity=quantity):
                self.directory = Path(self.temporary.name) / f"partial_{quantity}"
                self.session.reset_mock()
                pair, _ = self.prepare()
                self.install_venue(fills=(quantity, 1))
                result, legs = self.execute(pair)
                self.assertEqual(self.session.post.call_count, 1)
                self.assertEqual(self.session.delete.call_count, 1)
                self.assertEqual(legs[1]["state"], "PREPARED")
                self.assertEqual(paired.filled_quantity(legs[0]), quantity)
                self.assertEqual(result["state"], "UNMATCHED" if quantity else "CLOSED_NO_FILL")

    def test_late_full_first_fill_during_cancel_can_complete_second_leg(self):
        pair, _ = self.prepare()
        self.install_venue(fills=(.5, 1), late_full=True)
        result, legs = self.execute(pair)
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(self.session.delete.call_count, 1)
        self.assertEqual([paired.filled_quantity(leg) for leg in legs], [1, 1])

    def test_second_partial_fill_is_preserved_after_cancel_and_blocks_another_pair(self):
        pair, _ = self.prepare()
        self.install_venue(fills=(1, .5))
        result, legs = self.execute(pair)
        self.assertEqual(result["state"], "UNMATCHED")
        self.assertEqual([paired.filled_quantity(leg) for leg in legs], [1, .5])
        self.assertAlmostEqual(sum(leg["observation"]["filled_cost"] for leg in legs), .6)
        self.assertEqual(self.session.delete.call_count, 1)
        with self.assertRaises(single.TestError):
            self.execute(result)
        self.assertEqual(self.session.post.call_count, 2)

    def test_lost_placement_ack_stops_without_replay_even_after_restart_checks(self):
        for index in (0, 1):
            with self.subTest(index=index):
                self.directory = Path(self.temporary.name) / f"unknown_{index}"
                self.session.reset_mock()
                pair, _ = self.prepare()
                self.install_venue(unknown_at=index)
                with self.assertRaises(single.TestError):
                    self.execute(pair)
                restored, legs = paired.load_pair(self.directory)
                self.assertEqual(restored["state"], "UNKNOWN")
                self.assertEqual(legs[index]["state"], "UNKNOWN")
                self.assertIsNone(legs[index]["order_id"])
                with self.assertRaises(single.TestError):
                    self.execute(restored)
                result, _ = paired.check_pair(self.session, self.directory)
                self.assertEqual(result["state"], "UNKNOWN")
                self.assertEqual(self.session.post.call_count, index + 1)
                self.session.delete.assert_not_called()

    def test_second_preflight_failure_retains_first_share_and_does_not_retry(self):
        pair, _ = self.prepare()
        self.install_venue()
        def inputs(session, market_id, side, slug):
            if market_id == "2":
                raise single.TestError("Current second quote exceeds the limit")
            return self.account, "11", .4, single.account_risk(self.account, ["11"])
        with patch.object(paired, "preflight_pair"), patch.object(single, "read_test_inputs", side_effect=inputs):
            with self.assertRaises(single.TestError):
                paired.submit_pair(self.session, self.directory, pair["approval"])
        self.assertEqual(self.session.post.call_count, 1)
        result, legs = paired.check_pair(self.session, self.directory)
        self.assertEqual(result["state"], "UNMATCHED")
        self.assertEqual(paired.filled_quantity(legs[0]), 1)
        self.assertEqual(legs[1]["state"], "PREPARED")

    def test_wrong_approval_and_controller_save_failure_prevent_all_posts(self):
        pair, _ = self.prepare()
        with self.assertRaises(single.TestError):
            paired.submit_pair(self.session, self.directory, "wrong")
        self.session.get.assert_not_called()
        with patch.object(paired, "preflight_pair"), patch.object(paired, "save_pair", side_effect=single.TestError("save failed")):
            with self.assertRaises(single.TestError):
                paired.submit_pair(self.session, self.directory, pair["approval"])
        self.session.post.assert_not_called()
        self.assertEqual(paired.load_pair(self.directory)[0]["state"], "PREPARED")

    def test_leg_save_failure_after_acceptance_keeps_submitting_and_blocks_replacement(self):
        pair, _ = self.prepare()
        self.install_venue()
        original = single.save_intent
        def save(intent, path, new=False):
            if intent["state"] == "ACCEPTED":
                raise single.TestError("Accepted receipt cannot be saved")
            return original(intent, path, new=new)
        with patch.object(single, "save_intent", side_effect=save):
            with self.assertRaises(single.TestError):
                self.execute(pair)
        restored, legs = paired.load_pair(self.directory)
        self.assertEqual(restored["state"], "UNKNOWN")
        self.assertEqual(legs[0]["state"], "SUBMITTING")
        self.assertEqual(self.session.post.call_count, 1)
        with self.assertRaises(single.TestError):
            self.execute(restored)

    def test_cancellation_of_completed_pair_never_erases_shares_or_costs(self):
        pair, _ = self.prepare()
        self.install_venue()
        result, legs = self.execute(pair)
        original = [copy.deepcopy(leg["observation"]) for leg in legs]
        result, legs = paired.cancel_pair(self.session, self.directory, pair["approval"])
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual([leg["observation"] for leg in legs], original)
        self.session.delete.assert_not_called()
        self.assertEqual(self.session.post.call_count, 2)

    def test_lock_blocks_concurrent_actions_and_incomplete_journals_never_reset(self):
        pair, _ = self.prepare()
        with paired.operation_lock(self.directory):
            with self.assertRaises(single.TestError):
                self.execute(pair)
        self.session.post.assert_not_called()
        paired.leg_path(self.directory, 1).unlink()
        with self.assertRaises(single.TestError):
            paired.load_pair(self.directory)
        with self.assertRaises(single.TestError):
            self.prepare()

    def test_mutated_request_policy_and_false_complete_state_fail_closed(self):
        pair, legs = self.prepare()
        for field, value in (("policy", "conditional-bypass"), ("state", "COMPLETE")):
            bad = copy.deepcopy(pair)
            bad[field] = value
            bad["approval"] = paired.approval_hash(bad)
            if field == "policy":
                with self.assertRaises(single.TestError):
                    paired.validate_pair(bad)
            else:
                paired.save_pair(bad, self.directory)
                with self.assertRaises(single.TestError):
                    paired.load_pair(self.directory)
        paired.save_pair(pair, self.directory)
        changed = legs[0]
        changed["request"]["price"] = .405
        changed["approval"] = single.approval_hash(changed)
        single.save_intent(changed, paired.leg_path(self.directory, 0))
        with self.assertRaises(single.TestError):
            paired.load_pair(self.directory)

    def test_pair_preflight_checks_rules_limits_and_approved_total_cash(self):
        pair, _ = self.prepare()
        for change in ("fingerprint", "price", "cash"):
            preview = copy.deepcopy(self.preview)
            account = copy.deepcopy(self.account)
            if change == "fingerprint":
                preview["market_context"]["settlement_fingerprint"] = "b" * 64
            elif change == "price":
                preview["request"]["legs"][0]["price"] = .405
            else:
                account["tournament"]["myBalance"] = .79
            with self.subTest(change=change), patch.object(paired, "read_account", return_value=account), \
                    patch.object(paired, "preview_pair", return_value=preview), self.assertRaises(ValueError):
                paired.preflight_pair(self.session, pair)
        self.session.post.assert_not_called()

    def test_prepare_real_parsers_use_gets_only_and_keep_paper_files_unchanged(self):
        payloads = [tournament(), holdings([]), orders_page([]), party_market("Democratic", 1, 11),
                    party_market("Republican", 2, 12), page([pair_relationship()]),
                    election_tree("Democratic", 1), election_tree("Republican", 2),
                    exchange_book(1, 11, bid=.6, ask=.65), exchange_book(2, 12, bid=.6, ask=.65)]
        self.session.get.side_effect = [self.response(payload) for payload in payloads]
        portfolio = Path(self.temporary.name) / "paper_portfolio.json"
        trades = Path(self.temporary.name) / "paper_trades.csv"
        portfolio.write_text("saved portfolio sentinel")
        trades.write_text("saved trade sentinel")
        with patch.object(paper, "PORTFOLIO_PATH", portfolio), patch.object(paper, "TRADE_LOG_PATH", trades):
            pair, legs = paired.prepare_pair(self.session, self.directory, "1", "2")
        self.assertEqual(self.session.get.call_count, 10)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()
        self.assertEqual(pair["state"], "PREPARED")
        self.assertTrue(all(leg["request"]["side"] == "no" and leg["request"]["quantity"] == 1 for leg in legs))
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in self.session.get.call_args_list))
        self.assertEqual(portfolio.read_text(), "saved portfolio sentinel")
        self.assertEqual(trades.read_text(), "saved trade sentinel")

    def test_missing_settlement_and_existing_single_test_block_preparation(self):
        with patch.object(paired, "read_account", return_value=self.account), \
                patch.object(paired, "preview_pair", side_effect=order_preview.PreviewBlocked("No active relationship")):
            with self.assertRaises(ValueError):
                paired.prepare_pair(self.session, self.directory, "1", "2")
        self.assertFalse(self.directory.exists())
        single.STATE_PATH.write_text("unfinished test")
        with self.assertRaises(single.TestError):
            self.prepare()
        self.assertFalse(self.directory.exists())

    def test_cli_show_and_wrong_approval_do_not_read_env_or_open_session(self):
        pair, _ = self.prepare()
        for action, extra in (("show", []), ("submit", ["--approve", "wrong"])):
            with self.subTest(action=action), patch("sys.argv", ["paired_account_test.py", action] + extra), \
                    patch.object(paired, "STATE_DIR", self.directory), patch.object(paired, "load_dotenv") as dotenv, \
                    patch.object(paired.requests, "Session") as factory, contextlib.redirect_stdout(self.output):
                self.assertEqual(paired.main(), 0 if action == "show" else 1)
                dotenv.assert_not_called()
                factory.assert_not_called()

    def test_cancel_timeout_on_first_leg_stops_before_second_post(self):
        pair, _ = self.prepare()
        self.install_venue(fills=(.5, 1))
        self.session.delete.side_effect = requests.Timeout("synthetic-secret")
        with self.assertRaises(single.TestError):
            self.execute(pair)
        result, legs = paired.load_pair(self.directory)
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(legs[0]["state"], "CANCEL_UNKNOWN")
        self.assertEqual(paired.filled_quantity(legs[0]), .5)
        self.assertEqual(legs[1]["state"], "PREPARED")
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.session.delete.call_count, 1)

    def test_first_order_reporting_failure_stops_before_second_and_can_be_read_later(self):
        pair, _ = self.prepare()
        self.install_venue()
        original = self.session.get.side_effect
        self.session.get.side_effect = requests.HTTPError("503 with synthetic-secret")
        with self.assertRaises(single.TestError):
            self.execute(pair)
        self.assertEqual(self.session.post.call_count, 1)
        self.session.delete.assert_not_called()
        self.session.get.side_effect = original
        result, legs = paired.check_pair(self.session, self.directory)
        self.assertEqual(result["state"], "UNMATCHED")
        self.assertEqual(paired.filled_quantity(legs[0]), 1)
        self.assertEqual(legs[1]["state"], "PREPARED")
        self.assertEqual(self.session.post.call_count, 1)

    def test_first_noop_does_not_submit_second_or_guess_an_order_id(self):
        pair, _ = self.prepare()
        body = pair["requests"][0]["request"]
        self.session.post.return_value = self.response({"orderId": None, "exchangeId": body["exchangeId"],
                                                       "action": "buy", "side": "no", "quantity": 1,
                                                       "price": body["price"], "open": False, "quantityTraded": 0,
                                                       "totalCost": 0, "remainingQuantity": 0, "fillPrice": None})
        result, legs = self.execute(pair)
        self.assertEqual(result["state"], "CLOSED_NO_FILL")
        self.assertEqual(legs[0]["state"], "NOOP")
        self.assertIsNone(legs[0]["order_id"])
        self.assertEqual(self.session.post.call_count, 1)
        self.session.get.assert_not_called()
        self.session.delete.assert_not_called()

    def test_cli_does_not_offer_an_alternate_directory_to_bypass_unresolved_attempt(self):
        with patch("sys.argv", ["paired_account_test.py", "prepare", "--state", "another-path"]), \
                patch.object(paired, "load_dotenv") as dotenv, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                paired.main()
            dotenv.assert_not_called()

    def test_single_order_cli_cannot_prepare_while_a_paired_journal_exists(self):
        single.STATE_PATH.with_name("paired_account_test").mkdir()
        with patch("sys.argv", ["account_test.py", "prepare", "--market", "1", "--side", "no"]), \
                patch.object(single, "load_dotenv") as dotenv, patch.object(single.requests, "Session") as factory, \
                contextlib.redirect_stdout(self.output):
            self.assertEqual(single.main(), 1)
            dotenv.assert_not_called()
            factory.assert_not_called()

    def test_cli_missing_settlement_explains_policy_and_never_exposes_fake_key(self):
        with patch("sys.argv", ["paired_account_test.py", "prepare", "--dem-market", "1", "--rep-market", "2"]), \
                patch.object(paired, "STATE_DIR", self.directory), patch.object(paired, "load_dotenv"), \
                patch.object(paired.os, "getenv", return_value="synthetic-secret"), \
                patch.object(paired.requests, "Session", return_value=self.session), \
                patch.object(paired, "read_account", return_value=self.account), \
                patch.object(paired, "preview_pair", side_effect=order_preview.PreviewBlocked("No active relationship verifies this pair")), \
                contextlib.redirect_stdout(self.output):
            self.assertEqual(paired.main(), 1)
        self.assertIn("No active relationship", self.output.getvalue())
        self.assertNotIn("synthetic-secret", self.output.getvalue())
        self.assertFalse(self.directory.exists())
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_conditional_preparation_is_get_only_capped_and_binds_risks(self):
        pair, legs = self.prepare_conditional()
        self.assertEqual(pair["policy"], paired.CONDITIONAL_POLICY)
        self.assertEqual(pair["market_context"]["assumptions"], paired.CONDITIONAL_ASSUMPTIONS)
        self.assertFalse(pair["market_context"]["relationship_verified"])
        self.assertEqual([leg["request"]["price"] for leg in legs], [.125, .845])
        self.assertTrue(all(leg["request"]["quantity"] == 1 and leg["request"]["side"] == "no" for leg in legs))
        self.assertEqual(self.session.get.call_count, 10)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()
        self.assertEqual(paired.load_pair(self.directory)[0], pair)
        with contextlib.redirect_stdout(self.output):
            paired.print_pair(pair, legs)
        self.assertIn("CONDITIONAL SETTLEMENT POLICY", self.output.getvalue())
        self.assertIn("relationship at preparation: MISSING", self.output.getvalue())
        self.assertNotIn("evidence was verified", self.output.getvalue())

    def test_conditional_acceptance_does_not_change_default_settlement_gate(self):
        self.session.get.side_effect = [self.response(payload) for payload in self.conditional_payloads()]
        with self.assertRaisesRegex(order_preview.PreviewBlocked, "settlement approval"):
            paired.prepare_pair(self.session, self.directory, "387", "388")
        self.assertFalse(self.directory.exists())
        self.session.post.assert_not_called()

    def test_conditional_policy_cannot_select_another_pair_or_tournament(self):
        for dem, rep, slug in (("1", "2", "midterm-elections"), ("387", "388", "other-cup"),
                               ("388", "387", "midterm-elections")):
            with self.subTest(dem=dem, rep=rep, slug=slug), self.assertRaises(single.TestError):
                paired.prepare_pair(self.session, self.directory, dem, rep, slug, conditional=True)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_conditional_preparation_blocks_cost_above_accepted_cap(self):
        payloads = self.conditional_payloads()
        payloads[8] = exchange_book(387, 1076, bid=.87, ask=.875)
        self.session.get.side_effect = [self.response(payload) for payload in payloads]
        with self.assertRaisesRegex(single.TestError, "0.970"):
            paired.prepare_pair(self.session, self.directory, "387", "388", conditional=True)
        self.assertFalse(self.directory.exists())
        self.session.post.assert_not_called()

    def test_conditional_preflight_refreshes_observations_without_writes(self):
        pair, _ = self.prepare_conditional()
        self.session.get.side_effect = [self.response(payload) for payload in self.conditional_payloads()]
        paired.preflight_pair(self.session, pair)
        self.assertEqual(self.session.get.call_count, 20)
        self.assertEqual(paired.load_pair(self.directory)[0]["state"], "PREPARED")
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_conditional_changed_rules_price_cash_and_overlap_block_before_post(self):
        pair, _ = self.prepare_conditional()
        for change in ("rules", "prices", "cash", "order"):
            payloads = self.conditional_payloads()
            if change == "rules":
                payloads[6]["root"]["contract_details"]["extraResolutionDetail"] = "changed"
            elif change == "prices":
                payloads[8] = exchange_book(387, 1076, bid=.87, ask=.875)
                payloads[9] = exchange_book(388, 1077, bid=.16, ask=.165)
            elif change == "cash":
                payloads[0]["myBalance"] = .96
            else:
                from test_account_reader import order
                payloads[2] = orders_page([order(exchange_id=1076)])
            self.session.get.side_effect = [self.response(payload) for payload in payloads]
            with self.subTest(change=change), self.assertRaises(ValueError):
                paired.submit_pair(self.session, self.directory, pair["approval"])
            self.assertEqual(paired.load_pair(self.directory)[0]["state"], "PREPARED")
        self.session.post.assert_not_called()

    def test_conditional_journal_cannot_drop_assumptions_or_become_verified(self):
        pair, _ = self.prepare_conditional()
        for change in ("policy", "assumptions", "cap", "exchange"):
            bad = copy.deepcopy(pair)
            if change == "policy":
                bad["policy"] = paired.VERIFIED_POLICY
            elif change == "assumptions":
                bad["market_context"]["assumptions"] = []
            elif change == "exchange":
                bad["market_context"]["exchange_ids"] = ["1076", "99"]
            else:
                bad["requests"][0]["request"]["price"] = .13
                bad["market_context"]["leg_prices"][0] = .13
            bad["approval"] = paired.approval_hash(bad)
            with self.subTest(change=change), self.assertRaises(ValueError):
                paired.validate_pair(bad)
        changed = copy.deepcopy(pair)
        changed["market_context"]["assumptions"][0] += " altered"
        self.assertNotEqual(paired.approval_hash(changed), pair["approval"])

    def test_conditional_policy_uses_same_sequencing_and_preserves_unmatched_share(self):
        pair, _ = self.prepare_conditional()
        self.install_venue(fills=(1, .5))
        def inputs(session, market_id, side, slug):
            exchange, price = ("1076", .125) if market_id == "387" else ("1077", .845)
            return self.account, exchange, price, single.account_risk(self.account, [exchange])
        with patch.object(paired, "preflight_pair"), patch.object(single, "read_test_inputs", side_effect=inputs):
            result, legs = paired.submit_pair(self.session, self.directory, pair["approval"])
        self.assertEqual(result["state"], "UNMATCHED")
        self.assertEqual([paired.filled_quantity(leg) for leg in legs], [1, .5])
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(self.session.delete.call_count, 1)
        with self.assertRaises(single.TestError):
            paired.submit_pair(self.session, self.directory, pair["approval"])
        self.assertEqual(self.session.post.call_count, 2)

    def test_conditional_cli_flag_cannot_rewrite_policy_at_submission(self):
        with patch("sys.argv", ["paired_account_test.py", "submit", "--conditional"]), \
                patch.object(paired, "load_dotenv") as dotenv, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                paired.main()
            dotenv.assert_not_called()

    def unknown_pair(self):
        pair, legs = self.prepare_conditional()
        single.commit_state(legs[0], paired.leg_path(self.directory, 0), "UNKNOWN")
        paired.set_state(pair, self.directory, "UNKNOWN")
        self.session.reset_mock()
        return pair

    def audit_row(self, pair, event_id="event-1", exchange_id="1076", offset=1):
        at = single.parse_api_timestamp(pair["created_at"]) + timedelta(seconds=offset)
        return {"event_id": event_id, "event_type": "trade", "createdAt": at.isoformat(),
                "tournamentId": TOURNAMENT_ID, "exchangeId": exchange_id, "quantity": -1, "price": .125,
                "description": "never-print-fake-secret"}

    def audit_page(self, rows, more=False, cursor=None, complete=True):
        return {"data": rows, "coverage": {"complete": complete},
                "pagination": {"hasMore": more, "nextCursor": cursor}}

    def diagnostic_reads(self, pages):
        payloads = [tournament(), holdings([]), orders_page([])] + pages
        self.session.get.side_effect = [self.response(payload) for payload in payloads]

    def test_diagnosis_empty_trade_audit_keeps_unknown_journals_unchanged(self):
        pair = self.unknown_pair()
        paths = [self.directory / "pair.json"] + [paired.leg_path(self.directory, index) for index in range(2)]
        before = [path.read_bytes() for path in paths]
        self.diagnostic_reads([self.audit_page([])])
        with contextlib.redirect_stdout(self.output):
            result, legs = paired.diagnose_pair(self.session, self.directory)
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertIn("trade events since preparation: 0", self.output.getvalue())
        self.assertIn("Empty results cannot prove non-execution", self.output.getvalue())
        self.assertIn("No placement error diagnostic was recorded", self.output.getvalue())
        self.assertEqual(self.session.get.call_count, 4)
        call = self.session.get.call_args_list[-1]
        self.assertEqual(call.args[0], scanner.API_BASE_URL + '/tournaments/midterm-elections/portfolio/transactions')
        self.assertEqual(call.kwargs["params"], {"type": "trade", "limit": 200})
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_diagnosis_follows_audit_pages_and_prints_selected_trade_amounts_only(self):
        pair = self.unknown_pair()
        pages = [self.audit_page([self.audit_row(pair, exchange_id="90", offset=2)], True, "page-2"),
                 self.audit_page([self.audit_row(pair, event_id="event-2")])]
        self.diagnostic_reads(pages)
        with contextlib.redirect_stdout(self.output):
            paired.diagnose_pair(self.session, self.directory)
        self.assertEqual(self.session.get.call_count, 5)
        self.assertIn('"exchange_id": "1076"', self.output.getvalue())
        self.assertNotIn("never-print-fake-secret", self.output.getvalue())
        self.assertEqual(self.session.get.call_args_list[-1].kwargs["params"]["cursor"], "page-2")
        self.assertEqual(paired.load_pair(self.directory)[0]["state"], "UNKNOWN")
        self.session.post.assert_not_called()

    def test_diagnosis_stops_at_verified_old_boundary_without_old_trade_output(self):
        pair = self.unknown_pair()
        self.diagnostic_reads([self.audit_page([self.audit_row(pair, offset=-1)], True, "old-page")])
        with contextlib.redirect_stdout(self.output):
            paired.diagnose_pair(self.session, self.directory)
        self.assertEqual(self.session.get.call_count, 4)
        self.assertIn("trade events since preparation: 0", self.output.getvalue())
        self.session.post.assert_not_called()

    def test_incomplete_or_mismatched_audit_cannot_report_zero_as_complete(self):
        pair = self.unknown_pair()
        for change in ("coverage", "scope", "duplicate", "ordering"):
            rows = [self.audit_row(pair)]
            page = self.audit_page(rows)
            if change == "coverage":
                page["coverage"]["complete"] = False
            elif change == "scope":
                rows[0]["tournamentId"] = "550e8400-e29b-41d4-a716-446655440001"
            elif change == "duplicate":
                rows.append(copy.deepcopy(rows[0]))
            else:
                rows.append(self.audit_row(pair, event_id="event-2", offset=2))
            self.diagnostic_reads([page])
            with self.subTest(change=change), self.assertRaises(single.TestError):
                paired.diagnose_pair(self.session, self.directory)
        self.assertEqual(paired.load_pair(self.directory)[0]["state"], "UNKNOWN")
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()

    def test_pair_directory_creation_must_be_durable_before_leg_journals_are_saved(self):
        with patch.object(paired, "read_account", return_value=self.account), \
                patch.object(paired, "preview_pair", return_value=copy.deepcopy(self.preview)), \
                patch.object(single, "sync_directory", side_effect=OSError('synthetic directory failure')) as flush, \
                self.assertRaises(single.TestError):
            paired.prepare_pair(self.session, self.directory, '1', '2')
        flush.assert_called_once_with(self.directory.parent)
        self.assertTrue(self.directory.exists())
        self.assertFalse(paired.leg_path(self.directory, 0).exists())
        with self.assertRaises(single.TestError):
            self.prepare()
        self.session.post.assert_not_called()

    def test_failed_controller_directory_flush_prevents_any_post_or_retry(self):
        pair, _ = self.prepare()
        with patch.object(paired, "preflight_pair"), \
                patch.object(single, "sync_directory", side_effect=OSError('synthetic directory failure')), \
                self.assertRaises(single.TestError):
            paired.submit_pair(self.session, self.directory, pair['approval'])
        restored, legs = paired.load_pair(self.directory)
        self.assertEqual(restored['state'], 'EXECUTING')
        self.assertTrue(all(leg['state'] == 'PREPARED' for leg in legs))
        with self.assertRaises(single.TestError):
            paired.submit_pair(self.session, self.directory, pair['approval'])
        self.session.post.assert_not_called()

    def test_failed_receipt_directory_flush_stops_second_leg_and_recovers_known_fill(self):
        pair, _ = self.prepare()
        self.install_venue()
        original_flush = single.sync_directory
        failed = False
        def flush(directory):
            nonlocal failed
            first = single.load_intent(paired.leg_path(self.directory, 0))
            if not failed and first['state'] == 'ACCEPTED':
                failed = True
                raise OSError('synthetic receipt directory failure')
            return original_flush(directory)
        with patch.object(single, "sync_directory", side_effect=flush), self.assertRaises(single.TestError):
            self.execute(pair)
        self.assertTrue(failed)
        restored, legs = paired.load_pair(self.directory)
        self.assertEqual(restored['state'], 'UNKNOWN')
        self.assertEqual(legs[1]['state'], 'PREPARED')
        self.assertEqual(self.session.post.call_count, 1)
        checked, legs = paired.check_pair(self.session, self.directory)
        self.assertEqual(checked['state'], 'UNMATCHED')
        self.assertEqual(paired.filled_quantity(legs[0]), 1)
        with self.assertRaises(single.TestError):
            paired.submit_pair(self.session, self.directory, pair['approval'])
        self.assertEqual(self.session.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
