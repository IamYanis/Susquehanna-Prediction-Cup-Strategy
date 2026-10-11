"""Aggregate and lead/lag research tests; no live data or account writes."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import external_shadow_report as report
import external_shadow_tracker as tracker
from test_external_shadow_tracker import START, observation

D = Decimal


class ShadowReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "research.sqlite3"

    def normalized(self, *rows):
        return [tracker.normalize(row) for row in rows]

    def test_strength_bucket_boundaries(self):
        for edge, expected in ((".03", 0), (".049999", 0), (".05", 1), (".07999", 1), (".08", 2)):
            self.assertEqual(report.bucket(edge), report.BUCKETS[expected])

    def test_group_counts_returns_race_side_mapping_and_missing(self):
        rows = [observation(), observation(side="NO", fair=".58"),
                observation(300, sell=".52", reference=".53"),
                observation(300, side="NO", sell=".49", reference=".50", fair=".58")]
        tracker.collect(rows, self.path, now=START + 1200)
        result = report.load_report(self.path)
        self.assertEqual(result["observation_count"], 4)
        for dimension in ("bucket", "race", "side", "mapping"):
            self.assertIn(dimension, result["groups"])
        yes = next(r for r in result["groups"]["side"] if r["group"] == "YES" and r["horizon_minutes"] == 5)
        no = next(r for r in result["groups"]["side"] if r["group"] == "NO" and r["horizon_minutes"] == 5)
        self.assertEqual(yes["profitable_count"], 1)
        self.assertEqual(yes["win_rate"], 1)
        self.assertEqual(yes["average_executable_return"], D(".04"))
        self.assertEqual(yes["median_executable_return"], D(".04"))
        self.assertEqual(yes["average_mark_return"], D(".06"))
        self.assertEqual(no["profitable_count"], 0)
        self.assertEqual(no["average_executable_return"], D("-.02"))
        missing = next(r for r in result["groups"]["side"] if r["group"] == "YES" and r["horizon_minutes"] == 15)
        self.assertEqual(missing["missing_data_count"], 1)
        self.assertIsNone(missing["average_executable_return"])
        self.assertIsNone(missing["win_rate"])

    def test_missing_and_pending_never_count_as_zero_return(self):
        tracker.collect([observation()], self.path, now=START + 500)
        result = report.load_report(self.path)
        self.assertEqual(result["coverage"][5]["MISSING_HORIZON_DATA"], 1)
        self.assertEqual(result["coverage"][15]["PENDING"], 1)
        bucket = next(r for r in result["groups"]["bucket"] if r["group"] == report.BUCKETS[1] and r["horizon_minutes"] == 5)
        self.assertEqual(bucket["sample_count"], 1)
        self.assertEqual(bucket["evaluated_count"], 0)
        self.assertIsNone(bucket["average_executable_return"])

    def test_report_uses_read_only_database_connection(self):
        tracker.collect([observation()], self.path, now=START)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        statements = []
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        with patch.object(report.sqlite3, "connect", side_effect=traced_connect):
            result = report.load_report(self.path)
        self.assertEqual(statements[0], "BEGIN")
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(len(result["open_shadows"]), 1)
        absent = Path(self.temp.name) / "missing.sqlite3"
        with self.assertRaises(sqlite3.Error):
            report.load_report(absent)
        self.assertFalse(absent.exists())

    def test_lead_lag_direction_hit_rate_positive_negative_and_flat(self):
        rows = self.normalized(observation(0, fair=".55"), observation(60, fair=".56", reference=".49"),
                               observation(120, fair=".54", reference=".50"), observation(180, fair=".54", reference=".48"))
        result = next(r for r in report.lead_lag(rows) if r["lag_minutes"] == 1)
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["direction_hit_rate"], 1)
        self.assertEqual(result["average_sig_move"], D("-.005"))
        self.assertEqual(result["correlation"], 1)

    def test_lead_lag_wrong_direction_is_not_hit(self):
        rows = self.normalized(observation(0), observation(60, fair=".56"), observation(120, fair=".56", reference=".48"))
        result = next(r for r in report.lead_lag(rows) if r["lag_minutes"] == 1)
        self.assertEqual(result["direction_hit_rate"], 0)
        self.assertIsNone(result["correlation"])  # One sample cannot define a correlation.

    def test_lead_lag_all_lags_use_nearest_subsequent_sig_move(self):
        rows = self.normalized(observation(0), observation(60, fair=".56"),
                               *[observation(60 + minutes * 60 + 2, fair=".56", reference=".50") for minutes in report.LAGS])
        results = report.lead_lag(rows)
        self.assertEqual({r["lag_minutes"] for r in results}, {1, 5, 15, 30})
        self.assertTrue(all(r["sample_count"] == 1 and r["average_sig_move"] == D(".01") for r in results))

    def test_sparse_external_history_no_short_lag_claim_and_no_double_no_sample(self):
        rows = self.normalized(observation(), observation(10000, fair=".56"), observation(side="NO"),
                               observation(10000, side="NO", fair=".58"))
        results = report.lead_lag(rows)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["sample_count"] == 0 and r["correlation"] is None for r in results))

    def test_stale_followup_or_late_quote_not_used_for_lead_lag(self):
        rows = self.normalized(observation(), observation(60, fair=".56"),
                               observation(120, sig_book_at=START), observation(241, fair=".56"))
        result = next(r for r in report.lead_lag(rows) if r["lag_minutes"] == 1)
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["missing_data_count"], 1)

    def test_correlation_insufficient_constant_and_opposite_data(self):
        self.assertIsNone(report.correlation([]))
        self.assertIsNone(report.correlation([(D(1), D(2)), (D(1), D(3))]))
        self.assertEqual(report.correlation([(D(1), D(3)), (D(2), D(2)), (D(3), D(1))]), -1)

    def test_report_cli_outputs_valid_json_without_modifying_database(self):
        tracker.collect([observation()], self.path, now=START)
        with patch("sys.argv", ["external_shadow_report.py", "--database", str(self.path), "--json"]), patch("builtins.print") as output:
            self.assertEqual(report.main(), 0)
            self.assertEqual(json.loads(output.call_args.args[0])["shadow_count"], 1)


if __name__ == "__main__":
    unittest.main()
