"""Research fixtures use temporary SQLite files and no API/order adapters."""
import ast
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import external_shadow_tracker as tracker

D = Decimal
START = 1791676800  # A fixed UTC timestamp; test horizons never depend on wall clock.


def observation(seconds=0, side="YES", buy=".50", sell=".48", reference=".49", fair=".55", **changes):
    timestamp = START + seconds
    buy, sell, reference, fair = map(D, (buy, sell, reference, fair))
    yes_fair = fair if side == "YES" else 1 - fair
    book = {"bid": yes_fair - D(".01"), "ask": yes_fair + D(".01"), "midpoint": yes_fair,
            "bid_depth": D(100), "ask_depth": D(100), "book_at": timestamp, "fetched_at": timestamp}
    row = {"observed_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds"),
           "race": "Test Senate", "proposition": "Democratic Party wins", "side": side,
           "tournament_id": "test-tournament", "sig_market_id": "1", "sig_exchange_id": "11",
           "mapping_version": 1, "mapping_confidence": "EXACT_RESEARCH_ONLY", "source_urls": ["https://official.test/rules"],
           "sig_root_sha256": "root", "polymarket_rules_sha256": "rules", "sig_market_status": "open",
           "external_market_open": True, "polymarket_market_id": "101", "polymarket_condition_id": "condition",
           "polymarket_tokens": {"Yes": "token-yes", "No": "token-no"}, "polymarket_reference_outcome": "Yes",
           "sig_buy": buy, "sig_sell": sell, "sig_depth": D(10), "sig_sell_depth": D(10),
           "sig_bid_depth": D(10), "sig_ask_depth": D(10),
           "sig_buy_yes": buy if side == "YES" else 1 - sell, "sig_buy_no": buy if side == "NO" else 1 - sell,
           "sig_sell_yes": sell if side == "YES" else 1 - buy, "sig_sell_no": sell if side == "NO" else 1 - buy,
           "sig_yes_midpoint": reference if side == "YES" else 1 - reference, "sig_book_at": timestamp,
           "polymarket_books": {"Yes": book}, "external_fair": fair, "external_fair_yes": yes_fair,
           "external_fair_no": 1 - yes_fair, "external_edge": fair - buy, "external_bid": book["bid"],
           "external_ask": book["ask"], "external_midpoint": yes_fair, "external_depth": D(100),
           "external_book_at": timestamp, "data_confidence": "HIGH", "reference_type": "BOOK_MIDPOINT",
           "signal": "BUY_SIG_" + side if fair - buy >= D(".03") else "NO_SIGNAL", "flags": []}
    row.update(changes)
    return row


class ShadowTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "research.sqlite3"

    def collect(self, rows=(), seconds=0):
        return tracker.collect(rows, self.path, now=START + seconds)

    def records(self, table):
        with sqlite3.connect(str(self.path)) as db:
            return [json.loads(r[0]) for r in db.execute(f"SELECT record_json FROM {table}")]

    def horizon(self, minutes=5, index=0):
        shadows = self.records("shadow_trades")
        with sqlite3.connect(str(self.path)) as db:
            return json.loads(db.execute("SELECT record_json FROM shadow_horizons WHERE shadow_id=? AND horizon_minutes=?",
                                        (shadows[index]["shadow_id"], minutes)).fetchone()[0])

    def test_persistence_contains_identity_prices_flags_and_threshold(self):
        self.collect([observation()])
        saved = self.records("observations")[0]
        for field in ("observation_id", "observed_at", "mapping_key", "sig_buy_yes", "sig_buy_no", "sig_bid_depth",
                      "sig_ask_depth", "polymarket_tokens", "buy_yes_edge", "buy_no_edge", "freshness_flags", "liquidity_flags"):
            self.assertIn(field, saved)
        self.assertEqual(D(saved["buy_yes_edge"]), D(".05"))
        self.assertTrue(saved["threshold_met"])
        self.assertEqual(saved["signal_side"], "YES")

    def test_identical_timestamped_observation_deduplicated(self):
        self.collect([observation()])
        self.assertEqual(self.collect([observation()])["new_observations"], 0)
        self.assertEqual(len(self.records("observations")), 1)
        self.assertEqual(len(self.records("comparisons")), 1)

    def test_conflicting_same_timestamp_fails_atomically(self):
        self.collect([observation()])
        with self.assertRaises(ValueError):
            self.collect([observation(60), observation(fair=".60")], 60)
        self.assertEqual(len(self.records("observations")), 1)

    def test_shadow_boundary_at_three_percent_only_valid_signal(self):
        for fair, count in [(".529999999", 0), (".53", 1), (".55", 1)]:
            path = Path(self.temp.name) / (fair + ".sqlite3")
            self.assertEqual(tracker.collect([observation(fair=fair)], path, now=START)["new_shadows"], count)
        for changes in ({"flags": ["INSUFFICIENT_EXTERNAL_DEPTH"]}, {"mapping_confidence": "NO_EQUIVALENT_EXTERNAL_MARKET"},
                        {"sig_depth": 0}, {"signal": "BLOCKED"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.collect([observation(**changes)])["new_shadows"], 0)
                # Distinct temp DB for each blocked observation.
                self.path = Path(self.temp.name) / (str(len(list(Path(self.temp.name).iterdir()))) + ".sqlite3")

    def test_executable_entry_not_midpoint_and_gap_reference(self):
        self.collect([observation()])
        shadow = self.records("shadow_trades")[0]
        self.assertEqual(D(shadow["entry_price"]), D(".50"))
        self.assertEqual(D(shadow["entry_reference"]), D(".49"))
        self.assertEqual(D(shadow["initial_external_gap"]), D(".06"))
        self.assertEqual(shadow["status"], "OPEN_SHADOW")

    def test_entry_with_one_sided_sig_quote_labels_reference_fallback(self):
        self.collect([observation(sig_yes_midpoint=None)])
        shadow = self.records("shadow_trades")[0]
        self.assertEqual(shadow["entry_reference_type"], "ENTRY_BUY_FALLBACK")
        self.assertEqual(D(shadow["entry_reference"]), D(".50"))

    def test_unchanged_signal_suppressed_but_material_change_creates_shadow(self):
        self.collect([observation(), observation(60), observation(120, sig_depth=20)], 120)
        self.assertEqual(len(self.records("shadow_trades")), 1)
        self.collect([observation(180, fair=".555")], 180)
        self.assertEqual(len(self.records("shadow_trades")), 2)

    def test_material_entry_price_change_also_creates_shadow(self):
        self.collect([observation(), observation(60, buy=".495")], 60)
        self.assertEqual(len(self.records("shadow_trades")), 2)

    def test_yes_executable_pnl_and_return(self):
        self.collect([observation(), observation(300, sell=".52", reference=".53")], 300)
        result = self.horizon()
        self.assertEqual(D(result["executable_pnl"]), D(".02"))
        self.assertEqual(D(result["executable_return"]), D(".04"))
        self.assertEqual(D(result["mark_pnl"]), D(".03"))

    def test_no_executable_pnl_uses_no_sell_not_yes_quote(self):
        self.collect([observation(side="NO"), observation(300, side="NO", sell=".51", reference=".52")], 300)
        self.assertEqual(D(self.horizon()["executable_pnl"]), D(".01"))

    def test_all_four_horizons_nearest_at_or_after_no_cherry_picking(self):
        rows = [observation()]
        for minutes in tracker.HORIZONS:
            rows += [observation(minutes * 60 - 1, sell=".59"), observation(minutes * 60 + 3, sell=".51"),
                     observation(minutes * 60 + 30, sell=".58")]
        self.collect(rows, 3700)
        for minutes in tracker.HORIZONS:
            with self.subTest(minutes=minutes):
                result = self.horizon(minutes)
                self.assertEqual(result["status"], "EVALUATED")
                self.assertEqual(D(result["delay_seconds"]), 3)
                self.assertEqual(D(result["sell_price"]), D(".51"))

    def test_horizon_window_boundary_and_missing_pending(self):
        self.collect([observation(), observation(420, sell=".51")], 420)
        self.assertEqual(self.horizon()["status"], "EVALUATED")
        self.assertEqual(self.horizon(15)["status"], "PENDING")
        self.collect(seconds=1021)
        self.assertEqual(self.horizon(15)["status"], "MISSING_HORIZON_DATA")
        self.assertNotIn("executable_pnl", self.horizon(15))

    def test_observation_after_window_never_fabricates_horizon(self):
        self.collect([observation(), observation(421, sell=".51")], 500)
        self.assertEqual(self.horizon()["status"], "MISSING_HORIZON_DATA")

    def test_stale_sig_or_insufficient_sell_depth_rejected(self):
        for changes in ({"sig_book_at": START + 290}, {"sig_sell_depth": 0}, {"sig_market_status": "closed"},
                        {"mapping_confidence": "SETTLEMENT_RULE_MISMATCH"}):
            path = Path(self.temp.name) / (str(len(list(Path(self.temp.name).iterdir()))) + ".sqlite3")
            tracker.collect([observation(), observation(300, **changes)], path, now=START + 500)
            with sqlite3.connect(str(path)) as db:
                self.assertEqual(db.execute("SELECT status FROM shadow_horizons WHERE horizon_minutes=5").fetchone()[0], "MISSING_HORIZON_DATA")

    def test_sig_return_available_when_external_reference_stale(self):
        self.collect([observation(), observation(300, sell=".51", data_confidence="LOW", flags=["STALE_OR_MISSING_EXTERNAL_BOOK"])], 300)
        result = self.horizon()
        self.assertEqual(result["status"], "EVALUATED")
        self.assertIsNone(result["external_fair"])
        self.assertIsNone(result["contemporaneous_convergence"])
        self.assertEqual(D(result["executable_pnl"]), D(".01"))

    def test_convergence_frozen_fair_and_contemporaneous_are_distinct(self):
        self.collect([observation(), observation(300, sell=".51", reference=".52", fair=".58")], 300)
        result = self.horizon()
        self.assertEqual(D(result["convergence"]), D(".03"))
        self.assertEqual(D(result["convergence_percentage"]), D(".5"))
        self.assertEqual(D(result["contemporaneous_convergence"]), 0)
        self.assertEqual(D(result["external_edge_remaining"]), D(".08"))
        self.assertTrue(result["executable_profitable"])

    def test_divergence_widening_can_differ_with_moving_external_fair(self):
        self.collect([observation(), observation(300, reference=".48", fair=".51")], 300)
        result = self.horizon()
        self.assertTrue(result["divergence_widened"])
        self.assertFalse(result["contemporaneous_divergence_widened"])
        self.assertLess(D(result["convergence_percentage"]), 0)

    def test_early_target_first_profit_mfe_mae_and_fixed_horizons_independent(self):
        self.collect([observation(), observation(60, sell=".47"), observation(120, sell=".505"),
                      observation(180, sell=".51"), observation(300, sell=".53")], 300)
        shadow = self.records("shadow_trades")[0]
        self.assertEqual(shadow["status"], "CLOSED_SHADOW_TARGET")
        self.assertEqual(D(shadow["first_profit_seconds"]), 120)
        self.assertEqual(D(shadow["first_target_seconds"]), 180)
        self.assertEqual(D(shadow["early_exit_pnl"]), D(".01"))
        self.assertEqual(D(shadow["mfe"]), D(".03"))
        self.assertEqual(D(shadow["mae"]), D("-.03"))
        self.assertEqual(D(self.horizon()["executable_pnl"]), D(".03"))

    def test_horizon_excursions_do_not_leak_future_observations(self):
        self.collect([observation(), observation(300, sell=".49"), observation(900, sell=".40")], 900)
        self.assertEqual(D(self.horizon(5)["mae"]), D("-.02"))
        self.assertIsNone(self.horizon(5)["first_profit_seconds"])
        self.assertEqual(D(self.horizon(15)["mae"]), D("-.10"))

    def test_expired_without_quotes_not_invented_exit(self):
        self.collect([observation()], 3721)
        shadow = self.records("shadow_trades")[0]
        self.assertEqual(shadow["status"], "EXPIRED_SHADOW_NO_TARGET")
        self.assertIsNone(shadow["early_exit_proceeds"])

    def test_migrates_legacy_history_without_removing_or_duplicate_rows(self):
        with sqlite3.connect(str(self.path)) as db:
            tracker.initialize(db)
            yes, no = observation(), observation(side="NO", fair=".45")
            for row in (yes, no):
                for field in ("sig_sell_depth", "sig_bid_depth", "sig_ask_depth", "sig_market_status", "external_market_open"):
                    row.pop(field)
                db.execute("INSERT INTO comparisons(observed_at,race,sig_market_id,sig_exchange_id,side,mapping_version,signal,record_json) VALUES (?,?,?,?,?,?,?,?)",
                           (row["observed_at"], row["race"], "1", "11", row["side"], 1, row["signal"], tracker.encode(row)))
        self.collect()
        self.collect()
        self.assertEqual(len(self.records("comparisons")), 2)
        self.assertEqual(len(self.records("observations")), 2)
        self.assertTrue(all(D(r["sig_sell_depth"]) == 10 for r in self.records("observations")))
        self.assertEqual(len(self.records("shadow_trades")), 1)

    def test_changed_evidence_cannot_supply_previous_mapping_horizon(self):
        self.collect([observation(), observation(300, sig_root_sha256="different")], 500)
        self.assertEqual(self.horizon()["status"], "MISSING_HORIZON_DATA")

    def test_offline_tracker_never_loads_key_account_pilot_or_orders(self):
        for name in ("external_shadow_tracker.py", "external_shadow_report.py"):
            source = (Path(tracker.__file__).parent / name).read_text()
            tree = ast.parse(source)
            modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                      {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            self.assertFalse(modules & {"requests", "autonomous_pilot", "live_pilot", "pilot_account", "dotenv", "account_reader"})
            calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            self.assertFalse(calls & {"post", "delete", "put", "patch", "submit", "prepare"})
        sentinel = Path(self.temp.name) / "live_pilot_state.json"
        sentinel.write_text('{"DO_NOT_CHANGE": true}')
        self.collect([observation()])
        self.assertEqual(sentinel.read_text(), '{"DO_NOT_CHANGE": true}')


if __name__ == "__main__":
    unittest.main()
