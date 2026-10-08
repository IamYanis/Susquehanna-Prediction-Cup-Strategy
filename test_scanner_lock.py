"""Real process tests for the scanner lock; no credentials, API or paper trades."""
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_trader
import price_reader as scanner


# Exercise main() with the actual lock, while replacing all account/network and
# trading work. The first process waits inside its scan until the test ends it.
PROCESS_SCRIPT = """
import sys
from pathlib import Path
from unittest.mock import patch
import paper_trader
import price_reader as scanner

paper_trader.PORTFOLIO_PATH = Path(sys.argv[1]) / 'paper_portfolio.json'
mode = sys.argv[2]

def load_portfolio():
    print('PORTFOLIO_LOADED', flush=True)
    return False

def scan_once(*args, **kwargs):
    print('READY', flush=True)
    if mode == 'hold':
        command = sys.stdin.readline().strip()
        if command == 'error':
            raise RuntimeError('simulated scanner failure')

with patch.object(scanner, 'load_dotenv'), \
     patch.object(scanner.os, 'getenv', return_value='offline-lock-test-key'), \
     patch.object(scanner, 'load_portfolio', side_effect=load_portfolio), \
     patch.object(scanner, 'get_tournament', return_value='550e8400-e29b-41d4-a716-446655440000'), \
     patch.object(scanner, 'scan_once', side_effect=scan_once), \
     patch.object(scanner, 'print_portfolio_summary'), \
     patch.object(scanner.requests, 'Session'), \
     patch('sys.argv', ['price_reader.py', '--once']):
    result = scanner.main()
raise SystemExit(result)
"""


class ScannerLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        patcher = patch.object(paper_trader, "PORTFOLIO_PATH", self.directory / "paper_portfolio.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.first = subprocess.Popen(
            self.command("hold"), cwd=Path(scanner.__file__).parent,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_first)
        # A pipe handshake establishes that the first process owns the lock and
        # is scanning. Use bounded reads so a startup failure cannot hang tests.
        output = b""
        deadline = time.monotonic() + 10
        while b"READY\n" not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.first.stdout], [], [], remaining)[0]:
                self.fail("First scanner did not become ready")
            chunk = os.read(self.first.stdout.fileno(), 4096)
            if not chunk:
                self.fail("First scanner exited before acquiring its lock")
            output += chunk
        self.first_output = output.decode()

    def command(self, mode):
        return [sys.executable, "-c", PROCESS_SCRIPT, str(self.directory), mode]

    def stop_first(self):
        if self.first.poll() is None:
            try:
                self.first.communicate("exit\n", timeout=5)
            except subprocess.TimeoutExpired:
                self.first.kill()
                self.first.communicate(timeout=5)
        for stream in (self.first.stdin, self.first.stdout, self.first.stderr):
            stream.close()

    def probe(self):
        return subprocess.run(self.command("probe"), cwd=Path(scanner.__file__).parent,
                              capture_output=True, text=True, timeout=10)

    def assert_probe_acquires(self):
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PORTFOLIO_LOADED", result.stdout)
        self.assertIn("READY", result.stdout)

    def test_first_process_acquires_lock(self):
        self.assertIsNone(self.first.poll())
        self.assertIn("PORTFOLIO_LOADED", self.first_output)
        self.assertTrue((self.directory / ".paper_scanner.lock").exists())
        with self.assertRaisesRegex(scanner.ScannerLockError, "already running"):
            with scanner.scanner_lock():
                self.fail("Competing caller unexpectedly acquired the first process's lock")

    def test_second_process_is_blocked_before_loading_portfolio(self):
        result = self.probe()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Another paper scanner instance is already running; exiting.", result.stdout)
        self.assertNotIn("PORTFOLIO_LOADED", result.stdout)
        self.assertNotIn("READY", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_lock_released_when_first_process_exits_normally(self):
        self.first.communicate("exit\n", timeout=10)
        self.assertEqual(self.first.returncode, 0)
        # The persistent file is harmless: ownership is the OS lock, not the
        # file's presence. A new process must acquire the same file successfully.
        self.assertTrue((self.directory / ".paper_scanner.lock").exists())
        self.assert_probe_acquires()

    def test_lock_released_after_ctrl_c(self):
        self.first.send_signal(signal.SIGINT)
        output, _ = self.first.communicate(timeout=10)
        self.assertEqual(self.first.returncode, 0)
        self.assertIn("Scanner stopped.", output)
        self.assert_probe_acquires()

    def test_lock_released_after_unexpected_exception(self):
        _, error = self.first.communicate("error\n", timeout=10)
        self.assertNotEqual(self.first.returncode, 0)
        self.assertIn("simulated scanner failure", error)
        self.assert_probe_acquires()


if __name__ == "__main__":
    unittest.main()
