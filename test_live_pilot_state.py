"""Durability/recovery tests: temporary state, fake exchange reads, real processes."""
import contextlib
import copy
import io
import json
import select
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import live_pilot as pilot
import paired_account_test as paired
import test_live_pilot as fixtures
from test_account_reader import order
from test_order_preview import account_snapshot


RESTART_SCRIPT = """
import json, sys
from pathlib import Path
import live_pilot as pilot
try:
    print(json.dumps(pilot.load_checkpoint(Path(sys.argv[1]))))
except pilot.PilotBlocked as error:
    print(str(error))
    raise SystemExit(1)
"""

LOCK_SCRIPT = """
import sys
from pathlib import Path
import live_pilot as pilot
with pilot.pilot_lock(Path(sys.argv[1])):
    print('LOCKED', flush=True)
    sys.stdin.readline()
"""


class PilotStateTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing fake orders/account and quarantine isolation. Its
        # setup patches every runtime path into a TemporaryDirectory.
        self.fixture = fixtures.LivePilotTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.path = pilot.ALLOCATION_PATH
        self.session = self.fixture.session

    def restart(self):
        return subprocess.run([sys.executable, "-c", RESTART_SCRIPT, str(self.path)],
                              cwd=Path(pilot.__file__).parent, capture_output=True, text=True, timeout=10)

    def restored(self):
        result = self.restart()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def reconcile(self, submitted=(True, True), quantities=(1, 1)):
        pair, legs = self.fixture.synthetic_pair(submitted)
        account = self.fixture.venue_observations(legs, quantities)
        with patch.object(paired, "read_account", return_value=account):
            result = pilot.reconcile_pilot(self.session, pair, legs, self.fixture.checkpoint)
        self.fixture.assert_no_writes()
        return result

    def test_confirmed_debits_and_positions_survive_a_new_python_process(self):
        result = self.reconcile()
        restored = self.restored()
        self.assertEqual(restored, result["checkpoint"])
        self.assertEqual(pilot.amount(restored["configured_allocation"]), 5000)
        self.assertEqual(pilot.amount(restored["confirmed_cumulative_debits"]), pilot.amount("0.8"))
        self.assertEqual(pilot.amount(restored["calculated_remaining_allocation"]), pilot.amount("4999.2"))
        exposure = next(iter(restored["live_exposures"].values()))
        self.assertEqual(exposure["market_ids"], ["1", "2"])
        self.assertEqual(exposure["exchange_ids"], ["11", "12"])
        self.assertEqual(list(map(pilot.amount, exposure["confirmed_quantities"])), [1, 1])
        self.assertEqual(list(map(pilot.amount, exposure["confirmed_costs"])), [pilot.amount("0.4")] * 2)

    def test_restart_and_extra_account_cash_cannot_replenish_allocation(self):
        consumed = self.fixture.consumed_checkpoint(4999.5)
        pilot.save_checkpoint(consumed)
        restored = self.restored()
        for _ in range(2):
            self.assertEqual(pilot.amount(self.restored()["allocated_cash_remaining"]), pilot.amount("0.5"))
        # Even the old 20,000 balance or a much larger deposit cannot overwrite
        # this baseline; unexpected cash changes also fail the existing risk gate.
        for cash in (20000, 1000000):
            with self.subTest(cash=cash), self.assertRaisesRegex(pilot.PilotBlocked, "never reset"):
                pilot.initialize_checkpoint(account_snapshot(cash=cash))
            with self.assertRaisesRegex(pilot.PilotBlocked, "cash changed"):
                self.fixture.risk(account=account_snapshot(cash=cash), checkpoint=restored)
        self.assertEqual(self.fixture.risk(.5, account=account_snapshot(cash=15000.5), checkpoint=restored)[
            "allocation_remaining_after_reserves"], .5)
        with self.assertRaisesRegex(pilot.PilotBlocked, "Insufficient remaining"):
            self.fixture.risk(.505, account=account_snapshot(cash=15000.5), checkpoint=restored)

    def test_partial_execution_halt_survives_restart_without_new_api_reads(self):
        result = self.reconcile((True, False), (.5, 0))
        restored = self.restored()
        self.assertEqual(restored["state"], pilot.HALTED)
        self.assertEqual(restored["review_reason"], result["reason"])
        self.assertEqual(pilot.amount(restored["confirmed_cumulative_debits"]), pilot.amount("0.2"))
        self.assertEqual(pilot.amount(restored["reserved_unconfirmed_capital"]), pilot.amount("0.4"))
        self.assertEqual(pilot.amount(restored["total_live_exposure"]), pilot.amount("0.6"))
        self.assertEqual(pilot.amount(restored["calculated_remaining_allocation"]), pilot.amount("4999.4"))
        self.assertEqual(pilot.amount(restored["last_reconciled_account_cash"]), pilot.amount("19999.8"))
        pair, legs = self.fixture.synthetic_pair()
        self.session.get.reset_mock()
        again = pilot.reconcile_pilot(self.session, pair, legs, restored)
        self.assertEqual(again["checkpoint"], restored)
        self.session.get.assert_not_called()
        cleared = copy.deepcopy(restored)
        cleared.update(state=pilot.DISABLED, manual_review_required=False, review_reason="", live_exposures={})
        pilot.refresh_totals(cleared)
        with self.assertRaisesRegex(pilot.PilotBlocked, "cannot be cleared"):
            pilot.save_checkpoint(cleared)
        self.assertEqual(self.restored(), restored)

    def test_first_leg_only_restarts_halted_with_second_leg_reserved(self):
        result = self.reconcile((True, False), (1, 0))
        self.assertEqual(result["checkpoint"]["state"], pilot.EXECUTING)
        restored = self.restored()
        self.assertEqual(restored["state"], pilot.HALTED)
        self.assertEqual(pilot.amount(restored["confirmed_cumulative_debits"]), pilot.amount("0.4"))
        self.assertEqual(pilot.amount(restored["calculated_remaining_allocation"]), pilot.amount("4999.2"))
        self.assertEqual(restored["revision"], result["checkpoint"]["revision"] + 1)
        self.assertEqual(self.restored(), restored)  # No repeated recovery writes.
        with self.assertRaisesRegex(pilot.PilotBlocked, "halted"):
            pilot.consider_second_leg(self.session, self.fixture.synthetic_pair((True, False))[0], result,
                                      manually_supervised=True, restarted=False)

    def test_unexpected_crash_between_legs_preserves_first_fill_and_halts_restart(self):
        pair, legs = self.fixture.synthetic_pair()
        self.fixture.venue_observations(legs)
        original_get = self.session.get.side_effect

        def crash_on_second_order(url, **kwargs):
            if url.endswith("/101"):
                saved = json.loads(self.path.read_text())
                self.assertEqual(saved["state"], pilot.EXECUTING)
                self.assertEqual(pilot.amount(saved["reserved_unconfirmed_capital"]), pilot.amount("0.8"))
            if url.endswith("/102"):
                saved = json.loads(self.path.read_text())
                self.assertEqual(saved["state"], pilot.EXECUTING)
                self.assertEqual(pilot.amount(saved["confirmed_cumulative_debits"]), pilot.amount("0.4"))
                raise SystemExit("simulated process crash")
            return original_get(url, **kwargs)

        self.session.get.side_effect = crash_on_second_order
        with self.assertRaises(SystemExit):
            pilot.reconcile_pilot(self.session, pair, legs, self.fixture.checkpoint)
        restored = self.restored()
        self.assertEqual(restored["state"], pilot.HALTED)
        self.assertEqual(pilot.amount(restored["confirmed_cumulative_debits"]), pilot.amount("0.4"))
        self.assertEqual(pilot.amount(restored["reserved_unconfirmed_capital"]), pilot.amount("0.4"))
        self.fixture.assert_no_writes()

    def test_ctrl_c_persists_halt_before_returning(self):
        pair, legs = self.fixture.synthetic_pair()
        self.session.get.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            pilot.reconcile_pilot(self.session, pair, legs, self.fixture.checkpoint)
        restored = self.restored()
        self.assertEqual(restored["state"], pilot.HALTED)
        self.assertEqual(pilot.amount(restored["reserved_unconfirmed_capital"]), pilot.amount("0.8"))
        self.fixture.assert_no_writes()

    def test_missing_state_is_not_reconstructed_from_paper(self):
        self.path.unlink()
        (self.path.parent / "paper_portfolio.json").write_text('{"cash_balance": 999999}')
        result = self.restart()
        self.assertEqual(result.returncode, 1)
        self.assertIn("HALTED_MANUAL_REVIEW", result.stdout)
        self.assertFalse(self.path.exists())

    def test_corrupt_and_inconsistent_state_fails_closed_without_rewriting_bytes(self):
        valid = json.loads(self.path.read_text())
        mutations = []
        for key, value in (("configured_allocation", "6000"), ("allocated_cash_remaining", "6000"),
                           ("confirmed_cumulative_debits", "1"), ("total_live_exposure", "10"),
                           ("manual_review_required", True), ("state", "NOT_A_STATE"), ("revision", 0),
                           ("updated_at", "invalid"), ("version", 1), ("live_exposures", [])):
            changed = copy.deepcopy(valid)
            changed[key] = value
            mutations.append(json.dumps(changed))
        mutations.extend(("{broken", json.dumps(valid)[:-1] + ', "state": "READY"}'))
        for content in mutations:
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaisesRegex(pilot.PilotBlocked, "HALTED_MANUAL_REVIEW"):
                    pilot.load_checkpoint()
                self.assertEqual(self.path.read_text(), content)
        result = self.restart()
        self.assertEqual(result.returncode, 1)
        self.assertIn("manual review required", result.stdout)

    def test_failed_atomic_replace_preserves_previous_file_and_removes_temporary_file(self):
        before = self.path.read_bytes()
        changed = pilot.halted_result(self.fixture.checkpoint, "offline manual review")["checkpoint"]
        with patch.object(pilot.os, "replace", side_effect=OSError("fake disk error")), \
                self.assertRaisesRegex(pilot.PilotBlocked, "write failed"):
            pilot.save_checkpoint(changed)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob(".allocation.json.*.tmp")))
        self.assertEqual(pilot.load_checkpoint(), self.fixture.checkpoint)

    def test_failed_directory_sync_stops_with_a_complete_recoverable_halt(self):
        changed = pilot.halted_result(self.fixture.checkpoint, "offline manual review")["checkpoint"]
        with patch.object(pilot.single, "sync_directory", side_effect=OSError("fake disk error")), \
                self.assertRaisesRegex(pilot.PilotBlocked, "write failed"):
            pilot.save_checkpoint(changed)
        self.assertEqual(self.restored()["state"], pilot.HALTED)

    def test_stale_writes_or_removing_confirmed_costs_cannot_reset_state(self):
        result = self.reconcile()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(pilot.PilotBlocked, "Stale pilot state revision"):
            pilot.save_checkpoint(self.fixture.checkpoint)
        reset = copy.deepcopy(result["checkpoint"])
        reset.update(accounted_pair_costs={}, live_exposures={}, allocated_cash_remaining="5000")
        pilot.refresh_totals(reset)
        with self.assertRaisesRegex(pilot.PilotBlocked, "debits cannot disappear"):
            pilot.save_checkpoint(reset)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reloaded_positions_are_checked_against_actual_account(self):
        result = self.reconcile()
        with self.assertRaisesRegex(pilot.PilotBlocked, "holdings disagree"):
            self.fixture.risk(account=account_snapshot(cash=19999.2), checkpoint=self.restored())
        self.assertFalse(result["submission_enabled"])

    def test_external_orders_and_saved_pending_reserves_both_count(self):
        with pilot.pilot_lock():
            result = self.reconcile((True, False), (1, 0))
            account = copy.deepcopy(result["account"])
            account["orders"] = [order(exchange_id=99)]
            risk = pilot.pilot_risk(account, ["12"], .4, 1, result["checkpoint"])
            self.assertAlmostEqual(risk["allocation_remaining_after_reserves"], 4997.2)
            self.assertAlmostEqual(risk["total_exposure_after"], 3.2)

    def test_cli_readiness_api_error_persists_halt_without_order_writes(self):
        self.session.get.side_effect = requests.Timeout("fake-sensitive-header")
        output = io.StringIO()
        with patch("sys.argv", ["live_pilot.py", "--dem-market", "1", "--rep-market", "2"]), \
                patch.object(pilot, "load_dotenv"), patch.object(pilot.os, "getenv", return_value="offline-key"), \
                patch.object(pilot.requests, "Session") as session_factory, contextlib.redirect_stdout(output):
            session_factory.return_value.__enter__.return_value = self.session
            self.assertEqual(pilot.main(), 1)
        self.assertNotIn("fake-sensitive-header", output.getvalue())
        self.assertEqual(self.restored()["state"], pilot.HALTED)
        self.fixture.assert_no_writes()

    def test_local_report_displays_saved_state_and_halt_without_credentials(self):
        halted = pilot.halted_result(self.fixture.checkpoint, "Persistent offline test halt")["checkpoint"]
        pilot.save_checkpoint(halted)
        output = io.StringIO()
        with patch("sys.argv", ["live_pilot.py"]), patch.object(pilot, "load_dotenv") as dotenv, \
                patch.object(pilot.requests, "Session") as session, contextlib.redirect_stdout(output):
            self.assertEqual(pilot.main(), 1)
        self.assertIn("HALTED_MANUAL_REVIEW", output.getvalue())
        self.assertIn("Persistent offline test halt", output.getvalue())
        self.assertIn("confirmed cumulative debits", output.getvalue())
        dotenv.assert_not_called()
        session.assert_not_called()

    def test_cli_initialization_refuses_existing_baseline_before_network_access(self):
        with patch("sys.argv", ["live_pilot.py", "--initialize-state"]), \
                patch.object(pilot, "load_dotenv") as dotenv, patch.object(pilot.requests, "Session") as session, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.main(), 1)
        dotenv.assert_not_called()
        session.assert_not_called()

    def test_explicit_cli_initialization_creates_only_a_disabled_accounting_baseline(self):
        self.path.unlink()  # This test has no prior pilot execution/debits.
        with patch("sys.argv", ["live_pilot.py", "--initialize-state"]), \
                patch.object(pilot, "load_dotenv"), patch.object(pilot.os, "getenv", return_value="offline-key"), \
                patch.object(pilot.requests, "Session") as session_factory, \
                patch.object(pilot, "read_account", return_value=self.fixture.account) as account_read, \
                contextlib.redirect_stdout(io.StringIO()):
            session_factory.return_value.__enter__.return_value = self.session
            self.assertEqual(pilot.main(), 0)
        account_read.assert_called_once_with(self.session, "midterm-elections")
        self.assertEqual(self.restored()["state"], pilot.DISABLED)
        self.assertEqual(pilot.amount(self.restored()["allocated_cash_remaining"]), 5000)
        self.fixture.assert_no_writes()

    def test_successful_cli_diagnostics_persist_ready_but_cannot_enable_submission(self):
        self.session.get.side_effect = [self.fixture.response(payload) for payload in self.fixture.candidate_payloads()]
        with patch("sys.argv", ["live_pilot.py", "--dem-market", "1", "--rep-market", "2"]), \
                patch.object(pilot, "load_dotenv"), patch.object(pilot.os, "getenv", return_value="offline-key"), \
                patch.object(pilot.requests, "Session") as session_factory, contextlib.redirect_stdout(io.StringIO()):
            session_factory.return_value.__enter__.return_value = self.session
            self.assertEqual(pilot.main(), 0)
        self.assertEqual(self.restored()["state"], pilot.READY)
        with self.assertRaisesRegex(pilot.PilotBlocked, "submission is disabled"):
            pilot.require_live_submission(pilot.config.LIVE_PILOT)
        self.fixture.assert_no_writes()

    def test_restart_halt_blocks_candidate_cli_before_credentials_or_api(self):
        self.reconcile((True, False), (1, 0))
        with patch("sys.argv", ["live_pilot.py", "--dem-market", "1", "--rep-market", "2"]), \
                patch.object(pilot, "load_dotenv") as dotenv, patch.object(pilot.requests, "Session") as session, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.main(), 1)
        self.assertEqual(self.restored()["state"], pilot.HALTED)
        dotenv.assert_not_called()
        session.assert_not_called()

    def test_private_write_requires_exclusive_lock(self):
        with self.assertRaisesRegex(pilot.PilotBlocked, "exclusive process lock"):
            pilot._save_checkpoint_locked(self.fixture.checkpoint, self.path.resolve())

    def test_second_process_is_blocked_and_lock_releases_after_exit_or_crash(self):
        for stop in ("normal", "crash"):
            with self.subTest(stop=stop):
                first = subprocess.Popen([sys.executable, "-c", LOCK_SCRIPT, str(self.path)],
                                         cwd=Path(pilot.__file__).parent, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertTrue(select.select([first.stdout], [], [], 10)[0], "Lock owner did not start")
                    self.assertEqual(first.stdout.readline().strip(), "LOCKED")
                    blocked = self.restart()
                    self.assertEqual(blocked.returncode, 1)
                    self.assertIn("Another live-pilot process", blocked.stdout)
                    if stop == "normal":
                        first.communicate("exit\n", timeout=10)
                    else:
                        first.send_signal(signal.SIGKILL)
                        first.communicate(timeout=10)
                    self.assertEqual(self.restored()["state"], pilot.DISABLED)
                finally:
                    if first.poll() is None:
                        first.kill()
                        first.communicate(timeout=10)
                    for stream in (first.stdin, first.stdout, first.stderr):
                        stream.close()


if __name__ == "__main__":
    unittest.main()
