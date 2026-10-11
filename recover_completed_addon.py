"""Finalize only the reviewed Alaska add-on; default dry-run is GET-only.

--apply repeats all proofs under the exclusive lock, then makes one audited
atomic local write. There is no order submission, cancellation or resume path.
"""
import argparse
import copy
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
import account_reader
import live_pilot as pilot
import live_settlement as live
import pilot_account
import price_reader as scanner
import supervised_accounting_probe as probe
from recover_completed_pair import history_delta, fill_identity, transaction_identity
from recover_rejected_attempt import read_orders
from recover_settlement_halt import SettlementGets
from senate_readiness_watcher import readonly_pilot_lock

TARGET = {"attempt": "ec41968a3ed423632f1ade9f33c6a6be05340ab59ff786ffc612db065666b873",
          "parent": "b620ac32c3bd51fdde79a6491dc31d440e0972995b9d9107ab4a2750341e31ab",
          "revision": 407, "markets": ["377", "378"], "exchanges": ["1066", "1067"],
          "orders": [30101693, 30102782], "fills": [125972089, 125976445],
          "transactions": ["engine-125972094", "engine-125976450"],
          "prices": [Decimal(".295"), Decimal(".690")], "cash": Decimal("20568.43")}
HALT_REASON = "STALE_ACCOUNT_DATA | Account read exceeded the existing 15-second freshness window"


def eligible_attempt(checkpoint, key):
    """An age-only halt cannot excuse a missing, partial or unknown execution."""
    pilot.validate_checkpoint(checkpoint)
    journal = checkpoint.get("autonomous_execution", {})
    pilot.require(key == TARGET["attempt"] and journal.get("active_attempt") == key
                  and checkpoint["revision"] == TARGET["revision"]
                  and checkpoint["state"] == pilot.HALTED and checkpoint["manual_review_required"]
                  and checkpoint["review_reason"] == HALT_REASON, "Not the reviewed completed-add-on freshness halt")
    attempt = journal["attempts"][key]
    parent = checkpoint["autonomous_positions"][TARGET["parent"]]
    pilot.require(attempt["state"] == pilot.HALTED and attempt["halted_from"] == pilot.FINAL_RECONCILING
                  and attempt["halt_reason"] == HALT_REASON and "completed_addon_recovery" not in attempt
                  and attempt["authorization"]["market_ids"] == TARGET["markets"]
                  and attempt["authorization"]["exchange_ids"] == TARGET["exchanges"]
                  and attempt["addon"]["position_id"] == TARGET["parent"]
                  and attempt["addon"]["baseline_quantity"] == parent["quantity"] == 1
                  and parent["status"] == "OPEN" and parent["remaining_quantities"] == ["1", "1"]
                  and list(map(Decimal, parent["actual_entry_prices"])) == TARGET["prices"]
                  and Decimal(parent["total_entry_cost"]) == sum(TARGET["prices"])
                  and checkpoint["autonomous_execution"]["attempts"][TARGET["parent"]]["authorization"] == attempt["authorization"]
                  and all(s is not None for s in attempt["after"]), "Saved parent/add-on binding changed")
    for leg, oid in zip(attempt["legs"], TARGET["orders"]):
        pilot.require(leg["intent"] is not None and leg["post_attempted"] and leg["receipt"] is not None
                      and leg["intent"]["order_id"] == oid and leg["intent"]["state"] == "OBSERVED_TERMINAL"
                      and leg["intent"]["observation"]["filled_quantity"] == 1
                      and leg["intent"]["observation"]["open"] is False
                      and leg["review"]["observations"]["isolated_execution_reconciled"] is True,
                      "Both add-on legs must already be fully filled and individually reconciled")
    exposure = checkpoint["live_exposures"][key]
    pilot.require(list(map(Decimal, exposure["confirmed_quantities"])) == [1, 1]
                  and list(map(Decimal, exposure["confirmed_costs"])) == TARGET["prices"]
                  and all(Decimal(q) == 0 for q in exposure["possible_additional_quantities"])
                  and exposure["execution_status"] == "MANUAL_REVIEW"
                  and Decimal(exposure["accounting_buffer"]) == Decimal(".040"), "Saved exposure differs from the completed add-on")
    pilot.require(all(k == key or a["state"] in {pilot.READY, pilot.REJECTED_RETIRED} for k, a in journal["attempts"].items())
                  and all(p["status"] == "OPEN" and p["exit_execution"] is None
                          for p in checkpoint["autonomous_positions"].values()), "Another execution or sale needs review")
    return attempt


def prove_complete(attempt, snapshot, orders):
    """Reconcile exact terminal receipts and incremental holdings/history."""
    before, account = attempt["before"], snapshot["account"]
    duration = snapshot["freshness"]["completed_monotonic"] - snapshot["freshness"]["started_monotonic"]
    stamp = auto.single.parse_api_timestamp(attempt["created_at"])
    pilot.require(snapshot["data_complete"] is True and all(snapshot["coverage"][k]["complete"] is True
                  for k in ("fills", "transactions")) and 0 <= duration <= pilot_account.MAX_ACCOUNT_READ_AGE
                  and auto.single.parse_api_timestamp(snapshot["history_since"]) <= stamp
                  and auto.single.parse_api_timestamp(snapshot["freshness"]["observed_at"]) >= stamp
                  and account["tournament"]["id"] == attempt["authorization"]["tournament_id"]
                  and account["tournament"]["slug"] == attempt["authorization"]["tournament_slug"]
                  and not account["orders"], "Fresh account scope, history or orders are inconsistent")
    # This also protects Colorado/New Hampshire, costs/lots and quarantine.
    # It permits only mutable marks, never added/removed financial activity.
    auto.require_unchanged_final_account(attempt["after"][1], snapshot)
    fills = history_delta(before["recent_fills"], snapshot["recent_fills"], "id", fill_identity)
    trades = history_delta(before["recent_transactions"], snapshot["recent_transactions"], "event_id", transaction_identity)
    pilot.require(len(fills) == 2 and {r["id"] for r in fills} == set(TARGET["fills"])
                  and len(trades) == 2 and {r["event_id"] for r in trades} == set(TARGET["transactions"]),
                  "Missing, additional or contradictory fill/transaction activity")
    activities = {r["order"]["id"]: r for r in snapshot["order_activity"]}
    pilot.require(len(snapshot["order_activity"]) == 2 and set(activities) == set(TARGET["orders"]),
                  "Missing or duplicate terminal order evidence")
    for i, (mid, eid, oid, fid, tid, price) in enumerate(zip(TARGET["markets"], TARGET["exchanges"],
            TARGET["orders"], TARGET["fills"], TARGET["transactions"], TARGET["prices"])):
        leg, activity = attempt["legs"][i], activities[oid]
        intent, receipt, order = leg["intent"], leg["receipt"], activity["order"]
        auto.single.verify_order_intent(order, intent)
        pilot.require(intent["market_id"] == mid and intent["request"]["exchangeId"] == eid
                      and intent["request"]["action"] == "buy" and intent["request"]["side"] == "no"
                      and intent["request"]["quantity"] == order["quantity"] == order["quantityFilled"] == 1
                      and Decimal(str(intent["request"]["price"])) == price and order["open"] is False
                      and len(activity["fills"]) == 1 and Decimal(activity["fill_notional"]) == price
                      and activity == leg["activity"] and receipt["orderId"] == oid
                      and scanner.numeric_id(receipt["exchangeId"]) == eid and receipt["action"] == "buy"
                      and receipt["side"] == "no" and receipt["quantity"] == receipt["quantityTraded"] == 1
                      and receipt["open"] is False and receipt["remainingQuantity"] == 0
                      and all(Decimal(str(receipt[k])) == price for k in ("price", "fillPrice", "totalCost"))
                      and probe.ordinary_cash_receipt(receipt["all"], price), "Order/receipt differs from the ordinary full buy")
        fill = activity["fills"][0]
        pf = next(r for r in fills if r["id"] == fid)
        pilot.require(fill["id"] == fid and fill["side"] == "no" and fill["quantity"] == -1
                      and Decimal(str(fill["price"])) == price and pf["orderId"] == oid
                      and scanner.numeric_id(pf["marketId"]) == mid and scanner.numeric_id(pf["exchangeId"]) == eid
                      and pf["side"] == "no" and pf["quantity"] == -1
                      and abs(Decimal(str(pf["price"])) - price) <= Decimal(".000000001")
                      and auto.single.parse_api_timestamp(pf["filledAt"]) == auto.single.parse_api_timestamp(fill["filledAt"]),
                      "Known full fill differs")
        holding = [p for p in account["positions"] if scanner.numeric_id(p["exchangeId"]) == eid]
        pilot.require(len(holding) == 1 and scanner.numeric_id(holding[0]["marketId"]) == mid
                      and holding[0]["quantity"] == -2 and holding[0]["settled"] is False
                      and account_reader.no_holding_cost_matches(holding[0], 2, 2 * price),
                      "Expected exactly two corresponding NO shares with reconciled costs")
        trade = next(t for t in trades if t["event_id"] == tid)
        pilot.require(trade["event_type"] == "trade" and trade["orderType"] == "BUY"
                      and scanner.numeric_id(trade["marketId"]) == mid and scanner.numeric_id(trade["exchangeId"]) == eid
                      and trade["quantity"] == -1 and Decimal(str(trade["price"])) == price
                      and auto.single.parse_api_timestamp(trade["createdAt"]) == auto.single.parse_api_timestamp(fill["filledAt"])
                      and all(trade.get(k) is None for k in ("amount", "transactionType", "collateralDelta",
                                                            "outstandingAdvanceAfter", "componentId", "reason")),
                      "Known transaction contradicts the full buy")
    new_orders = [o for o in orders if auto.single.parse_api_timestamp(o["createdAt"]) >= stamp]
    pilot.require(len(new_orders) == 2 and {o["id"] for o in new_orders} == set(TARGET["orders"])
                  and all(o == activities[o["id"]]["order"] for o in new_orders), "Additional or contradictory order activity")
    cost = sum(TARGET["prices"])
    cash = pilot.amount(account["tournament"]["myBalance"])
    debit = pilot.amount(before["account"]["tournament"]["myBalance"]) - cash
    pilot.require(cash == TARGET["cash"] and debit >= 0 and abs(debit - cost) <= pilot_account.BALANCE_DELTA_TOLERANCE,
                  "Cash differs from the reviewed rounded add-on accounting")
    return {"pair_notional": str(cost), "displayed_debit": str(debit), "difference": str(debit - cost),
            "tolerance": str(pilot_account.BALANCE_DELTA_TOLERANCE)}


def proposed_checkpoint(checkpoint, key, record):
    proposed = copy.deepcopy(checkpoint)
    attempt = proposed["autonomous_execution"]["attempts"][key]
    attempt["completed_addon_recovery"] = copy.deepcopy(record)
    attempt["reads"].extend(copy.deepcopy(record["reads"]))
    attempt["stages"].extend([{"state": pilot.HALTED, "at": checkpoint["updated_at"]},
                              {"state": pilot.FINAL_RECONCILING, "at": record["recovered_at"]},
                              {"state": pilot.READY, "at": record["recovered_at"]}])
    attempt.update(state=pilot.READY, halt_reason="", halted_from=None)
    proposed["live_exposures"][key].update(execution_status="RECONCILED_PAIR", accounting_buffer="0")
    proposed["autonomous_execution"]["active_attempt"] = None
    proposed.update(state=pilot.READY, manual_review_required=False, review_reason="")
    auto.record_position(proposed, key, record["snapshot"])
    auto.recalculate_budget(proposed)  # Existing charges stay; only the buffer is released.
    return proposed


def validate_completed(checkpoint, key):
    """Validate the archived proof on every later load, without current GETs."""
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    record = attempt["completed_addon_recovery"]
    pilot.require(key == TARGET["attempt"] and set(record) == {"version", "kind", "recovered_at", "prior_checkpoint_hash",
                  "prior_revision", "prior_updated_at", "prior_halt_reason", "snapshot", "orders", "reads", "accounting",
                  "retained_idempotency_keys"} and record["version"] == 1 and record["kind"] == "COMPLETED_ADDON_FRESHNESS"
                  and record["prior_revision"] == TARGET["revision"] and record["prior_halt_reason"] == HALT_REASON
                  and len(record["prior_checkpoint_hash"]) == 64 and attempt["state"] == pilot.READY
                  and auto.single.parse_api_timestamp(record["recovered_at"]) >= auto.single.parse_api_timestamp(record["prior_updated_at"])
                  and record["retained_idempotency_keys"] == [l["intent"]["request"]["idempotencyKey"] for l in attempt["legs"]]
                  and record["reads"] and all(r.get("http_status") == 200 and not r.get("credential_redacted", True) for r in record["reads"])
                  and record["accounting"] == prove_complete(attempt, record["snapshot"], record["orders"]),
                  "Completed add-on recovery evidence changed")


def validate_transition(previous, proposed):
    key = previous["autonomous_execution"]["active_attempt"]
    eligible_attempt(previous, key)
    record = proposed["autonomous_execution"]["attempts"][key]["completed_addon_recovery"]
    pilot.require(record["prior_checkpoint_hash"] == pilot.snapshot_hash(previous)
                  and record["prior_revision"] == previous["revision"] and record["prior_updated_at"] == previous["updated_at"]
                  and proposed == proposed_checkpoint(previous, key, record), "Recovery does not bind this exact halted state")
    model = sum((sum(map(pilot.amount, e["confirmed_costs"])) for e in proposed["live_exposures"].values()), Decimal(0))
    returned = sum((pilot.amount(p["exit_proceeds"]) for p in proposed["autonomous_positions"].values()), Decimal(0))
    cash = pilot.amount(proposed["last_reconciled_account_cash"])
    pilot.require(cash == TARGET["cash"] and abs(pilot.amount(proposed["autonomous_execution"]["reference_cash"]) - cash - model + returned)
                  <= pilot_account.BALANCE_DELTA_TOLERANCE and cash - Decimal(".01") >= pilot.amount(proposed["untouchable_cash_reserve"])
                  and proposed["accounted_pair_costs"] == previous["accounted_pair_costs"]
                  and proposed["confirmed_cumulative_debits"] == previous["confirmed_cumulative_debits"]
                  and proposed["quarantine_reserve"] == previous["quarantine_reserve"], "Cash, allocation charge or quarantine changed")
    pilot_account.check_fresh(record["snapshot"]["freshness"]["started_monotonic"])


def recover(session, key, apply=False):
    with readonly_pilot_lock() as path:
        checkpoint = pilot._read_checkpoint_locked(path)
        attempt = eligible_attempt(checkpoint, key)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        reads = probe.EvidenceReads(session)
        gets = SettlementGets(reads)
        pilot.read_live_pair(gets, attempt["authorization"])
        orders = read_orders(gets, checkpoint["tournament_id"])
        # Terminal receipts are immutable and read separately. Start the final
        # account clock AFTER these GETs, so they cannot age its snapshot.
        old = attempt["after"][1]
        activities = [pilot_account.read_order_activity(gets, oid, old["recent_fills"], old["account"], time.monotonic())
                      for oid in TARGET["orders"]]
        snapshot = pilot_account.read_snapshot(gets, attempt["authorization"]["tournament_slug"], checkpoint, order_ids=[])
        snapshot["order_activity"] = activities
        pilot.require(read_orders(gets, checkpoint["tournament_id"]) == orders, "Order history changed during recovery")
        accounting = prove_complete(attempt, snapshot, orders)
        record = {"version": 1, "kind": "COMPLETED_ADDON_FRESHNESS", "recovered_at": datetime.now(timezone.utc).isoformat(),
                  "prior_checkpoint_hash": pilot.snapshot_hash(checkpoint), "prior_revision": checkpoint["revision"],
                  "prior_updated_at": checkpoint["updated_at"], "prior_halt_reason": checkpoint["review_reason"],
                  "snapshot": snapshot, "orders": orders, "reads": reads.reads, "accounting": accounting,
                  "retained_idempotency_keys": [l["intent"]["request"]["idempotencyKey"] for l in attempt["legs"]]}
        proposed = proposed_checkpoint(checkpoint, key, record)
        pilot.validate_checkpoint(proposed)
        validate_transition(checkpoint, proposed)
        live.revalidate_authorization(attempt["authorization"])
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        pilot_account.check_fresh(snapshot["freshness"]["started_monotonic"])
        if apply:
            pilot.require(path not in pilot._state_lock_owners, "Another operation owns pilot state")
            pilot._state_lock_owners[path] = (os.getpid(), threading.get_ident())
            try:
                proposed = pilot._save_checkpoint_locked(proposed, path, completed_recovery=True)
            finally:
                pilot._state_lock_owners.pop(path, None)
        fields = ("state", "revision", "manual_review_required", "confirmed_cumulative_debits", "reserved_unconfirmed_capital",
                  "quarantine_reserve", "calculated_remaining_allocation", "last_reconciled_account_cash")
        after = {f: proposed[f] for f in fields}
        if not apply:
            after["revision"] += 1
        parent = proposed["autonomous_positions"][TARGET["parent"]]
        return {"result": "RECOVERED" if apply else "DRY_RUN_PASS", "attempt": key, "accounting": accounting,
                "before": {f: checkpoint[f] for f in fields}, "after": after,
                "position": {f: parent[f] for f in ("pair", "quantity", "remaining_quantities", "per_leg_costs", "average_entry_prices",
                     "total_entry_cost", "average_pair_cost", "execution_ids", "status")},
                "active_attempt_after": proposed["autonomous_execution"]["active_attempt"],
                "orders_submitted": 0, "state_written": apply, "get_requests": len(reads.reads)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", required=True, help="Exact reviewed completed add-on attempt ID")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="GET-only preview (default)")
    mode.add_argument("--apply", action="store_true", help="Audited atomic local finalization; no orders")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
    key = os.getenv("SIG_API_KEY")
    if not key:
        print("RECOVERY_BLOCKED | Local SIG_API_KEY unavailable")
        return 1
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": "Bearer " + key})
            print(json.dumps(recover(session, args.attempt, apply=args.apply), indent=2))
        return 0
    except (OSError, *scanner.API_ERRORS) as error:
        # Never print API response bodies, headers or credentials.
        safe = (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked, live.LiveSettlementBlocked)
        reason = str(error) if isinstance(error, safe) else "Completed add-on proof unavailable or inconsistent; halt preserved"
        print("RECOVERY_BLOCKED | " + reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
