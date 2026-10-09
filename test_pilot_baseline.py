"""GET-only baseline initialization: fake account data and temporary state."""
import copy
import json
import unittest
from unittest.mock import patch

import live_pilot as pilot
import pilot_account
import test_pilot_account as account_fixtures


class PilotBaselineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = account_fixtures.PilotAccountTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.path = self.fixture.fixture.root / "new-baseline.json"
        self.session = self.fixture.session

    def initialize(self):
        return pilot.initialize_from_account_reads(self.session, self.path)

    def test_full_observation_survives_reload_without_replenishment(self):
        self.fixture.add_activity()  # Historical personal holding, not a pilot debit.
        saved = self.initialize()
        restored = pilot.load_checkpoint(self.path)
        self.assertEqual(saved, restored)
        self.assertEqual(saved["state"], pilot.DISABLED)
        self.assertEqual(pilot.amount(saved["configured_allocation"]), 5000)
        self.assertEqual(pilot.amount(saved["allocated_cash_remaining"]), 5000)
        self.assertEqual(pilot.amount(saved["untouchable_cash_reserve"]), 15000)
        self.assertEqual(pilot.amount(saved["confirmed_cumulative_debits"]), 0)
        snapshot = saved["baseline_snapshot"]
        self.assertEqual(snapshot["account"]["positions"], self.fixture.current["positions"])
        self.assertEqual(snapshot["recent_fills"], self.fixture.fills)
        self.assertEqual(snapshot["recent_transactions"], self.fixture.transactions)
        self.assertEqual(saved["baseline_snapshot_hash"], pilot.snapshot_hash(snapshot))
        risk = pilot.pilot_risk(snapshot["account"], ["11", "12"], .8, 1, saved)
        self.assertAlmostEqual(risk["total_exposure_after"], 1)  # .2 holding + .8 pair
        self.fixture.fixture.assert_no_writes()
        self.session.get.reset_mock()
        with self.assertRaisesRegex(pilot.PilotBlocked, "never reset"):
            self.initialize()
        self.session.get.assert_not_called()

    def test_open_orders_are_retained_and_counted(self):
        from test_account_reader import order
        from test_pilot_account import history
        pending = order(302, "99")
        pending.update(quantity=1, side="no", action="buy")
        self.fixture.current["orders"] = [pending]
        lifecycle = history()
        lifecycle.update(orderId=302, exchangeId="99", tournamentId=self.fixture.checkpoint["tournament_id"],
                         totalQuantityFilled=0, avgFillPrice=None)
        self.fixture.receipts[302] = pending, lifecycle
        saved = self.initialize()
        snapshot = saved["baseline_snapshot"]
        self.assertEqual(snapshot["account"]["orders"], [pending])
        risk = pilot.pilot_risk(snapshot["account"], ["11", "12"], .8, 1, saved)
        self.assertAlmostEqual(risk["total_exposure_after"], 1.8)
        self.fixture.fixture.assert_no_writes()

    def test_prior_probe_or_orphan_write_prevents_initialization_before_get(self):
        for filename in ("accounting_probe.json", f".{self.path.name}.orphan.tmp",
                         ".accounting_probe.json.orphan.tmp"):
            evidence = self.path.with_name(filename)
            evidence.write_text("unresolved diagnostic evidence")
            try:
                self.session.get.reset_mock()
                with self.assertRaisesRegex(pilot.PilotBlocked, "manual review"):
                    self.initialize()
                self.session.get.assert_not_called()
                self.assertFalse(self.path.exists())
                self.assertEqual(evidence.read_text(), "unresolved diagnostic evidence")
            finally:
                evidence.unlink()  # Only this test's temporary evidence.

    def test_stale_or_unavailable_snapshot_does_not_initialize(self):
        snapshot = self.fixture.snapshot()
        snapshot["freshness"]["started_monotonic"] -= 16
        with patch.object(pilot_account, "read_snapshot", return_value=snapshot):
            with self.assertRaises(pilot_account.AccountReadinessBlocked):
                self.initialize()
        self.assertFalse(self.path.exists())
        self.fixture.failed_path = "/orders"
        with self.assertRaises(pilot_account.AccountReadinessBlocked):
            self.initialize()
        self.assertFalse(self.path.exists())

    def test_insufficient_cash_cannot_create_a_smaller_or_inflated_baseline(self):
        self.fixture.current["tournament"]["myBalance"] = 4999
        with self.assertRaisesRegex(pilot.PilotBlocked, "fixed 5000"):
            self.initialize()
        self.assertFalse(self.path.exists())

    def test_archived_snapshot_is_immutable_and_corruption_fails_closed(self):
        saved = self.initialize()
        changed = copy.deepcopy(saved)
        changed["baseline_snapshot"]["history_since"] = "2026-01-01T00:00:00+00:00"
        changed["baseline_snapshot_hash"] = pilot.snapshot_hash(changed["baseline_snapshot"])
        with self.assertRaisesRegex(pilot.PilotBlocked, "Immutable baseline"):
            pilot.save_checkpoint(changed, self.path)
        del changed["baseline_snapshot"]
        del changed["baseline_snapshot_hash"]
        with self.assertRaisesRegex(pilot.PilotBlocked, "Immutable baseline"):
            pilot.save_checkpoint(changed, self.path)
        corrupt = copy.deepcopy(saved)
        corrupt["baseline_snapshot"]["account"]["tournament"]["myBalance"] += 1
        self.path.write_text(json.dumps(corrupt))
        original = self.path.read_bytes()
        with self.assertRaisesRegex(pilot.PilotBlocked, "manual review"):
            pilot.load_checkpoint(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_atomic_write_failure_leaves_baseline_absent(self):
        with patch.object(pilot.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(pilot.PilotBlocked, "state write failed"):
                self.initialize()
        self.assertFalse(self.path.exists())
        self.fixture.fixture.assert_no_writes()


if __name__ == "__main__":
    unittest.main()
