"""Autonomous execution uses fake HTTP and temporary allocation/approval files."""
import copy
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import Mock, patch

import requests

import account_test as single
import autonomous_pilot as auto
import config
import live_pilot as pilot
import live_settlement as live
import pilot_account
import price_reader as scanner
import test_live_pilot as live_fixtures
import test_pilot_account as account_fixtures
import test_live_pilot_state as state_fixtures
from test_account_reader import holdings, position
from test_api_audit import election_tree, exchange_book, page, pair_relationship, party_market
from test_order_preview import parsed_book


class InjectedCrash(BaseException):
    pass


class AutonomousPilotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = account_fixtures.PilotAccountTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.base = self.fixture.fixture
        self.path = pilot.ALLOCATION_PATH
        self.session = self.fixture.session
        # Production permissions must never leak into the fake exchange's
        # candidate scan. Each test starts with its own empty manual allowlist.
        manual_path = self.base.root / "manual_autonomous_approvals.json"
        manual_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT",
            "authorization_tier": live.MANUAL_AUTONOMOUS, "pairs": []}))
        manual_permissions = patch.object(live, "MANUAL_AUTONOMOUS_PATH", manual_path)
        manual_permissions.start()
        self.addCleanup(manual_permissions.stop)
        self.approval = live_fixtures.live_approval()
        self.approval["execution_mode"] = live.AUTONOMOUS_MODE
        self.write_authorizations([self.approval])
        self.books = {"11": exchange_book(1, 11, bid=.6, ask=.65), "12": exchange_book(2, 12, bid=.6, ask=.65)}
        self.markets = {"1": party_market("Democratic", 1, 11), "2": party_market("Republican", 2, 12)}
        self.nodes = {"1": election_tree("Democratic", 1), "2": election_tree("Republican", 2)}
        self.relationships = [pair_relationship()]
        self.session.get.side_effect = self.get
        self.session.post.side_effect = self.post
        self.posts, self.wal_states = [], []
        self.scripts = [{}, {}]
        self.post_enabled = patch.object(config, "LIVE_PILOT_SUBMISSION_ENABLED", True)
        self.auto_enabled = patch.object(config, "AUTONOMOUS_LIVE_PILOT_ENABLED", True)
        self.post_enabled.start()
        self.auto_enabled.start()
        self.addCleanup(self.post_enabled.stop)
        self.addCleanup(self.auto_enabled.stop)
        # The fake server projects balances immediately; skip real waiting.
        cache_wait = patch.object(auto, "wait_for_account_cache")
        cache_wait.start()
        self.addCleanup(cache_wait.stop)

    def write_authorizations(self, entries):
        self.base.live_approval_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT", "pairs": entries}))

    def get(self, url, **kwargs):
        path = urlparse(url).path.removeprefix("/api/v1")
        if path.startswith("/markets/"):
            mid = path.split("/")[2]
            payload = self.nodes[mid] if path.endswith("/nodes") else self.markets[mid]
        elif path == "/relationships":
            payload = page(self.relationships)
        elif path.startswith("/exchanges/"):
            payload = self.books[path.split("/")[2]]
        else:
            return self.fixture.get(url, **kwargs)
        return self.base.response(payload)

    def post(self, url, **kwargs):
        self.assertEqual(url, scanner.API_BASE_URL + "/orders")
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["timeout"], scanner.REQUEST_TIMEOUT)
        # Read real durable bytes INSIDE the fake server call: before every
        # possible POST, the key, body, marker, allocation reserve and stage
        # must already have reached the same atomic file.
        cp = pilot._read_checkpoint_locked(self.path.resolve())
        attempt = auto.active_attempt(cp)
        index = len(self.posts)
        self.assertIn(index, (0, 1))
        self.assertEqual(cp["state"], pilot.LEG1_SUBMITTING if index == 0 else pilot.LEG2_SUBMITTING)
        self.assertEqual(attempt["legs"][index]["intent"]["request"], kwargs["json"])
        self.assertTrue(attempt["legs"][index]["post_attempted"])
        self.assertEqual(attempt["legs"][index]["intent"]["state"], "SUBMITTING")
        self.assertEqual(kwargs["json"]["quantity"], 1)
        self.assertLessEqual(Decimal(str(cp["calculated_remaining_allocation"])), Decimal(5000))
        self.wal_states.append(copy.deepcopy(cp))
        self.posts.append(copy.deepcopy(kwargs["json"]))
        script = self.scripts[index]
        if script.get("before_fill_error"):
            raise script["before_fill_error"]
        status = script.get("status", 200)
        if status != 200:
            response = self.base.response({"error": "Synthetic rejection"})
            response.status_code = status
            return response
        body = kwargs["json"]
        quantity = Decimal(str(script.get("quantity", 1)))
        price = Decimal(str(script.get("fill_price", body["price"])))
        fee = Decimal(str(script.get("fee", 0)))
        eid = body["exchangeId"]
        mid, oid, fid = str(index + 1), 301 + index, 501 + index
        stamp = datetime.now(timezone.utc).isoformat()
        notional = quantity * price
        cash = Decimal(str(self.fixture.current["tournament"]["myBalance"]))
        self.fixture.current["tournament"]["myBalance"] = float(cash - notional - fee)
        holding = position(eid, mid, float(-quantity))
        holding.update(costBasis=float(notional), marketValue=float(notional), currentPrice=float(price), unrealizedPnl=0)
        self.fixture.current.update(holdings([*self.fixture.current["positions"], holding]))
        fill = {"id": fid, "orderId": oid, "exchangeId": eid, "marketId": mid, "side": "no",
                "quantity": float(-quantity), "price": float(price), "filledAt": stamp}
        self.fixture.fills.insert(0, fill)
        transaction = {"event_id": f"engine-{fid}", "event_type": "trade", "createdAt": stamp,
                       "tournamentId": body["tournamentId"], "exchangeId": eid, "marketId": mid,
                       "quantity": float(-quantity), "price": float(price), "amount": None,
                       "transactionType": None, "orderType": "BUY"}
        self.fixture.transactions.insert(0, transaction)
        order = {"id": oid, "exchangeId": eid, "side": "no", "action": "buy", "tournamentId": body["tournamentId"],
                 "quantity": 1, "quantityFilled": float(quantity), "priceLimit": body["price"],
                 "expirationDate": body["expirationDate"], "createdAt": stamp, "open": script.get("open", False)}
        if order["open"]:
            order["quantity"] = float(1 - quantity)
            self.fixture.current["orders"] = [order]
        fills = account_fixtures.history([{key: value for key, value in fill.items()
                                           if key not in {"orderId", "marketId", "exchangeId"}}])
        fills.update(orderId=oid, exchangeId=eid, tournamentId=body["tournamentId"],
                     totalQuantityFilled=float(-quantity), avgFillPrice=float(price))
        self.fixture.receipts[oid] = order, fills
        if "next_quote" in script:
            limit = Decimal(str(script["next_quote"]))
            self.books["12"] = exchange_book(2, 12, bid=float(1 - limit), ask=float(1 - limit + Decimal(".005")))
        if "relationships" in script:
            self.relationships = script["relationships"]
        if script.get("fee_entry"):
            self.fixture.transactions.insert(0, {"event_id": f"fee-{oid}", "event_type": "fee", "createdAt": stamp,
                "tournamentId": body["tournamentId"], "amount": float(-fee), "quantity": 0, "transactionType": "FEE"})
        if script.get("remove_first_holding"):
            self.fixture.current.update(holdings([holding]))
        if script.get("after_fill_error"):
            raise script["after_fill_error"]
        receipt = {"orderId": oid, "exchangeId": eid, "side": "no", "action": "buy", "quantity": 1,
                   "quantityTraded": float(quantity), "price": body["price"], "totalCost": float(notional),
                   "fillPrice": float(price), "open": order["open"],
                   "remainingQuantity": float(1 - quantity) if order["open"] else 0, "all": None}
        receipt.update(script.get("receipt_changes", {}))
        return self.base.response(receipt)

    def execute(self):
        result = auto.execute_candidate(self.session, ["1", "2"])
        for method in ("delete", "put", "patch"):
            getattr(self.session, method).assert_not_called()
        return result

    def checkpoint(self):
        return pilot.load_checkpoint()

    def assert_halted(self, expected_posts):
        result = self.execute()
        self.assertEqual(result["state"], pilot.HALTED, result)
        cp = self.checkpoint()
        self.assertTrue(cp["manual_review_required"])
        self.assertEqual(len(self.posts), expected_posts)
        self.session.post.reset_mock()
        with self.assertRaises(pilot.PilotBlocked):
            auto.execute_candidate(self.session, ["1", "2"])
        self.session.post.assert_not_called()
        return cp

    def test_full_pair_returns_ready_only_after_durable_complete_reconciliation(self):
        result = self.execute()
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(len(self.posts), 2)
        cp = self.checkpoint()
        self.assertEqual(cp["state"], pilot.READY)
        self.assertIsNone(cp["autonomous_execution"]["active_attempt"])
        fingerprint = result["execution_id"]
        attempt = cp["autonomous_execution"]["attempts"][fingerprint]
        self.assertEqual([entry["state"] for entry in attempt["stages"]],
            [pilot.READY, pilot.LEG1_SUBMITTING, pilot.LEG1_RECONCILING, pilot.LEG2_RECHECK,
             pilot.LEG2_SUBMITTING, pilot.FINAL_RECONCILING, pilot.READY])
        self.assertEqual(cp["live_exposures"][fingerprint]["confirmed_quantities"], ["1.0", "1.0"])
        self.assertEqual(Decimal(cp["allocated_cash_remaining"]), Decimal("4999.18"))
        self.assertEqual(Decimal(cp["last_reconciled_account_cash"]), Decimal("19999.2"))
        self.assertEqual(cp["autonomous_execution"]["policy"], auto.POLICY)
        self.assertFalse(result["formally_verified"])
        self.assertNotEqual(self.posts[0]["idempotencyKey"], self.posts[1]["idempotencyKey"])
        self.assertTrue(attempt["reads"])
        for leg in attempt["legs"]:
            self.assertEqual(leg["receipt"]["quantityTraded"], 1)
            self.assertTrue(leg["review"]["observations"]["isolated_execution_reconciled"])

    def test_single_autonomous_switch_blocks_before_api_keys_or_state_mutation(self):
        before = self.path.read_bytes()
        for submit, autonomous in ((False, False), (True, False)):
            with patch.object(config, "LIVE_PILOT_SUBMISSION_ENABLED", submit), \
                    patch.object(config, "AUTONOMOUS_LIVE_PILOT_ENABLED", autonomous), \
                    patch.object(auto, "uuid4") as key:
                with self.assertRaisesRegex(pilot.PilotBlocked, "disabled"):
                    self.execute()
                key.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_paper_mode_cannot_reach_autonomous_submission(self):
        with self.assertRaises(pilot.PilotBlocked):
            auto.execute_candidate(self.session, ["1", "2"], mode=config.PAPER)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_changed_hard_limits_block_before_api(self):
        for target, name, value in ((config, "LIVE_ALLOCATION", 5001), (config, "MAX_LIVE_CAPITAL_PER_TRADE", 51),
                                   (config, "MAX_LIVE_CAPITAL_PER_RACE", 101), (config, "MAX_TOTAL_LIVE_EXPOSURE", 501),
                                   (config, "MAX_LIVE_QUANTITY_PER_LEG", 2), (auto, "MIN_EDGE", Decimal(".020")),
                                   (auto, "MIN_COMPLETION_EDGE", Decimal(".020"))):
            with self.subTest(limit=name), patch.object(target, name, value), self.assertRaises(pilot.PilotBlocked):
                self.execute()
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_paper_only_or_unlisted_pair_cannot_execute(self):
        self.write_authorizations([])
        self.assert_halted(0)
        self.session.get.assert_not_called()

    def test_supervised_only_live_permission_cannot_execute_autonomously(self):
        self.write_authorizations([live_fixtures.live_approval()])
        self.assert_halted(0)
        self.session.get.assert_not_called()

    def test_manual_supervised_route_cannot_be_extended_to_autonomy(self):
        approval = copy.deepcopy(self.approval)
        approval["verification_route"] = live.MANUAL
        approval["source_refs"] = live.evidence_sources(approval)
        with self.assertRaisesRegex(live.LiveSettlementBlocked, "machine-readable"):
            live.validate_authorization(approval)
        self.session.post.assert_not_called()

    def test_missing_machine_relationship_blocks_before_any_post(self):
        self.relationships = []
        self.assert_halted(0)

    def test_settlement_changes_after_leg_one_preserve_one_sided_exposure(self):
        self.scripts[0]["relationships"] = []
        cp = self.assert_halted(1)
        exposure = next(iter(cp["live_exposures"].values()))
        self.assertEqual(exposure["confirmed_quantities"], ["1.0", "0"])
        self.assertEqual(exposure["possible_additional_quantities"], ["0", "0"])
        self.assertGreaterEqual(Decimal(exposure["capital_charge"]), Decimal(".4"))

    def test_stale_initial_book_prevents_submission(self):
        self.books["11"]["asOf"]["at"] = "2026-10-07T11:59:00Z"
        self.assert_halted(0)

    def test_autonomous_half_percent_boundary_passes_full_mocked_execution(self):
        self.books["12"] = exchange_book(2, 12, bid=.405, ask=.41)  # .4 + .595, edge .005.
        result = self.execute()
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(len(self.posts), 2)
        attempt = self.checkpoint()["autonomous_execution"]["attempts"][result["execution_id"]]
        self.assertEqual(Decimal(attempt["initial_edge"]), Decimal(".005"))
        self.assertEqual(Decimal(attempt["completion"]["minimum_edge"]), Decimal("0"))

    def test_decimal_edge_just_below_half_percent_has_no_epsilon_allowance(self):
        # A sub-tick result cannot normally come from the current price grid.
        # Supply it at the shared pricing boundary to prove the autonomous
        # policy never rounds a smaller Decimal edge up to the accepted floor.
        edge = Decimal("0.004999999999999999999")
        with patch.object(auto, "executable_limits", return_value=(
                [.4, .595], 1, Decimal("1.000") - edge, edge)):
            with self.assertRaisesRegex(auto.PreviewBlocked, "0.5%"):
                auto.observed_autonomous_limits([parsed_book(), parsed_book()], 100)
        self.session.post.assert_not_called()

    def test_sub_threshold_zero_and_negative_quotes_never_create_an_intent(self):
        baseline = self.path.read_bytes()
        # First NO limit is .400. The near-boundary quote rounds the other
        # buy limit up to .600, rather than accepting a raw edge below .005.
        for bid in (.4049999999999, .400, .395):
            with self.subTest(second_yes_bid=bid):
                self.books["12"] = exchange_book(2, 12, bid=bid, ask=bid + .005)
                with patch.object(auto, "uuid4") as key, self.assertRaises(auto.PreviewBlocked):
                    auto.fresh_candidate(self.session, ["1", "2"], self.base.checkpoint)
                key.assert_not_called()
                self.assertEqual(self.path.read_bytes(), baseline)
        self.session.post.assert_not_called()

    def test_tick_rounding_happens_before_autonomous_edge_calculation(self):
        # Raw NO prices .4001 + .5949 appear to offer exactly .005 edge.
        # Executable buy limits are .405 + .595: no ordinary edge remains.
        books = [parsed_book(bid=.5999), parsed_book(bid=.4051)]
        raw_edge = Decimal("1.000") - Decimal(".4001") - Decimal(".5949")
        self.assertEqual(raw_edge, Decimal(".005"))
        with self.assertRaisesRegex(auto.PreviewBlocked, "positive ordinary edge"):
            auto.observed_autonomous_limits(books, 100)
        prices, quantity, cost, edge = auto.observed_autonomous_limits(
            [parsed_book(bid=.6), parsed_book(bid=.409)], 100)
        self.assertEqual(prices, [.4, .595])  # Raw second price .591 rounds UP.
        self.assertEqual(quantity, 1)
        self.assertEqual((cost, edge), (Decimal(".995"), Decimal(".005")))
        self.assertIsInstance(edge, Decimal)
        self.session.post.assert_not_called()

    def test_every_entry_and_completion_checkpoint_accepts_half_percent(self):
        self.books["12"] = exchange_book(2, 12, bid=.405, ask=.41)
        with patch.object(auto, "observed_autonomous_limits", wraps=auto.observed_autonomous_limits) as entry, \
                patch.object(auto, "completion_limits", wraps=auto.completion_limits) as completion:
            result = self.execute()
        self.assertEqual(result["state"], pilot.READY, result)
        # Detection, repeated full preflight, after intent persistence, and
        # immediately before leg one's POST all use the same fixed checker.
        self.assertEqual(entry.call_count, 4)
        # Fresh leg-two quote, after leg-two persistence, and just before POST.
        self.assertEqual(completion.call_count, 3)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.posts[1]["price"], .595)

    def test_every_entry_checkpoint_can_stop_submission(self):
        baseline = self.path.read_bytes()
        original = auto.observed_autonomous_limits
        for blocked_call in (1, 2, 3, 4):
            with self.subTest(checkpoint=blocked_call):
                # Restore only the disposable fixture between independent runs.
                self.path.write_bytes(baseline)
                self.session.reset_mock()
                calls = []
                def recheck(*args):
                    calls.append(args)
                    if len(calls) == blocked_call:
                        raise auto.PreviewBlocked("Tick-rounded limits fall below the 0.5% autonomous edge requirement")
                    return original(*args)
                with patch.object(auto, "observed_autonomous_limits", side_effect=recheck):
                    result = self.execute()
                self.assertEqual(result["state"], pilot.HALTED, result)
                self.assertEqual(len(calls), blocked_call)
                self.session.post.assert_not_called()

    def test_every_completion_checkpoint_blocks_a_negative_edge(self):
        baseline = self.path.read_bytes()
        original = auto.completion_limits
        for blocked_call in (1, 2, 3):
            with self.subTest(checkpoint=blocked_call):
                self.path.write_bytes(baseline)
                self.fixture.current = copy.deepcopy(self.base.account)
                self.fixture.fills, self.fixture.transactions, self.fixture.receipts = [], [], {}
                self.posts, self.wal_states = [], []
                self.session.reset_mock()
                self.books["12"] = exchange_book(2, 12, bid=.405, ask=.41)
                calls = []
                def recheck(attempt, book, account_started):
                    calls.append(book)
                    if len(calls) == blocked_call:
                        book = copy.deepcopy(book)
                        book["bid"] = .395  # NO limit .605 + filled .400 = negative edge.
                    return original(attempt, book, account_started)
                with patch.object(auto, "completion_limits", side_effect=recheck):
                    result = self.execute()
                self.assertEqual(result["state"], pilot.HALTED, result)
                self.assertEqual(len(calls), blocked_call)
                self.assertEqual(len(self.posts), 1)
                self.assertTrue(self.checkpoint()["manual_review_required"])

    def test_account_read_failure_prevents_submission(self):
        self.fixture.failed_path = "/tournaments/midterm-elections/portfolio/transactions"
        self.assert_halted(0)

    def test_timeout_after_server_fill_never_retries_or_infers_order_id(self):
        self.scripts[0]["after_fill_error"] = requests.Timeout("Private HTTP details must not enter halt reason")
        cp = self.assert_halted(1)
        leg = auto.active_attempt(cp)["legs"][0]
        self.assertTrue(leg["post_attempted"])
        self.assertIsNone(leg["intent"]["order_id"])
        exposure = next(iter(cp["live_exposures"].values()))
        self.assertEqual(exposure["possible_additional_quantities"], ["1", "0"])
        self.assertGreaterEqual(Decimal(cp["total_live_exposure"]), Decimal(".44"))
        self.assertNotIn("Private HTTP details", cp["review_reason"])

    def test_connection_failure_before_acknowledgement_remains_possible_exposure(self):
        self.scripts[0]["before_fill_error"] = requests.ConnectionError("Ambiguous connection")
        cp = self.assert_halted(1)
        self.assertEqual(next(iter(cp["live_exposures"].values()))["possible_additional_quantities"], ["1", "0"])

    def test_http_rejection_preserves_raw_body_and_does_not_retry(self):
        self.scripts[0]["status"] = 400
        cp = self.assert_halted(1)
        self.assertEqual(auto.active_attempt(cp)["legs"][0]["placement_response"]["http_status"], 400)

    def test_http_idempotency_conflict_is_not_retried_with_new_key(self):
        self.scripts[0]["status"] = 409
        self.assert_halted(1)

    def test_post_rate_limit_halts_instead_of_retrying(self):
        self.scripts[0]["status"] = 429
        self.assert_halted(1)

    def test_missing_receipt_id_halts_without_guessing_from_fills(self):
        self.scripts[0]["receipt_changes"] = {"orderId": None}
        cp = self.assert_halted(1)
        self.assertIsNone(auto.active_attempt(cp)["legs"][0]["intent"]["order_id"])

    def test_partial_open_leg_one_records_fill_and_reserves_remainder(self):
        self.scripts[0].update(quantity=.5, open=True)
        cp = self.assert_halted(1)
        exposure = next(iter(cp["live_exposures"].values()))
        self.assertEqual(list(map(Decimal, exposure["confirmed_quantities"])), [Decimal(".5"), Decimal(0)])
        self.assertEqual(list(map(Decimal, exposure["possible_additional_quantities"])), [Decimal(".5"), Decimal(0)])
        self.assertEqual(list(map(Decimal, exposure["confirmed_costs"])), [Decimal(".2"), Decimal(0)])

    def test_partial_closed_leg_one_stops_with_exact_known_half_contract(self):
        self.scripts[0].update(quantity=.5, open=False)
        cp = self.assert_halted(1)
        exposure = next(iter(cp["live_exposures"].values()))
        self.assertEqual(list(map(Decimal, exposure["confirmed_quantities"])), [Decimal(".5"), Decimal(0)])
        self.assertEqual(list(map(Decimal, exposure["possible_additional_quantities"])), [Decimal(0), Decimal(0)])

    def test_partial_second_leg_halts_with_both_known_and_possible_inventory(self):
        self.scripts[1].update(quantity=.5, open=True)
        cp = self.assert_halted(2)
        exposure = next(iter(cp["live_exposures"].values()))
        self.assertEqual(list(map(Decimal, exposure["confirmed_quantities"])), [Decimal(1), Decimal(".5")])
        self.assertEqual(list(map(Decimal, exposure["possible_additional_quantities"])), [Decimal(0), Decimal(".5")])

    def test_first_receipt_cost_disagreeing_with_fills_halts(self):
        self.scripts[0]["receipt_changes"] = {"totalCost": .3}
        self.assert_halted(1)

    def test_order_state_disagreeing_with_fills_halts(self):
        original = self.get
        def mismatched(url, **kwargs):
            if url.endswith("/orders/301"):
                self.fixture.receipts[301][0]["quantityFilled"] = .5
            return original(url, **kwargs)
        self.session.get.side_effect = mismatched
        self.assert_halted(1)

    def test_fill_lookup_unavailable_stops_before_leg_two(self):
        self.fixture.failed_path = "/orders/301/fills"
        self.assert_halted(1)

    def test_second_timeout_retains_leg_one_and_full_possible_leg_two(self):
        self.scripts[1]["after_fill_error"] = requests.Timeout("Lost second receipt")
        cp = self.assert_halted(2)
        exposure = next(iter(cp["live_exposures"].values()))
        self.assertEqual(list(map(Decimal, exposure["confirmed_quantities"])), [Decimal(1), Decimal(0)])
        self.assertEqual(list(map(Decimal, exposure["possible_additional_quantities"])), [Decimal(0), Decimal(1)])

    def test_one_tick_completion_passes_using_actual_improved_first_fill(self):
        self.scripts[0].update(fill_price=.395, next_quote=.405)
        result = self.execute()
        self.assertEqual(result["state"], pilot.READY)
        self.assertEqual(self.posts[1]["price"], .405)
        completion = self.checkpoint()["autonomous_execution"]["attempts"][result["execution_id"]]["completion"]
        self.assertEqual(Decimal(completion["actual_leg1_notional"]), Decimal(".395"))
        self.assertEqual(Decimal(completion["edge"]), Decimal(".200"))

    def test_one_tick_adverse_completion_preserves_relative_edge_limit(self):
        self.scripts[0]["next_quote"] = .405
        result = self.execute()
        self.assertEqual(result["state"], pilot.READY)
        self.assertEqual(self.posts[1]["price"], .405)
        completion = self.checkpoint()["autonomous_execution"]["attempts"][result["execution_id"]]["completion"]
        self.assertEqual(Decimal(completion["minimum_edge"]), Decimal(".195"))

    def test_one_tick_completion_can_pass_at_exact_half_percent_floor(self):
        self.books["12"] = exchange_book(2, 12, bid=.410, ask=.415)  # Initial edge .010.
        self.scripts[0]["next_quote"] = .595  # One tick worse; completion edge .005.
        result = self.execute()
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(self.posts[1]["price"], .595)
        completion = self.checkpoint()["autonomous_execution"]["attempts"][result["execution_id"]]["completion"]
        self.assertEqual(Decimal(completion["edge"]), Decimal(".005"))
        self.assertEqual(Decimal(completion["minimum_edge"]), Decimal(".005"))

    def test_larger_deterioration_halts_instead_of_chasing(self):
        self.scripts[0]["next_quote"] = .410
        cp = self.assert_halted(1)
        self.assertIn("completion rule", cp["review_reason"])
        self.assertIsNone(auto.active_attempt(cp)["legs"][1]["intent"])

    def test_tick_rounding_can_turn_raw_acceptable_completion_into_failure(self):
        self.scripts[0]["next_quote"] = .4051  # Buy limit rounds up to .410.
        self.assert_halted(1)

    def test_break_even_completion_passes_for_one_tick_movement(self):
        self.books["12"] = exchange_book(2, 12, bid=.405, ask=.41)
        self.scripts[0]["next_quote"] = .600
        self.assertEqual(self.execute()["state"], pilot.READY)

    def test_better_first_fill_can_keep_completion_at_exact_two_percent(self):
        self.books["12"] = exchange_book(2, 12, bid=.42, ask=.425)
        self.scripts[0].update(fill_price=.395, next_quote=.585)
        self.assertEqual(self.execute()["state"], pilot.READY)

    def test_unexplained_cash_delta_halts_after_first_leg(self):
        self.scripts[0]["fee"] = .05
        cp = self.assert_halted(1)
        self.assertIn("ACCOUNTING_MODEL_MISMATCH", cp["review_reason"])
        self.assertIsNotNone(auto.active_attempt(cp)["after"][0])

    def test_fee_entry_halts_even_if_displayed_cash_matches_notional(self):
        self.scripts[0].update(fee_entry=True, fee=0)
        cp = self.assert_halted(1)
        review = auto.active_attempt(cp)["legs"][0]["review"]
        self.assertTrue(review["observations"]["fee_like_transactions"])

    def test_exact_rounding_tolerance_passes_but_capital_is_charged_conservatively(self):
        self.scripts[0]["fee"] = .02  # Represents the maximum observed rounding discrepancy.
        self.assertEqual(self.execute()["state"], pilot.READY)
        self.assertEqual(Decimal(self.checkpoint()["allocated_cash_remaining"]), Decimal("4999.16"))

    def test_discrepancy_just_above_rounding_tolerance_halts(self):
        self.scripts[0]["fee"] = .020001
        self.assert_halted(1)

    def test_small_individual_deltas_cannot_accumulate_beyond_cumulative_tolerance(self):
        self.scripts[0]["fee"] = .015
        self.scripts[1]["fee"] = .015
        cp = self.assert_halted(2)
        self.assertIn("cumulative", cp["review_reason"])
        self.assertTrue(auto.active_attempt(cp)["legs"][1]["review"]["observations"]["isolated_execution_reconciled"])

    def test_final_missing_first_holding_cannot_return_ready(self):
        self.scripts[1]["remove_first_holding"] = True
        self.assert_halted(2)

    def test_duplicate_generated_key_for_leg_two_halts_without_second_post(self):
        with patch.object(auto, "uuid4", side_effect=[SimpleNamespace(hex="a" * 32),
                SimpleNamespace(hex="b" * 32), SimpleNamespace(hex="b" * 32)]):
            cp = self.assert_halted(1)
        self.assertIn("Duplicate idempotency", cp["review_reason"])

    def test_completed_pair_cannot_be_opened_again(self):
        self.assertEqual(self.execute()["state"], pilot.READY)
        before = len(self.posts)
        result = self.execute()
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertEqual(len(self.posts), before)

    def test_extra_account_cash_never_enlarges_fixed_allocation(self):
        for cash in (5000, 20000, 1000000):
            snapshot = copy.deepcopy(self.fixture.current)
            snapshot["tournament"]["myBalance"] = cash
            self.fixture.current = snapshot
            cp = pilot.allocation_checkpoint(snapshot)
            candidate = auto.fresh_candidate(self.session, ["1", "2"], cp)
            self.assertEqual(candidate["risk"]["live_allocation"], 5000)
            self.assertEqual(candidate["risk"]["allocation_remaining_after_reserves"], 5000)
            self.assertEqual(candidate["risk"]["untouchable_cash_reserve"], cash - 5000)
        self.session.post.assert_not_called()

    def test_existing_exposure_and_proposed_capital_enforce_all_risk_caps(self):
        account = copy.deepcopy(self.fixture.current)
        cp = self.base.checkpoint
        with self.assertRaisesRegex(pilot.PilotBlocked, "per-trade"):
            pilot.pilot_risk(account, ["11", "12"], 50.01, 1, cp)
        holding = position("99", "9", -1)
        holding.update(costBasis=99.5, marketValue=99.5, unrealizedPnl=0)
        account.update(holdings([holding]))
        with self.assertRaisesRegex(pilot.PilotBlocked, "per-race"):
            pilot.pilot_risk(account, ["11", "12"], .845, 1, cp)
        holding.update(costBasis=499.5, marketValue=499.5)
        account.update(holdings([holding]))
        with self.assertRaisesRegex(pilot.PilotBlocked, "total exposure"):
            pilot.pilot_risk(account, ["11", "12"], .845, 1, cp)
        with self.assertRaisesRegex(pilot.PilotBlocked, "exactly one"):
            pilot.pilot_risk(self.fixture.current, ["11", "12"], .845, 2, cp)

    def test_nearly_exhausted_allocation_does_not_replenish_from_account_cash(self):
        cp = self.base.consumed_checkpoint(4999.2)
        account = copy.deepcopy(self.fixture.current)
        account["tournament"]["myBalance"] = 15000.8
        with self.assertRaisesRegex(pilot.PilotBlocked, "remaining pilot allocation"):
            pilot.pilot_risk(account, ["11", "12"], .845, 1, cp)
        self.assertEqual(Decimal(cp["allocated_cash_remaining"]), Decimal(".8"))

    def test_halting_never_clears_on_restart_or_restored_good_quotes(self):
        self.scripts[0]["next_quote"] = .410
        cp = self.assert_halted(1)
        self.books["12"] = exchange_book(2, 12, bid=.6, ask=.65)
        self.assertEqual(self.checkpoint(), cp)
        proposed = copy.deepcopy(cp)
        proposed.update(state=pilot.READY, manual_review_required=False, review_reason="")
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(proposed)
        self.assertEqual(self.checkpoint(), cp)

    def test_completed_keys_cannot_be_corrupted_or_forgotten_on_restart(self):
        result = self.execute()
        cp = self.checkpoint()
        attempt = cp["autonomous_execution"]["attempts"][result["execution_id"]]
        leg = attempt["legs"][1]
        leg["intent"]["request"]["idempotencyKey"] = attempt["legs"][0]["intent"]["request"]["idempotencyKey"]
        leg["intent"]["approval"] = single.approval_hash(leg["intent"])
        self.path.write_text(json.dumps(cp))
        corrupted = self.path.read_bytes()
        with self.assertRaises(pilot.PilotBlocked):
            pilot.load_checkpoint()
        self.assertEqual(self.path.read_bytes(), corrupted)

    def test_execution_history_cannot_be_removed_to_reset_keys_or_debits(self):
        self.execute()
        cp = self.checkpoint()
        proposed = copy.deepcopy(cp)
        del proposed["autonomous_execution"]
        with self.assertRaises(pilot.PilotBlocked):
            pilot.save_checkpoint(proposed)
        self.assertEqual(self.checkpoint(), cp)

    def test_restart_of_every_inflight_stage_halts_without_any_resubmission(self):
        # Reset ONLY the disposable test baseline between independent crashes.
        baseline = self.path.read_bytes()
        cases = [(phase, None) for phase in (pilot.LEG1_SUBMITTING, pilot.LEG1_RECONCILING,
                 pilot.LEG2_RECHECK, pilot.LEG2_SUBMITTING, pilot.FINAL_RECONCILING)]
        cases += [(pilot.LEG1_SUBMITTING, 0), (pilot.LEG2_SUBMITTING, 1)]
        original_persist = auto.persist
        for phase, wal_index in cases:
            with self.subTest(stage=phase, post_marker=wal_index):
                self.path.write_bytes(baseline)
                self.fixture.current = copy.deepcopy(self.base.account)
                self.fixture.fills, self.fixture.transactions, self.fixture.receipts = [], [], {}
                self.posts, self.wal_states = [], []
                self.session.reset_mock()
                def crash_after_write(state):
                    original_persist(state)
                    cp = state["checkpoint"]
                    if cp["state"] == phase and (wal_index is None or
                            auto.active_attempt(cp)["legs"][wal_index]["post_attempted"]):
                        raise InjectedCrash()
                with patch.object(auto, "persist", side_effect=crash_after_write), \
                        patch.object(auto, "halt", side_effect=InjectedCrash), self.assertRaises(InjectedCrash):
                    self.execute()
                saved = json.loads(self.path.read_text())
                self.assertEqual(saved["state"], phase)
                before_posts = len(self.posts)
                restarted = subprocess.run([sys.executable, "-c", state_fixtures.RESTART_SCRIPT, str(self.path)],
                    cwd=Path(auto.__file__).parent, capture_output=True, text=True, timeout=10)
                self.assertEqual(restarted.returncode, 0, restarted.stderr)
                cp = json.loads(restarted.stdout)
                self.assertEqual(cp["state"], pilot.HALTED)
                self.assertEqual(auto.active_attempt(cp)["halted_from"], phase)
                self.assertEqual(cp["allocated_cash_remaining"], saved["allocated_cash_remaining"])
                self.assertEqual(cp["confirmed_cumulative_debits"], saved["confirmed_cumulative_debits"])
                self.session.get.reset_mock()
                with self.assertRaises(pilot.PilotBlocked):
                    self.execute()
                self.session.get.assert_not_called()
                self.assertEqual(len(self.posts), before_posts)

    def test_process_crash_after_realistic_server_fill_preserves_unknown_receipt(self):
        self.scripts[0]["after_fill_error"] = InjectedCrash()
        with patch.object(auto, "halt", side_effect=InjectedCrash), self.assertRaises(InjectedCrash):
            self.execute()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["state"], pilot.LEG1_SUBMITTING)
        self.assertIsNone(auto.active_attempt(saved)["legs"][0]["intent"]["order_id"])
        cp = self.checkpoint()
        self.assertEqual(cp["state"], pilot.HALTED)
        self.assertEqual(next(iter(cp["live_exposures"].values()))["possible_additional_quantities"], ["1", "0"])
        self.assertEqual(len(self.posts), 1)

    def test_failed_write_ahead_fsync_never_reaches_post(self):
        original = pilot.os.fsync
        failed = [False]
        def fail_marker_once(fd):
            # Failure occurs during the marker's next write, after the initial
            # PREPARED intent/reserves are already durable.
            cp = json.loads(self.path.read_text())
            if not failed[0] and cp["state"] == pilot.LEG1_SUBMITTING and not auto.active_attempt(cp)["legs"][0]["post_attempted"]:
                failed[0] = True
                raise OSError("Synthetic fsync failure")
            return original(fd)
        with patch.object(pilot.os, "fsync", side_effect=fail_marker_once):
            cp = self.assert_halted(0)
        self.assertTrue(failed[0])
        self.assertFalse(auto.active_attempt(cp)["legs"][0]["post_attempted"])

    def test_failed_final_ready_write_leaves_both_fills_and_permanent_halt(self):
        original = pilot.os.replace
        failed = [False]
        def fail_ready_once(source, target):
            proposed = json.loads(Path(source).read_text())
            if proposed["state"] == pilot.READY and proposed.get("autonomous_execution", {}).get("attempts") and not failed[0]:
                failed[0] = True
                raise OSError("Synthetic final write failure")
            return original(source, target)
        with patch.object(pilot.os, "replace", side_effect=fail_ready_once):
            cp = self.assert_halted(2)
        self.assertTrue(failed[0])
        self.assertEqual(list(map(Decimal, next(iter(cp["live_exposures"].values()))["confirmed_quantities"])), [Decimal(1), Decimal(1)])
        self.assertEqual(Decimal(cp["allocated_cash_remaining"]), Decimal("4999.18"))

    def test_quote_expiring_during_final_write_ahead_check_blocks_second_post(self):
        original = auto.persist
        with patch.object(auto.time, "monotonic", return_value=100) as clock:
            def delayed(state):
                original(state)
                if state["checkpoint"]["state"] == pilot.LEG2_SUBMITTING and auto.active_attempt(state["checkpoint"])["legs"][1]["post_attempted"]:
                    clock.return_value = 106
            with patch.object(auto, "persist", side_effect=delayed):
                self.assert_halted(1)

    def test_quote_expiring_during_final_write_ahead_check_blocks_first_post(self):
        original = auto.persist
        with patch.object(auto.time, "monotonic", return_value=100) as clock:
            def delayed(state):
                original(state)
                if state["checkpoint"]["state"] == pilot.LEG1_SUBMITTING and auto.active_attempt(state["checkpoint"])["legs"][0]["post_attempted"]:
                    clock.return_value = 106
            with patch.object(auto, "persist", side_effect=delayed):
                self.assert_halted(0)

    def test_revocation_during_last_write_blocks_second_post(self):
        original = auto.persist
        revoked = [False]
        def revoke(state):
            original(state)
            if state["checkpoint"]["state"] == pilot.LEG2_SUBMITTING and auto.active_attempt(state["checkpoint"])["legs"][1]["post_attempted"]:
                self.write_authorizations([])
                revoked[0] = True
        with patch.object(auto, "persist", side_effect=revoke):
            self.assert_halted(1)
        self.assertTrue(revoked[0])

    def test_completed_keys_remain_reserved_across_reload(self):
        self.execute()
        cp = self.checkpoint()
        key = self.posts[0]["idempotencyKey"].removeprefix("account-test-")
        with patch.object(auto, "uuid4", return_value=SimpleNamespace(hex=key)), \
                self.assertRaisesRegex(pilot.PilotBlocked, "Duplicate idempotency"):
            auto.new_intent(cp, self.approval, 0, .4)

    def test_historical_accounting_assumption_does_not_open_the_verified_account_gate(self):
        snapshot = pilot_account.read_snapshot(self.session, checkpoint=self.base.checkpoint)
        assumed = pilot_account.assess_autonomous_snapshot(snapshot, self.base.checkpoint, ["11", "12"], .845)
        ordinary = pilot_account.assess_snapshot(snapshot, self.base.checkpoint, ["11", "12"], .845)
        self.assertTrue(assumed["ready"])
        self.assertEqual(assumed["accounting"]["policy"], auto.POLICY)
        self.assertFalse(assumed["accounting"]["formally_verified"])
        self.assertFalse(ordinary["ready"])
        with self.assertRaises(pilot_account.AccountReadinessBlocked):
            pilot_account.require_ready(ordinary)

    def test_execution_modes_do_not_grant_permission_to_other_coordinators(self):
        with self.assertRaises(live.LiveSettlementBlocked):
            auto.probe.describe_pair(["1", "2"], self.approval["tournament_id"])
        live.revalidate_authorization(self.approval)  # Its own autonomous scope is valid.
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_missing_or_corrupt_allocation_never_initializes_from_paper_or_account(self):
        original = self.path.read_bytes()
        for content in (None, b"{corrupt"):
            if content is None:
                self.path.unlink()
            else:
                self.path.write_bytes(content)
            with self.assertRaises(pilot.PilotBlocked):
                self.execute()
            self.session.get.assert_not_called()
            self.session.post.assert_not_called()
            if content is None:
                self.assertFalse(self.path.exists())
            else:
                self.assertEqual(self.path.read_bytes(), content)
            self.path.write_bytes(original)

    def test_loop_remains_disabled_before_loading_any_credentials_or_account(self):
        before = self.path.read_bytes()
        with patch.object(config, "AUTONOMOUS_LIVE_PILOT_ENABLED", False), self.assertRaises(pilot.PilotBlocked):
            auto.run(self.session)
        self.assertEqual(self.path.read_bytes(), before)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_loop_ignores_supervised_permissions_and_uses_fifteen_second_cadence(self):
        self.write_authorizations([live_fixtures.live_approval()])
        before = self.path.read_bytes()
        with patch.object(auto.time, "sleep", side_effect=KeyboardInterrupt) as sleep, self.assertRaises(KeyboardInterrupt):
            auto.run(self.session)
        sleep.assert_called_once_with(15)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_loop_completes_one_pair_and_skips_it_on_later_cycles(self):
        with patch.object(auto.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep, self.assertRaises(KeyboardInterrupt):
            auto.run(self.session)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.checkpoint()["state"], pilot.READY)
        self.assertEqual(len(self.checkpoint()["autonomous_execution"]["attempts"]), 1)

    def test_loop_stops_immediately_on_unknown_execution(self):
        self.scripts[0]["after_fill_error"] = requests.Timeout("Lost receipt")
        with patch.object(auto.time, "sleep") as sleep:
            result = auto.run(self.session)
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertEqual(len(self.posts), 1)
        sleep.assert_not_called()

    def test_corrupt_completed_model_reference_fails_closed_before_new_order(self):
        self.execute()
        cp = self.checkpoint()
        cp["autonomous_execution"]["reference_cash"] = "21000"
        self.path.write_text(json.dumps(cp))
        self.session.get.reset_mock()
        with self.assertRaises(pilot.PilotBlocked):
            self.execute()
        self.session.get.assert_not_called()
        self.assertEqual(len(self.posts), 2)


if __name__ == "__main__":
    unittest.main()
