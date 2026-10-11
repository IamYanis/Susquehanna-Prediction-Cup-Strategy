"""A fake exchange exercises entry -> hold -> sell/settle -> repeat.

Every checkpoint and approval lives in the existing temporary fixture. No real
credentials, API calls, allocation state or execution journals are used.
"""
import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import requests

import autonomous_pilot as auto
import config
import live_pilot as pilot
import live_settlement as live
import test_autonomous_pilot as fixtures
import test_pilot_account as account_fixtures
from test_account_reader import holdings, position
from test_api_audit import exchange_book


class AutonomousLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AutonomousPilotTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.session = self.fixture.session
        self.session.post.side_effect = self.post
        self.posts, self.scripts = [], {}

    def post(self, url, **kwargs):
        body = kwargs["json"]
        self.assertEqual(url, auto.scanner.API_BASE_URL + "/orders")
        cp = pilot._read_checkpoint_locked(pilot.ALLOCATION_PATH.resolve())
        if body["action"] == "buy":
            leg = next(l for l in auto.active_attempt(cp)["legs"] if l["intent"] and l["intent"]["request"] == body)
        else:
            p = next(p for p in cp["autonomous_positions"].values() if p["status"] == "EXITING")
            leg = next(l for l in p["exit_execution"]["legs"] if l["request"] == body)
            self.assertEqual(auto.held_quantity(self.fixture.fixture.current, body["exchangeId"]), body["quantity"])
        self.assertTrue(leg["post_attempted"])
        self.assertTrue(1 <= body["quantity"] <= config.MAX_MATCHED_PAIRS_PER_RACE)
        if body["action"] == "buy":
            self.assertEqual(body["quantity"], 1)
        self.assertEqual(len({b["idempotencyKey"] for b in self.posts}), len(self.posts))
        self.posts.append(copy.deepcopy(body))
        script = self.scripts.get(len(self.posts), {})
        if script.get("timeout_before"):
            raise requests.Timeout("Synthetic ambiguous POST")
        oid, fid = 1000 + len(self.posts), 2000 + len(self.posts)
        eid, mid = body["exchangeId"], str(int(body["exchangeId"]) - 10)
        q = Decimal(str(script.get("quantity", body["quantity"])))
        price = Decimal(str(body["price"]))
        notional = q * price
        stamp = datetime.now(timezone.utc).isoformat()
        account = self.fixture.fixture.current
        cash = Decimal(str(account["tournament"]["myBalance"]))
        cash += notional if body["action"] == "sell" else -notional
        cash -= Decimal(str(script.get("fee", 0)))
        account["tournament"]["myBalance"] = float(cash)
        old = next((r for r in account["positions"] if r["exchangeId"] == eid), None)
        rows = [r for r in account["positions"] if r["exchangeId"] != eid]
        prior_q = auto.held_quantity(account, eid)
        remaining = prior_q - q if body["action"] == "sell" else prior_q + q
        if remaining:
            row = position(eid, mid, float(-remaining))
            old_cost = Decimal(str(old["avgCost"])) * prior_q if old else Decimal(0)
            basis = old_cost / prior_q * remaining if body["action"] == "sell" else old_cost + notional
            row.update(avgCost=float(basis / remaining), costBasis=float(basis),
                       marketValue=float(basis), currentPrice=float(price), unrealizedPnl=0)
            rows.append(row)
        account.update(holdings(rows))
        fill = {"id": fid, "orderId": oid, "exchangeId": eid, "marketId": mid,
                "side": "no", "quantity": float(-q), "price": float(price), "filledAt": stamp}
        self.fixture.fixture.fills.insert(0, fill)
        self.fixture.fixture.transactions.insert(0, {"event_id": f"trade-{fid}", "event_type": "trade",
            "createdAt": stamp, "tournamentId": body["tournamentId"], "exchangeId": eid, "marketId": mid,
            "quantity": float(-q), "price": float(price), "amount": None, "transactionType": None,
            "orderType": body["action"].upper()})
        order = {"id": oid, "exchangeId": eid, "tournamentId": body["tournamentId"], "side": "no",
                 "action": body["action"], "quantity": body["quantity"], "quantityFilled": float(q), "priceLimit": body["price"],
                 "expirationDate": body["expirationDate"], "createdAt": stamp, "open": False}
        fills = account_fixtures.history([{k: v for k, v in fill.items() if k not in {"orderId", "marketId", "exchangeId"}}])
        fills.update(orderId=oid, exchangeId=eid, tournamentId=body["tournamentId"],
                     totalQuantityFilled=float(-q), avgFillPrice=float(price))
        self.fixture.fixture.receipts[oid] = order, fills
        if "next_ask" in script:
            self.fixture.books["12"] = exchange_book(2, 12, bid=.3, ask=script["next_ask"])
        if script.get("timeout_after"):
            raise requests.Timeout("Synthetic lost receipt after fill")
        return self.fixture.base.response({"orderId": oid, "exchangeId": eid, "side": "no", "action": body["action"],
            "price": body["price"], "quantity": body["quantity"], "quantityTraded": float(q), "totalCost": float(notional),
            "open": False, "remainingQuantity": 0, "fillPrice": float(price), "all": None})

    def cycle(self):
        with pilot.pilot_lock() as path:
            return auto.cycle_locked(self.session, pilot._read_checkpoint_locked(path))

    def manage(self):
        with pilot.pilot_lock() as path:
            return auto.manage_positions_locked(self.session, pilot._read_checkpoint_locked(path))

    def quote_exit(self, yes_ask):
        self.fixture.books = {"11": exchange_book(1, 11, bid=.3, ask=yes_ask),
                              "12": exchange_book(2, 12, bid=.3, ask=yes_ask)}

    def settle(self, mid, amount, with_event=True):
        account = self.fixture.fixture.current
        eid = str(int(mid) + 10)
        quantity = auto.held_quantity(account, eid)
        self.fixture.markets[mid].update(status="settled", settledWith="NO" if amount else "YES")
        account.update(holdings([r for r in account["positions"] if r["exchangeId"] != eid]))
        account["tournament"]["myBalance"] = float(Decimal(str(account["tournament"]["myBalance"])) + Decimal(str(amount)))
        if with_event:
            self.fixture.fixture.transactions.insert(0, {"event_id": "settle-" + mid, "event_type": "settlement",
                "createdAt": datetime.now(timezone.utc).isoformat(), "tournamentId": self.fixture.approval["tournament_id"],
                "exchangeId": eid, "marketId": mid, "quantity": float(-quantity), "price": amount / float(quantity), "amount": amount,
                "transactionType": "SETTLEMENT", "orderType": None})

    def test_complete_entry_hold_early_exit_repeat_and_settlement(self):
        result = self.cycle()
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(len(self.posts), 2)
        self.quote_exit(.51)  # .980 > entry .800, but below the 1.010 rule.
        held = self.manage()
        self.assertEqual(held["positions"][0]["action"], "HOLD")
        self.assertEqual(len(self.posts), 2)
        self.quote_exit(.495)  # .505 + .505 = exactly 1.010.
        sold = self.cycle()
        self.assertEqual(sold["state"], pilot.READY, sold)
        self.assertEqual(sold["positions"][0]["action"], "EARLY_EXIT")
        self.assertEqual(Decimal(sold["positions"][0]["realized_pnl"]), Decimal("0.210"))
        self.assertEqual(len(self.posts), 4)  # No overlapping entry in the exit cycle.
        self.assertEqual(Decimal(self.fixture.checkpoint()["total_live_exposure"]), 0)
        self.assertEqual(Decimal(self.fixture.checkpoint()["allocated_cash_remaining"]), 5000)
        # Same still-open race may be entered again only after its prior close.
        self.fixture.books = {"11": exchange_book(1, 11), "12": exchange_book(2, 12)}
        self.assertEqual(self.cycle()["state"], pilot.READY)
        self.assertEqual(len(self.posts), 6)
        self.settle("1", 0)
        self.settle("2", 1)
        settled = self.manage()
        self.assertEqual(settled["state"], pilot.READY, settled)
        self.assertEqual(settled["positions"][0]["action"], "SETTLEMENT")
        self.assertEqual(Decimal(settled["positions"][0]["realized_pnl"]), Decimal(".2"))
        cp = self.fixture.checkpoint()
        self.assertTrue(all(p["status"] == "CLOSED" for p in cp["autonomous_positions"].values()))
        self.assertEqual(Decimal(cp["total_live_exposure"]), 0)
        self.assertEqual(Decimal(cp["allocated_cash_remaining"]), 5000)
        self.assertEqual(Decimal(cp["last_reconciled_account_cash"]), Decimal("20000.410"))
        self.assertEqual(len({b["idempotencyKey"] for b in self.posts}), 6)
        self.session.delete.assert_not_called()

    def test_duplicate_open_position_remains_blocked_across_cycles(self):
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(len(self.fixture.checkpoint()["autonomous_positions"]), 1)

    def test_normalized_api_expiry_passes_entry_and_sale(self):
        original_post = self.post
        def normalized(url, **kwargs):
            response = original_post(url, **kwargs)
            order = self.fixture.fixture.receipts[response.json()["orderId"]][0]
            expiry = auto.single.parse_api_timestamp(order["expirationDate"])
            order["expirationDate"] = expiry.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            return response
        self.session.post.side_effect = normalized
        self.assertEqual(self.cycle()["state"], pilot.READY)
        self.quote_exit(.495)
        self.assertEqual(self.manage()["state"], pilot.READY)
        self.assertEqual(len(self.posts), 4)
        for body in self.posts:
            self.assertRegex(body["expirationDate"], r"\.\d{3}\+00:00$")

    def test_different_sale_expiry_millisecond_still_halts(self):
        self.assertEqual(self.cycle()["state"], pilot.READY)
        self.quote_exit(.495)
        original_post = self.post
        def changed(url, **kwargs):
            response = original_post(url, **kwargs)
            order = self.fixture.fixture.receipts[response.json()["orderId"]][0]
            from datetime import timedelta
            expiry = auto.single.parse_api_timestamp(order["expirationDate"]) + timedelta(milliseconds=1)
            order["expirationDate"] = expiry.isoformat(timespec="milliseconds")
            return response
        self.session.post.side_effect = changed
        self.assertEqual(self.manage()["state"], pilot.HALTED)
        self.assertEqual(len(self.posts), 3)  # No second sale after the mismatch.

    def test_closed_market_without_settlement_event_remains_hold(self):
        self.cycle()
        for m in self.fixture.markets.values():
            m["status"] = "closed"
        result = self.manage()
        self.assertEqual(result["positions"][0]["action"], "HOLD")
        self.assertGreater(Decimal(self.fixture.checkpoint()["total_live_exposure"]), 0)
        self.assertEqual(len(self.posts), 2)

    def test_stale_read_only_exit_evaluation_skips_without_persistent_halt(self):
        self.assertEqual(self.cycle()["state"], pilot.READY)
        original_quote = auto.exit_quote
        def slow_quote(*args, **kwargs):
            # A valid snapshot began at 100; serial GETs have now taken 16s.
            auto.time.monotonic.return_value = 116
            return original_quote(*args, **kwargs)
        with patch.object(auto, "exit_quote", side_effect=slow_quote), patch.object(auto, "halt") as halt:
            result = self.manage()
        self.assertEqual(result["state"], pilot.READY)
        self.assertEqual(result["positions"][0]["reason"], "HOLD_SKIPPED_STALE_ACCOUNT")
        halt.assert_not_called()
        cp = self.fixture.checkpoint()
        self.assertFalse(cp["manual_review_required"])
        self.assertEqual(cp["review_reason"], "")
        self.assertIsNone(next(iter(cp["autonomous_positions"].values()))["exit_execution"])
        self.assertEqual(len(self.posts), 2)  # Only the earlier mocked entry.

    def test_ctrl_c_during_read_only_account_capture_does_not_write_halt(self):
        self.cycle()
        before = self.fixture.path.read_bytes()
        with patch.object(auto.pilot_account, "read_snapshot", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.manage()
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assertEqual(len(self.posts), 2)

    def test_ctrl_c_during_hold_quote_does_not_change_state_or_positions(self):
        self.cycle()
        captured = []
        def interrupt(*args, **kwargs):
            captured.append(self.fixture.path.read_bytes())
            raise KeyboardInterrupt
        with patch.object(auto, "exit_quote", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.manage()
        self.assertEqual(self.fixture.path.read_bytes(), captured[0])
        self.assertFalse(self.fixture.checkpoint()["manual_review_required"])
        self.assertEqual(len(self.posts), 2)

    def test_ctrl_c_before_sale_intent_exits_cleanly(self):
        self.cycle()
        self.quote_exit(.495)
        original, captured = auto.exit_quote, []
        calls = []
        def interrupt(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:  # execute_exit_locked's quote BEFORE EXITING.
                captured.append(self.fixture.path.read_bytes())
                raise KeyboardInterrupt
            return original(*args, **kwargs)
        with patch.object(auto, "exit_quote", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.manage()
        self.assertEqual(self.fixture.path.read_bytes(), captured[0])
        self.assertIsNone(next(iter(self.fixture.checkpoint()["autonomous_positions"].values()))["exit_execution"])
        self.assertEqual(len(self.posts), 2)

    def interrupt_after_sale_intent(self, error):
        self.cycle()
        self.quote_exit(.495)
        original, raised = auto.persist, []
        def interrupt(state):
            original(state)
            if state["checkpoint"]["state"] == pilot.EXECUTING and not raised:
                raised.append(True)
                raise error
        with patch.object(auto, "persist", side_effect=interrupt), self.assertRaises(type(error)):
            self.manage()
        cp = self.fixture.checkpoint()
        self.assertEqual(cp["state"], pilot.HALTED)
        self.assertTrue(cp["manual_review_required"])
        self.assertIn("stage=EXECUTING", cp["review_reason"])
        self.assertIn("sale_intent=True", cp["review_reason"])
        self.assertIn("post_attempted=False", cp["review_reason"])
        self.assertIn("exception=" + type(error).__name__, cp["review_reason"])
        self.assertEqual(len(self.posts), 2)

    def test_ctrl_c_after_sale_intent_halts(self):
        self.interrupt_after_sale_intent(KeyboardInterrupt())

    def test_runtime_fault_after_sale_intent_halts_with_safe_diagnostics(self):
        self.interrupt_after_sale_intent(RuntimeError("Sensitive text must not be persisted"))
        self.assertNotIn("Sensitive text", self.fixture.checkpoint()["review_reason"])

    def test_ctrl_c_after_sell_post_attempt_halts_and_never_retries(self):
        self.cycle()
        self.quote_exit(.495)
        self.session.post.side_effect = KeyboardInterrupt
        calls = self.session.post.call_count
        with self.assertRaises(KeyboardInterrupt):
            self.manage()
        cp = self.fixture.checkpoint()
        self.assertEqual(cp["state"], pilot.HALTED)
        self.assertIn("post_attempted=True", cp["review_reason"])
        p = next(iter(cp["autonomous_positions"].values()))
        self.assertIsNotNone(p["exit_execution"]["legs"][0]["request"]["idempotencyKey"])
        self.assertEqual(self.session.post.call_count, calls + 1)
        self.manage()
        self.assertEqual(self.session.post.call_count, calls + 1)

    def test_ctrl_c_after_receipt_before_reconciliation_halts(self):
        self.cycle()
        self.quote_exit(.495)
        with patch.object(auto, "wait_for_account_cache", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.manage()
        cp = self.fixture.checkpoint()
        self.assertEqual(cp["state"], pilot.HALTED)
        self.assertIn("receipt_pending=True", cp["review_reason"])
        self.assertEqual(len(self.posts), 3)

    def test_system_exit_during_read_only_hold_propagates_without_halt(self):
        self.cycle()
        before = self.fixture.path.read_bytes()
        with patch.object(auto.pilot_account, "read_snapshot", side_effect=SystemExit(7)), \
                self.assertRaises(SystemExit) as error:
            self.manage()
        self.assertEqual(error.exception.code, 7)
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assertEqual(len(self.posts), 2)

    def test_system_exit_after_sale_intent_is_not_swallowed_and_halts(self):
        self.interrupt_after_sale_intent(SystemExit(7))

    def test_stale_hold_snapshot_capture_skips_with_zero_state_writes(self):
        self.cycle()
        before = self.fixture.path.read_bytes()
        error = auto.pilot_account.AccountReadinessBlocked(auto.pilot_account.STALE, "Synthetic slow GETs")
        with patch.object(auto.pilot_account, "read_snapshot", side_effect=error):
            result = self.manage()
        self.assertEqual(result["state"], pilot.READY)
        self.assertEqual(result["positions"][0]["reason"], "HOLD_SKIPPED_STALE_ACCOUNT")
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assertEqual(len(self.posts), 2)

    def test_stale_fresh_account_immediately_before_sell_still_halts_without_post(self):
        self.cycle()
        self.quote_exit(.495)
        original = auto.pilot_account.read_snapshot
        calls = []
        def expires_before_sell(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(result)
            if len(calls) == 2:  # First is HOLD; second is the pre-SELL read.
                auto.time.monotonic.return_value = 116
            return result
        with patch.object(auto.pilot_account, "read_snapshot", side_effect=expires_before_sell):
            result = self.manage()
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("STALE_ACCOUNT_DATA", result["reason"])
        self.assertEqual(len(self.posts), 2)
        self.assertTrue(self.fixture.checkpoint()["manual_review_required"])

    def test_final_freshness_check_after_sale_authorization_blocks_post(self):
        self.cycle()
        self.quote_exit(.495)
        original = live.revalidate_authorization
        def slow_final_authorization(approval):
            result = original(approval)
            cp = pilot._read_checkpoint_locked(self.fixture.path.resolve())
            if any(p["exit_execution"] and p["exit_execution"]["legs"][0]["post_attempted"]
                   for p in cp["autonomous_positions"].values()):
                auto.time.monotonic.return_value = 116
            return result
        with patch.object(live, "revalidate_authorization", side_effect=slow_final_authorization):
            result = self.manage()
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("STALE_ACCOUNT_DATA", result["reason"])
        self.assertEqual(len(self.posts), 2)

    def test_unexplained_hold_cash_change_still_halts(self):
        self.cycle()
        self.fixture.fixture.current["tournament"]["myBalance"] -= .1
        result = self.manage()
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("Unexplained account cash change", result["reason"])
        self.assertEqual(len(self.posts), 2)

    def test_financial_transaction_change_before_sell_blocks_post(self):
        self.cycle()
        self.quote_exit(.495)
        original = auto.pilot_account.read_snapshot
        calls = []
        def changed_before_sell(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            calls.append(snapshot)
            if len(calls) == 2:
                snapshot["recent_transactions"][0]["price"] += .005
            return snapshot
        with patch.object(auto.pilot_account, "read_snapshot", side_effect=changed_before_sell):
            result = self.manage()
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("Account changed before sale", result["reason"])
        self.assertEqual(len(self.posts), 2)

    def test_missing_holding_without_settlement_evidence_halts(self):
        self.cycle()
        self.settle("1", 0, with_event=False)
        self.assertEqual(self.manage()["state"], pilot.HALTED)
        self.assertEqual(len(self.posts), 2)

    def test_staggered_settlement_records_each_credit_once(self):
        self.cycle()
        self.settle("1", 1)
        self.assertEqual(self.manage()["state"], pilot.READY)
        p = next(iter(self.fixture.checkpoint()["autonomous_positions"].values()))
        self.assertEqual(p["status"], "OPEN")
        self.assertEqual(list(map(Decimal, p["remaining_quantities"])), [Decimal(0), Decimal(1)])
        self.manage()
        self.assertEqual(next(iter(self.fixture.checkpoint()["autonomous_positions"].values()))["exit_proceeds"], "1")
        self.settle("2", 0)
        self.assertEqual(self.manage()["positions"][0]["action"], "SETTLEMENT")

    def test_ambiguous_sale_post_never_retries_after_restart(self):
        self.cycle()
        self.quote_exit(.495)
        self.scripts[3] = {"timeout_after": True}
        self.assertEqual(self.manage()["state"], pilot.HALTED)
        self.assertEqual(len(self.posts), 3)
        cp = self.fixture.checkpoint()
        self.assertTrue(cp["manual_review_required"])
        p = next(iter(cp["autonomous_positions"].values()))
        self.assertTrue(p["exit_execution"]["legs"][0]["post_attempted"])
        self.assertEqual(self.manage()["state"], pilot.HALTED)
        self.assertEqual(len(self.posts), 3)

    def test_partial_sale_halts_and_preserves_known_remaining_quantity(self):
        self.cycle()
        self.quote_exit(.495)
        self.scripts[3] = {"quantity": .5}
        result = self.manage()
        self.assertEqual(result["state"], pilot.HALTED, result)
        p = next(iter(self.fixture.checkpoint()["autonomous_positions"].values()))
        self.assertEqual(Decimal(p["remaining_quantities"][0]), Decimal(".5"))
        self.assertEqual(len(self.posts), 3)

    def test_quote_worsening_after_first_sale_stops_before_second(self):
        self.cycle()
        self.quote_exit(.495)
        self.scripts[3] = {"next_ask": .5}
        result = self.manage()
        self.assertEqual(result["state"], pilot.HALTED, result)
        p = next(iter(self.fixture.checkpoint()["autonomous_positions"].values()))
        self.assertEqual(list(map(Decimal, p["remaining_quantities"])), [Decimal(0), Decimal(1)])
        self.assertEqual(len(self.posts), 3)

    def test_unexplained_sale_cash_delta_halts(self):
        self.cycle()
        self.quote_exit(.495)
        self.scripts[3] = {"fee": .05}
        self.assertEqual(self.manage()["state"], pilot.HALTED)
        self.assertEqual(len(self.posts), 3)

    def test_ranking_uses_edge_then_depth(self):
        approvals = [dict(self.fixture.approval, market_ids=["1", "2"]),
                     dict(self.fixture.approval, market_ids=["3", "4"])]
        candidates = [{"authorization": approvals[0], "edge": ".010", "books": [{"bid_quantity": 100}] * 2},
                      {"authorization": approvals[1], "edge": ".015", "books": [{"bid_quantity": 1}] * 2}]
        with patch.object(live, "autonomous_authorizations", return_value=approvals), \
                patch.object(auto, "scoped_markets", return_value=[{"status": "open"}] * 2), \
                patch.object(auto, "fresh_candidate", side_effect=candidates):
            self.assertEqual(auto.best_candidate(self.session, self.fixture.checkpoint()), candidates[1])
        candidates[1]["edge"] = ".010"
        with patch.object(live, "autonomous_authorizations", return_value=approvals), \
                patch.object(auto, "scoped_markets", return_value=[{"status": "open"}] * 2), \
                patch.object(auto, "fresh_candidate", side_effect=candidates):
            self.assertEqual(auto.best_candidate(self.session, self.fixture.checkpoint()), candidates[0])
        self.session.post.assert_not_called()

    def test_disabled_main_never_loads_credentials_or_starts_loop(self):
        with patch.object(config, "AUTONOMOUS_LIVE_PILOT_ENABLED", False), \
                patch.object(auto, "load_dotenv") as env, patch.object(auto, "run") as run:
            self.assertEqual(auto.main(), 0)
            env.assert_not_called()
            run.assert_not_called()

    def test_reconciliation_waits_for_the_documented_account_cache(self):
        # Model the documented stale tournament balance after a successful
        # POST. Fills/holdings are already projected; only balance is cached.
        cached = {"balance": None}
        original_get = self.fixture.get
        def get_with_cache(url, **kwargs):
            response = original_get(url, **kwargs)
            if url.endswith("/tournaments/midterm-elections") and cached["balance"] is not None:
                data = copy.deepcopy(response.json())
                data["myBalance"] = cached["balance"]
                return self.fixture.base.response(data)
            return response
        def place_then_cache(url, **kwargs):
            balance = self.fixture.fixture.current["tournament"]["myBalance"]
            response = self.post(url, **kwargs)
            cached["balance"] = balance
            return response
        def expire_cache():
            cached["balance"] = None
        self.session.get.side_effect = get_with_cache
        self.session.post.side_effect = place_then_cache
        with patch.object(auto, "wait_for_account_cache", side_effect=expire_cache):
            result = self.cycle()
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(len(self.posts), 2)  # Waiting never resubmits an order.

    def test_loss_releases_exposure_without_replenishing_allocation_on_restart(self):
        self.cycle()
        self.settle("1", 0)
        self.settle("2", 0)  # Exceptional adverse settlement; recognize the loss.
        self.assertEqual(self.manage()["state"], pilot.READY)
        first = self.fixture.checkpoint()
        self.assertEqual(Decimal(first["total_live_exposure"]), 0)
        self.assertEqual(Decimal(first["allocated_cash_remaining"]), Decimal("4999.18"))
        self.assertEqual(self.fixture.checkpoint()["allocated_cash_remaining"], first["allocated_cash_remaining"])

    def test_exactly_one_share_depth_is_enough_for_entry(self):
        for b in self.fixture.books.values():
            b["bids"][0]["quantity"] = 1
        self.assertEqual(self.cycle()["state"], pilot.READY)
        self.assertEqual(len(self.posts), 2)

    def test_high_first_leg_does_not_inherit_diagnostic_probe_debit_cap(self):
        self.fixture.books = {"11": exchange_book(1, 11, bid=.010, ask=.015),
                              "12": exchange_book(2, 12, bid=.995, ask=.995)}
        result = self.cycle()
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(len(self.posts), 2)
        p = next(iter(self.fixture.checkpoint()["autonomous_positions"].values()))
        self.assertEqual(Decimal(p["entry_edge"]), Decimal(".005"))

    def test_settlement_management_continues_after_tournament_ends(self):
        self.cycle()
        self.settle("1", 0)
        self.settle("2", 1)
        self.fixture.fixture.current["tournament"]["status"] = "completed"
        result = self.cycle()
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(result["positions"][0]["action"], "SETTLEMENT")
        self.assertFalse(result["account_active"])
        self.assertEqual(len(self.posts), 2)

    def test_crash_after_saved_sale_intent_halts_on_restart_without_post(self):
        self.cycle()
        self.quote_exit(.495)
        original = auto.persist
        def crash_after_marker(state):
            original(state)
            cp = state["checkpoint"]
            if cp["state"] == pilot.EXECUTING:
                p = next(p for p in cp["autonomous_positions"].values() if p["status"] == "EXITING")
                if p["exit_execution"]["legs"][0]["post_attempted"]:
                    raise fixtures.InjectedCrash()
        with patch.object(auto, "persist", side_effect=crash_after_marker), \
                patch.object(auto, "halt", side_effect=OSError("Simulate unavailable halt write")):
            with self.assertRaises(OSError):
                self.manage()
        self.assertEqual(len(self.posts), 2)
        cp = self.fixture.checkpoint()  # Reacquires the lock: restart latches HALTED.
        self.assertEqual(cp["state"], pilot.HALTED)
        self.assertEqual(self.manage()["state"], pilot.HALTED)
        self.assertEqual(len(self.posts), 2)


if __name__ == "__main__":
    unittest.main()
