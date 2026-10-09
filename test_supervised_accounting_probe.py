"""Disabled probe gates and offline comparisons; no real account/order reads."""
import contextlib
import copy
import io
import json
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import Mock, patch

import requests

import config
import live_pilot
import live_settlement
import pilot_account
import supervised_accounting_probe as probe
import test_live_pilot as fixtures
from test_account_reader import holdings, position
from test_order_preview import parsed_book


def no_pair_books(first, second):
    """Fresh fake YES books whose complements are the desired raw NO prices."""
    return [parsed_book(bid=float(Decimal(1) - Decimal(price)),
                        ask=float(Decimal(1) - Decimal(price) + Decimal(".005")))
            for price in (first, second)]


class AccountingProbeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LivePilotTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.design = probe.describe_pair(["1", "2"])
        self.before = {"account": copy.deepcopy(self.fixture.account), "data_complete": True,
                       "recent_transactions": [], "recent_fills": [], "order_activity": [],
                       "freshness": {"started_monotonic": 100, "completed_monotonic": 101}}
        self.after = copy.deepcopy(self.before)
        self.after["freshness"] = {"started_monotonic": 102, "completed_monotonic": 103}
        self.after["account"]["tournament"]["myBalance"] = 19999.6
        row = position("11", "1", -1)
        row.update(costBasis=.4, marketValue=.4, currentPrice=.4, unrealizedPnl=0)
        self.after["account"].update(holdings([row]))
        self.after["recent_transactions"] = [self.trade()]
        self.receipt = {"orderId": 101, "exchangeId": "11", "open": False, "remainingQuantity": 0,
                        "side": "no", "action": "buy", "quantity": 1, "quantityTraded": 1,
                        "price": .4, "totalCost": .4, "fillPrice": .4, "all": None}
        self.activity = {"order": {"id": 101, "exchangeId": "11", "tournamentId": fixtures.TOURNAMENT_ID,
                                   "side": "no", "action": "buy", "quantity": 1, "quantityFilled": 1,
                                   "priceLimit": .4, "open": False},
                         "fills": [{"id": 501, "side": "no", "price": .4, "quantity": -1,
                                    "filledAt": "2026-10-07T12:00:00Z"}]}

    def trade(self, event_id="trade-1", eid="11"):
        return {"event_id": event_id, "event_type": "trade", "exchangeId": eid, "marketId": "1",
                "tournamentId": fixtures.TOURNAMENT_ID, "createdAt": "2026-10-07T12:00:00Z",
                "quantity": -1, "price": .4, "amount": None, "transactionType": None, "orderType": "BUY"}

    def review(self):
        result = probe.review_observation(self.before, self.after, self.receipt, self.activity, "1", "11")
        self.assertFalse(result["accounting_verified"])
        self.assertFalse(result["submission_enabled"])
        self.assertEqual(result["state"], "HALTED_MANUAL_REVIEW")
        self.assertEqual(result["code"], pilot_account.ACCOUNTING_UNVERIFIED)
        self.fixture.assert_no_writes()
        return result

    def test_command_without_pair_selection_blocks_before_config_credentials_and_requests(self):
        with patch("sys.argv", ["supervised_accounting_probe.py", "--supervised-accounting-probe"]), \
                patch.object(probe, "describe_pair") as describe, \
                patch.object(probe, "require_manual_confirmation") as confirm, \
                patch.object(live_pilot.single, "prepare_test") as prepare, \
                patch.object(live_pilot.single, "submit_test") as submit, \
                patch.object(pilot_account, "read_snapshot") as read, \
                patch.object(pilot_account.requests, "Session") as session, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main(), 1)
        for call in (describe, confirm, prepare, submit, read, session):
            call.assert_not_called()
        self.assertFalse(config.LIVE_PILOT_SUBMISSION_ENABLED)

    def test_paper_approval_cannot_authorize_probe_and_empty_live_list_blocks(self):
        self.fixture.live_approval_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT", "pairs": []}))
        with self.assertRaises(live_settlement.LiveSettlementBlocked):
            probe.describe_pair(["1", "2"])
        self.assertTrue(self.fixture.approval_path.exists())
        self.fixture.session.get.assert_not_called()

    def test_design_bound_to_explicit_ids_one_per_leg_and_unverified_one_susqie_cap(self):
        self.assertEqual(self.design["live_authorization"]["market_ids"], ["1", "2"])
        self.assertEqual(self.design["live_authorization"]["exchange_ids"], ["11", "12"])
        self.assertEqual(self.design["quantity_per_leg"], 1)
        self.assertEqual(self.design["maximum_pair_debit_target"], "1")
        self.assertFalse(self.design["debit_cap_verified"])
        self.assertFalse(self.design["enabled"])
        self.assertNotIn("request", self.design)
        self.assertNotIn("idempotencyKey", json.dumps(self.design))

    def test_probe_edge_exactly_point_five_percent_passes_using_decimal(self):
        prices, quantity, cost, edge = probe.observed_probe_limits(no_pair_books(".360", ".635"), 100)
        self.assertEqual(prices, [.360, .635])
        self.assertEqual(quantity, 1)
        self.assertIsInstance(cost, Decimal)
        self.assertIsInstance(edge, Decimal)
        self.assertEqual(cost, Decimal(".995"))
        self.assertEqual(edge, Decimal(".005"))
        self.fixture.assert_no_writes()

    def test_probe_edge_below_point_five_percent_fails(self):
        with self.assertRaisesRegex(probe.ProbeBlocked, "0.5% probe edge"):
            probe.observed_probe_limits(no_pair_books(".360", ".636"), 100)

    def test_probe_zero_edge_fails(self):
        with self.assertRaisesRegex(probe.ProbeBlocked, "0.5% probe edge"):
            probe.observed_probe_limits(no_pair_books(".360", ".640"), 100)

    def test_probe_negative_edge_and_excess_pair_notional_fail(self):
        with self.assertRaisesRegex(probe.ProbeBlocked, "one-SUSQie probe target"):
            probe.observed_probe_limits(no_pair_books(".360", ".645"), 100)

    def test_raw_qualifying_edge_lost_to_tick_rounding_fails(self):
        # Raw cost .9941 has edge .0059. Executable buy limits .365 + .635
        # cost 1.000, so checking the raw prices would incorrectly admit it.
        raw_edge = Decimal("1.000") - Decimal(".3601") - Decimal(".634")
        self.assertGreater(raw_edge, Decimal(".005"))
        with self.assertRaisesRegex(probe.ProbeBlocked, "0.5% probe edge"):
            probe.observed_probe_limits(no_pair_books(".3601", ".634"), 100)

    def test_small_probe_edge_still_requires_freshness_depth_and_version(self):
        for mutation in ("account_age", "book_age", "depth", "version"):
            books = no_pair_books(".360", ".635")
            started = 100
            if mutation == "account_age":
                started = 84
            elif mutation == "book_age":
                books[0]["received_at"] = 94
            elif mutation == "depth":
                books[0]["bid_quantity"] = 49
            else:
                books[0]["version"]["at"] = "2026-10-07T12:00:01Z"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                probe.observed_probe_limits(books, started)
        self.fixture.assert_no_writes()

    def test_quarantine_excludes_pair_design(self):
        with patch.object(probe.quarantine, "require_unblocked_markets", side_effect=probe.quarantine.QuarantineError("Blocked")), \
                self.assertRaises(probe.quarantine.QuarantineError):
            probe.describe_pair(["1", "2"])

    def test_confirmation_requires_manual_flag_tty_and_exact_pair_phrase(self):
        ask = Mock(return_value="yes")
        stream = Mock()
        stream.isatty.return_value = True
        with self.assertRaises(probe.ProbeBlocked):
            probe.require_manual_confirmation(self.design, False, stream, ask)
        ask.assert_not_called()
        stream.isatty.return_value = False
        with self.assertRaises(probe.ProbeBlocked):
            probe.require_manual_confirmation(self.design, True, stream, ask)
        ask.assert_not_called()
        stream.isatty.return_value = True
        with self.assertRaises(probe.ProbeBlocked):
            probe.require_manual_confirmation(self.design, True, stream, ask)
        ask.return_value = self.design["confirmation"]
        probe.require_manual_confirmation(self.design, True, stream, ask)
        self.fixture.session.get.assert_not_called()

    def test_changed_authorization_during_confirmation_blocks(self):
        def confirm(prompt):
            self.fixture.live_approval_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT", "pairs": []}))
            return self.design["confirmation"]
        stream = Mock()
        stream.isatty.return_value = True
        with self.assertRaises(live_settlement.LiveSettlementBlocked):
            probe.require_manual_confirmation(self.design, True, stream, confirm)

    def test_consistent_receipt_fills_position_balance_still_does_not_verify_fees(self):
        result = self.review()
        observations = result["observations"]
        self.assertEqual(observations["fill_notional"], "0.4")
        self.assertEqual(observations["reported_balance_debit"], "0.4")
        self.assertEqual(observations["possible_balance_debit"], ["0.38", "0.42"])
        self.assertTrue(observations["notional_consistent_with_rounded_balance"])
        self.assertEqual(result["evidence"]["placement_receipt"], self.receipt)
        self.assertEqual(result["evidence"]["order_activity"], self.activity)
        self.receipt["all"] = {"raw": "later change"}
        self.assertIsNone(result["evidence"]["placement_receipt"]["all"])

    def test_half_cent_fill_and_hidden_subcent_fee_cannot_be_distinguished(self):
        self.receipt.update(price=.335, totalCost=.335, fillPrice=.335)
        self.activity["order"]["priceLimit"] = .335
        self.activity["fills"][0]["price"] = .335
        self.after["account"]["tournament"]["myBalance"] = 19999.66
        row = self.after["account"]["positions"][0]
        row.update(costBasis=.335, marketValue=.335, unrealizedPnl=0)
        self.after["account"].update(holdings([row]))
        self.after["recent_transactions"][0]["price"] = .335
        result = self.review()
        self.assertEqual(result["observations"]["possible_balance_debit"], ["0.32", "0.36"])
        self.assertTrue(result["observations"]["notional_consistent_with_rounded_balance"])

    def test_fee_and_collateral_events_preserved_without_attributing_them_to_receipt(self):
        for kind in ("fee", "collateral"):
            transaction = {"event_id": "extra", "event_type": "fee" if kind == "fee" else "deposit",
                           "createdAt": "2026-10-07T12:00:00Z", "tournamentId": fixtures.TOURNAMENT_ID,
                           "quantity": 0, "amount": -.01 if kind == "fee" else .2,
                           "transactionType": "TRADE_FEE" if kind == "fee" else "ALL_COLLATERAL_ADVANCE"}
            self.after["recent_transactions"] = [self.trade(), transaction]
            result = self.review()
            self.assertIn("LEDGER_ATTRIBUTION_UNVERIFIED", result["issues"])
            self.assertEqual(result["evidence"]["after"]["recent_transactions"][1], transaction)
            if kind == "collateral":
                self.assertIn("COLLATERAL_CASH_EFFECT_UNVERIFIED", result["issues"])

    def test_all_receipt_economics_retained_but_never_assumed_fee_inclusive(self):
        self.receipt["all"] = {"fullNotionalCost": .4, "effectiveEntryCost": .2, "collateralSavings": .2,
                               "collateralRepayment": 0, "netBuyingPowerImpact": .2}
        result = self.review()
        self.assertEqual(result["observations"]["all_execution_economics"], self.receipt["all"])
        self.assertIn("COLLATERAL_CASH_EFFECT_UNVERIFIED", result["issues"])

    def test_unknown_or_partial_receipt_halts_and_preserves_every_supplied_field(self):
        for receipt in (None, {**self.receipt, "quantityTraded": .5}, {**self.receipt, "open": True}):
            self.receipt = receipt
            result = self.review()
            self.assertEqual(result["evidence"]["placement_receipt"], receipt)
            self.assertEqual(result["evidence"]["before"], self.before)
            self.assertEqual(result["evidence"]["after"], self.after)

    def test_balance_discrepancy_and_debit_cap_ambiguity_require_review(self):
        self.after["account"]["tournament"]["myBalance"] = 19999
        result = self.review()
        self.assertIn("BALANCE_CHANGE_NOT_EXPLAINED_BY_NOTIONAL", result["issues"])
        self.assertIn("ONE_SUSQIE_DEBIT_CAP_NOT_PROVEN", result["issues"])

    def test_unrelated_order_inventory_or_ledger_activity_is_not_silently_assigned_to_probe(self):
        self.after["recent_transactions"].append(self.trade("other", "99"))
        self.assertIn("LEDGER_ATTRIBUTION_UNVERIFIED", self.review()["issues"])
        self.after["account"]["orders"] = [{"id": 999}]
        self.assertIn("Open orders prevent isolated accounting attribution", self.review()["issues"])
        self.after["account"]["orders"] = []
        self.after["account"].update(holdings([*self.after["account"]["positions"], position("99", "9", 7)]))
        self.assertIn("Unrelated inventory changed during probe", self.review()["issues"])

    def test_stale_incomplete_or_mismatched_evidence_remains_preserved_and_halted(self):
        self.after["freshness"]["completed_monotonic"] = 118
        self.assertIn("Snapshot capture was stale", self.review()["issues"])
        self.after["freshness"]["completed_monotonic"] = 103
        self.receipt["orderId"] = 999
        self.assertIn("Missing or mismatched receipt; never retry or infer an order ID", self.review()["issues"])
        self.receipt["orderId"] = 101
        self.before["data_complete"] = False
        self.assertIn("Incomplete account evidence", self.review()["issues"])


class ManualProbeExecutionTests(unittest.TestCase):
    """Exercise the one-POST coordinator with fake exchange responses only."""
    def setUp(self):
        self.offline = AccountingProbeTests()
        self.offline.setUp()
        self.addCleanup(self.offline.doCleanups)
        self.fixture = self.offline.fixture
        self.session = self.fixture.session
        self.before, self.after = copy.deepcopy(self.offline.before), copy.deepcopy(self.offline.after)
        for snapshot in (self.before, self.after):
            snapshot["freshness"] = {"started_monotonic": 100, "completed_monotonic": 100}
        self.receipt, self.activity = copy.deepcopy(self.offline.receipt), copy.deepcopy(self.offline.activity)
        self.after["order_activity"] = [self.activity]
        self.after["recent_fills"] = [{**self.activity["fills"][0], "orderId": 101, "marketId": "1", "exchangeId": "11"}]
        self.journal = self.fixture.root / "accounting_probe.json"
        self.stream = Mock()
        self.stream.isatty.return_value = True
        self.confirm = lambda prompt: prompt.split("Type exactly: ", 1)[1].rstrip("\n")
        self.request = None
        self.expected_exchange = "11"
        self.books = [parsed_book(bid=.6), parsed_book(bid=.6)]
        for target, name, kwargs in (
                (probe, "JOURNAL_PATH", {"new": self.journal}),
                (pilot_account, "read_snapshot", {"side_effect": self.snapshot}),
                (live_pilot, "read_live_pair", {"return_value": ([{}, {}], {"NO-PAIR"}, {})}),
                (probe.scanner, "get_best_prices", {"side_effect": lambda *args: self.books[self.book_index()]})):
            p = patch.object(target, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.book_calls = 0
        self.session.get.side_effect = self.get
        self.session.post.side_effect = self.post
        self.session.headers = {"Authorization": "Bearer synthetic-secret"}

    def book_index(self):
        index = self.book_calls % 2
        self.book_calls += 1
        return index

    def snapshot(self, *args, **kwargs):
        return copy.deepcopy(self.after if self.session.post.call_count else self.before)

    def post(self, url, **kwargs):
        self.request = copy.deepcopy(kwargs["json"])
        persisted = json.loads(self.journal.read_text())
        self.assertEqual(persisted["state"], "SUBMITTING")
        self.assertEqual(persisted["request"], self.request)
        self.assertEqual(persisted["preview"]["before"]["account"], self.before["account"])
        self.assertFalse(persisted["preview"]["before"]["supplemental_default_balance"]["scope_verified"])
        self.assertEqual(persisted["preview"]["pilot_state"], self.fixture.checkpoint)
        self.assertEqual(len(persisted["preview"]["books"]), 2)
        self.assertEqual(live_pilot.load_checkpoint()["state"], live_pilot.EXECUTING)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(self.request["quantity"], 1)
        self.assertEqual(self.request["exchangeId"], self.expected_exchange)
        return self.fixture.response(self.receipt)

    def get(self, url, **kwargs):
        if url.endswith("/account"):
            snapshot = self.after if self.session.post.call_count else self.before
            return self.fixture.response({"balance": snapshot["account"]["tournament"]["myBalance"] + .005})
        order = copy.deepcopy(self.activity["order"])
        order.update(createdAt=self.offline.activity["fills"][0]["filledAt"], expirationDate=self.request["expirationDate"],
                     terminalReasonCode=None)
        if url.endswith("/orders/101"):
            return self.fixture.response(order)
        if url.endswith("/orders/101/fills"):
            fills = self.activity["fills"]
            return self.fixture.response({"orderId": 101, "exchangeId": self.request["exchangeId"], "tournamentId": fixtures.TOURNAMENT_ID,
                "data": fills, "coverage": {"complete": True, "projectedThroughSequence": 1},
                "pagination": {"hasMore": False, "nextCursor": None},
                "totalQuantityFilled": sum(r["quantity"] for r in fills), "avgFillPrice": fills[0]["price"]})
        raise AssertionError("Unexpected fake GET")

    def run_probe(self, requested=True, confirm=None, first="1"):
        with contextlib.redirect_stdout(io.StringIO()):
            return probe.execute_first_leg(self.session, ["1", "2"], first, requested, self.stream,
                                           self.confirm if confirm is None else confirm)

    def assert_halted(self, spent="0"):
        checkpoint = live_pilot.load_checkpoint()
        self.assertEqual(checkpoint["state"], live_pilot.HALTED)
        self.assertEqual(Decimal(checkpoint["confirmed_cumulative_debits"]), Decimal(spent))
        self.assertEqual(Decimal(checkpoint["allocated_cash_remaining"]), Decimal(5000) - Decimal(spent))
        self.assertFalse(config.LIVE_PILOT_SUBMISSION_ENABLED)
        self.session.delete.assert_not_called()
        self.session.put.assert_not_called()
        self.session.patch.assert_not_called()
        return checkpoint

    def test_one_post_after_durable_evidence_and_exact_confirmation_then_stop(self):
        record = self.run_probe()
        self.session.post.assert_called_once()
        self.assertEqual(record["raw_receipt"], self.receipt)
        self.assertEqual(record["state"], live_pilot.HALTED)
        self.assertEqual(record["review"]["observations"]["zero_extra_fee_model"], "MATCH_AT_REPORTED_PRECISION")
        self.assertFalse(record["review"]["accounting_verified"])
        checkpoint = self.assert_halted(".4")
        self.assertEqual(checkpoint["last_reconciled_account_cash"], "19999.6")
        exposure = checkpoint["live_exposures"][record["fingerprint"]]
        self.assertEqual(exposure["confirmed_quantities"], ["1", "0"])
        self.assertEqual(exposure["possible_additional_quantities"], ["0", "0"])
        self.assertEqual(exposure["observed_cash_debit"], "0.4")
        self.assertEqual(json.loads(self.journal.read_text())["raw_receipt"], self.receipt)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-secret", self.journal.read_text())
        self.assertEqual(len(record["reads"]), 5)
        self.assertFalse(record["review"]["observations"]["default_balance_scope_verified"])

    def test_small_edge_preview_creates_no_key_journal_or_submission(self):
        self.books = no_pair_books(".400", ".595")
        with patch.object(probe, "uuid4") as key:
            result = probe.fresh_preview(self.session, ["1", "2"], "1", self.fixture.checkpoint)
        self.assertEqual(Decimal(result["edge"]), Decimal(".005"))
        self.assertEqual(Decimal(result["pair_notional"]), Decimal(".995"))
        key.assert_not_called()
        self.session.post.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_all_three_gates_accept_probe_threshold_then_halt_after_one_fake_post(self):
        self.books = no_pair_books(".400", ".595")
        actual_gate = probe.observed_probe_limits
        confirmed, stages = False, []

        def confirm(prompt):
            nonlocal confirmed
            self.assertEqual(stages, ["preview"])
            confirmed = True
            return self.confirm(prompt)

        def checked_limits(books, started):
            stage = "pre_submission" if self.journal.exists() else (
                "after_confirmation" if confirmed else "preview")
            prices, quantity, cost, edge = actual_gate(books, started)
            self.assertEqual(quantity, 1)
            self.assertEqual(cost, Decimal(".995"))
            self.assertEqual(edge, Decimal(".005"))
            stages.append(stage)
            return prices, quantity, cost, edge

        with patch.object(probe, "observed_probe_limits", side_effect=checked_limits):
            record = self.run_probe(confirm=confirm)
        self.assertEqual(stages, ["preview", "after_confirmation", "pre_submission"])
        self.assertEqual(record["state"], live_pilot.HALTED)
        self.session.post.assert_called_once()  # Fake exchange; never a real order.
        self.assert_halted(".4")

    def test_probe_edge_lost_during_confirmation_blocks_before_key_or_post(self):
        self.books = no_pair_books(".400", ".595")

        def confirm(prompt):
            self.books[1]["bid"] = .400  # New cost is .400 + .600 = 1.000.
            return self.confirm(prompt)

        with patch.object(probe, "uuid4") as key, self.assertRaisesRegex(probe.ProbeBlocked, "0.5% probe edge"):
            self.run_probe(confirm=confirm)
        key.assert_not_called()
        self.session.post.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_final_probe_edge_recheck_blocks_before_post_and_preserves_halt(self):
        self.books = no_pair_books(".400", ".595")
        save = live_pilot.save_checkpoint

        def changed_after_state_save(checkpoint, *args, **kwargs):
            result = save(checkpoint, *args, **kwargs)
            if checkpoint["state"] == live_pilot.EXECUTING:
                self.books[1]["bid"] = .400
            return result

        with patch.object(live_pilot, "save_checkpoint", side_effect=changed_after_state_save), \
                self.assertRaisesRegex(probe.ProbeBlocked, "0.5% probe edge"):
            self.run_probe()
        self.session.post.assert_not_called()
        self.assert_halted()
        journal = json.loads(self.journal.read_text())
        self.assertFalse(journal["post_attempted"])
        self.assertEqual(journal["state"], live_pilot.HALTED)

    def test_missing_flag_non_tty_or_wrong_confirmation_never_creates_key_or_journal(self):
        for flag, tty, phrase in ((False, True, self.confirm), (True, False, self.confirm), (True, True, lambda p: "yes")):
            self.stream.isatty.return_value = tty
            with patch.object(probe, "uuid4") as key, self.assertRaises(probe.ProbeBlocked):
                self.run_probe(flag, phrase)
            key.assert_not_called()
            self.assertFalse(self.journal.exists())
        self.session.post.assert_not_called()

    def test_explicitly_selected_second_market_is_the_only_submitted_first_leg(self):
        self.expected_exchange = "12"
        self.receipt["exchangeId"] = "12"
        self.activity["order"]["exchangeId"] = "12"
        self.after["recent_fills"][0].update(marketId="2", exchangeId="12")
        self.after["recent_transactions"][0].update(marketId="2", exchangeId="12")
        self.after["account"]["positions"][0].update(marketId="2", exchangeId="12")
        record = self.run_probe(first="2")
        checkpoint = self.assert_halted(".4")
        self.assertEqual(checkpoint["live_exposures"][record["fingerprint"]]["confirmed_quantities"], ["0", "1"])
        self.session.post.assert_called_once()

    def test_default_cli_cannot_read_account_or_generate_key(self):
        with patch("sys.argv", ["supervised_accounting_probe.py"]), patch.object(probe, "uuid4") as key, \
                patch.object(probe, "load_dotenv") as env, patch.object(probe.requests, "Session") as session, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main(), 0)
        key.assert_not_called()
        env.assert_not_called()
        session.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_unlisted_or_paper_only_pair_blocks_before_account_reads(self):
        self.fixture.live_approval_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT", "pairs": []}))
        with patch.object(pilot_account, "read_snapshot") as read, self.assertRaises(live_settlement.LiveSettlementBlocked):
            self.run_probe()
        read.assert_not_called()
        self.session.post.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_quote_movement_while_human_confirms_blocks_without_submission(self):
        def confirm(prompt):
            self.books[0]["bid"] = .595
            return self.confirm(prompt)
        with self.assertRaises(probe.ProbeBlocked):
            self.run_probe(confirm=confirm)
        self.session.post.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_stale_books_or_insufficient_allocation_block_before_post(self):
        self.books[0]["received_at"] = 90
        with self.assertRaises(ValueError):
            self.run_probe()
        self.books[0]["received_at"] = 100
        self.before["account"]["tournament"]["myBalance"] = 15000.5
        with self.assertRaises(probe.ProbeBlocked):
            self.run_probe()
        self.session.post.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_settlement_revalidation_failure_latches_halt_before_post(self):
        with patch.object(live_pilot, "read_live_pair", side_effect=live_settlement.LiveSettlementBlocked("Evidence changed")), \
                self.assertRaises(live_settlement.LiveSettlementBlocked):
            self.run_probe()
        self.assert_halted()
        self.session.post.assert_not_called()

    def test_timeout_preserves_key_and_unknown_exposure_without_retry_or_charge(self):
        def timeout(url, **kwargs):
            self.post(url, **kwargs)
            raise requests.Timeout("Do not print this response")
        self.session.post.side_effect = timeout
        record = self.run_probe()
        checkpoint = self.assert_halted()
        self.session.post.assert_called_once()
        self.assertIsNone(record["raw_receipt"])
        self.assertEqual(checkpoint["live_exposures"][record["fingerprint"]]["possible_additional_quantities"], ["1", "0"])
        old_bytes = self.journal.read_bytes()
        with patch.object(probe, "uuid4") as key, self.assertRaises(ValueError):
            self.run_probe()
        key.assert_not_called()
        self.assertEqual(self.journal.read_bytes(), old_bytes)
        self.session.post.assert_called_once()

    def test_rejected_post_body_is_saved_and_never_retried(self):
        response = self.fixture.response({"error": {"code": "VALIDATION_ERROR"}})
        response.status_code = 400
        self.session.post.side_effect = lambda url, **kwargs: (self.post(url, **kwargs), response)[1]
        record = self.run_probe()
        self.assertEqual(record["placement_response"]["http_status"], 400)
        self.assertIn("VALIDATION_ERROR", record["placement_response"]["body"])
        self.assert_halted()
        self.session.post.assert_called_once()

    def test_partial_fill_remains_reserved_and_never_submits_leg_two(self):
        self.receipt.update(open=True, remainingQuantity=.5, quantityTraded=.5, totalCost=.2)
        self.activity["order"].update(open=True, quantity=.5, quantityFilled=None)
        self.activity["fills"][0]["quantity"] = -.5
        self.after["account"]["orders"] = [self.activity["order"]]
        record = self.run_probe()
        self.assertEqual(record["review"]["observations"]["order_state"], "OPEN")
        self.assertEqual(record["review"]["observations"]["fill_notional"], "0.20")
        self.assert_halted()
        self.session.post.assert_called_once()

    def test_unexplained_cash_change_preserves_holdings_without_inventing_a_debit(self):
        self.after["account"]["tournament"]["myBalance"] = 19999.3
        record = self.run_probe()
        checkpoint = self.assert_halted()
        exposure = checkpoint["live_exposures"][record["fingerprint"]]
        self.assertEqual(exposure["confirmed_quantities"], ["1", "0"])
        self.assertIsNone(exposure["observed_cash_debit"])
        self.assertEqual(checkpoint["last_reconciled_account_cash"], "20000")
        self.assertEqual(record["review"]["observations"]["zero_extra_fee_model"], "DIFFERS_OR_UNEXPLAINED")

    def test_fee_entry_kept_for_review_and_not_automatically_attributed(self):
        self.after["recent_transactions"].append({"event_id": "fee", "event_type": "fee", "amount": -.01,
            "transactionType": "TRADE_FEE", "quantity": 0, "tournamentId": fixtures.TOURNAMENT_ID,
            "createdAt": "2026-10-07T12:00:00Z"})
        self.after["account"]["tournament"]["myBalance"] = 19999.59
        record = self.run_probe()
        self.assertEqual(len(record["review"]["observations"]["fee_like_transactions"]), 1)
        self.assert_halted()

    def test_reported_cash_debit_separate_from_notional_survives_restart(self):
        self.books[0]["bid"] = .665
        self.receipt.update(price=.335, totalCost=.335, fillPrice=.335)
        self.activity["order"]["priceLimit"] = .335
        self.activity["fills"][0]["price"] = .335
        self.after["recent_fills"][0]["price"] = .335
        self.after["recent_transactions"][0]["price"] = .335
        self.after["account"]["tournament"]["myBalance"] = 19999.66
        row = self.after["account"]["positions"][0]
        row.update(costBasis=.335, marketValue=.335, currentPrice=.335)
        self.after["account"].update(holdings([row]))
        record = self.run_probe()
        checkpoint = self.assert_halted(".34")
        exposure = checkpoint["live_exposures"][record["fingerprint"]]
        self.assertEqual(exposure["confirmed_costs"], ["0.335", "0"])
        self.assertEqual(exposure["observed_cash_debit"], "0.34")
        self.assertEqual(record["review"]["observations"]["zero_extra_fee_model"], "ROUNDING_AMBIGUOUS")
        self.assertEqual(live_pilot.load_checkpoint(), checkpoint)
        changed = copy.deepcopy(checkpoint)
        changed["live_exposures"][record["fingerprint"]].pop("observed_cash_debit")
        with self.assertRaises(ValueError):
            live_pilot.save_checkpoint(changed)

    def test_write_failure_before_post_stops_without_submission(self):
        original = probe.save_journal
        def fail_once(record):
            if record["state"] == "SUBMITTING":
                raise OSError("Synthetic disk failure")
            original(record)
        with patch.object(probe, "save_journal", side_effect=fail_once), self.assertRaises(OSError):
            self.run_probe()
        self.session.post.assert_not_called()
        self.assert_halted()

    def test_malformed_receipt_preserves_raw_body_and_never_retries(self):
        response = self.fixture.response({})
        response._content = b'<invalid receipt>'
        self.session.post.side_effect = lambda url, **kwargs: (self.post(url, **kwargs), response)[1]
        record = self.run_probe()
        self.assertEqual(record["placement_response"]["body"], '<invalid receipt>')
        self.assert_halted()
        self.session.post.assert_called_once()

    def test_write_failure_after_post_preserves_response_and_never_replays(self):
        original = probe.save_journal
        def fail_once(record):
            if record["state"] == "SUBMITTING" and record["placement_response"] is not None:
                raise OSError("Synthetic disk failure")
            original(record)
        with patch.object(probe, "save_journal", side_effect=fail_once), self.assertRaises(OSError):
            self.run_probe()
        self.session.post.assert_called_once()
        record = json.loads(self.journal.read_text())
        self.assertEqual(json.loads(record["placement_response"]["body"]), self.receipt)
        self.assert_halted()

    def test_unavailable_after_snapshot_preserves_before_receipt_and_halt(self):
        def snapshot(*args, **kwargs):
            if self.session.post.call_count:
                raise pilot_account.AccountReadinessBlocked(pilot_account.RECONCILIATION_UNAVAILABLE, "Unavailable")
            return copy.deepcopy(self.before)
        with patch.object(pilot_account, "read_snapshot", side_effect=snapshot):
            record = self.run_probe()
        self.assertEqual(record["raw_receipt"], self.receipt)
        self.assertEqual(record["review"]["observations"]["balance_before"], "20000")
        self.assert_halted()
        self.session.post.assert_called_once()

    def test_missing_new_portfolio_fill_or_unrelated_fill_prevents_debit_attribution(self):
        self.after["recent_fills"] = []
        record = self.run_probe()
        self.assertIn("UNRELATED_OR_INCOMPLETE_FILL_ACTIVITY", record["review"]["issues"])
        self.assert_halted()

    def test_normal_live_enable_flag_does_not_bypass_manual_probe_requirements(self):
        with patch.object(config, "LIVE_PILOT_SUBMISSION_ENABLED", True), self.assertRaises(probe.ProbeBlocked):
            self.run_probe()
        self.session.post.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_interrupt_after_post_leaves_halt_and_reserved_possible_fill(self):
        def interrupt(url, **kwargs):
            self.post(url, **kwargs)
            raise KeyboardInterrupt()
        self.session.post.side_effect = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_probe()
        self.assert_halted()
        self.session.post.assert_called_once()
        self.assertEqual(json.loads(self.journal.read_text())["state"], live_pilot.HALTED)

    def test_credential_reflection_is_redacted_and_cannot_become_raw_receipt(self):
        self.receipt["unexpected_echo"] = "synthetic-secret"
        record = self.run_probe()
        self.assertTrue(record["placement_response"]["credential_redacted"])
        self.assertNotIn("synthetic-secret", self.journal.read_text())
        self.assertIsNone(record["raw_receipt"])
        self.assert_halted()


if __name__ == "__main__":
    unittest.main()
