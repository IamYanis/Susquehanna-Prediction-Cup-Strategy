"""Real account-readiness gates with fake GETs and isolated temporary state."""
import contextlib
import copy
import io
import json
import unittest
from datetime import datetime, timezone
from urllib.parse import urlparse
from unittest.mock import patch

import requests

import live_pilot as pilot
import pilot_account as account
import test_live_pilot as fixtures
from test_account_reader import holdings, order, orders_page, position
from test_order_preview import account_snapshot


def history(rows=None, more=False, cursor=None, transactions=False):
    coverage = {"complete": True}
    if not transactions:
        coverage["projectedThroughSequence"] = 12
    return {"data": [] if rows is None else rows, "coverage": coverage,
            "pagination": {"hasMore": more, "nextCursor": cursor}}


class PilotAccountTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LivePilotTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        # Exercise the actual new gate here, unlike existing component tests.
        self.fixture.account_gate_patch.stop()
        self.fixture.accounting_gate_patch.stop()
        self.session = self.fixture.session
        self.checkpoint = self.fixture.checkpoint
        self.current = copy.deepcopy(self.fixture.account)
        self.fills, self.transactions, self.receipts = [], [], {}
        self.failed_path = None
        self.session.get.side_effect = self.get
        self.stamp = datetime.now(timezone.utc).isoformat()

    def get(self, url, **kwargs):
        path = urlparse(url).path.removeprefix("/api/v1")
        if path == self.failed_path:
            raise requests.Timeout("Synthetic timeout must not be printed")
        if path == "/tournaments/midterm-elections":
            payload = self.current["tournament"]
        elif path.endswith("/portfolio/positions"):
            payload = {"positions": self.current["positions"], "summary": self.current["summary"]}
        elif path == "/orders":
            payload = orders_page(self.current["orders"], coverage={"complete": True, "projectedThroughSequence": 12})
        elif path.endswith("/portfolio/fills"):
            payload = history(self.fills)
        elif path.endswith("/portfolio/transactions"):
            payload = history(self.transactions, transactions=True)
        elif path.endswith("/fills"):
            oid = int(path.split("/")[-2])
            payload = self.receipts[oid][1]
        else:
            oid = int(path.split("/")[-1])
            payload = self.receipts[oid][0]
        return self.fixture.response(payload)

    def snapshot(self):
        result = account.read_snapshot(self.session, checkpoint=self.checkpoint)
        self.fixture.assert_no_writes()
        return result

    def assess(self, capital=.8):
        return account.assess_snapshot(self.snapshot(), self.checkpoint, ["11", "12"], capital)

    def codes(self, result):
        return [failure["code"] for failure in result["failures"]]

    def add_activity(self):
        self.fills = [{"id": 501, "orderId": 301, "exchangeId": "99", "marketId": "9", "side": "no",
                       "quantity": -.5, "price": .4, "filledAt": self.stamp}]
        self.transactions = [{"event_id": "engine-501", "event_type": "trade", "createdAt": self.stamp,
                              "tournamentId": self.checkpoint["tournament_id"], "exchangeId": "99", "marketId": "9",
                              "price": .4, "quantity": -.5, "amount": None, "transactionType": None}]
        receipt = order(301, "99")
        receipt.update(side="no", quantity=.5, open=False, quantityFilled=.5)
        fills = history([{key: value for key, value in self.fills[0].items()
                          if key not in {"orderId", "marketId", "exchangeId"}}])
        fills.update(orderId=301, exchangeId="99", tournamentId=self.checkpoint["tournament_id"],
                     totalQuantityFilled=-.5, avgFillPrice=.4)
        self.receipts[301] = receipt, fills
        holding = position("99", "9", -.5)
        holding.update(costBasis=.2, marketValue=.2, unrealizedPnl=0, currentPrice=.4)
        self.current.update(holdings([holding]))

    def test_healthy_snapshot_includes_receipts_history_and_freshness(self):
        self.add_activity()
        snapshot = self.snapshot()
        self.assertTrue(snapshot["data_complete"])
        self.assertEqual(snapshot["account"]["tournament"]["myBalance"], 20000)
        self.assertEqual(snapshot["recent_fills"], self.fills)
        self.assertEqual(snapshot["recent_transactions"], self.transactions)
        self.assertEqual(snapshot["order_activity"][0]["fill_notional"], "0.20")
        self.assertEqual(snapshot["freshness"]["started_monotonic"], 100)
        result = account.assess_snapshot(snapshot, self.checkpoint, ["11", "12"], .8)
        self.assertTrue(result["account_checks_passed"])
        self.assertEqual(self.codes(result), [account.ACCOUNTING_UNVERIFIED])
        paths = [urlparse(c.args[0]).path for c in self.session.get.call_args_list]
        self.assertIn("/api/v1/orders/301", paths)
        self.assertIn("/api/v1/orders/301/fills", paths)
        for call in self.session.get.call_args_list:
            if call.args[0].endswith("/transactions"):
                self.assertNotIn("type", call.kwargs["params"])

    def test_insufficient_actual_balance_blocks_even_with_saved_allocation(self):
        self.current["tournament"]["myBalance"] = 15000.5
        result = self.assess()
        self.assertFalse(result["ready"])
        self.assertIn(account.INSUFFICIENT_BALANCE, self.codes(result))

    def test_extra_cash_never_enlarges_the_fixed_allocation(self):
        for cash in (5000, 20000, 1000000):
            self.current = account_snapshot(cash=cash)
            self.checkpoint = pilot.allocation_checkpoint(self.current)
            result = self.assess()
            self.assertEqual(result["risk"]["live_allocation"], 5000)
            self.assertEqual(result["risk"]["allocation_remaining_after_reserves"], 5000)
            self.assertEqual(result["risk"]["untouchable_cash_reserve"], cash - 5000)
        self.current["tournament"]["myBalance"] += 1000
        self.assertIn(account.INCONSISTENT, self.codes(self.assess()))

    def test_unexpected_position_counts_toward_race_and_total_exposure(self):
        self.current.update(holdings([position("99", "9", 7)]))
        result = self.assess()
        self.assertEqual(result["risk"]["existing_holdings_cost_basis"], 3.5)
        self.assertEqual(result["risk"]["total_exposure_after"], 4.3)
        self.current["positions"][0]["costBasis"] = 100
        self.current.update(holdings(self.current["positions"]))
        self.assertIn("LIVE_ACCOUNT_RISK_FAILED", self.codes(self.assess()))

    def test_external_open_order_reserves_cash_and_own_pair_overlap_blocks(self):
        pending = order(401, "99")
        pending.update(quantityFilled=None)
        self.current["orders"] = [pending]
        fills = history()
        fills.update(orderId=401, exchangeId="99", tournamentId=self.checkpoint["tournament_id"],
                     totalQuantityFilled=0, avgFillPrice=None)
        self.receipts[401] = pending, fills
        result = self.assess()
        self.assertEqual(result["risk"]["allocation_remaining_after_reserves"], 4998)
        self.assertEqual(result["risk"]["total_exposure_after"], 2.8)
        pending["exchangeId"] = "11"
        fills["exchangeId"] = "11"
        self.assertIn("LIVE_ACCOUNT_RISK_FAILED", self.codes(self.assess()))

    def test_stale_snapshot_and_slow_snapshot_read_are_blocked(self):
        snapshot = self.snapshot()
        with patch.object(account.time, "monotonic", return_value=116), self.assertRaises(account.AccountReadinessBlocked) as caught:
            account.assess_snapshot(snapshot, self.checkpoint, ["11", "12"], .8)
        self.assertEqual(caught.exception.code, account.STALE)
        with self.assertRaises(account.AccountReadinessBlocked) as caught:
            account.read_snapshot(self.session, checkpoint=self.checkpoint, started=84)
        self.assertEqual(caught.exception.code, account.STALE)

    def test_cash_or_scope_mismatch_with_durable_state_blocks(self):
        self.current["tournament"]["myBalance"] -= .4
        self.assertIn(account.INCONSISTENT, self.codes(self.assess()))
        self.current["tournament"]["myBalance"] += .4
        self.checkpoint = copy.deepcopy(self.checkpoint)
        self.checkpoint["tournament_id"] = "550e8400-e29b-41d4-a716-446655440001"
        self.assertIn(account.INCONSISTENT, self.codes(self.assess()))

    def test_missing_persisted_holdings_and_orphan_debits_are_inconsistent(self):
        self.checkpoint = self.fixture.consumed_checkpoint(.8)
        self.current["tournament"]["myBalance"] = 19999.2
        self.assertIn(account.INCONSISTENT, self.codes(self.assess()))
        pair, legs = self.fixture.synthetic_pair()
        for leg in legs:
            leg["state"] = "OBSERVED_TERMINAL"
            leg["observation"].update(filled_quantity=1, filled_cost=.4, open=False, terminal_reason=None)
        self.checkpoint = pilot.checkpoint_with_exposure(self.fixture.checkpoint, pair, legs, "RECONCILED_PAIR")
        self.checkpoint["last_reconciled_account_cash"] = "19999.2"
        self.assertIn(account.INCONSISTENT, self.codes(self.assess()))

    def test_no_fee_rows_is_not_a_verified_zero_fee_model(self):
        result = self.assess()
        self.assertFalse(result["ready"])
        self.assertFalse(result["accounting"]["verified"])
        self.assertIsNone(result["accounting"]["confirmed_pilot_debit_field"])
        self.assertEqual(result["accounting"]["observed_fee_events"], 0)
        with self.assertRaises(account.AccountReadinessBlocked) as caught:
            account.require_ready(result)
        self.assertEqual(caught.exception.code, account.ACCOUNTING_UNVERIFIED)

    def test_fee_and_collateral_events_are_retained_without_guessing_cash_debit(self):
        self.transactions = [{"event_id": "fee-1", "event_type": "fee", "createdAt": self.stamp,
                              "tournamentId": self.checkpoint["tournament_id"], "amount": -.02, "quantity": 0},
                             {"event_id": "collateral-1", "event_type": "deposit", "createdAt": self.stamp,
                              "tournamentId": self.checkpoint["tournament_id"], "amount": .65, "quantity": 0,
                              "transactionType": "ALL_COLLATERAL_ADVANCE"}]
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["recent_transactions"]), 2)
        model = account.accounting_model(snapshot)
        self.assertEqual(model["observed_fee_events"], 1)
        self.assertEqual(model["status"], account.ACCOUNTING_UNVERIFIED)

    def test_each_unavailable_endpoint_has_a_specific_failure_code(self):
        for suffix, code in (("/tournaments/midterm-elections", account.BALANCE_UNAVAILABLE),
                             ("/tournaments/midterm-elections/portfolio/positions", account.POSITIONS_UNAVAILABLE),
                             ("/orders", account.ORDERS_UNAVAILABLE),
                             ("/tournaments/midterm-elections/portfolio/fills", account.RECONCILIATION_UNAVAILABLE),
                             ("/tournaments/midterm-elections/portfolio/transactions", account.RECONCILIATION_UNAVAILABLE)):
            self.failed_path = suffix
            with self.subTest(path=suffix), self.assertRaises(account.AccountReadinessBlocked) as caught:
                self.snapshot()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("Synthetic timeout", str(caught.exception))

    def test_incomplete_history_and_repeated_cursor_fail_closed(self):
        original_get = self.get
        bad = history(more=True, cursor="same")
        bad["data"] = []
        def get(url, **kwargs):
            return self.fixture.response(bad) if url.endswith("/portfolio/transactions") else original_get(url, **kwargs)
        self.session.get.side_effect = get
        for complete in (False, True):
            bad["coverage"]["complete"] = complete
            with self.assertRaises(account.AccountReadinessBlocked) as caught:
                self.snapshot()
            self.assertEqual(caught.exception.code, account.RECONCILIATION_UNAVAILABLE)

    def test_fill_scope_sign_totals_and_missing_receipts_block(self):
        for kind in ("scope", "side", "total", "no-receipt"):
            self.add_activity()
            receipt, fills = self.receipts[301]
            if kind == "scope":
                fills["tournamentId"] = "550e8400-e29b-41d4-a716-446655440001"
            elif kind == "side":
                fills["data"][0]["quantity"] = .5
            elif kind == "total":
                fills["totalQuantityFilled"] = -.6
            else:
                self.fills[0]["orderId"] = None
            with self.subTest(kind=kind), self.assertRaises(account.AccountReadinessBlocked) as caught:
                self.snapshot()
            self.assertEqual(caught.exception.code, account.RECONCILIATION_UNAVAILABLE)

    def test_moving_account_or_activity_is_inconsistent(self):
        original_get, reads = self.get, 0
        def changing(url, **kwargs):
            nonlocal reads
            if url.endswith("/tournaments/midterm-elections"):
                reads += 1
                if reads == 2:
                    self.current["tournament"]["myBalance"] += .01
            return original_get(url, **kwargs)
        self.session.get.side_effect = changing
        with self.assertRaises(account.AccountReadinessBlocked) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.code, account.INCONSISTENT)

    def test_429_fails_without_retry_and_without_exchange_writes(self):
        original_get = self.get
        def limited(url, **kwargs):
            if url.endswith("/portfolio/transactions"):
                response = self.fixture.response({})
                response.status_code = 429
                response.headers["Retry-After"] = "60"
                return response
            return original_get(url, **kwargs)
        self.session.get.side_effect = limited
        with self.assertRaises(account.AccountReadinessBlocked) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.code, account.RECONCILIATION_UNAVAILABLE)
        self.assertEqual(sum(c.args[0].endswith("/transactions") for c in self.session.get.call_args_list), 1)

    def test_real_candidate_gate_blocks_unverified_accounting_and_persists_halt(self):
        candidate = self.fixture.candidate_payloads()
        responses = [self.fixture.response(p) for p in candidate]
        original_get = self.get
        def get(url, **kwargs):
            return responses.pop(0) if responses else original_get(url, **kwargs)
        self.session.get.side_effect = get
        with patch.object(pilot.single, "prepare_test") as prepare, patch.object(pilot.single, "submit_test") as submit:
            with self.assertRaises(account.AccountReadinessBlocked) as caught:
                pilot.audit_candidate(self.session, ["1", "2"], self.checkpoint)
        self.assertEqual(caught.exception.code, account.ACCOUNTING_UNVERIFIED)
        saved = pilot.load_checkpoint()
        self.assertEqual(saved["state"], pilot.HALTED)
        self.assertTrue(saved["review_reason"].startswith(account.ACCOUNTING_UNVERIFIED))
        self.assertEqual(saved["confirmed_cumulative_debits"], "0")
        prepare.assert_not_called()
        submit.assert_not_called()
        self.fixture.assert_no_writes()
        self.session.get.reset_mock()
        with self.assertRaises(pilot.PilotBlocked):
            pilot.audit_candidate(self.session, ["1", "2"], saved)
        self.session.get.assert_not_called()

    def test_read_only_cli_never_initializes_a_missing_baseline(self):
        self.fixture.root.joinpath("allocation.json").unlink()
        with patch("sys.argv", ["pilot_account.py"]), patch.object(account, "load_dotenv"), \
                patch.object(account.os, "getenv", return_value="fake-key"), \
                patch.object(account.requests, "Session") as factory, contextlib.redirect_stdout(io.StringIO()) as output:
            factory.return_value.__enter__.return_value = self.session
            self.assertEqual(account.main(), 1)
        self.assertIn(account.ACCOUNTING_UNVERIFIED, output.getvalue())
        self.assertIn("not initialized", output.getvalue())
        self.assertFalse(pilot.ALLOCATION_PATH.exists())
        self.fixture.assert_no_writes()

    def test_reconciliation_cannot_confirm_all_in_debit_under_an_unverified_model(self):
        pair, legs = self.fixture.synthetic_pair()
        actual = self.fixture.venue_observations(legs, (1, 1))
        with patch.object(pilot.paired, "read_account", return_value=actual):
            result = pilot.reconcile_pilot(self.session, pair, legs, self.checkpoint)
        self.assertEqual(result["status"], "MANUAL_REVIEW")
        saved = pilot.load_checkpoint()
        self.assertEqual(saved["state"], pilot.HALTED)
        self.assertTrue(saved["review_reason"].startswith(account.ACCOUNTING_UNVERIFIED))
        # Observed inventory/notional cannot disappear, but unverified cash
        # accounting does not advance the last authoritative cash baseline.
        self.assertEqual(saved["last_reconciled_account_cash"], self.checkpoint["last_reconciled_account_cash"])
        self.assertEqual(next(iter(saved["live_exposures"].values()))["confirmed_quantities"], ["1", "1"])
        self.assertEqual(saved["calculated_remaining_allocation"], "4999.2")
        self.fixture.assert_no_writes()

    def test_second_leg_also_requires_the_actual_account_and_accounting_gate(self):
        pair, legs = self.fixture.synthetic_pair((True, False))
        actual = self.fixture.venue_observations(legs, (1, 0))
        actual["quarantine_reserve"] = 0
        with pilot.pilot_lock():
            # Construct the older reconciled first-leg scenario in temporary
            # state only. The actual second-leg/account gates remain unmocked.
            with patch.object(pilot.paired, "read_account", return_value=actual), \
                    patch.object(account, "require_verified_accounting"):
                result = pilot.reconcile_pilot(self.session, pair, legs, self.checkpoint)
            self.current = actual
            responses = [self.fixture.response(p) for p in self.fixture.candidate_payloads()[3:]]
            original_get = self.get
            self.session.get.side_effect = lambda url, **kw: responses.pop(0) if responses else original_get(url, **kw)
            with self.assertRaises(account.AccountReadinessBlocked) as caught:
                pilot.consider_second_leg(self.session, pair, result, manually_supervised=True, restarted=False)
            self.assertEqual(caught.exception.code, account.ACCOUNTING_UNVERIFIED)
            self.assertTrue(pilot.load_checkpoint()["review_reason"].startswith(account.ACCOUNTING_UNVERIFIED))
        self.fixture.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
