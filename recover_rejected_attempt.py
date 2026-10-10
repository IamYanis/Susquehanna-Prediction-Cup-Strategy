"""Explicit recovery of a first-leg HTTP 403 missing-trade-scope rejection.

Default: GET-only dry run. --apply is a separate, local atomic state change.
There is no order submission/cancellation adapter or retry in this command.
"""
import argparse
import copy
import json
import os
import threading
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import autonomous_pilot as auto
import execution_quarantine as quarantine
import live_pilot as pilot
import pilot_account
import price_reader as scanner
import supervised_accounting_probe as probe
from senate_readiness_watcher import GetOnly, readonly_pilot_lock


def rejected_leg(attempt):
    """Do not treat timeouts, generic 403s or empty history as a rejection."""
    first, second = attempt["legs"]
    pilot.require(attempt["halted_from"] == pilot.LEG1_SUBMITTING
                  and auto.missing_trade_scope(first["placement_response"])
                  and first["post_attempted"] and first["intent"] is not None
                  and first["intent"]["state"] == "SUBMITTING"
                  and first["intent"]["order_id"] is None and first["intent"]["observation"] is None
                  and all(first[k] is None for k in ("receipt", "activity", "review"))
                  and second["intent"] is None and not second["post_attempted"]
                  and all(second[k] is None for k in ("placement_response", "receipt", "activity", "review"))
                  and attempt["after"] == [None, None] and attempt["completion"] is None,
                  "Recovery only supports the definite first-leg HTTP 403 missing-trade rejection")
    return first


def eligible_attempt(checkpoint, key):
    pilot.require(checkpoint["state"] == pilot.HALTED and checkpoint["manual_review_required"],
                  "Recovery requires an existing persistent halt")
    journal = checkpoint.get("autonomous_execution", {})
    pilot.require(journal.get("active_attempt") == key and key in journal.get("attempts", {}),
                  "Recovery must name the active rejected attempt exactly")
    attempt = journal["attempts"][key]
    pilot.require(attempt["state"] == pilot.HALTED and "rejection_recovery" not in attempt,
                  "Attempt is not an unrecovered rejection; never repeat recovery")
    rejected_leg(attempt)
    pilot.require(all(k == key or a["state"] in {pilot.READY, pilot.REJECTED_RETIRED}
                      for k, a in journal["attempts"].items())
                  and all(k == key or e["execution_status"] in {"RECONCILED_PAIR", pilot.REJECTED_RETIRED}
                          for k, e in checkpoint["live_exposures"].items())
                  and not any(p["status"] == "EXITING" for p in checkpoint.get("autonomous_positions", {}).values()),
                  "Another unresolved execution prevents recovery")
    exposure = checkpoint["live_exposures"][key]
    pilot.require(exposure["execution_status"] == "MANUAL_REVIEW"
                  and all(pilot.amount(v) == 0 for k in ("confirmed_quantities", "confirmed_costs") for v in exposure[k])
                  and pilot.amount(exposure["capital_charge"]) == 0
                  and list(map(pilot.amount, exposure["possible_additional_quantities"])) == [1, 0],
                  "Rejected attempt has unexpected confirmed or possible exposure")
    return attempt


def read_orders(session, tournament_id):
    """All statuses, full pagination and explicit projection coverage."""
    class CompleteOrders:
        def get(self, url, **kwargs):
            response = session.get(url, **kwargs)
            response.raise_for_status()
            pilot.require(response.json().get("coverage", {}).get("complete") is True,
                          "All-order history coverage is incomplete")
            return response

    rows = scanner.fetch_pages(CompleteOrders(), scanner.API_BASE_URL + "/orders",
                               {"tournamentId": tournament_id, "status": "all", "limit": 200})
    for row in rows:
        pilot.require(type(row["id"]) is int and row["id"] > 0 and row["tournamentId"] == tournament_id,
                      "Order history identity or tournament scope is invalid")
        scanner.numeric_id(row["exchangeId"])
        auto.single.parse_api_timestamp(row["createdAt"])
    return rows


def prove_clear(checkpoint, attempt, snapshot, orders):
    """Saved 403 PLUS complete account evidence, never absence alone."""
    before, after = attempt["before"], snapshot
    first = rejected_leg(attempt)
    stamp = auto.single.parse_api_timestamp(first["intent"]["created_at"])
    account = after["account"]
    pilot.require(after["data_complete"] is True
                  and all(after["coverage"][k]["complete"] is True for k in ("fills", "transactions"))
                  and auto.single.parse_api_timestamp(after["history_since"]) <= stamp,
                  "Recovery history does not completely cover the rejected attempt")
    duration = after["freshness"]["completed_monotonic"] - after["freshness"]["started_monotonic"]
    pilot.require(0 <= duration <= pilot_account.MAX_ACCOUNT_READ_AGE,
                  "Recovery account snapshot capture was stale")
    pilot.require(account["tournament"]["id"] == checkpoint["tournament_id"]
                  and account["tournament"]["slug"] == attempt["authorization"]["tournament_slug"],
                  "Recovery account scope changed")
    cash = pilot.amount(account["tournament"]["myBalance"])
    pilot.require(cash == pilot.amount(before["account"]["tournament"]["myBalance"])
                  == pilot.amount(checkpoint["last_reconciled_account_cash"]),
                  "Balance changed; rejected attempt cannot be cleared")
    pilot.require(not before["account"]["orders"] and not account["orders"],
                  "Open orders prevent rejection recovery")
    pilot.require(probe.inventory(account, "") == probe.inventory(before["account"], "")
                  and not any(scanner.numeric_id(r["exchangeId"]) in attempt["authorization"]["exchange_ids"]
                              for r in account["positions"]),
                  "Position exists or inventory changed; recovery blocked")
    pilot.require(after["recent_fills"] == before["recent_fills"],
                  "Fill history changed; recovery blocked")
    pilot.require(after["recent_transactions"] == before["recent_transactions"],
                  "Transaction/debit history changed; recovery blocked")
    pilot.require(not any(scanner.numeric_id(r["exchangeId"]) in attempt["authorization"]["exchange_ids"]
                          and auto.single.parse_api_timestamp(r["createdAt"]) >= stamp for r in orders),
                  "An order exists for this attempt's instruments/time; recovery blocked")
    pilot.require(pilot.amount(account["quarantine_reserve"]) == pilot.amount(checkpoint["quarantine_reserve"]),
                  "Quarantine reserve changed; recovery blocked")


def proposed_checkpoint(checkpoint, key, record):
    """Preserve the original request/response; retire, never replay or erase."""
    proposed = copy.deepcopy(checkpoint)
    journal = proposed["autonomous_execution"]
    attempt = journal["attempts"][key]
    attempt["state"] = pilot.REJECTED_RETIRED
    attempt["rejection_recovery"] = copy.deepcopy(record)
    attempt["stages"].append({"state": pilot.REJECTED_RETIRED, "at": record["recovered_at"]})
    journal["active_attempt"] = None
    proposed["live_exposures"][key].update(execution_status=pilot.REJECTED_RETIRED,
        possible_additional_quantities=["0", "0"], accounting_buffer="0")
    proposed.update(state=pilot.READY, manual_review_required=False, review_reason="")
    pilot.refresh_totals(proposed)
    return proposed


def validate_retired(checkpoint, key):
    """Validate retained recovery evidence on every load, including restart."""
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    first = rejected_leg(attempt)
    record = attempt["rejection_recovery"]
    pilot.require(set(record) == {"version", "kind", "recovered_at", "prior_checkpoint_hash", "intent_status",
                  "retired_idempotency_key", "reserved_exposure_before", "released_reservation", "snapshot", "orders", "reads"}
                  and record["version"] == 1 and record["kind"] == "HTTP_403_MISSING_TRADE"
                  and record["intent_status"] == "RETIRED_NEVER_RETRY"
                  and record["retired_idempotency_key"] == first["intent"]["request"]["idempotencyKey"]
                  and len(record["prior_checkpoint_hash"]) == 64
                  and attempt["stages"][-1] == {"state": pilot.REJECTED_RETIRED, "at": record["recovered_at"]}
                  and checkpoint["autonomous_execution"]["active_attempt"] != key
                  and checkpoint["live_exposures"][key]["execution_status"] == pilot.REJECTED_RETIRED,
                  "Invalid retired rejection evidence/key")
    pilot.require(auto.single.parse_api_timestamp(record["recovered_at"]) >=
                  auto.single.parse_api_timestamp(first["placement_response"]["received_at"]),
                  "Recovery predates its rejection")
    exposure = record["reserved_exposure_before"]
    expected = sum((pilot.amount(q) * pilot.amount(p) for q, p in
                    zip(exposure["possible_additional_quantities"], exposure["limit_prices"])), pilot.amount(exposure["accounting_buffer"]))
    pilot.require(pilot.amount(record["released_reservation"]) == expected and bool(record["reads"])
                  and all(r.get("http_status") == 200 and r.get("credential_redacted") is False for r in record["reads"]),
                  "Invalid recovery reservation or GET evidence")
    # Cash/quarantine can change after later trades. Validate this archived
    # observation against its saved before-account, not today's balance.
    historical = copy.deepcopy(checkpoint)
    historical["last_reconciled_account_cash"] = attempt["before"]["account"]["tournament"]["myBalance"]
    historical["quarantine_reserve"] = attempt["before"]["account"]["quarantine_reserve"]
    prove_clear(historical, attempt, record["snapshot"], record["orders"])


def validate_transition(previous, proposed):
    """Only this exact atomic recovery delta can clear a persistent halt."""
    key = previous.get("autonomous_execution", {}).get("active_attempt")
    attempt = eligible_attempt(previous, key)
    record = proposed["autonomous_execution"]["attempts"][key]["rejection_recovery"]
    pilot.require(record["prior_checkpoint_hash"] == pilot.snapshot_hash(previous)
                  and record["reserved_exposure_before"] == previous["live_exposures"][key],
                  "Recovery evidence does not bind the current checkpoint/reservation")
    prove_clear(previous, attempt, record["snapshot"], record["orders"])
    pilot_account.check_fresh(record["snapshot"]["freshness"]["started_monotonic"])
    pilot.require(proposed == proposed_checkpoint(previous, key, record),
                  "Recovery may only retire this rejection and release its reservation")


def recover(session, key, apply=False):
    """Hold the existing flock across all GETs and optional one atomic write."""
    # Unlike pilot_lock/load_checkpoint, this cannot create a lock or latch a
    # restart halt. A dry run must leave every runtime byte unchanged.
    with readonly_pilot_lock() as path:
        checkpoint = pilot._read_checkpoint_locked(path)
        attempt = eligible_attempt(checkpoint, key)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        reads = probe.EvidenceReads(session)  # record=None: collect in memory only.
        get_only = GetOnly(reads)  # No POST/DELETE method exposed to any reader.
        started = time.monotonic()
        orders = read_orders(get_only, checkpoint["tournament_id"])
        snapshot = pilot_account.read_snapshot(get_only, attempt["authorization"]["tournament_slug"],
                                               checkpoint, started=started, order_ids=[])
        pilot.require(read_orders(get_only, checkpoint["tournament_id"]) == orders,
                      "Order history changed during recovery; keep the halt")
        prove_clear(checkpoint, attempt, snapshot, orders)
        exposure = checkpoint["live_exposures"][key]
        released = sum((pilot.amount(q) * pilot.amount(p) for q, p in
                        zip(exposure["possible_additional_quantities"], exposure["limit_prices"])), pilot.amount(exposure["accounting_buffer"]))
        record = {"version": 1, "kind": "HTTP_403_MISSING_TRADE", "recovered_at": datetime.now(timezone.utc).isoformat(),
                  "prior_checkpoint_hash": pilot.snapshot_hash(checkpoint), "intent_status": "RETIRED_NEVER_RETRY",
                  "retired_idempotency_key": attempt["legs"][0]["intent"]["request"]["idempotencyKey"],
                  "reserved_exposure_before": copy.deepcopy(exposure), "released_reservation": str(released),
                  "snapshot": snapshot, "orders": orders, "reads": reads.reads}
        proposed = proposed_checkpoint(checkpoint, key, record)
        pilot.validate_checkpoint(proposed)
        validate_transition(checkpoint, proposed)
        # Recheck local journals and freshness immediately before any write.
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        pilot.require(pilot.amount(quarantine.reserved_cost(checkpoint["tournament_id"])) ==
                      pilot.amount(checkpoint["quarantine_reserve"]), "Quarantine changed during recovery")
        pilot_account.check_fresh(started)
        if apply:
            owner = (os.getpid(), threading.get_ident())
            pilot.require(path not in pilot._state_lock_owners, "Another operation owns pilot state")
            pilot._state_lock_owners[path] = owner  # We already hold the same OS flock.
            try:
                proposed = pilot._save_checkpoint_locked(proposed, path, rejected_recovery=True)
            finally:
                pilot._state_lock_owners.pop(path, None)
        fields = ("state", "manual_review_required", "confirmed_cumulative_debits", "reserved_unconfirmed_capital",
                  "quarantine_reserve", "calculated_remaining_allocation")
        return {"result": "RECOVERED" if apply else "DRY_RUN_PASS", "attempt": key,
                "released_reservation": str(released), "retired_idempotency_key": record["retired_idempotency_key"],
                "before": {f: checkpoint[f] for f in fields}, "after": {f: proposed[f] for f in fields},
                "balance": str(pilot.amount(snapshot["account"]["tournament"]["myBalance"])),
                "get_requests": len(reads.reads), "orders_submitted": 0, "state_written": apply,
                "trade_scope": "Confirm externally in API-key settings; no documented GET scope endpoint"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", required=True, help="Exact active rejected execution ID")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="GET-only preview (default)")
    mode.add_argument("--apply", action="store_true", help="Explicitly persist recovery after fresh GET reconciliation")
    args = parser.parse_args(argv)
    load_dotenv(override=False)
    key = os.getenv("SIG_API_KEY")
    if not key:
        print("RECOVERY_BLOCKED | Local SIG_API_KEY is unavailable")
        return 1
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": "Bearer " + key})
            print(json.dumps(recover(session, args.attempt, apply=args.apply), indent=2))
        return 0
    except (OSError, *scanner.API_ERRORS) as error:
        reason = str(error) if isinstance(error, (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked)) else "Read-only reconciliation unavailable; halt retained"
        print("RECOVERY_BLOCKED | " + reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
