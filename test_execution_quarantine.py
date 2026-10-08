"""Quarantine safety checks: fake HTTP, temporary journals, no credentials."""
import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import account_reader
import account_test as single
import execution_quarantine as quarantine
import order_preview as preview
import paired_account_test as paired
import price_reader as scanner
from test_account_reader import holdings, order, orders_page, position, tournament
from test_api_audit import QUOTE_TIME, TOURNAMENT_ID, election_tree, exchange_book, page, pair_relationship, party_market
from test_order_preview import account_snapshot


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.active = self.root / "active"
        self.registry = self.root / "quarantine.json"
        self.session = MagicMock()
        for target, name, value in (
            (quarantine, "STATE_PATH", self.registry), (quarantine, "SOURCE_DIR", self.source),
            (quarantine, "ACTIVE_DIR", self.active), (paired, "STATE_DIR", self.source),
            (single, "STATE_PATH", self.root / "single.json"),
            (scanner, "READ_REQUEST_SPACING", 0), (scanner, "_last_request_started", None),
            (scanner, "_read_cooldown_until", 0),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("time", QUOTE_TIME), ("monotonic", 100)):
            patcher = patch.object(paired.time, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        payloads = [tournament(), holdings([]), orders_page([]),
                    party_market("Democratic", 387, 1076), party_market("Republican", 388, 1077),
                    page([]), election_tree("Democratic", 387), election_tree("Republican", 388),
                    exchange_book(387, 1076, bid=.875, ask=.88), exchange_book(388, 1077, bid=.155, ask=.16)]
        self.set_reads(payloads)
        pair, legs = paired.prepare_pair(self.session, self.source, "387", "388", conditional=True)
        single.commit_state(legs[0], paired.leg_path(self.source, 0), "UNKNOWN")
        paired.set_state(pair, self.source, "UNKNOWN")
        self.original = {name: (self.source / name).read_bytes() for name in quarantine.JOURNALS}
        quarantine.create_quarantine()
        self.session.reset_mock()

    def set_reads(self, payloads):
        responses = []
        for payload in payloads:
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            responses.append(response)
        self.session.get.side_effect = responses

    def unrelated_reads(self, cash=10):
        metadata = tournament()
        metadata["myBalance"] = cash
        return [metadata, holdings([]), orders_page([]),
                party_market("Democratic", 1, 11), party_market("Republican", 2, 12),
                page([pair_relationship()]), election_tree("Democratic", 1), election_tree("Republican", 2),
                exchange_book(1, 11, bid=.6), exchange_book(2, 12, bid=.6)]

    def assert_source_unchanged(self):
        self.assertEqual(self.original, {name: (self.source / name).read_bytes() for name in quarantine.JOURNALS})
        pair, legs = paired.load_pair(self.source)
        self.assertEqual(pair["state"], "UNKNOWN")
        self.assertEqual([leg["state"] for leg in legs], ["UNKNOWN", "PREPARED"])

    def test_market_and_every_pair_containing_387_are_blocked_before_gets(self):
        for ids in (("387", "388"), ("387", "2"), ("1", "387")):
            with self.subTest(ids=ids), self.assertRaises(quarantine.QuarantineError):
                preview.read_selected_pair(self.session, TOURNAMENT_ID, *ids)
            with self.assertRaises(quarantine.QuarantineError):
                paired.prepare_pair(self.session, self.active, *ids)
        with self.assertRaises(quarantine.QuarantineError):
            single.read_test_inputs(self.session, "387", "yes", "midterm-elections")
        with self.assertRaises(quarantine.QuarantineError):
            preview.account_risk(account_snapshot(), ["1076"])
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()
        self.assert_source_unchanged()

    def test_manual_prepared_copy_cannot_submit_387_or_replay_original(self):
        original = single.load_intent(paired.leg_path(self.source, 0))
        copy_intent = copy.deepcopy(original)
        copy_intent["state"] = "PREPARED"
        path = self.root / "copied.json"
        single.save_intent(copy_intent, path, new=True)
        for intent, destination in ((copy_intent, path), (original, paired.leg_path(self.source, 0))):
            with self.assertRaises(quarantine.QuarantineError):
                single.submit_test(self.session, intent, destination, intent["approval"])
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()
        self.assert_source_unchanged()

    def test_full_exposure_reserved_even_when_api_reports_nothing_and_after_expiry(self):
        risk = preview.account_risk(account_snapshot(cash=10), ["11", "12"])
        self.assertEqual(risk["quarantine_reserve"], .125)
        self.assertEqual(risk["available_cash"], 9.875)
        self.assertEqual(risk["race_exposure_upper_bound"], .125)
        with patch.object(paired.time, "time", return_value=QUOTE_TIME + 10 ** 8):
            self.assertEqual(preview.account_risk(account_snapshot(), ["11", "12"])["quarantine_reserve"], .125)
        self.assert_source_unchanged()

    def test_visible_possible_fill_does_not_release_quarantine_reserve(self):
        holding = position(1076, 387, -1)
        holding.update(costBasis=.125)
        resting = order(900, 1076)
        resting.update(side="no", quantity=1, priceLimit=.125)
        risk = preview.account_risk(account_snapshot([holding], [resting], cash=10), ["11", "12"])
        self.assertEqual(risk["available_cash"], 8.875)
        self.assertEqual(risk["race_exposure_upper_bound"], 1.25)
        self.assertEqual(risk["quarantine_reserve"], .125)

    def test_unrelated_verified_pair_can_prepare_and_pass_fresh_preflight(self):
        self.set_reads(self.unrelated_reads())
        pair, _ = paired.prepare_pair(self.session, self.active, "1", "2")
        self.assertEqual(pair["policy"], paired.VERIFIED_POLICY)
        self.assertEqual(pair["state"], "PREPARED")
        self.set_reads(self.unrelated_reads())
        self.assertEqual(paired.preflight_pair(self.session, pair), 10)
        self.assertEqual(quarantine.active_pair_directory(self.source), self.active)
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()
        self.assert_source_unchanged()

    def test_cli_routes_unrelated_readiness_to_one_fixed_active_journal(self):
        self.set_reads(self.unrelated_reads())
        self.session.__enter__.return_value = self.session
        args = ["paired_account_test.py", "prepare", "--dem-market", "1", "--rep-market", "2"]
        with patch("sys.argv", args), patch.object(paired, "load_dotenv"), \
                patch.object(paired.os, "getenv", return_value="fake-quarantine-test-key"), \
                patch.object(paired.requests, "Session", return_value=self.session), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(paired.main(), 0)
        self.assertEqual(paired.load_pair(self.active)[0]["state"], "PREPARED")
        with patch("sys.argv", args), patch.object(paired, "load_dotenv") as dotenv, \
                patch.object(paired.requests, "Session") as factory, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(paired.main(), 1)
            dotenv.assert_not_called()
            factory.assert_not_called()
        self.session.post.assert_not_called()
        self.assert_source_unchanged()

    def test_reserve_still_enforces_cash_and_race_caps_for_unrelated_pair(self):
        self.set_reads(self.unrelated_reads(cash=.9))  # .8 pair + .125 reserve exceeds cash.
        with self.assertRaises(preview.PreviewBlocked):
            paired.prepare_pair(self.session, self.active, "1", "2")
        self.assertFalse(self.active.exists())
        holding = position(99, 99, 1)
        holding.update(costBasis=149.2)
        payloads = self.unrelated_reads(cash=1000)
        payloads[1] = holdings([holding])
        self.set_reads(payloads)
        with self.assertRaises(preview.PreviewBlocked):
            paired.prepare_pair(self.session, self.active, "1", "2")
        self.session.post.assert_not_called()

    def test_late_quarantined_fill_cannot_be_ignored_by_new_pair_cash_reconciliation(self):
        self.set_reads(self.unrelated_reads())
        pair, legs = paired.prepare_pair(self.session, self.active, "1", "2")
        pair["starting_cash"] = 10
        paired.set_state(pair, self.active, "EXECUTING")
        single.commit_state(legs[0], paired.leg_path(self.active, 0), "OBSERVED_TERMINAL", order_id=101,
                            observation={"placement_quantity": 1, "placement_cost": .4,
                                         "filled_quantity": 1, "filled_cost": .4, "open": False,
                                         "terminal_reason": "closed"})
        holding = position(11, 1, -1)
        holding.update(costBasis=.4)
        account = account_snapshot([holding], cash=10 - .4 - .125)
        with patch.object(paired, "read_account", return_value=account), \
                self.assertRaisesRegex(single.TestError, "Account cash disagrees"):
            paired.reconcile_pair_account(self.session, pair, legs)
        self.assertEqual(legs[1]["state"], "PREPARED")
        self.session.post.assert_not_called()
        self.assert_source_unchanged()

    def test_original_controller_and_legs_cannot_be_written_checked_or_cancelled(self):
        pair, legs = paired.load_pair(self.source)
        for operation in (
            lambda: paired.submit_pair(self.session, self.source, pair["approval"]),
            lambda: paired.check_pair(self.session, self.source),
            lambda: paired.cancel_pair(self.session, self.source, pair["approval"]),
            lambda: paired.save_pair(pair, self.source),
            lambda: single.save_intent(legs[0], paired.leg_path(self.source, 0)),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()
        self.session.delete.assert_not_called()
        self.assert_source_unchanged()

    def test_changed_or_missing_source_fails_closed_for_unrelated_readiness(self):
        path = self.source / "leg_1.json"
        for contents in (self.original["leg_1.json"] + b"\n", None):
            if contents is None:
                path.unlink()
            else:
                path.write_bytes(contents)
            with self.assertRaises(quarantine.QuarantineError):
                preview.account_risk(account_snapshot(), ["11", "12"])
            path.write_bytes(self.original["leg_1.json"])
        self.assert_source_unchanged()

    def test_missing_or_corrupt_registry_never_unlocks_an_active_pair(self):
        self.active.mkdir()
        contents = self.registry.read_bytes()
        for replacement in (None, b"{}", b"not-json"):
            if replacement is None:
                self.registry.unlink()
            else:
                self.registry.write_bytes(replacement)
            with self.assertRaises(quarantine.QuarantineError):
                preview.account_risk(account_snapshot(), ["11", "12"])
            self.registry.write_bytes(contents)
        self.assert_source_unchanged()

    def test_missing_registry_before_any_active_pair_also_fails_closed(self):
        self.registry.unlink()
        with self.assertRaises(quarantine.QuarantineError):
            preview.account_risk(account_snapshot(), ["11", "12"])
        with self.assertRaises(quarantine.QuarantineError):
            quarantine.active_pair_directory(self.source)
        self.assert_source_unchanged()

    def test_account_and_readiness_output_show_quarantine_and_full_reserve(self):
        output = io.StringIO()
        self.set_reads(self.unrelated_reads())
        account = account_reader.read_account(self.session)
        draft = preview.preview_pair(self.session, account, "1", "2", "NO-PAIR", 100, quantity=1)
        with contextlib.redirect_stdout(output):
            account_reader.print_account_summary(account)
            preview.print_preview(draft)
        self.assertIn("QUARANTINE | market 387", output.getvalue())
        self.assertIn("0.125", output.getvalue())
        self.assertIn("9.875", output.getvalue())
        self.assertEqual(account["quarantine_reserve"], .125)
        self.session.post.assert_not_called()

    def test_restart_reloads_reserve_and_block_without_changing_unknown_journal(self):
        script = '''
import json, sys
from pathlib import Path
import execution_quarantine as q
q.STATE_PATH, q.SOURCE_DIR, q.ACTIVE_DIR = map(Path, sys.argv[1:])
exposure = q.load_quarantine()
assert exposure["state"] == "UNKNOWN" and exposure["reserved_cost"] == .125
assert q.reserved_cost(exposure["tournament_id"]) == .125
try:
    q.require_unblocked_markets(["387"])
except q.QuarantineError:
    pass
else:
    raise AssertionError("Quarantined market was unblocked after restart")
assert q.active_pair_directory(q.SOURCE_DIR) == q.ACTIVE_DIR
print("Restart preserved quarantine, reserve and market block")
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.registry), str(self.source), str(self.active)],
                                cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Restart preserved quarantine", result.stdout)
        self.assert_source_unchanged()

    def test_local_quarantine_cli_and_show_never_load_credentials_or_open_http_session(self):
        self.registry.unlink()  # Recreate only this test's local registry through the CLI.
        for action in ("quarantine", "show"):
            with patch("sys.argv", ["paired_account_test.py", action]), \
                    patch.object(paired, "load_dotenv") as dotenv, patch.object(paired.requests, "Session") as factory, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(paired.main(), 0)
                dotenv.assert_not_called()
                factory.assert_not_called()
        self.assert_source_unchanged()


if __name__ == "__main__":
    unittest.main()
