"""Disabled pilot tests: fake HTTP, synthetic accounting, temporary files only."""
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
import config
import account_test as single
import paired_account_test as paired
import live_pilot as pilot
import price_reader as scanner
from test_account_reader import holdings, isolate_quarantine, orders_page, position, tournament, order
from test_api_audit import QUOTE_TIME, TOURNAMENT_ID, approved_pair, election_tree, exchange_book, page, pair_relationship, party_market
from test_order_preview import account_snapshot, context, parsed_book


class LivePilotTests(unittest.TestCase):
    def setUp(self):
        isolate_quarantine(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.approval_path = self.root / "approved_settlements.json"
        self.approval_path.write_text(json.dumps({"version": 1, "allowed_mode": "paper-only",
                                                  "pairs": [approved_pair()]}))
        for target, name, value in ((scanner, "APPROVED_SETTLEMENTS_PATH", self.approval_path),
                                    (single, "STATE_PATH", self.root / "single.json"),
                                    (pilot, "ALLOCATION_PATH", self.root / "allocation.json"),
                                    (scanner, "READ_REQUEST_SPACING", 0),
                                    (scanner, "_last_request_started", None),
                                    (scanner, "_read_cooldown_until", 0)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("monotonic", 100), ("time", QUOTE_TIME)):
            patcher = patch.object(pilot.time, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = account_snapshot(cash=20000)
        self.checkpoint = pilot.allocation_checkpoint(self.account)
        self.session = MagicMock()

    def assert_no_writes(self):
        for method in ("post", "delete", "put", "patch"):
            getattr(self.session, method).assert_not_called()

    def risk(self, capital=.8, quantity=1, account=None, checkpoint=None):
        return pilot.pilot_risk(self.account if account is None else account, ["11", "12"], capital, quantity,
                                self.checkpoint if checkpoint is None else checkpoint)

    def response(self, payload):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    def consumed_checkpoint(self, consumed):
        checkpoint = copy.deepcopy(self.checkpoint)
        remaining = Decimal(str(consumed))
        index = 0
        while remaining:
            cost = min(remaining, Decimal(50))
            checkpoint["accounted_pair_costs"][f"{index:064x}"] = str(cost)
            index += 1
            remaining -= cost
        checkpoint["allocated_cash_remaining"] = str(Decimal(5000) - Decimal(str(consumed)))
        checkpoint["last_reconciled_account_cash"] = str(Decimal(20000) - Decimal(str(consumed)))
        return checkpoint

    def test_cash_above_5000_never_increases_allocation(self):
        for cash in (5000, 20000, 10000000):
            account = account_snapshot(cash=cash)
            checkpoint = pilot.allocation_checkpoint(account)
            risk = self.risk(account=account, checkpoint=checkpoint)
            self.assertEqual(risk["allocation_remaining_after_reserves"], 5000)
            self.assertEqual(risk["untouchable_cash_reserve"], cash - 5000)
        small_account = account_snapshot(cash=100)
        self.assertEqual(self.risk(account=small_account, checkpoint=pilot.allocation_checkpoint(small_account))[
            "allocation_remaining_after_reserves"], 100)

    def test_spent_allocation_cannot_be_replenished_from_reserve_after_restart(self):
        checkpoint = self.consumed_checkpoint(4999.5)
        pilot.ALLOCATION_PATH.write_text(json.dumps(checkpoint))
        restored = pilot.load_checkpoint()
        account = account_snapshot(cash=15000.5)
        self.assertEqual(self.risk(.5, account=account, checkpoint=restored)["allocation_remaining_after_reserves"], .5)
        with self.assertRaisesRegex(pilot.PilotBlocked, "Insufficient remaining pilot allocation"):
            self.risk(.505, account=account, checkpoint=restored)
        exhausted = self.consumed_checkpoint(5000)
        with self.assertRaisesRegex(pilot.PilotBlocked, "Insufficient remaining pilot allocation"):
            self.risk(.005, account=account_snapshot(cash=15000), checkpoint=exhausted)
        corrupted = copy.deepcopy(exhausted)
        corrupted["allocated_cash_remaining"] = "5000"
        with self.assertRaisesRegex(pilot.PilotBlocked, "confirmed debits"):
            pilot.validate_checkpoint(corrupted)

    def test_new_external_cash_or_broken_reserve_requires_review(self):
        for cash in (20001, 19999, 14999):
            with self.subTest(cash=cash), self.assertRaisesRegex(pilot.PilotBlocked, "manual review"):
                self.risk(account=account_snapshot(cash=cash))
        changed = copy.deepcopy(self.checkpoint)
        changed["untouchable_cash_reserve"] = "0"
        with self.assertRaisesRegex(pilot.PilotBlocked, "reserve changed"):
            self.risk(checkpoint=changed)

    def test_per_trade_race_total_and_quantity_limits(self):
        self.assertEqual(self.risk(50)["max_new_capital"], 50)
        with self.assertRaisesRegex(pilot.PilotBlocked, "per-trade"):
            self.risk(50.005)
        for cost, message in ((100, "per-race"), (500, "total exposure")):
            holding = position(99, 99, -1)
            holding["costBasis"] = cost
            account = account_snapshot([holding], cash=20000)
            with self.subTest(cost=cost), self.assertRaisesRegex(pilot.PilotBlocked, message):
                self.risk(.005, account=account)
        holding["costBasis"] = 50
        self.assertEqual(self.risk(50, account=account_snapshot([holding], cash=20000))["total_exposure_after"], 100)
        for quantity in (0, 2, .5, True):
            with self.subTest(quantity=quantity), self.assertRaisesRegex(pilot.PilotBlocked, "one contract"):
                self.risk(quantity=quantity)

    def test_orders_quarantine_and_duplicate_exposure_are_not_ignored(self):
        pending = order(exchange_id=99)
        with patch.object(pilot.quarantine, "reserved_cost", return_value=.125):
            risk = self.risk(account=account_snapshot(orders=[pending], cash=20000))
        self.assertEqual(risk["allocation_remaining_after_reserves"], 4997.875)
        self.assertEqual(risk["total_exposure_after"], 2.925)
        for account in (account_snapshot([position()], cash=20000),
                        account_snapshot(orders=[order()], cash=20000)):
            with self.subTest(account=account), self.assertRaises(ValueError):
                self.risk(account=account)

    def candidate_payloads(self, relationships=None, quote_age=0, depth=100):
        metadata = tournament()
        metadata["myBalance"] = 20000
        books = [exchange_book(1, 11, .6, .65), exchange_book(2, 12, .6, .65)]
        if quote_age:
            books[1]["asOf"]["at"] = "2026-10-07T11:59:00Z"
        for book in books:
            book["bids"][0]["quantity"] = depth
        return [metadata, holdings([]), orders_page([]), party_market("Democratic", 1, 11),
                party_market("Republican", 2, 12), page([pair_relationship()] if relationships is None else relationships),
                election_tree("Democratic", 1), election_tree("Republican", 2), *books]

    def test_candidate_assessment_reuses_scoped_gets_without_preparing_orders(self):
        self.session.get.side_effect = [self.response(payload) for payload in self.candidate_payloads()]
        with patch.object(single, "prepare_test") as prepare_single, \
                patch.object(paired, "prepare_pair") as prepare_pair, \
                patch.object(single, "submit_test") as submit_single, \
                patch.object(paired, "submit_pair") as submit_pair:
            result = pilot.audit_candidate(self.session, ["1", "2"], self.checkpoint)
        self.assertTrue(result["checks_passed"])
        self.assertFalse(result["live_eligible"])
        self.assertFalse(result["submission_enabled"])
        self.assertEqual(result["capital"], .8)
        self.assertNotIn("request", result)
        self.assertNotIn("idempotencyKey", json.dumps(result))
        self.assertEqual(self.session.get.call_count, 10)
        for method in (prepare_single, prepare_pair, submit_single, submit_pair):
            method.assert_not_called()
        self.assert_no_writes()

    def test_unlisted_unverified_stale_or_shallow_candidates_are_blocked(self):
        with self.assertRaisesRegex(pilot.PilotBlocked, "not explicitly approved"):
            pilot.audit_candidate(self.session, ["3", "4"], self.checkpoint)
        self.session.get.assert_not_called()
        for kwargs in ({"relationships": []}, {"quote_age": 60}, {"depth": 49}):
            self.session.get.side_effect = [self.response(payload) for payload in self.candidate_payloads(**kwargs)]
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                pilot.audit_candidate(self.session, ["1", "2"], self.checkpoint)
        self.assert_no_writes()

    def test_live_edge_stays_two_percent(self):
        payloads = self.candidate_payloads()
        payloads[-2]["bids"][0]["price"] = .64
        payloads[-1]["bids"][0]["price"] = .37
        self.session.get.side_effect = [self.response(payload) for payload in payloads]
        with self.assertRaisesRegex(ValueError, "2% edge"):
            pilot.audit_candidate(self.session, ["1", "2"], self.checkpoint)
        self.assertEqual(scanner.MIN_EDGE, .02)
        self.assert_no_writes()

    def test_live_mode_is_impossible_even_if_enable_flag_is_changed(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), patch.object(config, "LIVE_PILOT_SUBMISSION_ENABLED", enabled), \
                    patch("sys.argv", ["live_pilot.py", "--mode", "LIVE_PILOT"]), \
                    patch.object(pilot, "load_dotenv") as dotenv, \
                    patch.object(pilot.requests, "Session") as session, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pilot.main(), 1)
            dotenv.assert_not_called()
            session.assert_not_called()
        self.assertEqual(config.DEFAULT_MODE, "PAPER")
        self.assertFalse(config.LIVE_PILOT_SUBMISSION_ENABLED)

    def test_missing_checkpoint_cannot_silently_initialize_a_new_allocation(self):
        with self.assertRaisesRegex(pilot.PilotBlocked, "checkpoint missing or invalid"):
            pilot.load_checkpoint()
        self.assertFalse(pilot.ALLOCATION_PATH.exists())

    def test_new_uncertain_execution_journal_blocks_all_candidate_account_reads(self):
        pair, legs = self.synthetic_pair()
        pair["state"] = "UNKNOWN"
        pilot.quarantine.SOURCE_DIR.mkdir()
        with patch.object(paired, "load_pair", return_value=(pair, legs)), \
                self.assertRaisesRegex(pilot.PilotBlocked, "halts all further pilot trading"):
            pilot.audit_candidate(self.session, ["1", "2"], self.checkpoint)
        self.session.get.assert_not_called()
        self.assert_no_writes()

    def synthetic_pair(self, submitted=(True, True)):
        """Pure dictionaries; never call a preparation or submission function."""
        market_context = context()
        market_context.update(leg_prices=[.4, .4], book_versions=[parsed_book()["version"]] * 2)
        legs = []
        for index in range(2):
            body = {"idempotencyKey": f"account-test-{index:032x}", "exchangeId": str(11 + index),
                    "side": "no", "action": "buy", "quantity": 1, "price": .4,
                    "tournamentId": TOURNAMENT_ID, "expirationDate": "2026-10-07T12:15:00Z"}
            leg = {"version": 1, "market_id": str(1 + index), "tournament_slug": "midterm-elections",
                   "created_at": "2026-10-07T12:00:00Z", "request": body,
                   "state": "ACCEPTED" if submitted[index] else "PREPARED",
                   "order_id": 101 + index if submitted[index] else None,
                   "observation": {"placement_quantity": 0, "placement_cost": 0} if submitted[index] else None}
            leg["approval"] = single.approval_hash(leg)
            legs.append(leg)
        pair = {"version": 1, "policy": paired.VERIFIED_POLICY, "state": "UNMATCHED",
                "created_at": legs[0]["created_at"], "market_context": market_context, "starting_cash": 20000,
                "requests": [{key: leg[key] for key in ("market_id", "tournament_slug", "request")} for leg in legs]}
        pair["approval"] = paired.approval_hash(pair)
        return pair, legs

    def venue_observations(self, legs, quantities=(1, 1), prices=(.4, .4), opened=False, incomplete=False):
        """Fresh order/fill GETs plus an actual signed inventory/cash snapshot."""
        rows = []
        for index, (leg, quantity, price) in enumerate(zip(legs, quantities, prices)):
            if quantity:
                row = position(11 + index, 1 + index, -quantity)
                row["costBasis"] = quantity * price
                rows.append(row)
        cash = float(Decimal(20000) - sum((Decimal(str(q)) * Decimal(str(p)) for q, p in zip(quantities, prices)), Decimal(0)))
        account = account_snapshot(rows, cash=cash)

        def get(url, **kwargs):
            is_fills = url.endswith("/fills")
            index = int(url.split("/")[-2 if is_fills else -1]) - 101
            leg, quantity, price = legs[index], quantities[index], prices[index]
            body = leg["request"]
            if is_fills:
                fills = [] if not quantity else [{"id": 501 + index, "side": "no", "quantity": -quantity,
                                                 "price": price, "filledAt": "2026-10-07T12:00:00Z"}]
                payload = {"orderId": 101 + index, "exchangeId": body["exchangeId"], "tournamentId": TOURNAMENT_ID,
                           "coverage": {"complete": not incomplete}, "totalQuantityFilled": -quantity,
                           "avgFillPrice": price if quantity else None, "data": fills,
                           "pagination": {"hasMore": False, "nextCursor": None}}
            else:
                payload = {"id": 101 + index, "exchangeId": body["exchangeId"], "tournamentId": TOURNAMENT_ID,
                           "side": "no", "action": "buy", "quantity": 1 - quantity, "quantityFilled": quantity,
                           "priceLimit": body["price"], "open": opened, "expirationDate": body["expirationDate"],
                           "terminalReasonCode": None if opened else "closed"}
            return self.response(payload)
        self.session.get.side_effect = get
        return account

    def test_reconciliation_is_get_only_idempotent_and_preserves_input_journals(self):
        pair, legs = self.synthetic_pair()
        original = copy.deepcopy((pair, legs, self.checkpoint))
        account = self.venue_observations(legs)
        with patch.object(paired, "read_account", return_value=account):
            result = pilot.reconcile_pilot(self.session, pair, legs, self.checkpoint)
            repeated = pilot.reconcile_pilot(self.session, pair, legs, result["checkpoint"])
        self.assertEqual(result["status"], "RECONCILED_PAIR")
        self.assertEqual(result["confirmed_quantities"], [1, 1])
        self.assertEqual(pilot.amount(result["checkpoint"]["allocated_cash_remaining"]), Decimal("4999.2"))
        self.assertEqual(repeated["checkpoint"], result["checkpoint"])
        self.assertEqual((pair, legs, self.checkpoint), original)
        self.assertEqual(self.session.get.call_count, 8)
        self.assert_no_writes()

    def test_first_fill_requires_manual_second_leg_and_restart_cannot_continue(self):
        pair, legs = self.synthetic_pair((True, False))
        account = self.venue_observations(legs, (1, 0))
        with patch.object(paired, "read_account", return_value=account):
            result = pilot.reconcile_pilot(self.session, pair, legs, self.checkpoint)
        self.assertEqual(result["status"], "AWAITING_MANUAL_SECOND_LEG")
        self.assertEqual(result["confirmed_quantities"], [1, 0])
        self.assertEqual(result["confirmed_cost"], .4)
        with self.assertRaisesRegex(pilot.PilotBlocked, "restart cannot continue"):
            pilot.consider_second_leg(self.session, pair, result)
        books = [parsed_book(bid=.6), parsed_book(bid=.6)]
        with patch.object(pilot, "read_selected_pair", return_value=[party_market("Democratic", 1, 11), party_market("Republican", 2, 12)]), \
                patch.object(scanner, "get_pair_rules", return_value=({"NO-PAIR"}, context())), \
                patch.object(scanner, "get_best_prices", side_effect=books):
            decision = pilot.consider_second_leg(self.session, pair, result, manually_supervised=True, restarted=False)
        self.assertTrue(decision["checks_passed"])
        self.assertFalse(decision["submission_enabled"])
        self.assert_no_writes()

    def test_quote_movement_after_first_fill_blocks_second_leg(self):
        pair, legs = self.synthetic_pair((True, False))
        account = self.venue_observations(legs, (1, 0))
        with patch.object(paired, "read_account", return_value=account):
            result = pilot.reconcile_pilot(self.session, pair, legs, self.checkpoint)
        with patch.object(pilot, "read_selected_pair", return_value=[party_market("Democratic", 1, 11), party_market("Republican", 2, 12)]), \
                patch.object(scanner, "get_pair_rules", return_value=({"NO-PAIR"}, context())), \
                patch.object(scanner, "get_best_prices", side_effect=[parsed_book(bid=.6), parsed_book(bid=.59)]), \
                self.assertRaisesRegex(pilot.PilotBlocked, "original second-leg limit"):
            pilot.consider_second_leg(self.session, pair, result, manually_supervised=True, restarted=False)
        stale = copy.deepcopy(result)
        stale["observed_at"] = 80
        with self.assertRaisesRegex(pilot.PilotBlocked, "state is stale"):
            pilot.consider_second_leg(self.session, pair, stale, manually_supervised=True, restarted=False)
        self.assert_no_writes()

    def test_second_leg_needs_same_pair_and_current_explicit_settlement_approval(self):
        pair, legs = self.synthetic_pair((True, False))
        account = self.venue_observations(legs, (1, 0))
        with patch.object(paired, "read_account", return_value=account):
            result = pilot.reconcile_pilot(self.session, pair, legs, self.checkpoint)
        wrong_pair = copy.deepcopy(result)
        wrong_pair["pair_approval"] = "0" * 64
        with self.assertRaisesRegex(pilot.PilotBlocked, "different pair"):
            pilot.consider_second_leg(self.session, pair, wrong_pair, manually_supervised=True, restarted=False)
        self.approval_path.write_text(json.dumps({"version": 1, "allowed_mode": "paper-only", "pairs": []}))
        with self.assertRaisesRegex(pilot.PilotBlocked, "not explicitly approved"):
            pilot.consider_second_leg(self.session, pair, result, manually_supervised=True, restarted=False)
        self.assert_no_writes()

    def test_partial_rejected_or_failed_second_leg_latches_manual_review(self):
        for quantities, submitted in (((.5, 0), (True, False)), ((1, 0), (True, True)), ((0, 0), (True, False))):
            pair, legs = self.synthetic_pair(submitted)
            account = self.venue_observations(legs, quantities)
            with self.subTest(quantities=quantities), patch.object(paired, "read_account", return_value=account):
                result = pilot.reconcile_pilot(self.session, pair, legs, self.checkpoint)
            self.assertEqual(result["status"], "MANUAL_REVIEW")
            self.assertTrue(result["checkpoint"]["manual_review_required"])
            with self.assertRaisesRegex(pilot.PilotBlocked, "halted for manual review"):
                self.risk(checkpoint=result["checkpoint"], account=account)
        self.assert_no_writes()

    def test_timeout_unknown_or_stale_reporting_halts_and_restart_keeps_halt(self):
        pair, legs = self.synthetic_pair()
        for failure in ("unknown", "timeout", "open", "incomplete", "account_mismatch", "rate_limit"):
            current_legs = copy.deepcopy(legs)
            account = self.venue_observations(current_legs, opened=failure == "open", incomplete=failure == "incomplete")
            if failure == "unknown":
                current_legs[0].update(state="UNKNOWN", order_id=None, observation=None)
            elif failure in ("timeout", "rate_limit"):
                self.session.get.side_effect = (requests.Timeout("fake-secret") if failure == "timeout"
                                                else scanner.RateLimitError("fake-secret"))
            elif failure == "account_mismatch":
                account["tournament"]["myBalance"] += 1
            with self.subTest(failure=failure), patch.object(paired, "read_account", return_value=account):
                result = pilot.reconcile_pilot(self.session, pair, current_legs, self.checkpoint)
            self.assertEqual(result["status"], "MANUAL_REVIEW")
            self.assertNotIn("fake-secret", result["reason"])
            pilot.ALLOCATION_PATH.write_text(json.dumps(result["checkpoint"]))
            restored = pilot.load_checkpoint()
            with self.assertRaisesRegex(pilot.PilotBlocked, "halted for manual review"):
                self.risk(checkpoint=restored)
            self.session.get.reset_mock()
            again = pilot.reconcile_pilot(self.session, pair, legs, restored)
            self.assertEqual(again["status"], "MANUAL_REVIEW")
            self.session.get.assert_not_called()
        self.assert_no_writes()

    def test_decimal_fill_noise_does_not_reset_or_corrupt_allocation(self):
        pair, legs = self.synthetic_pair()
        account = self.venue_observations(legs, prices=(.1 + .2, .4))
        with patch.object(paired, "read_account", return_value=account):
            result = pilot.reconcile_pilot(self.session, pair, legs, self.checkpoint)
        self.assertEqual(result["status"], "RECONCILED_PAIR")
        pilot.ALLOCATION_PATH.write_text(json.dumps(result["checkpoint"]))
        self.assertEqual(pilot.load_checkpoint(), result["checkpoint"])
        self.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
