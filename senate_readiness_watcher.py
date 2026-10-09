"""GET-only Senate preauthorization watcher. Never prepare or submit an order.

READY means the existing non-authorization gates for a supervised accounting
probe pass now. A separate explicit LIVE approval and manual probe command are
still required. Ordinary LIVE_PILOT accounting/activation gates stay closed.
"""
import argparse
import fcntl
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

import config
import execution_quarantine as quarantine
import live_pilot as pilot
import live_settlement as live
import paired_account_test as paired
import pilot_account
import price_reader as scanner
import supervised_accounting_probe as probe
from order_preview import PreviewBlocked, executable_limits

READY, NOT_READY = "READY", "NOT READY"
MARKETS = ["153", "154"]
EXCHANGES = ["842", "843"]
SLUG = "midterm-elections"


class GetOnly:
    """Expose only official GET reads to the reused readiness helpers."""
    def __init__(self, session):
        self._session = session

    def get(self, url, **kwargs):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.netloc != "sig.thesuper.market"
                or not parsed.path.startswith("/api/v1/")
                or kwargs.get("allow_redirects", False) is not False):
            raise ValueError("Watcher only permits official API GETs without redirects")
        return self._session.get(url, **dict(kwargs, allow_redirects=False))


@contextmanager
def readonly_pilot_lock():
    """Share the pilot's stable exclusive lock without creating or repairing state.

    pilot_lock/load_checkpoint can WRITE a restart halt for EXECUTING state.
    This watcher instead opens the existing lock read-only and uses the strict
    read-only loader. Closing the stream releases flock, including on Ctrl+C.
    Hold it for one complete observation, then release it between scan cycles.
    """
    path = pilot.ALLOCATION_PATH.resolve()
    lock_path = path.with_name("." + path.name + ".lock")
    with lock_path.open("r") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield path
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def reviewed_paper_pair():
    """Use recorded reviewed facts as an evidence anchor, never LIVE permission."""
    entries = scanner.load_approved_settlements()
    matches = [entry for entry in entries if entry["market_ids"] == MARKETS]
    if (len(matches) != 1 or matches[0]["exchange_ids"] != EXCHANGES
            or matches[0]["tournament_id"] != paired.MANUAL_TOURNAMENT
            or matches[0]["position_types"] != ["NO-PAIR"]
            or matches[0]["max_quantity"] != 1 or "manual_approval" not in matches[0]):
        raise live.LiveSettlementBlocked("Recorded Senate settlement review is missing or mismatched")
    return matches[0]


def blocked(code, reason, quote=None):
    return {"status": NOT_READY, "code": code, "reason": reason, "quote": quote,
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "submission_enabled": False, "all_non_authorization_gates_passed": False}


def check_readiness(session):
    """One full GET-only review; no intents, authorization drafts or state writes.

    Account/quote checks are the existing probe checks. Only its documented
    ACCOUNTING_MODEL_UNVERIFIED exception is allowed: verifying accounting is
    the probe's purpose, not permission for ordinary LIVE_PILOT trading.
    """
    stage, quote = "BASELINE", None
    try:
        pilot.require(config.LIVE_PILOT_SUBMISSION_ENABLED is False,
                      "LIVE_PILOT must remain disabled for this watcher")
        with readonly_pilot_lock() as state_path:
            state_bytes = state_path.read_bytes()
            checkpoint = pilot._read_checkpoint_locked(state_path)
            pilot.require_execution_clear(checkpoint)
            pilot.require_initial_evidence_clear(state_path)
            pilot.require(checkpoint["tournament_id"] == paired.MANUAL_TOURNAMENT,
                          "Pilot baseline belongs to a different tournament")

            stage = "SETTLEMENT"
            paper = reviewed_paper_pair()
            authorizations = live.load_authorizations()
            markets, evidence = paired.read_manual_evidence(session, paired.MANUAL_TOURNAMENT, *MARKETS)
            scanner.check_approved_pair(paper, markets, paired.MANUAL_TOURNAMENT)
            live.require(all(evidence[key] == value for key, value in paper["manual_approval"].items()),
                         "Fresh official settlement evidence differs from the recorded review")
            quarantine.require_unblocked_markets(MARKETS)
            quarantine.require_unblocked_exchanges(EXCHANGES)
            # Absence of LIVE permission is expected in this preauthorization
            # review. If already listed, validate that real entry without a
            # synthetic permission or a fallback to the paper approval.
            listed = any(set(entry["market_ids"]) == set(MARKETS) for entry in authorizations)
            if listed:
                authorization = live.configured_authorization(MARKETS, paired.MANUAL_TOURNAMENT, SLUG)
                live.verify_settlement(session, markets, paired.MANUAL_TOURNAMENT, authorization)

            stage = "ACCOUNT"
            snapshot = pilot_account.read_snapshot(session, SLUG, checkpoint)
            probe.capture_default_balance(session, snapshot)
            assessment = pilot_account.assess_snapshot(snapshot, checkpoint, EXCHANGES, probe.MAX_PAIR_DEBIT, 1)
            if [row["code"] for row in assessment["failures"]] != [pilot_account.ACCOUNTING_UNVERIFIED]:
                failure = assessment["failures"][0]
                return blocked(failure["code"], failure["reason"])
            if snapshot["account"]["orders"]:
                return blocked("OPEN_ORDERS_PREVENT_ISOLATION", "Open orders prevent an isolated accounting probe")

            stage = "QUOTES"
            books = [scanner.get_best_prices(session, market, paired.MANUAL_TOURNAMENT) for market in markets]
            started = snapshot["freshness"]["started_monotonic"]
            prices, quantity, cost, edge = executable_limits("NO-PAIR", books, started, quantity=1)
            quote = {"prices": [f"{price:.3f}" for price in prices], "pair_notional": str(cost),
                     "ordinary_edge": str(edge), "return_on_capital": str(edge / cost),
                     "depth": [book["bid_quantity"] for book in books], "quantity_per_leg": quantity}

            # Re-read the small local files before announcing READY. Edits or
            # new execution evidence during GETs must invalidate this cycle.
            stage = "SETTLEMENT"
            live.require(reviewed_paper_pair() == paper and live.load_authorizations() == authorizations,
                         "Settlement configuration changed during the observation")
            stage = "BASELINE"
            pilot.require(state_path.read_bytes() == state_bytes, "Pilot state changed during the observation")
            pilot.require_execution_clear(checkpoint)
            pilot.require_initial_evidence_clear(state_path)
            stage = "QUOTES"
            # Reuse the exact Decimal/tick-rounded 0.005 probe edge and <=1
            # notional policy, rechecking account/book freshness at the end.
            probe.observed_probe_limits(books, started)
            return {"status": READY, "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "quote": quote, "evidence_hash": evidence["evidence_hash"],
                    "authorization": "EXPLICITLY_CONFIGURED" if listed else "PENDING",
                    "allocation_remaining": assessment["risk"]["allocation_remaining_after_reserves"],
                    "all_non_authorization_gates_passed": True, "submission_enabled": False}
    except BlockingIOError:
        return blocked("BASELINE_LOCK_BUSY", "Pilot state is in use; this observation cannot verify readiness")
    except quarantine.QuarantineError:
        return blocked("QUARANTINE_ISSUE", "Pair exclusion or frozen quarantine evidence requires review")
    except pilot_account.AccountReadinessBlocked as error:
        return blocked(error.code, str(error))
    except live.LiveSettlementBlocked as error:
        return blocked(error.code, str(error))
    except scanner.RateLimitError:
        # fetch_json remembers Retry-After; later cycles honor that cooldown.
        return blocked("API_RATE_LIMITED", "Official API read cooldown is active; readiness is unavailable")
    except requests.RequestException:
        return blocked("API_UNAVAILABLE", "Official API response unavailable; readiness is not confirmed")
    except probe.ProbeBlocked:
        return blocked("PROBE_EDGE_NOT_READY", "Tick-rounded pair must cost <=1.000 with ordinary edge >=0.005", quote)
    except PreviewBlocked as error:
        return blocked("QUOTE_CHECK_FAILED" if stage == "QUOTES" else live.NEEDS_REVALIDATION, str(error), quote)
    except pilot.PilotBlocked as error:
        return blocked("BASELINE_ISSUE", str(error))
    except (OSError, *scanner.API_ERRORS):
        code = {"BASELINE": "BASELINE_ISSUE", "SETTLEMENT": live.NEEDS_REVALIDATION,
                "ACCOUNT": "ACCOUNT_DATA_UNAVAILABLE", "QUOTES": "QUOTE_DATA_UNAVAILABLE"}[stage]
        return blocked(code, "Required read-only data is missing, invalid or changed; manual review may be needed")


def status_key(observation):
    """Ignore moving timestamps/prices while the readiness verdict is unchanged."""
    if observation["status"] == READY:
        return (READY, observation["evidence_hash"], observation["authorization"])
    return (NOT_READY, observation["code"], observation["reason"])


def report_change(previous, observation):
    """Print the first verdict and meaningful changes; return the latest verdict."""
    if previous is not None and status_key(previous) == status_key(observation):
        return observation
    print(f"{observation['observed_at']} | Senate 153/154 (842/843) NO-PAIR | {observation['status']}")
    if observation["status"] == NOT_READY:
        print(f"  {observation['code']} | {observation['reason']}")
    else:
        q = observation["quote"]
        print(f"  NO limits {q['prices'][0]} / {q['prices'][1]} | 1 contract per leg | "
              f"pair notional {q['pair_notional']} SUSQies")
        print(f"  Ordinary edge {q['ordinary_edge']} SUSQies | "
              f"return on capital {float(q['return_on_capital']) * 100:.3f}% | depth {q['depth'][0]} / {q['depth'][1]}")
        print("  Every non-authorization supervised-probe gate passes; fees remain unverified for the diagnostic probe.")
        print(f"  LIVE authorization: {observation['authorization']} | LIVE_PILOT disabled | no probe execution.")
    return observation


def watch(session, once=False):
    previous = None
    try:
        while True:
            started = time.monotonic()
            previous = report_change(previous, check_readiness(session))
            if once:
                return 0 if previous["status"] == READY else 1
            # Same start-to-start cadence as the paper scanner. GETs remain
            # paced, with the existing API Retry-After cooldown respected.
            time.sleep(max(0, scanner.SCAN_INTERVAL - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("Stopped read-only watcher; no authorization or execution state was changed.")
        return 130


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Report one read-only observation and exit")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    key = os.getenv("SIG_API_KEY")
    if not key:
        print("NOT READY | Local SIG_API_KEY is unavailable")
        return 1
    with requests.Session() as session:
        session.headers.update({"Authorization": "Bearer " + key})
        return watch(GetOnly(session), once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
