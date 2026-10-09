"""Read-only watcher checks with fake GETs and disposable pilot/approval files."""
import contextlib
import copy
import fcntl
import io
import json
import unittest
from decimal import Decimal
from urllib.parse import urlparse
from unittest.mock import Mock, patch

import requests

import config
import live_pilot as pilot
import live_settlement as live
import paired_account_test as paired
import pilot_account
import price_reader as scanner
import senate_readiness_watcher as watcher
import supervised_accounting_probe as probe
import test_pilot_account as fixtures
from test_account_reader import holdings, order, position
from test_api_audit import TOURNAMENT_ID, approved_pair, election_tree, exchange_book, party_market


class ReadinessWatcherTests(unittest.TestCase):
    def setUp(self):
        # Reuse the complete account/receipt fake rather than bypass account
        # readiness. All state and authorization files are temporary.
        self.fixture = fixtures.PilotAccountTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.state_fixture = self.fixture.fixture
        self.session = self.fixture.session
        patcher = patch.object(paired, "MANUAL_TOURNAMENT", TOURNAMENT_ID)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.markets, self.nodes = {}, {}
        self.books = {"842": exchange_book(153, 842, bid=.64, ask=.65),
                      "843": exchange_book(154, 843, bid=.365, ask=.37)}
        for party, mid, eid in (("Democratic", "153", "842"), ("Republican", "154", "843")):
            market = party_market(party, mid, eid)
            market["title"] = f"Will the {party} Party win the U.S. Senate?"
            self.markets[mid] = market
            tree = election_tree(party, mid)
            tree["root"].update(contract_type="Freeform", contract_details={
                "description": paired.manual_rule_description(party), "contractType": "Freeform"})
            self.nodes[mid] = tree
        self.session.get.side_effect = self.get
        _, record = paired.read_manual_evidence(self.session, TOURNAMENT_ID, "153", "154")
        self.paper = approved_pair(TOURNAMENT_ID, watcher.MARKETS, watcher.EXCHANGES)
        self.paper.update(pair_name="U.S. Senate", position_types=["NO-PAIR"], max_quantity=1,
                          manual_approval={key: record[key] for key in
                                           ("evidence_hash", "proposition", "source_refs", "limitations")})
        self.paper_path = self.state_fixture.approval_path
        self.live_path = self.state_fixture.live_approval_path
        self.write_paper()
        self.live_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT", "pairs": []}))
        self.session.get.reset_mock()
        # Fail the test if the watcher ever reaches a mutating readiness or
        # execution helper, even if that helper would not submit in this test.
        for target, name in ((pilot, "pilot_lock"), (pilot, "load_checkpoint"), (pilot, "save_checkpoint"),
                             (pilot, "audit_candidate"), (probe, "fresh_preview"),
                             (probe, "save_journal"), (probe, "execute_first_leg")):
            patcher = patch.object(target, name, side_effect=AssertionError("Watcher reached a mutation/execution path"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_paper(self):
        self.paper_path.write_text(json.dumps({"version": 1, "allowed_mode": "paper-only", "pairs": [self.paper]}))

    def get(self, url, **kwargs):
        path = urlparse(url).path.removeprefix("/api/v1")
        if path == "/account":
            payload = {"balance": self.fixture.current["tournament"]["myBalance"]}
        elif path.startswith("/markets/"):
            mid = path.split("/")[2]
            payload = self.nodes[mid] if path.endswith("/nodes") else self.markets[mid]
        elif path.startswith("/exchanges/"):
            payload = self.books[path.split("/")[2]]
        else:
            return self.fixture.get(url, **kwargs)
        return self.state_fixture.response(payload)

    def check(self):
        paths = [pilot.ALLOCATION_PATH, self.paper_path, self.live_path]
        before = {path: path.read_bytes() for path in paths}
        existing = set(self.state_fixture.root.iterdir())
        result = watcher.check_readiness(watcher.GetOnly(self.session))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(set(self.state_fixture.root.iterdir()), existing)
        self.state_fixture.assert_no_writes()
        self.assertFalse(result["submission_enabled"])
        return result

    def ready_observation(self):
        result = self.check()
        self.assertEqual(result["status"], watcher.READY, result)
        return result

    def test_complete_ready_review_with_no_live_authorization_and_no_writes(self):
        result = self.ready_observation()
        self.assertTrue(result["all_non_authorization_gates_passed"])
        self.assertEqual(result["authorization"], "PENDING")
        self.assertEqual(result["quote"]["prices"], ["0.360", "0.635"])
        self.assertEqual(Decimal(result["quote"]["pair_notional"]), Decimal(".995"))
        self.assertEqual(Decimal(result["quote"]["ordinary_edge"]), Decimal(".005"))
        self.assertEqual(result["quote"]["quantity_per_leg"], 1)
        self.assertEqual(result["quote"]["depth"], [100, 100])
        self.assertEqual(result["allocation_remaining"], 5000)
        self.assertEqual(live.load_authorizations(), [])
        self.assertFalse(config.LIVE_PILOT_SUBMISSION_ENABLED)
        paths = [urlparse(call.args[0]).path for call in self.session.get.call_args_list]
        self.assertIn("/api/v1/tournaments/midterm-elections/portfolio/transactions", paths)
        self.assertIn("/api/v1/tournaments/midterm-elections/portfolio/fills", paths)
        self.assertIn("/api/v1/orders", paths)
        for call in self.session.get.call_args_list:
            self.assertIs(call.kwargs["allow_redirects"], False)

    def test_below_threshold_zero_and_negative_edge_fail(self):
        for bid in (.36, .35, .34):
            self.books["843"] = exchange_book(154, 843, bid=bid, ask=bid + .01)
            with self.subTest(bid=bid):
                result = self.check()
                self.assertEqual(result["status"], watcher.NOT_READY)
                self.assertEqual(result["code"], "PROBE_EDGE_NOT_READY")
                self.assertIsNotNone(result["quote"])

    def test_current_paper_evidence_and_market_mappings_are_revalidated(self):
        original = copy.deepcopy(self.nodes["153"])
        self.nodes["153"]["root"]["settlement_date"] = "2026-12-02T00:00:00Z"
        self.assertEqual(self.check()["code"], live.NEEDS_REVALIDATION)
        self.nodes["153"] = original
        self.markets["154"]["exchanges"][0]["id"] = "999"
        self.assertEqual(self.check()["code"], live.NEEDS_REVALIDATION)

    def test_missing_or_changed_review_is_not_inferred_from_titles(self):
        self.paper["manual_approval"]["evidence_hash"] = "a" * 64
        self.write_paper()
        self.assertEqual(self.check()["code"], live.NEEDS_REVALIDATION)
        self.paper_path.write_text(json.dumps({"version": 1, "allowed_mode": "paper-only", "pairs": []}))
        self.assertEqual(self.check()["code"], live.NEEDS_REVALIDATION)

    def test_changed_cash_or_duplicate_holding_withdraws_ready(self):
        self.ready_observation()
        self.fixture.current["tournament"]["myBalance"] -= .4
        self.assertEqual(self.check()["code"], pilot_account.INCONSISTENT)
        self.fixture.current["tournament"]["myBalance"] += .4
        self.fixture.current.update(holdings([position("842", "153", -7)]))
        self.assertEqual(self.check()["code"], "LIVE_ACCOUNT_RISK_FAILED")

    def test_stale_book_or_insufficient_depth_withdraws_ready(self):
        self.books["842"]["asOf"]["at"] = "2026-10-07T11:59:00Z"
        self.assertEqual(self.check()["status"], watcher.NOT_READY)
        self.books["842"] = exchange_book(153, 842, bid=.64, ask=.65)
        self.books["842"]["bids"][0]["quantity"] = 49
        self.assertEqual(self.check()["code"], "QUOTE_CHECK_FAILED")

    def test_account_failure_withdraws_ready_without_repeating_http_error(self):
        self.ready_observation()
        self.fixture.failed_path = "/tournaments/midterm-elections/portfolio/positions"
        result = self.check()
        self.assertEqual(result["code"], pilot_account.POSITIONS_UNAVAILABLE)
        self.assertNotIn("Synthetic timeout", str(result))

    def test_unrelated_open_order_blocks_probe_isolation(self):
        self.fixture.current["orders"] = [order(401, "99")]
        fills = fixtures.history()
        fills.update(orderId=401, exchangeId="99", tournamentId=TOURNAMENT_ID,
                     totalQuantityFilled=0, avgFillPrice=None)
        self.fixture.receipts[401] = self.fixture.current["orders"][0], fills
        self.assertEqual(self.check()["code"], "OPEN_ORDERS_PREVENT_ISOLATION")

    def test_frozen_quarantine_reserve_is_still_subtracted(self):
        exposure = {"market_id": "387", "exchange_id": "1076", "tournament_id": TOURNAMENT_ID,
                    "reserved_cost": .125}
        with patch.object(watcher.quarantine, "load_quarantine", return_value=exposure):
            self.assertEqual(self.ready_observation()["allocation_remaining"], 4999.875)

    def test_account_snapshot_expiring_before_quotes_cannot_be_ready(self):
        original = scanner.get_best_prices
        # Account capture starts at 100, then expires during the book read.
        with patch.object(watcher.time, "monotonic", return_value=100) as clock:
            def slow_book(*args, **kwargs):
                result = original(*args, **kwargs)
                if args[1]["id"] == "154":
                    clock.return_value = 116
                return result
            with patch.object(scanner, "get_best_prices", side_effect=slow_book):
                result = self.check()
            self.assertEqual(result["code"], "QUOTE_CHECK_FAILED")
            self.assertIn("Account observations are too old", result["reason"])

    def test_rate_limit_withdraws_ready_and_later_cycle_respects_cooldown(self):
        self.ready_observation()
        response = self.state_fixture.response({})
        response.status_code = 429
        response.headers["Retry-After"] = "60"
        self.session.get.side_effect = [response]
        self.session.get.reset_mock()
        self.assertEqual(self.check()["code"], "API_RATE_LIMITED")
        self.assertEqual(self.check()["code"], "API_RATE_LIMITED")
        self.assertEqual(self.session.get.call_count, 1)

    def test_executing_and_halted_state_report_without_mutating_recovery(self):
        original = self.state_fixture.checkpoint
        for state in (pilot.EXECUTING, pilot.HALTED):
            cp = copy.deepcopy(original)
            cp["state"] = state
            if state == pilot.HALTED:
                cp.update(manual_review_required=True, review_reason="Review previous uncertain response")
            pilot.validate_checkpoint(cp)
            pilot.ALLOCATION_PATH.write_text(json.dumps(cp))
            with self.subTest(state=state):
                self.assertEqual(self.check()["code"], "BASELINE_ISSUE")
                self.session.get.assert_not_called()
                self.assertEqual(json.loads(pilot.ALLOCATION_PATH.read_text())["state"], state)

    def test_corrupt_baseline_and_existing_probe_evidence_fail_closed(self):
        initial = pilot.ALLOCATION_PATH.read_bytes()
        pilot.ALLOCATION_PATH.write_text("{corrupt")
        self.assertEqual(self.check()["code"], "BASELINE_ISSUE")
        pilot.ALLOCATION_PATH.write_bytes(initial)
        pilot.ALLOCATION_PATH.with_name("accounting_probe.json").write_text('{"state":"UNKNOWN"}')
        self.assertEqual(self.check()["code"], "BASELINE_ISSUE")
        self.session.get.assert_not_called()

    def test_existing_lock_is_respected_and_released(self):
        path = pilot.ALLOCATION_PATH
        with path.with_name("." + path.name + ".lock").open("r") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.check()["code"], "BASELINE_LOCK_BUSY")
            self.session.get.assert_not_called()
        self.ready_observation()
        with watcher.readonly_pilot_lock():
            pass  # The completed observation released its lock.

    def test_missing_state_or_lock_is_not_created(self):
        path = pilot.ALLOCATION_PATH
        lock = path.with_name("." + path.name + ".lock")
        for missing in (path, lock):
            saved = missing.read_bytes()
            missing.unlink()
            existing = set(self.state_fixture.root.iterdir())
            result = watcher.check_readiness(watcher.GetOnly(self.session))
            self.assertEqual(result["code"], "BASELINE_ISSUE")
            self.assertEqual(set(self.state_fixture.root.iterdir()), existing)
            missing.write_bytes(saved)
        self.session.get.assert_not_called()

    def test_existing_live_entry_is_revalidated_without_auto_halt_write(self):
        approval = {"approval_version": 1, "pair_name": "U.S. Senate", "tournament_id": TOURNAMENT_ID,
                    "tournament_slug": watcher.SLUG, "market_ids": watcher.MARKETS, "exchange_ids": watcher.EXCHANGES,
                    "relationship_type": "mutually_exclusive", "position_types": ["NO-PAIR"], "max_quantity": 1,
                    "execution_mode": live.SUPERVISED_MODE, "verification_route": live.MANUAL,
                    "settlement_rationale": self.paper["manual_approval"]["proposition"],
                    "evidence_hash": self.paper["manual_approval"]["evidence_hash"],
                    "source_refs": self.paper["manual_approval"]["source_refs"],
                    "limitations": self.paper["manual_approval"]["limitations"],
                    "approved_at": "2026-10-07T12:00:00+00:00"}
        self.live_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT", "pairs": [approval]}))
        self.assertEqual(self.ready_observation()["authorization"], "EXPLICITLY_CONFIGURED")
        approval["evidence_hash"] = "a" * 64
        self.live_path.write_text(json.dumps({"version": 1, "allowed_mode": "LIVE_PILOT", "pairs": [approval]}))
        self.assertEqual(self.check()["code"], live.NEEDS_REVALIDATION)

    def test_configuration_edit_during_reads_invalidates_cycle(self):
        original = self.get
        def changing(url, **kwargs):
            if "/exchanges/843/" in url:
                self.paper["approval_note"] += " Changed during observation."
                self.write_paper()
            return original(url, **kwargs)
        self.session.get.side_effect = changing
        # Here the deliberate external edit is the expected changed file;
        # the watcher still must not write state or LIVE authorization.
        state_before = pilot.ALLOCATION_PATH.read_bytes()
        live_before = self.live_path.read_bytes()
        result = watcher.check_readiness(watcher.GetOnly(self.session))
        self.assertEqual(result["code"], live.NEEDS_REVALIDATION)
        self.assertEqual(pilot.ALLOCATION_PATH.read_bytes(), state_before)
        self.assertEqual(self.live_path.read_bytes(), live_before)
        self.state_fixture.assert_no_writes()

    def test_read_only_transport_has_no_write_methods_and_rejects_redirects_or_wrong_host(self):
        session = watcher.GetOnly(self.session)
        for method in ("post", "delete", "put", "patch"):
            self.assertFalse(hasattr(session, method))
        for url, kwargs in (("https://sig.thesuper.market/api/v1/orders", {"allow_redirects": True}),
                            ("https://evil.test/api/v1/orders", {}),
                            ("http://sig.thesuper.market/api/v1/orders", {})):
            with self.assertRaises(ValueError):
                session.get(url, **kwargs)
        self.session.get.assert_not_called()


class WatcherReportingTests(unittest.TestCase):
    def ready(self):
        return {"status": watcher.READY, "observed_at": "2026-10-09T01:00:00+00:00", "evidence_hash": "a" * 64,
                "authorization": "PENDING", "quote": {"prices": ["0.360", "0.635"], "pair_notional": "0.995",
                "ordinary_edge": "0.005", "return_on_capital": str(Decimal(".005") / Decimal(".995")),
                "depth": [100, 150]}}

    def report(self, observations):
        output = io.StringIO()
        previous = None
        with contextlib.redirect_stdout(output):
            for observation in observations:
                previous = watcher.report_change(previous, observation)
        return output.getvalue()

    def test_not_ready_ready_and_ready_not_ready_transitions_are_reported(self):
        blocked = watcher.blocked("PROBE_EDGE_NOT_READY", "Edge too low")
        text = self.report([blocked, self.ready(), blocked])
        self.assertEqual(text.count("| NOT READY"), 2)
        self.assertEqual(text.count("| READY"), 1)
        self.assertIn("0.360 / 0.635", text)
        self.assertIn("depth 100 / 150", text)
        self.assertIn("Every non-authorization", text)
        self.assertIn("2026-10-09T01:00:00+00:00", text)

    def test_repeated_ready_suppresses_even_when_quote_and_time_change(self):
        first, second = self.ready(), self.ready()
        second["observed_at"] = "2026-10-09T01:00:15+00:00"
        second["quote"].update(prices=["0.355", "0.635"], pair_notional="0.990", ordinary_edge="0.010")
        self.assertEqual(self.report([first, second, second]).count("| READY"), 1)

    def test_repeated_not_ready_suppresses_but_new_revalidation_or_account_issue_reports(self):
        blocked = watcher.blocked("PROBE_EDGE_NOT_READY", "Edge too low")
        later = copy.deepcopy(blocked)
        later["observed_at"] = "different time"
        text = self.report([blocked, later, watcher.blocked(live.NEEDS_REVALIDATION, "Evidence changed"),
                            watcher.blocked("BASELINE_ISSUE", "Account needs review")])
        self.assertEqual(text.count("| NOT READY"), 3)
        self.assertIn(live.NEEDS_REVALIDATION, text)
        self.assertIn("BASELINE_ISSUE", text)

    def test_api_failure_withdraws_ready_and_suppresses_identical_outage_reports(self):
        failure = watcher.blocked("API_UNAVAILABLE", "Required reads failed")
        text = self.report([self.ready(), failure, failure, self.ready()])
        self.assertEqual(text.count("| READY"), 2)
        self.assertEqual(text.count("| NOT READY"), 1)

    def test_loop_uses_existing_cadence_and_ctrl_c_stops_without_execution(self):
        observation = self.ready()
        with patch.object(watcher, "check_readiness", return_value=observation) as check, \
                patch.object(watcher.time, "monotonic", side_effect=[100, 102, 115, 117]), \
                patch.object(watcher.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(watcher.watch(Mock()), 130)
        self.assertEqual(check.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [13, 13])
        self.assertEqual(output.getvalue().count("| READY"), 1)
        self.assertIn("Stopped read-only watcher", output.getvalue())

    def test_once_checks_once_and_exits_with_verdict(self):
        for observation, code in ((self.ready(), 0), (watcher.blocked("BASELINE_ISSUE", "Missing"), 1)):
            with patch.object(watcher, "check_readiness", return_value=observation) as check, \
                    patch.object(watcher.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(watcher.watch(Mock(), once=True), code)
            check.assert_called_once()
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
