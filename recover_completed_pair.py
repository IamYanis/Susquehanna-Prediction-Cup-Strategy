"""Finalize the filled Colorado pair after its zero-collateral false positive.

Default --dry-run is GET-only and writes nothing. --apply repeats every check
under the existing exclusive lock and makes one atomic local completion write.
There is no order submission/cancellation or resume adapter in this command.
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
import live_pilot as pilot
import live_settlement as live
import pilot_account
import price_reader as scanner
import supervised_accounting_probe as probe
from recover_rejected_attempt import read_orders
from recover_settlement_halt import SettlementGets
from senate_readiness_watcher import readonly_pilot_lock

TARGET = {"attempt": "7d4dcd813328d03afef241e8baba01e8117ccf0bf924d19885105a2f4c724623",
          "markets": ["256", "257"], "exchanges": ["945", "946"],
          "orders": [29229136, 29294586], "fills": [122411192, 122673772],
          "prices": [Decimal(".075"), Decimal(".900")], "displayed_debit": Decimal(".980")}
HALT_REASON = "ACCOUNTING_MODEL_MISMATCH or incomplete isolated reconciliation; manual review required"
ORIGINAL_ISSUES = ["COLLATERAL_CASH_EFFECT_UNVERIFIED",
                   "Rounded balances and ledger proximity do not prove receipt-linked fee-inclusive debit"]


def eligible_attempt(checkpoint, key):
    journal = checkpoint.get("autonomous_execution", {})
    pilot.require(key == TARGET["attempt"] and journal.get("active_attempt") == key
                  and checkpoint["state"] == pilot.HALTED and checkpoint["manual_review_required"]
                  and checkpoint["review_reason"] == HALT_REASON, "Not the diagnosed completed-pair collateral halt")
    attempt = journal["attempts"][key]
    pilot.require(attempt["state"] == pilot.HALTED and attempt["halted_from"] == pilot.FINAL_RECONCILING
                  and attempt["halt_reason"] == HALT_REASON and "final_reconciliation_recovery" not in attempt
                  and attempt["authorization"]["market_ids"] == TARGET["markets"]
                  and attempt["authorization"]["exchange_ids"] == TARGET["exchanges"]
                  and attempt["filled_leg1_recovery"]["resume_consumed_at"] is not None
                  and attempt["legs"][1]["review"]["issues"] == ORIGINAL_ISSUES
                  and all(leg["post_attempted"] and leg["intent"]["state"] == "OBSERVED_TERMINAL"
                          and leg["intent"]["order_id"] == oid and leg["intent"]["observation"]["filled_quantity"] == 1
                          and leg["intent"]["observation"]["open"] is False
                          for leg, oid in zip(attempt["legs"], TARGET["orders"]))
                  and all(snapshot is not None for snapshot in attempt["after"])
                  and key not in checkpoint.get("autonomous_positions", {}), "Unexpected saved execution evidence; keep halt")
    pilot.require(all(k == key or a["state"] in {pilot.READY, pilot.REJECTED_RETIRED} for k, a in journal["attempts"].items())
                  and all(k == key or e["execution_status"] in {"RECONCILED_PAIR", pilot.REJECTED_RETIRED}
                          for k, e in checkpoint["live_exposures"].items())
                  and not any(p["status"] == "EXITING" for p in checkpoint.get("autonomous_positions", {}).values()),
                  "Another unresolved execution prevents recovery")
    exposure = checkpoint["live_exposures"][key]
    pilot.require(list(map(pilot.amount, exposure["confirmed_quantities"])) == [1, 1]
                  and list(map(pilot.amount, exposure["confirmed_costs"])) == TARGET["prices"]
                  and all(pilot.amount(q) == 0 for q in exposure["possible_additional_quantities"])
                  and exposure["execution_status"] == "MANUAL_REVIEW", "Durable exposure does not match the two full fills")
    return attempt


def transaction_identity(row):
    """Share the execution/cash comparison, including any order/fill linkage."""
    return pilot_account.canonical_transaction(row)


def fill_identity(row):
    return (row["orderId"], row["exchangeId"], row["marketId"], row["quantity"], row["side"],
            auto.single.parse_api_timestamp(row["filledAt"]))


def history_delta(before, after, key, identity):
    old, current = {r[key]: r for r in before}, {r[key]: r for r in after}
    pilot.require(len(old) == len(before) and len(current) == len(after)
                  and all(k in current and identity(row) == identity(current[k]) for k, row in old.items()),
                  "Existing history disappeared, changed or contains duplicate IDs")
    if key == "id":
        pilot.require(all(abs(Decimal(str(row["price"])) - Decimal(str(current[k]["price"]))) <= Decimal(".000000001")
                          for k, row in old.items()), "Existing fill prices changed")
    return [r for r in after if r[key] not in old]


def prove_complete(attempt, snapshot, orders):
    """Validate fresh exchange evidence without replaying an execution path."""
    before, account = attempt["before"], snapshot["account"]
    duration = snapshot["freshness"]["completed_monotonic"] - snapshot["freshness"]["started_monotonic"]
    stamp = auto.single.parse_api_timestamp(attempt["created_at"])
    pilot.require(snapshot["data_complete"] is True and all(snapshot["coverage"][k]["complete"] is True
                  for k in ("fills", "transactions")) and 0 <= duration <= pilot_account.MAX_ACCOUNT_READ_AGE
                  and auto.single.parse_api_timestamp(snapshot["history_since"]) <= stamp
                  and auto.single.parse_api_timestamp(snapshot["freshness"]["observed_at"]) >= stamp
                  and account["tournament"]["id"] == attempt["authorization"]["tournament_id"]
                  and account["tournament"]["slug"] == attempt["authorization"]["tournament_slug"]
                  and not account["orders"] and not before["account"]["orders"] and not before["account"]["positions"]
                  and account["quarantine_reserve"] == before["account"]["quarantine_reserve"],
                  "Account scope, capture, history, open orders or quarantine is inconsistent")
    pilot.require(len(account["positions"]) == 2 and len(snapshot["order_activity"]) == 2,
                  "Expected exactly two holdings and two order lifecycles")
    activities = {r["order"]["id"]: r for r in snapshot["order_activity"]}
    pilot.require(set(activities) == set(TARGET["orders"]), "Known order lifecycles are missing or duplicated")
    portfolio_fills = history_delta(before["recent_fills"], snapshot["recent_fills"], "id", fill_identity)
    transactions = history_delta(before["recent_transactions"], snapshot["recent_transactions"], "event_id", transaction_identity)
    archived_transactions = history_delta(before["recent_transactions"], attempt["after"][1]["recent_transactions"],
                                         "event_id", transaction_identity)
    pilot.require(len(portfolio_fills) == 2 and {r["id"] for r in portfolio_fills} == set(TARGET["fills"])
                  and len(transactions) == len(archived_transactions) == 2
                  and {r["event_id"]: transaction_identity(r) for r in transactions} ==
                      {r["event_id"]: transaction_identity(r) for r in archived_transactions},
                  "Unrelated, contradictory, missing or changed fill/ledger activity")
    for i, (mid, eid, oid, fid, price) in enumerate(zip(TARGET["markets"], TARGET["exchanges"],
                                                      TARGET["orders"], TARGET["fills"], TARGET["prices"])):
        leg, activity = attempt["legs"][i], activities[oid]
        intent, receipt = leg["intent"], leg["receipt"]
        order = activity["order"]
        auto.single.validate_intent(intent)
        auto.single.verify_order_intent(order, intent)
        pilot.require(intent["market_id"] == mid and intent["request"]["exchangeId"] == eid
                      and intent["request"]["side"] == "no" and intent["request"]["action"] == "buy"
                      and intent["request"]["quantity"] == 1 and Decimal(str(intent["request"]["price"])) == price
                      and order["open"] is False and order["quantity"] == order["quantityFilled"] == 1
                      and len(activity["fills"]) == 1 and Decimal(activity["fill_notional"]) == price
                      and receipt["orderId"] == oid and scanner.numeric_id(receipt["exchangeId"]) == eid
                      and receipt["side"] == "no" and receipt["action"] == "buy" and receipt["quantity"] == 1
                      and receipt["quantityTraded"] == 1 and receipt["remainingQuantity"] == 0 and receipt["open"] is False
                      and all(Decimal(str(receipt[k])) == price for k in ("price", "fillPrice", "totalCost"))
                      and probe.ordinary_cash_receipt(receipt["all"], price), "Order/receipt is not the exact ordinary completed buy")
        fill = activity["fills"][0]
        pilot.require(fill["id"] == fid and fill["side"] == "no" and fill["quantity"] == -1
                      and Decimal(str(fill["price"])) == price, "Known full fill differs")
        pf = next(r for r in portfolio_fills if r["id"] == fid)
        pilot.require(pf["orderId"] == oid and scanner.numeric_id(pf["marketId"]) == mid
                      and scanner.numeric_id(pf["exchangeId"]) == eid and pf["side"] == "no" and pf["quantity"] == -1
                      and auto.single.parse_api_timestamp(pf["filledAt"]) == auto.single.parse_api_timestamp(fill["filledAt"])
                      and abs(Decimal(str(pf["price"])) - price) <= Decimal(".000000001"), "Portfolio fill differs from the order fill")
        matching_positions = [p for p in account["positions"] if scanner.numeric_id(p["exchangeId"]) == eid]
        pilot.require(len(matching_positions) == 1, "Expected one exact holding per exchange")
        holding = matching_positions[0]
        prior = next(p for p in attempt["after"][1]["account"]["positions"] if scanner.numeric_id(p["exchangeId"]) == eid)
        pilot.require(scanner.numeric_id(holding["marketId"]) == mid and holding["quantity"] == -1 and holding["settled"] is False
                      and Decimal(str(holding["avgCost"])) == price and holding["costBasis"] == prior["costBasis"]
                      and abs(Decimal(str(holding["costBasis"])) - price) <= pilot_account.BALANCE_DELTA_TOLERANCE
                      and holding.get("lots") == prior.get("lots"), "Holding changed or differs from the exact NO fill")
        trades = [t for t in transactions if scanner.numeric_id(t["exchangeId"]) == eid]
        pilot.require(len(trades) == 1, "Expected one exact BUY transaction per leg")
        trade = trades[0]
        pilot_account.validate_transaction(trade, account["tournament"]["id"])
        pilot.require(trade["event_type"] == "trade" and trade["orderType"] == "BUY"
                      and scanner.numeric_id(trade["marketId"]) == mid and trade["quantity"] == -1
                      and Decimal(str(trade["price"])) == price and trade["amount"] is None and trade["transactionType"] is None
                      and auto.single.parse_api_timestamp(trade["createdAt"]) == auto.single.parse_api_timestamp(fill["filledAt"])
                      and all(trade.get(k) is None for k in ("outstandingAdvanceAfter", "componentId", "reason", "collateralDelta")),
                      "Transaction contradicts the fill or has unexplained cash effects")
    recent_orders = [r for r in orders if auto.single.parse_api_timestamp(r["createdAt"]) >= stamp]
    pilot.require(len(recent_orders) == 2 and {r["id"] for r in recent_orders} == set(TARGET["orders"]),
                  "An additional order exists since the attempt")
    for row in recent_orders:
        pilot.require(row == activities[row["id"]]["order"], "All-status and specific order reporting disagree")
    cost = sum(TARGET["prices"], Decimal(0))
    cash = pilot.amount(account["tournament"]["myBalance"])
    debit = pilot.amount(before["account"]["tournament"]["myBalance"]) - cash
    pilot.require(cash == pilot.amount(attempt["after"][1]["account"]["tournament"]["myBalance"])
                  and debit == TARGET["displayed_debit"] and abs(debit - cost) <= pilot_account.BALANCE_DELTA_TOLERANCE,
                  "Balance changed or displayed pair debit differs from the expected rounded model")
    return {"pair_notional": str(cost), "displayed_pair_debit": str(debit), "difference": str(debit - cost),
            "tolerance": str(pilot_account.BALANCE_DELTA_TOLERANCE),
            "conservative_allocation_charge": str(max(cost, debit + pilot_account.BALANCE_DELTA_TOLERANCE)),
            "policy": auto.POLICY, "formally_verified": False}


def corrected_review(attempt):
    leg = attempt["legs"][1]
    review = probe.review_observation(attempt["after"][0], attempt["after"][1], leg["receipt"], leg["activity"],
                                     TARGET["markets"][1], TARGET["exchanges"][1])
    pilot.require(review["observations"]["isolated_execution_reconciled"] is True,
                  "Corrected isolated review still fails; keep halt")
    return review


def proposed_checkpoint(checkpoint, key, record):
    proposed = copy.deepcopy(checkpoint)
    attempt = proposed["autonomous_execution"]["attempts"][key]
    attempt["final_reconciliation_recovery"] = copy.deepcopy(record)
    attempt["legs"][1]["review"] = copy.deepcopy(record["corrected_review"])
    attempt["reads"].extend(copy.deepcopy(record["reads"]))
    attempt["stages"].extend([{"state": pilot.HALTED, "at": checkpoint["updated_at"]},
                              {"state": pilot.FINAL_RECONCILING, "at": record["recovered_at"]},
                              {"state": pilot.READY, "at": record["recovered_at"]}])
    attempt.update(state=pilot.READY, halt_reason="", halted_from=None)
    exposure = proposed["live_exposures"][key]
    charge = record["accounting"]["conservative_allocation_charge"]
    exposure.update(execution_status="RECONCILED_PAIR", accounting_buffer="0", capital_charge=charge)
    proposed["accounted_pair_costs"][key] = charge
    proposed.update(state=pilot.READY, manual_review_required=False, review_reason="",
                    last_reconciled_account_cash=str(pilot.amount(record["snapshot"]["account"]["tournament"]["myBalance"])))
    proposed["autonomous_execution"]["active_attempt"] = None
    auto.record_position(proposed, key, record["snapshot"])
    auto.recalculate_budget(proposed)
    return proposed


def validate_completed(checkpoint, key):
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    record = attempt["final_reconciliation_recovery"]
    pilot.require(key == TARGET["attempt"] and set(record) == {"version", "kind", "recovered_at", "prior_checkpoint_hash",
                  "prior_revision", "prior_updated_at", "prior_halt_reason", "original_leg2_review", "corrected_review",
                  "snapshot", "orders", "reads", "accounting", "retained_idempotency_keys"}
                  and record["version"] == 1 and record["kind"] == "COMPLETED_PAIR_ZERO_COLLATERAL"
                  and record["prior_halt_reason"] == HALT_REASON and len(record["prior_checkpoint_hash"]) == 64
                  and type(record["prior_revision"]) is int and record["prior_revision"] > 0
                  and record["original_leg2_review"]["issues"] == ORIGINAL_ISSUES
                  and attempt["state"] == pilot.READY and attempt["legs"][1]["review"] == record["corrected_review"]
                  and record["retained_idempotency_keys"] == [leg["intent"]["request"]["idempotencyKey"] for leg in attempt["legs"]]
                  and attempt["filled_leg1_recovery"]["resume_consumed_at"] is not None,
                  "Completed-pair recovery audit/key changed")
    pilot.require(auto.single.parse_api_timestamp(record["recovered_at"]) >= auto.single.parse_api_timestamp(record["prior_updated_at"])
                  and record["reads"] and all(r.get("http_status") == 200 and r.get("credential_redacted") is False for r in record["reads"])
                  and record["accounting"] == prove_complete(attempt, record["snapshot"], record["orders"])
                  and record["corrected_review"] == corrected_review(attempt), "Invalid completed-pair recovery evidence")


def validate_transition(previous, proposed):
    key = previous["autonomous_execution"]["active_attempt"]
    attempt = eligible_attempt(previous, key)
    record = proposed["autonomous_execution"]["attempts"][key]["final_reconciliation_recovery"]
    pilot.require(record["prior_checkpoint_hash"] == pilot.snapshot_hash(previous)
                  and record["prior_revision"] == previous["revision"] and record["prior_updated_at"] == previous["updated_at"]
                  and record["original_leg2_review"] == attempt["legs"][1]["review"]
                  and record["accounting"] == prove_complete(attempt, record["snapshot"], record["orders"])
                  and proposed == proposed_checkpoint(previous, key, record), "Recovery does not bind the exact current halt/completion")
    model = sum((sum(map(pilot.amount, e["confirmed_costs"])) for e in proposed["live_exposures"].values()), Decimal(0))
    returned = sum((pilot.amount(p["exit_proceeds"]) for p in proposed.get("autonomous_positions", {}).values()), Decimal(0))
    cash = pilot.amount(proposed["last_reconciled_account_cash"])
    pilot.require(abs(pilot.amount(proposed["autonomous_execution"]["reference_cash"]) - cash - model + returned)
                  <= pilot_account.BALANCE_DELTA_TOLERANCE and cash - Decimal(".01") >= pilot.amount(proposed["untouchable_cash_reserve"]),
                  "Cumulative cash or untouchable reserve is inconsistent")
    pilot_account.check_fresh(record["snapshot"]["freshness"]["started_monotonic"])


def recover(session, key, apply=False):
    with readonly_pilot_lock() as path:
        checkpoint = pilot._read_checkpoint_locked(path)
        attempt = eligible_attempt(checkpoint, key)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        reads = probe.EvidenceReads(session)
        gets = SettlementGets(reads)  # Exposes GET only, never POST/DELETE.
        pilot.read_live_pair(gets, attempt["authorization"])
        started = time.monotonic()
        orders = read_orders(gets, checkpoint["tournament_id"])
        snapshot = pilot_account.read_snapshot(gets, attempt["authorization"]["tournament_slug"], checkpoint,
                                               started=started, order_ids=TARGET["orders"])
        pilot.require(read_orders(gets, checkpoint["tournament_id"]) == orders, "Order history changed during recovery")
        accounting = prove_complete(attempt, snapshot, orders)
        review = corrected_review(attempt)
        record = {"version": 1, "kind": "COMPLETED_PAIR_ZERO_COLLATERAL", "recovered_at": datetime.now(timezone.utc).isoformat(),
                  "prior_checkpoint_hash": pilot.snapshot_hash(checkpoint), "prior_revision": checkpoint["revision"],
                  "prior_updated_at": checkpoint["updated_at"], "prior_halt_reason": checkpoint["review_reason"],
                  "original_leg2_review": copy.deepcopy(attempt["legs"][1]["review"]), "corrected_review": review,
                  "snapshot": snapshot, "orders": orders, "reads": reads.reads, "accounting": accounting,
                  "retained_idempotency_keys": [leg["intent"]["request"]["idempotencyKey"] for leg in attempt["legs"]]}
        proposed = proposed_checkpoint(checkpoint, key, record)
        pilot.validate_checkpoint(proposed)
        validate_transition(checkpoint, proposed)
        live.revalidate_authorization(attempt["authorization"])
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        pilot_account.check_fresh(started)
        if apply:
            pilot.require(path not in pilot._state_lock_owners, "Another operation owns pilot state")
            pilot._state_lock_owners[path] = (os.getpid(), threading.get_ident())
            try:
                proposed = pilot._save_checkpoint_locked(proposed, path, completed_recovery=True)
            finally:
                pilot._state_lock_owners.pop(path, None)
        fields = ("state", "revision", "manual_review_required", "confirmed_cumulative_debits", "allocated_cash_remaining",
                  "reserved_unconfirmed_capital", "total_live_exposure", "quarantine_reserve", "calculated_remaining_allocation",
                  "last_reconciled_account_cash")
        position = proposed["autonomous_positions"][key]
        after = {f: proposed[f] for f in fields}
        if not apply:
            # The atomic save increments the revision once. Report that expected
            # revision in a dry run without changing the checkpoint or any file.
            after["revision"] += 1
        return {"result": "RECOVERED" if apply else "DRY_RUN_PASS", "attempt": key, "accounting": accounting,
                "before": {f: checkpoint[f] for f in fields}, "after": after,
                "position": {k: position[k] for k in ("pair", "market_ids", "exchange_ids", "status", "quantity",
                    "entry_timestamp", "actual_entry_prices", "total_entry_cost", "entry_edge", "remaining_quantities")},
                "active_attempt_after": proposed["autonomous_execution"]["active_attempt"],
                "resume_consumed_at": proposed["autonomous_execution"]["attempts"][key]["filled_leg1_recovery"]["resume_consumed_at"],
                "orders_submitted": 0, "state_written": apply, "get_requests": len(reads.reads)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", required=True, help="Exact diagnosed completed execution ID")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="GET-only preview (default)")
    mode.add_argument("--apply", action="store_true", help="Atomic local finalization only; no orders")
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
        reason = str(error) if isinstance(error, (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked,
                        auto.single.TestError, probe.ProbeBlocked, live.LiveSettlementBlocked)) else "GET reconciliation unavailable; halt retained"
        print("RECOVERY_BLOCKED | " + reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
