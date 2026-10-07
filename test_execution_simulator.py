"""Offline execution behavior, using disposable journals and fake venue files."""
import contextlib
import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import execution_simulator as simulator


class ExecutionSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal_path = Path(self.directory.name) / "execution_journal.json"
        self.venue_path = Path(self.directory.name) / "fake_venue.json"
        self.journal = simulator.create_journal()
        self.venue = simulator.create_venue()
        simulator.save_journal(self.journal, self.journal_path)
        simulator.save_venue(self.venue, self.venue_path)

    def prepare(self, race="Demo race", position_type="YES-PAIR", quantity=100,
                prices=(.4, .45), exchange_ids=("demo-dem", "demo-rep")):
        return simulator.prepare_pair(self.journal, self.journal_path, race,
                                      position_type, quantity, prices, exchange_ids=exchange_ids)

    def submit(self, attempt_id, scripts):
        return simulator.submit_pair(self.journal, self.journal_path, self.venue,
                                     self.venue_path, attempt_id, scripts=scripts)

    def reconcile(self, attempt_id, available=True):
        return simulator.reconcile(self.journal, self.journal_path, self.venue,
                                   attempt_id, available=available)

    def cancel(self, attempt_id, scripts=None):
        return simulator.request_cancellation(self.journal, self.journal_path, self.venue,
                                               self.venue_path, attempt_id, cancel_scripts=scripts)

    def attempt(self):
        return self.journal["attempts"][-1]

    def full_scripts(self, quantity=100):
        return [{"fill_quantity": quantity, "fill_price": .4},
                {"fill_quantity": quantity, "fill_price": .45}]

    def test_pair_intent_is_durable_before_any_fake_order(self):
        self.prepare()
        saved = simulator.load_journal(self.journal_path)
        self.assertEqual(len(saved["attempts"]), 1)
        self.assertEqual(simulator.pair_status(saved["attempts"][0]), "PREPARED")
        self.assertEqual(simulator.load_venue(self.venue_path)["orders"], {})
        self.assertAlmostEqual(simulator.pending_exposure(self.journal), 85)
        self.assertEqual(self.journal["cash"], 5000)

    def test_full_pair_fills_reconcile_quantity_cash_and_exposure(self):
        attempt_id = self.prepare()
        self.submit(attempt_id, self.full_scripts())
        self.reconcile(attempt_id)
        self.assertEqual(simulator.pair_status(self.attempt()), "COMPLETE")
        self.assertEqual([leg["filled_quantity"] for leg in self.attempt()["legs"]], [100, 100])
        self.assertAlmostEqual(self.journal["cash"], 4915)
        self.assertAlmostEqual(self.venue["cash"], 4915)
        self.assertEqual(simulator.pending_exposure(self.journal), 0)
        self.assertEqual(len(self.venue["orders"]), 2)

    def test_unequal_partial_fills_can_complete_by_filling_resting_remainders(self):
        attempt_id = self.prepare()
        self.submit(attempt_id, [{"fill_quantity": 30, "fill_price": .4},
                                {"fill_quantity": 10, "fill_price": .45}])
        self.reconcile(attempt_id)
        self.assertEqual([leg["filled_quantity"] for leg in self.attempt()["legs"]], [30, 10])
        self.assertAlmostEqual(self.journal["cash"], 4983.5)
        self.assertAlmostEqual(simulator.pending_exposure(self.journal), 68.5)
        for leg, quantity, price, fill_id in zip(self.attempt()["legs"], (70, 90), (.4, .45),
                                               ("dem-follow-up", "rep-follow-up")):
            simulator.fill_order(self.venue, self.venue_path, leg["client_order_id"],
                                 fill_id, quantity, price)
        self.reconcile(attempt_id)
        self.assertEqual(simulator.pair_status(self.attempt()), "COMPLETE")
        self.assertAlmostEqual(self.journal["cash"], 4915)
        self.assertEqual(simulator.pending_exposure(self.journal), 0)

    def test_second_leg_rejection_preserves_first_fill_and_blocks_new_pair(self):
        attempt_id = self.prepare()
        with contextlib.suppress(simulator.SimulatedReject):
            self.submit(attempt_id, [{"fill_quantity": 100, "fill_price": .4}, {"reject": True}])
        self.reconcile(attempt_id)
        with contextlib.suppress(simulator.SimulatedReject):
            self.cancel(attempt_id)
        self.reconcile(attempt_id)
        self.assertEqual(simulator.pair_status(self.attempt()), "UNHEDGED")
        self.assertEqual([leg["filled_quantity"] for leg in self.attempt()["legs"]], [100, 0])
        self.assertAlmostEqual(self.journal["cash"], 4960)
        with self.assertRaises(simulator.SimulationError):
            self.prepare(race="Different race", exchange_ids=("other-dem", "other-rep"))

    def test_cancellation_records_late_fills_before_remaining_orders_close(self):
        attempt_id = self.prepare()
        self.submit(attempt_id, [{"fill_quantity": 10, "fill_price": .4},
                                {"fill_quantity": 10, "fill_price": .45}])
        self.cancel(attempt_id, [{"late_fill_quantity": 20, "late_fill_price": .4}, {}])
        self.reconcile(attempt_id)
        self.assertEqual([leg["filled_quantity"] for leg in self.attempt()["legs"]], [30, 10])
        self.assertAlmostEqual(self.journal["cash"], 4983.5)
        self.assertEqual(simulator.pair_status(self.attempt()), "UNHEDGED")
        self.assertEqual(simulator.pending_exposure(self.journal), 0)

    def test_cancel_lost_ack_is_reconciled_without_reopening_or_refunding_fills(self):
        attempt_id = self.prepare()
        self.submit(attempt_id, [{"fill_quantity": 10, "fill_price": .4},
                                {"fill_quantity": 10, "fill_price": .45}])
        with contextlib.suppress(simulator.SimulatedTimeout):
            self.cancel(attempt_id, [{"lose_ack": True}, {}])
        self.reconcile(attempt_id)
        self.assertEqual(simulator.pair_status(self.attempt()), "PARTIAL_PAIR")
        self.assertAlmostEqual(self.journal["cash"], 4991.5)
        self.assertAlmostEqual(self.venue["cash"], 4991.5)
        self.assertEqual(simulator.pending_exposure(self.journal), 0)

    def test_unavailable_reconciliation_keeps_uncertainty_and_reserved_intent(self):
        attempt_id = self.prepare()
        with contextlib.suppress(simulator.SimulatedTimeout):
            self.submit(attempt_id, [{"fill_quantity": 25, "fill_price": .4, "lose_ack": True}, {}])
        before = copy.deepcopy(self.journal)
        with contextlib.suppress(simulator.SimulatedTimeout):
            self.reconcile(attempt_id, available=False)
        self.assertEqual(simulator.pair_status(self.attempt()), "UNKNOWN")
        self.assertEqual(self.journal["cash"], before["cash"])
        self.assertGreater(simulator.pending_exposure(self.journal), 0)
        with self.assertRaises(simulator.SimulationError):
            self.prepare(race="Other race", exchange_ids=("other-dem", "other-rep"))

    def test_duplicate_fill_id_is_idempotent_and_conflicting_retry_is_rejected(self):
        attempt_id = self.prepare(quantity=10)
        self.submit(attempt_id, [{}, {}])
        client_id = self.attempt()["legs"][0]["client_order_id"]
        simulator.fill_order(self.venue, self.venue_path, client_id, "one-fill", 10, .4)
        simulator.fill_order(self.venue, self.venue_path, client_id, "one-fill", 10, .4)
        self.reconcile(attempt_id)
        self.assertAlmostEqual(self.journal["cash"], 4996)
        self.assertAlmostEqual(self.venue["cash"], 4996)
        with self.assertRaises(simulator.SimulationError):
            simulator.fill_order(self.venue, self.venue_path, client_id, "one-fill", 9, .4)

    def test_overfill_price_violation_and_new_fill_after_close_do_not_change_cash(self):
        attempt_id = self.prepare(quantity=10)
        self.submit(attempt_id, [{}, {}])
        client_id = self.attempt()["legs"][0]["client_order_id"]
        for quantity, price in ((11, .4), (1, .5), (-1, .4), (True, .4)):
            with self.subTest(quantity=quantity, price=price), self.assertRaises(simulator.SimulationError):
                simulator.fill_order(self.venue, self.venue_path, client_id, "bad-fill", quantity, price)
        self.assertEqual(self.venue["cash"], 5000)
        simulator.fill_order(self.venue, self.venue_path, client_id, "complete-fill", 10, .4)
        with self.assertRaises(simulator.SimulationError):
            simulator.fill_order(self.venue, self.venue_path, client_id, "extra-fill", 1, .4)
        self.assertAlmostEqual(self.venue["cash"], 4996)

    def test_invalid_pair_inputs_never_create_venue_orders(self):
        for changes in ({"quantity": 0}, {"quantity": 101}, {"quantity": True},
                        {"prices": (.5, .49)}, {"prices": (-.1, .4)},
                        {"prices": (.4, float("nan"))}, {"position_type": "LIVE"},
                        {"exchange_ids": ("same", "same")}):
            with self.subTest(changes=changes), self.assertRaises(simulator.SimulationError):
                self.prepare(**changes)
        self.assertEqual(self.venue["orders"], {})
        self.assertEqual(self.journal["cash"], 5000)

    def test_same_race_capital_limit_survives_completed_pair(self):
        attempt_id = self.prepare()
        self.submit(attempt_id, self.full_scripts())
        self.reconcile(attempt_id)
        with self.assertRaises(simulator.SimulationError):
            self.prepare(position_type="NO-PAIR", exchange_ids=("other-dem", "other-rep"))
        self.assertEqual(len(self.journal["attempts"]), 1)
        self.assertAlmostEqual(self.journal["cash"], 4915)

    def test_renaming_a_race_cannot_bypass_instrument_limits_or_duplicates(self):
        attempt_id = self.prepare()
        self.submit(attempt_id, self.full_scripts())
        self.reconcile(attempt_id)
        for position_type in ("YES-PAIR", "NO-PAIR"):
            with self.subTest(position_type=position_type), self.assertRaises(simulator.SimulationError):
                self.prepare(race="Renamed race", position_type=position_type)
        self.assertEqual(len(self.journal["attempts"]), 1)

    def test_insufficient_cash_prevents_the_initial_pair_intent(self):
        self.journal = simulator.create_journal(starting_cash=50)
        self.venue = simulator.create_venue(starting_cash=50)
        simulator.save_journal(self.journal, self.journal_path)
        simulator.save_venue(self.venue, self.venue_path)
        with self.assertRaises(simulator.SimulationError):
            self.prepare()
        self.assertEqual(self.journal["attempts"], [])
        self.assertEqual(self.venue["orders"], {})
        self.assertEqual(self.journal["cash"], 50)

    def test_corrupt_saved_files_fail_closed_and_remain_untouched(self):
        for path, loader in ((self.journal_path, simulator.load_journal),
                             (self.venue_path, simulator.load_venue)):
            path.write_bytes(b"{broken JSON")
            with self.subTest(path=path), self.assertRaises(simulator.SimulationError):
                loader(path)
            self.assertEqual(path.read_bytes(), b"{broken JSON")

    def test_saved_cash_inconsistent_with_fills_is_rejected(self):
        for state, path, loader in ((self.journal, self.journal_path, simulator.load_journal),
                                    (self.venue, self.venue_path, simulator.load_venue)):
            tampered = copy.deepcopy(state)
            tampered["cash"] = 4999
            path.write_text(json.dumps(tampered))
            with self.subTest(path=path), self.assertRaises(simulator.SimulationError):
                loader(path)

    def test_journal_rejects_multiple_or_historical_unresolved_attempts(self):
        self.prepare()
        other = simulator.create_journal()
        simulator.prepare_pair(other, Path(self.directory.name) / "other_journal.json", "Other race",
                               "YES-PAIR", 100, (.4, .45), exchange_ids=("other-dem", "other-rep"))
        combined = copy.deepcopy(self.journal)
        combined["attempts"].extend(copy.deepcopy(other["attempts"]))
        with self.assertRaises(simulator.SimulationError):
            simulator.validate_journal(combined)
        for leg in other["attempts"][0]["legs"]:
            leg.update(status="filled", filled_quantity=100, filled_cost=100 * leg["limit_price"])
        other["cash"], other["reconciled"] = 4915, True
        simulator.validate_journal(other)
        combined["cash"] = 4915
        combined["attempts"] = copy.deepcopy(self.journal["attempts"] + other["attempts"])
        with self.assertRaises(simulator.SimulationError):
            simulator.validate_journal(combined)
        # Multiple attempts remain legitimate when the earlier one completed
        # and only the newest one is unresolved.
        combined["attempts"].reverse()
        simulator.validate_journal(combined)

    def test_confirmed_cancelled_order_cannot_reopen_in_a_later_snapshot(self):
        attempt_id = self.prepare(quantity=10)
        self.submit(attempt_id, [{"fill_quantity": 5, "fill_price": .4},
                                {"fill_quantity": 5, "fill_price": .45}])
        self.cancel(attempt_id)
        self.assertEqual(simulator.pair_status(self.attempt()), "PARTIAL_PAIR")
        changed = copy.deepcopy(self.venue)
        client_id = self.attempt()["legs"][0]["client_order_id"]
        changed["orders"][client_id]["status"] = "open"
        simulator.validate_venue(changed)
        with self.assertRaises(simulator.SimulationError):
            simulator.reconcile(self.journal, self.journal_path, changed, attempt_id)
        self.assertEqual(self.attempt()["legs"][0]["status"], "cancelled")
        self.assertAlmostEqual(self.journal["cash"], 4995.75)

    def test_confirmed_terminal_order_cannot_gain_a_new_fill(self):
        attempt_id = self.prepare(quantity=10)
        self.submit(attempt_id, [{"fill_quantity": 5, "fill_price": .4},
                                {"fill_quantity": 5, "fill_price": .45}])
        self.cancel(attempt_id)
        changed = copy.deepcopy(self.venue)
        client_id = self.attempt()["legs"][0]["client_order_id"]
        changed["orders"][client_id]["fills"].append({"id": "post-cancel-fill", "quantity": 1, "price": .4})
        changed["cash"] -= .4
        simulator.validate_venue(changed)
        with self.assertRaises(simulator.SimulationError):
            simulator.reconcile(self.journal, self.journal_path, changed, attempt_id)
        self.assertEqual(self.attempt()["legs"][0]["filled_quantity"], 5)
        self.assertAlmostEqual(self.journal["cash"], 4995.75)

    def test_same_fill_quantity_cannot_rewrite_previously_observed_cost(self):
        attempt_id = self.prepare(quantity=10)
        self.submit(attempt_id, [{"fill_quantity": 5, "fill_price": .3}, {}])
        changed = copy.deepcopy(self.venue)
        client_id = self.attempt()["legs"][0]["client_order_id"]
        changed["orders"][client_id]["fills"][0]["price"] = .4
        changed["cash"] -= .5
        simulator.validate_venue(changed)
        with self.assertRaises(simulator.SimulationError):
            simulator.reconcile(self.journal, self.journal_path, changed, attempt_id)
        self.assertEqual(self.attempt()["legs"][0]["filled_quantity"], 5)
        self.assertAlmostEqual(self.attempt()["legs"][0]["filled_cost"], 1.5)
        self.assertAlmostEqual(self.journal["cash"], 4998.5)

    def test_new_fill_cost_cannot_exceed_incremental_quantity_at_limit(self):
        attempt_id = self.prepare(quantity=10)
        self.submit(attempt_id, [{"fill_quantity": 5, "fill_price": .2}, {}])
        changed = copy.deepcopy(self.venue)
        client_id = self.attempt()["legs"][0]["client_order_id"]
        order = changed["orders"][client_id]
        # This snapshot obeys the venue's total-quantity and total-cost bounds,
        # but rewrites old prices while adding one share: cost rises by 1.40
        # although that new share's limit permits only 0.40 of extra spend.
        order["fills"][0]["price"] = .4
        order["fills"].append({"id": "new-share", "quantity": 1, "price": .4})
        changed["cash"] -= 1.4
        simulator.validate_venue(changed)
        with self.assertRaises(simulator.SimulationError):
            simulator.reconcile(self.journal, self.journal_path, changed, attempt_id)
        self.assertEqual(self.attempt()["legs"][0]["filled_quantity"], 5)
        self.assertAlmostEqual(self.attempt()["legs"][0]["filled_cost"], 1)
        self.assertAlmostEqual(self.journal["cash"], 4999)

    def test_intent_save_failure_prevents_any_fake_order(self):
        with patch.object(simulator, "save_journal", side_effect=simulator.SimulationError("disk full")), \
                self.assertRaises(simulator.SimulationError):
            self.prepare()
        self.assertEqual(simulator.load_venue(self.venue_path)["orders"], {})
        self.assertEqual(simulator.load_journal(self.journal_path)["attempts"], [])

    def test_venue_save_failure_has_no_fill_and_can_resume_saved_intent(self):
        attempt_id = self.prepare()
        with patch.object(simulator, "save_venue", side_effect=simulator.SimulationError("venue disk full")), \
                self.assertRaises(simulator.SimulationError):
            self.submit(attempt_id, self.full_scripts())
        self.assertEqual(simulator.load_venue(self.venue_path)["orders"], {})
        self.assertEqual(simulator.pair_status(self.attempt()), "UNKNOWN")
        self.assertEqual(self.journal["cash"], 5000)
        self.journal = simulator.load_journal(self.journal_path)
        self.venue = simulator.load_venue(self.venue_path)
        self.submit(attempt_id, self.full_scripts())
        self.reconcile(attempt_id)
        self.assertEqual(simulator.pair_status(self.attempt()), "COMPLETE")
        self.assertAlmostEqual(self.journal["cash"], 4915)
        self.assertEqual(len(self.venue["orders"]), 2)

    def test_accepted_order_survives_journal_save_failure_and_resume_is_idempotent(self):
        attempt_id = self.prepare()
        real_save = simulator.save_journal

        def fail_after_venue_acceptance(state, path):
            if simulator.load_venue(self.venue_path)["orders"]:
                raise simulator.SimulationError("journal disk full after acceptance")
            real_save(state, path)

        with patch.object(simulator, "save_journal", side_effect=fail_after_venue_acceptance), \
                self.assertRaises(simulator.SimulationError):
            self.submit(attempt_id, self.full_scripts())
        self.assertEqual(len(simulator.load_venue(self.venue_path)["orders"]), 1)
        self.journal = simulator.load_journal(self.journal_path)
        self.venue = simulator.load_venue(self.venue_path)
        self.reconcile(attempt_id)
        self.submit(attempt_id, self.full_scripts())
        self.reconcile(attempt_id)
        self.assertEqual(len(self.venue["orders"]), 2)
        self.assertEqual(simulator.pair_status(self.attempt()), "COMPLETE")
        self.assertAlmostEqual(self.journal["cash"], 4915)

    def test_actual_process_restart_does_not_duplicate_an_accepted_order(self):
        attempt_id = self.prepare()
        with self.assertRaises(simulator.SimulatedCrash):
            self.submit(attempt_id, [{"fill_quantity": 100, "fill_price": .4,
                                      "crash_after_accept": True},
                                     {"fill_quantity": 100, "fill_price": .45}])
        self.assertEqual(len(simulator.load_venue(self.venue_path)["orders"]), 1)
        code = """
import json, sys
from pathlib import Path
import execution_simulator as simulator
journal_path, venue_path = Path(sys.argv[1]), Path(sys.argv[2])
attempt_id = sys.argv[3]
journal = simulator.load_journal(journal_path)
venue = simulator.load_venue(venue_path)
simulator.reconcile(journal, journal_path, venue, attempt_id)
simulator.submit_pair(journal, journal_path, venue, venue_path, attempt_id,
                      scripts=[{'fill_quantity': 100, 'fill_price': .4},
                               {'fill_quantity': 100, 'fill_price': .45}])
simulator.reconcile(journal, journal_path, venue, attempt_id)
print(json.dumps({'orders': len(venue['orders']), 'cash': journal['cash'],
                  'status': simulator.pair_status(journal['attempts'][-1])}))
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.journal_path),
                                 str(self.venue_path), attempt_id],
                                cwd=Path(simulator.__file__).resolve().parent,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]),
                         {"orders": 2, "cash": 4915, "status": "COMPLETE"})

    def test_default_cli_is_offline_and_never_uses_credentials_or_paper_files(self):
        # These recognizable filenames contain disposable sentinels, never a
        # real key or the user's portfolio. Any attempt to open them is a failure.
        root = Path(self.directory.name)
        for name in (".env", "paper_portfolio.json", "paper_trades.csv"):
            (root / name).write_bytes(b"offline test sentinel")
        before = {path.name: path.read_bytes() for path in root.iterdir()}
        original_cwd = Path.cwd()
        real_open, real_getenv = io.open, os.getenv

        def guarded_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and Path(file).name in {
                    ".env", "paper_portfolio.json", "paper_trades.csv"}:
                raise AssertionError("The offline simulator accessed a protected file")
            return real_open(file, *args, **kwargs)

        def guarded_getenv(name, default=None):
            if name == "SIG_API_KEY":
                raise AssertionError("The offline simulator read an API key")
            return real_getenv(name, default)

        try:
            os.chdir(root)
            with patch("sys.argv", ["execution_simulator.py"]), \
                    patch("builtins.open", side_effect=guarded_open), \
                    patch.object(io, "open", side_effect=guarded_open), \
                    patch.object(os, "getenv", side_effect=guarded_getenv), \
                    patch.object(socket, "create_connection", side_effect=AssertionError("Network used")), \
                    patch.object(socket.socket, "connect", side_effect=AssertionError("Network used")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(simulator.main(), 0)
        finally:
            os.chdir(original_cwd)
        self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)
        for name in simulator.SCENARIOS:
            self.assertIn(f"SCENARIO: {name}", output.getvalue())
        self.assertIn("OFFLINE execution simulator", output.getvalue())


if __name__ == "__main__":
    unittest.main()
