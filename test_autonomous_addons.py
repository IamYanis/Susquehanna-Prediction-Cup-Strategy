"""v0.2 marginal-unit behavior against a fake exchange and temporary files."""
import copy
import unittest
from decimal import Decimal
from unittest.mock import patch

import autonomous_pilot as auto
import live_pilot as pilot
import live_settlement as live
import test_autonomous_lifecycle as lifecycle
from test_account_reader import holdings, position
from test_api_audit import exchange_book


class AutonomousAddonTests(unittest.TestCase):
    def setUp(self):
        self.life = lifecycle.AutonomousLifecycleTests()
        self.addCleanup(self.life.doCleanups)
        self.life.setUp()
        self.fixture, self.session = self.life.fixture, self.life.session
        self.books(".075", ".900", sequence=1)
        result = self.life.cycle()
        self.assertEqual(result["state"], pilot.READY, result.get("reason"))
        self.root = result["entry"]["execution_id"]
        self.original_attempt = copy.deepcopy(self.cp()["autonomous_execution"]["attempts"][self.root])
        self.session.delete.assert_not_called()

    def cp(self):
        return self.fixture.checkpoint()

    def books(self, a, b, sequence=2, depth=100):
        for mid, eid, price in ((1, 11, a), (2, 12, b)):
            raw = exchange_book(mid, eid, bid=float(Decimal(1) - Decimal(price)), ask=.995)
            raw["asOf"]["sequence"] = sequence
            raw["bids"][0]["quantity"] = depth
            self.fixture.books[str(eid)] = raw

    def candidate(self):
        return auto.fresh_candidate(self.session, ["1", "2"], self.cp())

    def add(self, a=".070", b=".890", sequence=2):
        self.books(a, b, sequence=sequence)
        result = self.life.cycle()
        self.assertEqual(result["state"], pilot.READY, result.get("reason"))
        self.assertIn("entry", result, result.get("reason"))
        return result

    def assert_blocked(self, text=None):
        count = len(self.life.posts)
        with self.assertRaises((auto.PreviewBlocked, pilot.PilotBlocked, auto.pilot_account.AccountReadinessBlocked)) as error:
            self.candidate()
        if text:
            self.assertIn(text, str(error.exception))
        self.assertEqual(len(self.life.posts), count)

    def test_cheaper_executable_pair_qualifies_for_exactly_one_addon(self):
        self.books(".070", ".890")
        c = self.candidate()
        self.assertEqual(c["execution_type"], "ADDON")
        self.assertEqual(c["position_id"], self.root)
        self.assertEqual(c["prices"], [.070, .890])
        self.assertEqual(Decimal(c["edge"]), Decimal(".040"))
        self.assertEqual(Decimal(c["quote"]["pair_cost"]), Decimal(".960"))
        self.assertEqual(c["quote"]["available_depth"], ["100.0", "100.0"])
        self.assertEqual(len(self.life.posts), 2)

    def test_displayed_now_cannot_replace_executable_asks(self):
        account = self.fixture.fixture.current
        for row in account["positions"]:
            row["currentPrice"] = .99
        self.books(".300", ".800")
        self.assert_blocked()

    def test_marginal_edge_below_one_percent_fails(self):
        self.books(".130", ".865")  # .995 pair cost, .005 edge.
        self.assert_blocked("below 1.0%")

    def test_one_percent_boundary_passes_when_no_worse_than_average(self):
        # A separate initial pair has the unchanged .005 entry threshold.
        self.life.quote_exit(.495)
        self.assertEqual(self.life.manage()["state"], pilot.READY)
        self.books(".130", ".865", sequence=2)
        result = self.life.cycle()
        self.assertEqual(result["state"], pilot.READY)
        self.root = result["entry"]["execution_id"]
        self.books(".130", ".860", sequence=3)
        self.assertEqual(Decimal(self.candidate()["edge"]), Decimal(".010"))

    def test_tick_rounding_precedes_addon_threshold(self):
        self.books(".132", ".858")  # Raw .990; tick-rounded .135+.860=.995.
        self.assert_blocked("below 1.0%")

    def test_marginal_cost_worse_than_existing_average_fails(self):
        self.books(".080", ".910")  # Edge .010 passes, but .990 > .975.
        self.assert_blocked("worse than existing average")

    def test_insufficient_executable_depth_fails(self):
        self.books(".070", ".890", depth=.5)
        self.assert_blocked("depth")

    def test_quantity_cap_stops_addons_at_five_matched_pairs(self):
        for sequence in range(2, 6):
            self.add(sequence=sequence)
        self.assertEqual(self.cp()["autonomous_positions"][self.root]["quantity"], 5)
        self.books(".070", ".890", sequence=6)
        self.assert_blocked("quantity cap")
        self.assertEqual(len(self.life.posts), 10)

    def external_exposure(self, cost):
        account = self.fixture.fixture.current
        row = position("99", "89", -cost)
        row.update(avgCost=1, costBasis=cost, currentPrice=1, marketValue=cost, unrealizedPnl=0)
        account.update(holdings(account["positions"] + [row]))

    def test_race_cap_stops_addon(self):
        self.books(".070", ".890")
        self.external_exposure(99)
        self.assert_blocked("per-race capital limit")

    def test_total_exposure_cap_stops_addon(self):
        self.books(".070", ".890")
        self.external_exposure(499)
        self.assert_blocked("total exposure limit")

    def test_one_addon_completes_and_merges_quantity_and_weighted_cost(self):
        result = self.add()
        key = result["entry"]["execution_id"]
        cp, p = self.cp(), self.cp()["autonomous_positions"][self.root]
        self.assertEqual(p["quantity"], 2)
        self.assertEqual(p["remaining_quantities"], ["2", "2"])
        self.assertEqual(list(map(Decimal, p["per_leg_costs"])), [Decimal(".145"), Decimal("1.790")])
        self.assertEqual(Decimal(p["total_entry_cost"]), Decimal("1.935"))
        self.assertEqual(list(map(Decimal, p["average_entry_prices"])), [Decimal(".0725"), Decimal(".895")])
        self.assertEqual(Decimal(p["average_pair_cost"]), Decimal(".9675"))
        self.assertEqual(p["execution_ids"], [self.root, key])
        self.assertEqual(p["actual_entry_prices"], ["0.075", "0.9"])
        self.assertEqual(cp["autonomous_execution"]["attempts"][self.root], self.original_attempt)
        self.assertEqual(Decimal(p["settlement_floor"]), 2)
        self.assertEqual(Decimal(p["embedded_settlement_edge"]), Decimal(".065"))
        self.assertEqual(len(self.life.posts), 4)
        self.assertTrue(all(r["quantity"] == 1 and r["action"] == "buy" for r in self.life.posts))
        self.assertIsNone(cp["autonomous_execution"]["active_attempt"])

    def move_after_first_addon(self, next_price):
        original = self.life.post
        def post(url, **kwargs):
            response = original(url, **kwargs)
            if len(self.life.posts) == 3:
                self.books(".070", next_price, sequence=3)
            return response
        self.session.post.side_effect = post

    def test_one_tick_leg_two_deterioration_still_completes(self):
        self.move_after_first_addon(".895")
        self.add()
        cp = self.cp()
        key = cp["autonomous_positions"][self.root]["execution_ids"][-1]
        completion = cp["autonomous_execution"]["attempts"][key]["completion"]
        self.assertEqual(Decimal(completion["edge"]), Decimal(".035"))
        self.assertEqual(Decimal(completion["minimum_edge"]), Decimal(".035"))
        self.assertEqual(len(self.life.posts), 4)

    def test_leg_two_deterioration_halts_with_preserved_one_sided_addon(self):
        self.books(".070", ".890")
        self.move_after_first_addon(".900")
        result = self.life.cycle()
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("deteriorated", result["reason"])
        cp = self.cp()
        active = cp["autonomous_execution"]["active_attempt"]
        self.assertEqual(cp["autonomous_positions"][self.root]["quantity"], 1)
        self.assertEqual(list(map(Decimal, cp["live_exposures"][active]["confirmed_quantities"])), [Decimal(1), Decimal(0)])
        self.assertIsNone(cp["autonomous_execution"]["attempts"][active]["legs"][1]["intent"])
        self.assertEqual(auto.held_quantity(self.fixture.fixture.current, "11"), 2)
        self.assertEqual(auto.held_quantity(self.fixture.fixture.current, "12"), 1)
        self.life.manage()
        self.assertEqual(len(self.life.posts), 3)

    def test_same_quote_snapshot_cannot_generate_another_addon(self):
        self.add()
        self.assert_blocked("already been consumed")
        result = self.life.cycle()
        self.assertNotIn("entry", result)
        self.assertEqual(len(self.life.posts), 4)
        self.assertEqual(self.cp()["autonomous_positions"][self.root]["quantity"], 2)

    def test_fresh_authoritative_version_can_support_a_later_single_unit(self):
        self.add(sequence=2)
        self.add(sequence=3)
        self.assertEqual(self.cp()["autonomous_positions"][self.root]["quantity"], 3)
        self.assertEqual(len(self.life.posts), 6)
        self.assertEqual(len({r["idempotencyKey"] for r in self.life.posts}), 6)

    def test_initial_entry_snapshot_is_also_consumed(self):
        self.books(".075", ".900", sequence=1)
        self.assert_blocked("already been consumed")

    def test_clock_only_quote_refresh_does_not_allow_duplicate_addon(self):
        self.add()
        for book in self.fixture.books.values():
            book["asOf"]["at"] = "2026-10-07T12:00:01Z"
        self.assert_blocked("already been consumed")

    def test_ambiguous_addon_post_halts_and_restart_never_retries(self):
        self.books(".070", ".890")
        self.life.scripts[3] = {"timeout_after": True}
        result = self.life.cycle()
        self.assertEqual(result["state"], pilot.HALTED)
        cp = self.cp()
        attempt = auto.active_attempt(cp)
        self.assertTrue(attempt["legs"][0]["post_attempted"])
        self.assertIsNone(attempt["legs"][0]["receipt"])
        self.assertIsNone(attempt["legs"][1]["intent"])
        self.assertGreater(Decimal(cp["reserved_unconfirmed_capital"]), 0)
        self.assertEqual(auto.held_quantity(self.fixture.fixture.current, "11"), 2)
        self.assertEqual(pilot.load_checkpoint()["state"], pilot.HALTED)
        self.life.cycle()
        self.assertEqual(len(self.life.posts), 3)
        self.assertEqual(self.cp()["autonomous_positions"][self.root]["quantity"], 1)

    def test_partial_addon_fill_preserves_exposure_and_blocks_second_leg(self):
        self.books(".070", ".890")
        self.life.scripts[3] = {"quantity": .5}
        result = self.life.cycle()
        self.assertEqual(result["state"], pilot.HALTED)
        self.assertIn("Partial", result["reason"])
        cp = self.cp()
        key = cp["autonomous_execution"]["active_attempt"]
        self.assertEqual(Decimal(cp["live_exposures"][key]["confirmed_quantities"][0]), Decimal(".5"))
        self.assertIsNone(auto.active_attempt(cp)["legs"][1]["intent"])
        self.assertEqual(auto.held_quantity(self.fixture.fixture.current, "11"), Decimal("1.5"))
        self.assertEqual(len(self.life.posts), 3)

    def test_account_holding_must_match_exact_managed_quantity(self):
        self.books(".070", ".890")
        self.fixture.fixture.current["positions"][0]["quantity"] = -2
        self.assert_blocked()

    def test_incomplete_saved_quote_evidence_fails_closed_on_restart(self):
        cp = self.cp()
        attempt = cp["autonomous_execution"]["attempts"][self.root]
        quote = attempt["quote"]
        quote["books"] = quote["books"][:1]
        quote["available_depth"] = quote["available_depth"][:1]
        quote["fingerprint"] = auto.book_fingerprint(attempt["authorization"], quote["books"])
        with self.assertRaisesRegex(pilot.PilotBlocked, "quote evidence"):
            pilot.validate_checkpoint(cp)
        self.assertEqual(len(self.life.posts), 2)

    def test_settled_market_cannot_receive_addon(self):
        self.books(".070", ".890")
        self.fixture.markets["1"]["status"] = "settled"
        with self.assertRaises(live.LiveSettlementBlocked):
            self.candidate()
        self.assertEqual(len(self.life.posts), 2)
        self.assertEqual(len(self.life.posts), 2)

    def test_exit_requires_depth_for_full_quantity_on_both_legs(self):
        self.add()
        self.life.quote_exit(.495)  # Both bids .505, full exit target 2.020.
        self.fixture.books["12"]["asks"][0]["quantity"] = 1
        held = self.life.manage()
        self.assertEqual(held["positions"][0]["action"], "HOLD")
        self.assertEqual(len(self.life.posts), 4)
        self.fixture.books["12"]["asks"][0]["quantity"] = 2
        sold = self.life.manage()
        self.assertEqual(sold["state"], pilot.READY, sold)
        self.assertEqual(sold["positions"][0]["action"], "EARLY_EXIT")
        self.assertEqual([b["quantity"] for b in self.life.posts[-2:]], [2, 2])
        p = self.cp()["autonomous_positions"][self.root]
        self.assertEqual(p["status"], "CLOSED")
        self.assertEqual(Decimal(p["exit_proceeds"]), Decimal("2.020"))
        self.assertEqual(Decimal(p["realized_pnl"]), Decimal(".085"))
        self.assertEqual(Decimal(self.cp()["total_live_exposure"]), 0)

    def test_one_profitable_leg_never_triggers_one_sided_sale(self):
        self.add()
        self.fixture.books["11"] = exchange_book(1, 11, bid=.3, ask=.4)  # Sell NO .600.
        self.fixture.books["12"] = exchange_book(2, 12, bid=.3, ask=.65)  # Sell NO .350.
        self.assertEqual(self.life.manage()["positions"][0]["action"], "HOLD")
        self.assertEqual(len(self.life.posts), 4)

    def test_settlement_and_pnl_scale_to_two_matched_pairs(self):
        self.add()
        self.life.settle("1", 0)
        self.life.settle("2", 2)
        result = self.life.manage()
        self.assertEqual(result["state"], pilot.READY, result)
        self.assertEqual(result["positions"][0]["action"], "SETTLEMENT")
        p = self.cp()["autonomous_positions"][self.root]
        self.assertEqual(Decimal(p["exit_proceeds"]), 2)
        self.assertEqual(Decimal(p["realized_pnl"]), Decimal(".065"))
        self.assertEqual(Decimal(self.cp()["total_live_exposure"]), 0)

    def test_per_lot_settlement_events_cover_exact_multi_unit_quantity(self):
        self.add()
        self.life.settle("1", 2)
        self.life.settle("2", 0)
        event = next(r for r in self.fixture.fixture.transactions if r["event_id"] == "settle-1")
        event.update(quantity=-1, amount=1)
        second = copy.deepcopy(event)
        second["event_id"] = "settle-1-lot-2"
        self.fixture.fixture.transactions.insert(self.fixture.fixture.transactions.index(event), second)
        self.assertEqual(self.life.manage()["state"], pilot.READY)
        p = self.cp()["autonomous_positions"][self.root]
        self.assertEqual(p["status"], "CLOSED")
        self.assertEqual(Decimal(p["realized_pnl"]), Decimal(".065"))
        self.assertEqual(len(p["settlement_event_ids"]), 3)

    def test_ambiguous_per_lot_settlement_quantity_halts(self):
        self.add()
        self.life.settle("1", 2)
        event = next(r for r in self.fixture.fixture.transactions if r["event_id"] == "settle-1")
        event.update(quantity=-1, amount=1)
        second = copy.deepcopy(event)
        second.update(event_id="settle-1-wrong-lot", quantity=-.5)
        self.fixture.fixture.transactions.insert(self.fixture.fixture.transactions.index(event), second)
        self.assertEqual(self.life.manage()["state"], pilot.HALTED)
        self.assertEqual(len(self.life.posts), 4)

    def test_multi_unit_position_and_consumed_quote_survive_restart(self):
        self.add()
        before = self.cp()
        self.assertEqual(pilot.load_checkpoint(), before)
        self.assertEqual(Decimal(auto.average_pair_cost(before, self.root)), Decimal(".9675"))
        self.assert_blocked("already been consumed")
        self.assertEqual(len(self.life.posts), 4)

    def test_closed_multi_unit_position_allows_a_new_initial_entry(self):
        self.add()
        self.life.quote_exit(.495)
        self.assertEqual(self.life.manage()["state"], pilot.READY)
        self.books(".075", ".900", sequence=3)
        result = self.life.cycle()
        self.assertEqual(result["state"], pilot.READY)
        self.assertIn("entry", result)
        cp = self.cp()
        self.assertEqual(cp["autonomous_positions"][self.root]["status"], "CLOSED")
        self.assertEqual(cp["autonomous_positions"][result["entry"]["execution_id"]]["quantity"], 1)
        self.assertEqual(len(self.life.posts), 8)

    def test_existing_and_new_pair_compete_by_marginal_edge(self):
        self.books(".070", ".890")
        addon = self.candidate()
        new = copy.deepcopy(addon)
        new["authorization"]["market_ids"], new["authorization"]["exchange_ids"] = ["3", "4"], ["13", "14"]
        new.update(edge=".015", position_id=None, execution_type="INITIAL")
        approvals = [new["authorization"], addon["authorization"]]
        candidates = lambda session, ids, cp: addon if ids == ["1", "2"] else new
        with patch.object(live, "autonomous_authorizations", return_value=approvals), \
                patch.object(auto, "scoped_markets", return_value=[{"status": "open"}] * 2), \
                patch.object(auto, "fresh_candidate", side_effect=candidates):
            self.assertEqual(auto.best_candidate(self.session, self.cp())["execution_type"], "ADDON")
            new["edge"] = ".045"
            self.assertEqual(auto.best_candidate(self.session, self.cp())["execution_type"], "INITIAL")
        self.assertEqual(len(self.life.posts), 2)

    def test_quantity_one_position_and_initial_threshold_remain_compatible(self):
        cp = self.cp()
        p = cp["autonomous_positions"][self.root]
        self.assertEqual(p["quantity"], 1)
        self.assertNotIn("execution_ids", p)
        self.assertEqual(auto.MIN_EDGE, Decimal(".005"))
        self.assertEqual(auto.MIN_COMPLETION_EDGE, 0)
        self.assertEqual(Decimal(auto.average_pair_cost(cp, self.root)), Decimal(".975"))
        pilot.validate_checkpoint(cp)


if __name__ == "__main__":
    unittest.main()
