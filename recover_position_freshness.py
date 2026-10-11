"""GET-only recovery of the exact revision-221 HOLD freshness halt.

Dry-run is the default. --apply may append an audit and clear only this halt;
it cannot change positions/capital, submit/cancel orders, or start the bot.
"""
import argparse
import copy
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import autonomous_pilot as auto
import live_pilot as pilot
import live_settlement as live
import manual_autonomous_settlement as manual
import pilot_account
import price_reader as scanner
import supervised_accounting_probe as probe
from recover_rejected_attempt import read_orders
from recover_settlement_halt import SettlementGets
from senate_readiness_watcher import readonly_pilot_lock

HALT_REASON = "STALE_ACCOUNT_DATA | Account read exceeded the existing 15-second freshness window"
KIND = "READ_ONLY_POSITION_FRESHNESS"
AUDIT_KEY = "position_freshness_recoveries"
SAVE_FLAG = "freshness_recovery"
TARGET_REVISION = 221
# Bind the entire diagnosed state, including all three positions and reserves.
# This command is deliberately not a generic way to clear any account halt.
TARGET_CHECKPOINT_HASH = "5b3cc00253b6b47395f32a2c3c64f1c1b6bc471ba46eeedaa0d0439dd28f5687"


def eligible_checkpoint(checkpoint, rules=None):
    # The interruption command reuses these proofs with its own exact revision,
    # reason and checkpoint hash. Neither command can clear arbitrary halts.
    rules = rules or sys.modules[__name__]
    pilot.validate_checkpoint(checkpoint)
    journal = checkpoint.get("autonomous_execution", {})
    pilot.require(checkpoint["revision"] == rules.TARGET_REVISION
                  and pilot.snapshot_hash(checkpoint) == rules.TARGET_CHECKPOINT_HASH
                  and checkpoint["state"] == pilot.HALTED and checkpoint["manual_review_required"]
                  and checkpoint["review_reason"] == rules.HALT_REASON
                  and journal.get("active_attempt") is None
                  and all(a["state"] in {pilot.READY, pilot.REJECTED_RETIRED} for a in journal["attempts"].values())
                  and pilot.amount(checkpoint["reserved_unconfirmed_capital"]) == 0,
                  f"Not the exact no-execution revision-{rules.TARGET_REVISION} {rules.KIND} halt")
    positions = checkpoint.get("autonomous_positions", {})
    pilot.require(positions and all(p["status"] == "OPEN" and p["remaining_quantities"] == ["1", "1"]
                  and not any(p["settled_legs"]) and p["exit_execution"] is None for p in positions.values()),
                  "Unfinished/changed managed position prevents read-only halt recovery")
    pilot.require(not auto.exit_keys(checkpoint), "Saved sale key prevents read-only halt recovery")
    baselines = [p["last_snapshot"] for p in positions.values()]
    pilot.require(all(b == baselines[0] for b in baselines), "Managed account baselines disagree")
    return baselines[0]


def prove_account(baseline, snapshot):
    """Ignore mutable marks, but preserve exact cash/inventory/event evidence."""
    pilot.require(snapshot["data_complete"] is True
                  and all(snapshot["coverage"][k]["complete"] is True for k in ("fills", "transactions"))
                  and auto.single.parse_api_timestamp(snapshot["history_since"]) <=
                  auto.single.parse_api_timestamp(baseline["freshness"]["observed_at"]),
                  "Recovery account history is incomplete")
    duration = snapshot["freshness"]["completed_monotonic"] - snapshot["freshness"]["started_monotonic"]
    pilot.require(0 <= duration <= pilot_account.MAX_ACCOUNT_READ_AGE, "Recovery account capture was stale")
    pilot.require(not snapshot["account"]["orders"]
                  and pilot_account.account_execution_state(snapshot["account"]) ==
                  pilot_account.account_execution_state(baseline["account"])
                  and snapshot["recent_fills"] == baseline["recent_fills"]
                  and pilot_account.transaction_histories_equal(snapshot["recent_transactions"], baseline["recent_transactions"]),
                  "Account cash, holdings, orders, fills or financial transactions changed; retain halt")


def prove_orders(checkpoint, attempt_ids, activities, orders, snapshot):
    expected = {}
    for key in attempt_ids:
        attempt = checkpoint["autonomous_execution"]["attempts"][key]
        pilot.require(attempt["state"] == pilot.READY, "An entry is not fully reconciled")
        for leg in attempt["legs"]:
            intent = leg["intent"]
            pilot.require(intent["state"] == "OBSERVED_TERMINAL" and leg["post_attempted"]
                          and leg["review"]["observations"]["isolated_execution_reconciled"] is True,
                          "A saved leg is not confirmed")
            oid = str(intent["order_id"])
            expected[oid] = leg
            activity = activities[oid]
            auto.single.verify_order_intent(activity["order"], intent)
            old_order, new_order = copy.deepcopy(leg["activity"]["order"]), copy.deepcopy(activity["order"])
            old_order.pop("expirationDate")
            new_order.pop("expirationDate")  # Shared verifier above checks UTC milliseconds.
            pilot.require(old_order == new_order and activity["order"]["open"] is False
                          and activity["order"]["quantity"] == activity["order"]["quantityFilled"] == 1
                          and activity["fills"] == leg["activity"]["fills"]
                          and pilot.amount(activity["fill_notional"]) == pilot.amount(leg["activity"]["fill_notional"]),
                          "Saved order/fill evidence changed; retain halt")
    pilot.require(set(activities) == set(expected), "Missing or extra order evidence")
    since = auto.single.parse_api_timestamp(checkpoint["created_at"])
    recent = [o for o in orders if auto.single.parse_api_timestamp(o["createdAt"]) >= since]
    pilot.require(len(recent) == len(expected) and {str(o["id"]) for o in recent} == set(expected)
                  and all(o == activities[str(o["id"])]["order"] for o in recent),
                  "New/missing/changed tournament order prevents recovery")
    for oid, leg in expected.items():
        rows = [f for f in snapshot["recent_fills"] if str(f["orderId"]) == oid]
        original = [f for f in leg["activity"]["fills"]]
        pilot.require(len(rows) == len(original) and {f["id"] for f in rows} == {f["id"] for f in original},
                      "Portfolio fill history lacks a saved fill")


def proposed_checkpoint(checkpoint, record, rules=None):
    rules = rules or sys.modules[__name__]
    proposed = copy.deepcopy(checkpoint)
    proposed.setdefault(rules.AUDIT_KEY, []).append(copy.deepcopy(record))
    proposed.update(state=pilot.READY, manual_review_required=False, review_reason="")
    return proposed


def validate_history(checkpoint, rules=None):
    """Archived GET evidence remains valid after later position/account changes."""
    rules = rules or sys.modules[__name__]
    records = checkpoint[rules.AUDIT_KEY]
    pilot.require(isinstance(records, list) and len(records) == 1, "Invalid read-only halt recovery history")
    fields = {"version", "kind", "recovered_at", "prior_checkpoint_hash", "prior_revision", "prior_updated_at",
              "prior_halt_reason", "attempt_ids", "approvals", "evidence", "baseline_snapshot", "snapshot",
              "activities", "orders", "reads"}
    for record in records:
        pilot.require(set(record) == fields and record["version"] == 1 and record["kind"] == rules.KIND
                      and record["prior_checkpoint_hash"] == rules.TARGET_CHECKPOINT_HASH
                      and record["prior_revision"] == rules.TARGET_REVISION and record["prior_halt_reason"] == rules.HALT_REASON
                      and auto.single.parse_api_timestamp(record["recovered_at"]) >=
                      auto.single.parse_api_timestamp(record["prior_updated_at"]), "Invalid archived read-only halt")
        pilot.require(record["attempt_ids"] and len(set(record["attempt_ids"])) == len(record["attempt_ids"])
                      and len(record["attempt_ids"]) == len(record["approvals"]) == len(record["evidence"]),
                      "Missing pair-specific settlement evidence")
        for key, approval, evidence in zip(record["attempt_ids"], record["approvals"], record["evidence"]):
            pilot.require(approval == checkpoint["autonomous_execution"]["attempts"][key]["authorization"],
                          "Archived settlement approval differs from the saved entry")
            manual.require_matching_evidence(approval, evidence)
        prove_account(record["baseline_snapshot"], record["snapshot"])
        prove_orders(checkpoint, record["attempt_ids"], record["activities"], record["orders"], record["snapshot"])
        pilot.require(record["reads"] and all(r.get("http_status") == 200 and r.get("credential_redacted") is False
                      for r in record["reads"]), "Missing/invalid recovery GET evidence")


def validate_transition(previous, proposed, rules=None):
    rules = rules or sys.modules[__name__]
    baseline = rules.eligible_checkpoint(previous)
    record = proposed[rules.AUDIT_KEY][-1]
    pilot.require(record["baseline_snapshot"] == baseline and record["prior_updated_at"] == previous["updated_at"]
                  and record["attempt_ids"] == list(previous["autonomous_positions"])
                  and proposed == rules.proposed_checkpoint(previous, record),
                  "Recovery may only archive and clear this exact read-only halt")
    rules.validate_history(proposed)
    pilot_account.check_fresh(record["snapshot"]["freshness"]["started_monotonic"])


def recover(session, apply=False, rules=None):
    rules = rules or sys.modules[__name__]
    with readonly_pilot_lock() as path:
        checkpoint = pilot._read_checkpoint_locked(path)
        baseline = rules.eligible_checkpoint(checkpoint)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        reads = probe.EvidenceReads(session)  # No disk evidence sink during dry-run.
        gets = SettlementGets(reads)  # No POST/DELETE adapter is exposed.
        attempt_ids = list(checkpoint["autonomous_positions"])
        approvals, evidence, activities = [], [], {}
        for key in attempt_ids:
            position = checkpoint["autonomous_positions"][key]
            approval = auto.autonomous_authorization(position["market_ids"], checkpoint["tournament_id"])
            pilot.require(approval == checkpoint["autonomous_execution"]["attempts"][key]["authorization"],
                          "Current authorization differs from the saved entry")
            markets, allowed, context = pilot.read_live_pair(gets, approval)
            pilot.require("NO-PAIR" in allowed and all(m["status"] == "open" for m in markets),
                          "Managed settlement authorization/status no longer passes")
            approvals.append(approval)
            evidence.append(context["manual_evidence"])
            for leg in checkpoint["autonomous_execution"]["attempts"][key]["legs"]:
                oid = leg["intent"]["order_id"]
                activities[str(oid)] = pilot_account.read_order_activity(
                    gets, oid, baseline["recent_fills"], baseline["account"], time.monotonic())
        # Terminal order histories are read separately. Recheck the complete
        # order set around a NEW account snapshot so six receipts cannot consume
        # its 15-second budget before the final recovery check.
        started = time.monotonic()
        orders = read_orders(gets, checkpoint["tournament_id"])
        snapshot = pilot_account.read_snapshot(gets, approvals[0]["tournament_slug"], checkpoint,
                                               started=started, order_ids=[])
        pilot.require(read_orders(gets, checkpoint["tournament_id"]) == orders, "Order history changed during recovery")
        prove_account(baseline, snapshot)
        prove_orders(checkpoint, attempt_ids, activities, orders, snapshot)
        checked = copy.deepcopy(checkpoint)
        auto.check_cash_model(checked, snapshot)
        pilot.require(pilot.amount(snapshot["account"]["quarantine_reserve"]) == pilot.amount(checkpoint["quarantine_reserve"]),
                      "Quarantine reserve changed")
        record = {"version": 1, "kind": rules.KIND, "recovered_at": datetime.now(timezone.utc).isoformat(),
                  "prior_checkpoint_hash": pilot.snapshot_hash(checkpoint), "prior_revision": checkpoint["revision"],
                  "prior_updated_at": checkpoint["updated_at"], "prior_halt_reason": checkpoint["review_reason"],
                  "attempt_ids": attempt_ids, "approvals": approvals, "evidence": evidence,
                  "baseline_snapshot": baseline, "snapshot": snapshot, "activities": activities,
                  "orders": orders, "reads": reads.reads}
        proposed = rules.proposed_checkpoint(checkpoint, record)
        rules.validate_transition(checkpoint, proposed)
        pilot.validate_checkpoint(proposed)
        for approval in approvals:
            live.revalidate_authorization(approval)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        pilot_account.check_fresh(started)
        if apply:
            pilot.require(path not in pilot._state_lock_owners, "Another operation owns pilot state")
            pilot._state_lock_owners[path] = (os.getpid(), threading.get_ident())
            try:
                proposed = pilot._save_checkpoint_locked(proposed, path, **{rules.SAVE_FLAG: True})
            finally:
                pilot._state_lock_owners.pop(path, None)
        fields = ("state", "revision", "manual_review_required", "review_reason", "confirmed_cumulative_debits",
                  "reserved_unconfirmed_capital", "quarantine_reserve", "calculated_remaining_allocation")
        after = {k: proposed[k] for k in fields}
        if not apply:
            after["revision"] += 1  # Show the revision an atomic apply would produce.
        return {"result": "RECOVERED" if apply else "DRY_RUN_PASS", "before": {k: checkpoint[k] for k in fields},
                "after": after, "active_attempt": None, "managed_positions_unchanged": True,
                "orders_verified": len(activities), "authorizations_verified": len(approvals),
                "get_requests": len(reads.reads), "orders_submitted": 0, "state_written": apply}


def main(argv=None, rules=None):
    rules = rules or sys.modules[__name__]
    parser = argparse.ArgumentParser(description=rules.__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="GET-only preview (default)")
    mode.add_argument("--apply", action="store_true", help="Explicit atomic local recovery; does not start trading")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    key = os.getenv("SIG_API_KEY")
    if not key:
        print("RECOVERY_BLOCKED | Local SIG_API_KEY is unavailable")
        return 1
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": "Bearer " + key})
            print(json.dumps(rules.recover(session, apply=args.apply), indent=2))
        return 0
    except (OSError, *scanner.API_ERRORS) as error:
        reason = str(error) if isinstance(error, (pilot.PilotBlocked, live.LiveSettlementBlocked,
                                                 pilot_account.AccountReadinessBlocked)) else "GET reconciliation unavailable; halt retained"
        print("RECOVERY_BLOCKED | " + reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
