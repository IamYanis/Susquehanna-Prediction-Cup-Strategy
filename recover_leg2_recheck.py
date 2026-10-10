"""Recover only Alaska's diagnosed mutable-mark halt; GET-only by default.

--apply writes one audited local transition. It cannot submit/cancel orders.
The already-consumed first recovery permission is preserved; continuation
requires a distinct command and a new one-use, checkpoint-bound permission.
"""
import argparse
import copy
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

import autonomous_pilot as auto
import live_pilot as pilot
import live_settlement as live
import pilot_account
import recover_filled_leg1 as filled
import supervised_accounting_probe as probe
from recover_rejected_attempt import read_orders
from recover_settlement_halt import SettlementGets
from senate_readiness_watcher import readonly_pilot_lock

HALT_REASON = "Account changed before leg two"
EXPECTED_CASH = Decimal("20571.10")
KIND = "LEG2_RECHECK_MUTABLE_TRANSACTION_MARK"


def permission_id(key, checkpoint_hash):
    # This is a one-use state identifier, not an API key or an order key.
    return hashlib.sha256((KIND + ":" + key + ":" + checkpoint_hash).encode()).hexdigest()


def eligible_attempt(checkpoint, key):
    target = filled.ALASKA_TARGET
    journal = checkpoint.get("autonomous_execution", {})
    pilot.require(key == target["attempt"] and journal.get("active_attempt") == key
                  and checkpoint["state"] == pilot.HALTED and checkpoint["manual_review_required"]
                  and checkpoint["review_reason"] == HALT_REASON, "Not the diagnosed Alaska leg-two mark halt")
    attempt = journal["attempts"][key]
    first, second = attempt["legs"]
    pilot.require(attempt["state"] == pilot.HALTED and attempt["halted_from"] == pilot.LEG2_RECHECK
                  and attempt["halt_reason"] == HALT_REASON and "leg2_recheck_recovery" not in attempt
                  and attempt["authorization"]["market_ids"] == target["markets"]
                  and attempt["authorization"]["exchange_ids"] == target["exchanges"]
                  and first["intent"]["order_id"] == target["order"]
                  and first["intent"]["state"] == "OBSERVED_TERMINAL" and first["post_attempted"]
                  and first["review"]["observations"]["isolated_execution_reconciled"] is True
                  and attempt["filled_leg1_recovery"]["resume_consumed_at"] is not None
                  and all(second[k] is None for k in ("intent", "receipt", "placement_response", "activity", "review"))
                  and second["post_attempted"] is False and attempt["after"][1] is None
                  and attempt["completion"] is None, "Execution/resume evidence is not the diagnosed unsubmitted leg two")
    exposure = checkpoint["live_exposures"][key]
    pilot.require(list(map(pilot.amount, exposure["confirmed_quantities"])) == [1, 0]
                  and list(map(pilot.amount, exposure["confirmed_costs"])) == [target["price"], 0]
                  and list(map(pilot.amount, exposure["possible_additional_quantities"])) == [0, 0]
                  and exposure["execution_status"] == "MANUAL_REVIEW"
                  and all(k == key or a["state"] in {pilot.READY, pilot.REJECTED_RETIRED}
                          for k, a in journal["attempts"].items())
                  and not any(p["status"] == "EXITING" for p in checkpoint.get("autonomous_positions", {}).values()),
                  "Other unresolved execution or unexpected exposure prevents recovery")
    return attempt


def prove_unchanged(attempt, snapshot, orders, observed):
    """Reuse the exact order/fill/ledger proof, then require no later activity."""
    target = filled.ALASKA_TARGET
    review = filled.prove_fill(attempt, snapshot, orders, observed, target)
    baseline, account = attempt["after"][0], snapshot["account"]
    pilot.require(auto.account_unchanged_after_leg1(baseline, snapshot)
                  and pilot.amount(account["tournament"]["myBalance"]) == EXPECTED_CASH,
                  "Cash, holdings, fills or financial transactions changed; retain halt")
    # The inventory guard ignores marks; average entry costs remain exact too.
    pilot.require({p["exchangeId"]: p["avgCost"] for p in account["positions"]} ==
                  {p["exchangeId"]: p["avgCost"] for p in baseline["account"]["positions"]}
                  and account["quarantine_reserve"] == baseline["account"]["quarantine_reserve"],
                  "Existing position entry costs or quarantine changed")
    pilot.require(observed == attempt["legs"][0]["intent"], "Confirmed first-leg intent/observation changed")
    return review


def proposed_checkpoint(checkpoint, key, record):
    proposed = copy.deepcopy(checkpoint)
    attempt = proposed["autonomous_execution"]["attempts"][key]
    attempt["leg2_recheck_recovery"] = copy.deepcopy(record)
    attempt["reads"].extend(copy.deepcopy(record["reads"]))
    attempt["stages"].extend([{"state": pilot.HALTED, "at": checkpoint["updated_at"]},
                              {"state": pilot.LEG2_RECHECK, "at": record["recovered_at"]}])
    attempt.update(state=pilot.LEG2_RECHECK, halt_reason="", halted_from=None)
    # Preserve confirmed debits/shares. Reserve only the not-yet-submitted
    # second leg plus the existing accounting buffer before continuation.
    proposed["live_exposures"][key].update(possible_additional_quantities=["0", "1"],
                                           execution_status="EXECUTING", accounting_buffer=str(auto.ACCOUNTING_BUFFER))
    proposed.update(state=pilot.LEG2_RECHECK, manual_review_required=False, review_reason="")
    pilot.refresh_totals(proposed)
    return proposed


def validate_recovered(checkpoint, key):
    """Validate archived proof on restart without querying or rewriting it."""
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    record = attempt["leg2_recheck_recovery"]
    fields = {"version", "kind", "attempt", "recovered_at", "prior_checkpoint_hash", "prior_revision",
              "prior_updated_at", "prior_halt_reason", "original_resume_consumed_at", "original_recovery_hash",
              "baseline_snapshot_hash", "observed_intent", "snapshot", "orders", "reads", "review",
              "permission_id", "consumed_at"}
    pilot.require(set(record) == fields and record["version"] == 1 and record["kind"] == KIND
                  and key == record["attempt"] == filled.ALASKA_TARGET["attempt"]
                  and record["prior_halt_reason"] == HALT_REASON and type(record["prior_revision"]) is int
                  and record["permission_id"] == permission_id(key, record["prior_checkpoint_hash"])
                  and record["original_resume_consumed_at"] is not None
                  and record["original_resume_consumed_at"] == attempt["filled_leg1_recovery"]["resume_consumed_at"]
                  and record["original_recovery_hash"] == pilot.snapshot_hash(attempt["filled_leg1_recovery"])
                  and record["baseline_snapshot_hash"] == pilot.snapshot_hash(attempt["after"][0]),
                  "Leg-two recovery identity, baseline or original consumed permission changed")
    parse = auto.single.parse_api_timestamp
    pilot.require(parse(record["recovered_at"]) >= parse(record["prior_updated_at"])
                  and parse(record["recovered_at"]) >= parse(record["original_resume_consumed_at"])
                  and record["reads"] and all(r.get("http_status") == 200 and r.get("credential_redacted") is False
                                            for r in record["reads"]), "Invalid recovery chronology or GET evidence")
    pilot.require(record["review"] == prove_unchanged(attempt, record["snapshot"], record["orders"], record["observed_intent"]),
                  "Archived leg-two reconciliation proof changed")
    if record["consumed_at"] is not None:
        pilot.require(parse(record["consumed_at"]) >= parse(record["recovered_at"]), "Continuation predates recovery")
    pilot.require(not attempt["legs"][1]["post_attempted"] or record["consumed_at"] is not None,
                  "Leg-two submission lacks consumed continuation permission")


def validate_transition(previous, proposed):
    key = previous["autonomous_execution"]["active_attempt"]
    attempt = eligible_attempt(previous, key)
    record = proposed["autonomous_execution"]["attempts"][key]["leg2_recheck_recovery"]
    pilot.require(record["prior_checkpoint_hash"] == pilot.snapshot_hash(previous)
                  and record["prior_revision"] == previous["revision"] and record["prior_updated_at"] == previous["updated_at"]
                  and record["consumed_at"] is None and proposed == proposed_checkpoint(previous, key, record),
                  "Recovery does not bind this halt or contains an unrelated state change")
    validate_recovered(proposed, key)
    pilot.require(pilot.amount(record["snapshot"]["account"]["tournament"]["myBalance"]) ==
                  pilot.amount(previous["last_reconciled_account_cash"])
                  and pilot.amount(previous["quarantine_reserve"]) ==
                      pilot.amount(record["snapshot"]["account"]["quarantine_reserve"]),
                  "Durable cash/quarantine differs from the recovery snapshot")
    # Recheck the unchanged allocation and reserve floor, using existing risk
    # accounting; the real first-leg share and Colorado are never released.
    pilot.require(pilot.amount(proposed["calculated_remaining_allocation"]) >= 0,
                  "Insufficient allocation for the restored second-leg reservation")
    model = sum((sum(map(pilot.amount, e["confirmed_costs"])) for e in proposed["live_exposures"].values()), Decimal(0))
    returned = sum((pilot.amount(p["exit_proceeds"]) for p in proposed.get("autonomous_positions", {}).values()), Decimal(0))
    cash = pilot.amount(previous["last_reconciled_account_cash"])
    pilot.require(abs(pilot.amount(proposed["autonomous_execution"]["reference_cash"]) - cash - model + returned)
                  <= pilot_account.BALANCE_DELTA_TOLERANCE and cash - Decimal(".01") >= pilot.amount(proposed["untouchable_cash_reserve"]),
                  "Cumulative accounting/reserve floor inconsistent")
    pilot_account.check_fresh(record["snapshot"]["freshness"]["started_monotonic"])


def require_resume(checkpoint, key, permission):
    pilot.require(checkpoint["state"] == pilot.LEG2_RECHECK and not checkpoint["manual_review_required"]
                  and checkpoint["autonomous_execution"]["active_attempt"] == key, "Recovered leg-two check is not resumable")
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    pilot.require("leg2_recheck_recovery" in attempt, "Missing new continuation permission")
    validate_recovered(checkpoint, key)
    record = attempt["leg2_recheck_recovery"]
    pilot.require(permission == record["permission_id"] and record["consumed_at"] is None
                  and attempt["legs"][1]["intent"] is None and not attempt["legs"][1]["post_attempted"],
                  "Wrong/consumed continuation permission or leg-two intent already exists; never replay")


def recover(session, key, apply=False):
    with readonly_pilot_lock() as path:
        checkpoint = pilot._read_checkpoint_locked(path)
        attempt = eligible_attempt(checkpoint, key)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        reads = probe.EvidenceReads(session)  # No disk sink: dry-run evidence stays in memory.
        gets = SettlementGets(reads)  # No POST/DELETE interface.
        pilot.read_live_pair(gets, attempt["authorization"])
        started = time.monotonic()
        orders = read_orders(gets, checkpoint["tournament_id"])
        snapshot = pilot_account.read_snapshot(gets, attempt["authorization"]["tournament_slug"], checkpoint,
                                               started=started, order_ids=[filled.ALASKA_TARGET["order"]])
        observed = copy.deepcopy(attempt["legs"][0]["intent"])
        activity = snapshot["order_activity"][0]
        auto.single.verify_order_intent(activity["order"], observed)
        pilot.require(read_orders(gets, checkpoint["tournament_id"]) == orders, "Order history changed during recovery")
        review = prove_unchanged(attempt, snapshot, orders, observed)
        stamp = datetime.now(timezone.utc).isoformat()
        prior_hash = pilot.snapshot_hash(checkpoint)
        record = {"version": 1, "kind": KIND, "attempt": key, "recovered_at": stamp,
                  "prior_checkpoint_hash": prior_hash, "prior_revision": checkpoint["revision"],
                  "prior_updated_at": checkpoint["updated_at"], "prior_halt_reason": checkpoint["review_reason"],
                  "original_resume_consumed_at": attempt["filled_leg1_recovery"]["resume_consumed_at"],
                  "original_recovery_hash": pilot.snapshot_hash(attempt["filled_leg1_recovery"]),
                  "baseline_snapshot_hash": pilot.snapshot_hash(attempt["after"][0]),
                  "observed_intent": observed, "snapshot": snapshot, "orders": orders, "reads": reads.reads,
                  "review": review, "permission_id": permission_id(key, prior_hash), "consumed_at": None}
        proposed = proposed_checkpoint(checkpoint, key, record)
        pilot.validate_checkpoint(proposed)
        validate_transition(checkpoint, proposed)
        live.revalidate_authorization(attempt["authorization"])
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        if apply:
            pilot.require(path not in pilot._state_lock_owners, "Another operation owns pilot state")
            pilot._state_lock_owners[path] = (os.getpid(), threading.get_ident())
            try:
                proposed = pilot._save_checkpoint_locked(proposed, path, leg2_recovery=True)
            finally:
                pilot._state_lock_owners.pop(path, None)
        fields = ("state", "revision", "manual_review_required", "review_reason", "confirmed_cumulative_debits",
                  "last_reconciled_account_cash", "reserved_unconfirmed_capital", "quarantine_reserve",
                  "calculated_remaining_allocation")
        after = {k: proposed[k] for k in fields}
        if not apply:
            after["revision"] += 1
        return {"result": "RECOVERED" if apply else "DRY_RUN_PASS", "attempt": key,
                "before": {k: checkpoint[k] for k in fields}, "after": after,
                "leg1_order": filled.ALASKA_TARGET["order"], "leg1_fill": filled.ALASKA_TARGET["fill"],
                "leg_two_intent": None, "original_resume_consumed_at": record["original_resume_consumed_at"],
                "continuation_permission": record["permission_id"], "continuation_consumed_at": None,
                "resume_command": f".venv/bin/python autonomous_pilot.py --resume-leg2-recheck {key} --continuation {record['permission_id']}",
                "get_requests": len(reads.reads), "orders_submitted": 0, "state_written": apply}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="GET-only preview (default)")
    mode.add_argument("--apply", action="store_true", help="Atomic local recovery only; never submits an order")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().with_name(".env"))  # Read only.
    key = os.getenv("SIG_API_KEY")
    if not key:
        print("RECOVERY_BLOCKED | Local API key unavailable")
        return 1
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": "Bearer " + key})
            print(json.dumps(recover(session, args.attempt, args.apply), indent=2))
        return 0
    except (OSError, *auto.scanner.API_ERRORS) as error:
        safe = (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked, live.LiveSettlementBlocked, auto.single.TestError)
        print("RECOVERY_BLOCKED | " + (str(error) if isinstance(error, safe) else "GET reconciliation unavailable; halt retained"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
