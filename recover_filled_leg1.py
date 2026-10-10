"""Reconcile diagnosed Colorado/Alaska first fills; GET-only dry run by default.

--apply makes one atomic local recovery write, never an order. The original
request, key and receipt remain immutable. Ordinary startup still halts an
unfinished execution; continuing leg two requires a separate explicit command.
"""
import argparse
import copy
import json
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

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

TARGET = {
    "attempt": "7d4dcd813328d03afef241e8baba01e8117ccf0bf924d19885105a2f4c724623",
    "markets": ["256", "257"], "exchanges": ["945", "946"],
    "order": 29229136, "fill": 122411192, "price": Decimal(".075"),
}
HALT_REASON = "Order expiry conflicts with the saved intent"
ALASKA_TARGET = {
    "attempt": "b620ac32c3bd51fdde79a6491dc31d440e0972995b9d9107ab4a2750341e31ab",
    "markets": ["377", "378"], "exchanges": ["1066", "1067"],
    "order": 29739740, "fill": 124491142, "price": Decimal(".295"),
}
ALASKA_HALT_REASON = "ACCOUNTING_MODEL_MISMATCH or incomplete isolated reconciliation; manual review required"


def recovery_target(key):
    """Only these two exact diagnosed attempts have a recovery permission."""
    if key == TARGET["attempt"]:
        return TARGET, "FILLED_LEG1_EXPIRY_NORMALIZATION", HALT_REASON
    pilot.require(key == ALASKA_TARGET["attempt"], "Not a supported diagnosed filled-leg attempt")
    return ALASKA_TARGET, "FILLED_LEG1_POSITION_COST_ROUNDING", ALASKA_HALT_REASON


def eligible_attempt(checkpoint, key):
    """This recovery is deliberately limited to the diagnosed filled attempt."""
    target, kind, reason = recovery_target(key)
    rounding = kind == "FILLED_LEG1_POSITION_COST_ROUNDING"
    journal = checkpoint.get("autonomous_execution", {})
    pilot.require(journal.get("active_attempt") == key
                  and checkpoint["state"] == pilot.HALTED and checkpoint["manual_review_required"]
                  and checkpoint["review_reason"] == reason,
                  "Not the diagnosed filled-leg halt")
    attempt = journal["attempts"][key]
    approval = attempt["authorization"]
    first, second = attempt["legs"]
    pilot.require(attempt["state"] == pilot.HALTED and attempt["halted_from"] == pilot.LEG1_RECONCILING
                  and attempt["halt_reason"] == reason and "filled_leg1_recovery" not in attempt
                  and approval["market_ids"] == target["markets"] and approval["exchange_ids"] == target["exchanges"]
                  and first["post_attempted"]
                  and first["intent"]["order_id"] == target["order"]
                  and first["placement_response"]["http_status"] == 200 and first["receipt"] is not None
                  and second["intent"] is None and not second["post_attempted"]
                  and all(second[k] is None for k in ("placement_response", "receipt", "activity", "review"))
                  and attempt["after"][1] is None and attempt["completion"] is None,
                  "Attempt contains unexpected execution evidence; keep the halt")
    if rounding:
        pilot.require(first["intent"]["state"] == "OBSERVED_TERMINAL" and first["activity"] is not None
                      and attempt["after"][0] is not None and first["review"]["issues"] == [
                          "POSITION_COST_SEMANTICS_UNVERIFIED",
                          "Rounded balances and ledger proximity do not prove receipt-linked fee-inclusive debit"],
                      "Not the isolated diagnosed position-cost rounding failure")
    else:
        pilot.require(first["intent"]["state"] == "ACCEPTED" and first["activity"] is None
                      and first["review"] is None and attempt["after"][0] is None,
                      "Unexpected pre-expiry-reconciliation evidence")
    pilot.require(all(k == key or a["state"] in {pilot.READY, pilot.REJECTED_RETIRED}
                      for k, a in journal["attempts"].items())
                  and all(k == key or e["execution_status"] in {"RECONCILED_PAIR", pilot.REJECTED_RETIRED}
                          for k, e in checkpoint["live_exposures"].items())
                  and not any(p["status"] == "EXITING" for p in checkpoint.get("autonomous_positions", {}).values()),
                  "Another unfinished execution prevents recovery")
    pilot.require(pilot.amount(checkpoint["last_reconciled_account_cash"]) ==
                  pilot.amount(attempt["before"]["account"]["tournament"]["myBalance"]),
                  "Saved cash baseline differs from the pre-leg-one account")
    exposure = checkpoint["live_exposures"][key]
    pilot.require(exposure["execution_status"] == "MANUAL_REVIEW"
                  and list(map(pilot.amount, exposure["confirmed_quantities"])) == ([1, 0] if rounding else [0, 0])
                  and list(map(pilot.amount, exposure["confirmed_costs"])) == ([target["price"], 0] if rounding else [0, 0])
                  and pilot.amount(exposure["capital_charge"]) == (target["price"] if rounding else 0)
                  and list(map(pilot.amount, exposure["possible_additional_quantities"])) == ([0, 0] if rounding else [1, 0]),
                  "Saved first-leg exposure is inconsistent")
    return attempt


def new_rows(before, after, identity, rounding=False):
    """Existing history must remain present and unchanged, with unique IDs."""
    if rounding:
        # Reuse stable financial identities: a display price/title refresh does
        # not change the original execution or cash amount of a ledger event.
        from recover_completed_pair import history_delta, fill_identity, transaction_identity
        return history_delta(before, after, identity, fill_identity if identity == "id" else transaction_identity)
    old = {r[identity]: r for r in before}
    new = {r[identity]: r for r in after}
    pilot.require(len(old) == len(before) and len(new) == len(after)
                  and all(new.get(key) == row for key, row in old.items()),
                  "History changed or contains duplicate identifiers")
    return [r for r in after if r[identity] not in old]


def prove_fill(attempt, snapshot, orders, observed, target=None):
    """Check the actual order, exact fill, inventory, ledger and rounded cash.

    This uses the existing historical zero-extra-fee assumption and rounding
    bound. It does not claim a formally verified fee-inclusive debit authority.
    Archived snapshots are checked by UTC chronology and capture duration;
    monotonic clocks from different processes must never be compared.
    """
    target = TARGET if target is None else target
    rounding = target is ALASKA_TARGET
    original = attempt["legs"][0]["intent"]
    body, receipt = original["request"], attempt["legs"][0]["receipt"]
    mid, eid = target["markets"][0], target["exchanges"][0]
    tid = attempt["authorization"]["tournament_id"]
    price = target["price"]
    pilot.require(original["market_id"] == mid and body["exchangeId"] == eid
                  and body["tournamentId"] == tid and body["side"] == "no" and body["action"] == "buy"
                  and body["quantity"] == 1 and Decimal(str(body["price"])) == price
                  and observed["request"] == body and observed["approval"] == original["approval"]
                  and observed["order_id"] == target["order"] and observed["state"] == "OBSERVED_TERMINAL",
                  "Saved/observed intent differs from the diagnosed one-contract buy")
    auto.single.validate_intent(observed)
    observation = observed["observation"]
    pilot.require(observation["open"] is False and observation["filled_quantity"] == 1
                  and Decimal(str(observation["filled_cost"])) == price
                  and receipt["orderId"] == target["order"] and scanner.numeric_id(receipt["exchangeId"]) == eid
                  and receipt["side"] == "no" and receipt["action"] == "buy" and receipt["quantity"] == 1
                  and receipt["quantityTraded"] == 1 and receipt["remainingQuantity"] == 0
                  and receipt["open"] is False and Decimal(str(receipt["price"])) == price
                  and Decimal(str(receipt["totalCost"])) == price and Decimal(str(receipt["fillPrice"])) == price,
                  "Original receipt or observed full fill is inconsistent")
    # The real receipt includes ALL economics even though no collateral was
    # used. Allow only its explicit zero-collateral case, never a cash advance.
    economics = receipt.get("all")
    if rounding:
        pilot.require(probe.ordinary_cash_receipt(economics, price), "Receipt has unexplained collateral/cash economics")
    if economics is not None:
        expected = {"fullNotionalCost": price, "effectiveEntryCost": price, "netBuyingPowerImpact": price,
                    "collateralSavings": Decimal(0), "guaranteedPayoutFloorAfter": Decimal(0),
                    "outstandingAdvanceAfter": Decimal(0), "collateralRepayment": Decimal(0), "redemptionCredit": Decimal(0)}
        pilot.require(set(economics) == set(expected) | {"relationshipIds", "componentId"}
                      and economics["relationshipIds"] == [] and economics["componentId"] is None
                      and all(Decimal(str(economics[k])) == value for k, value in expected.items()),
                      "Receipt has unexplained collateral/cash economics")
    before, account = attempt["before"], snapshot["account"]
    duration = snapshot["freshness"]["completed_monotonic"] - snapshot["freshness"]["started_monotonic"]
    stamp = auto.single.parse_api_timestamp(original["created_at"])
    pilot.require(snapshot["data_complete"] is True and all(snapshot["coverage"][k]["complete"] is True
                  for k in ("fills", "transactions")) and 0 <= duration <= pilot_account.MAX_ACCOUNT_READ_AGE
                  and auto.single.parse_api_timestamp(snapshot["history_since"]) <= stamp
                  and auto.single.parse_api_timestamp(snapshot["freshness"]["observed_at"]) >= stamp
                  and account["tournament"]["id"] == tid
                  and account["tournament"]["slug"] == attempt["authorization"]["tournament_slug"]
                  and not account["orders"] and not before["account"]["orders"]
                  and account["quarantine_reserve"] == before["account"]["quarantine_reserve"],
                  "Account scope, freshness, quarantine or history coverage is inconsistent")
    pilot.require(len(snapshot["order_activity"]) == 1, "Recovery must reconcile exactly the known order")
    activity = snapshot["order_activity"][0]
    order = activity["order"]
    # Reuse the common pure parameter verifier, including normalized expiry.
    auto.single.verify_order_intent(order, observed)
    pilot.require(order["open"] is False and order["quantityFilled"] == 1
                  and len(activity["fills"]) == 1 and Decimal(activity["fill_notional"]) == price,
                  "Order is not the fully filled first leg")
    fill = activity["fills"][0]
    pilot.require(fill["id"] == target["fill"] and fill["side"] == "no" and fill["quantity"] == -1
                  and Decimal(str(fill["price"])) == price, "Known fill is missing or differs")
    fills = new_rows(before["recent_fills"], snapshot["recent_fills"], "id", rounding)
    pilot.require(len(fills) == 1 and fills[0]["id"] == target["fill"] and fills[0]["orderId"] == target["order"]
                  and scanner.numeric_id(fills[0]["exchangeId"]) == eid and scanner.numeric_id(fills[0]["marketId"]) == mid
                  and all(fills[0][k] == fill[k] for k in ("side", "quantity", "filledAt"))
                  and abs(Decimal(str(fills[0]["price"])) - price) <= Decimal(".000000001"),
                  "Portfolio fill history differs or contains additional execution")
    trades = new_rows(before["recent_transactions"], snapshot["recent_transactions"], "event_id", rounding)
    pilot.require(len(trades) == 1, "Unexpected transaction, fee or debit prevents recovery")
    transaction = trades[0]
    pilot_account.validate_transaction(transaction, tid)
    pilot.require(transaction["event_type"] == "trade" and transaction["orderType"] == "BUY"
                  and scanner.numeric_id(transaction["exchangeId"]) == eid
                  and scanner.numeric_id(transaction["marketId"]) == mid and transaction["quantity"] == -1
                  and Decimal(str(transaction["price"])) == price and transaction["amount"] is None
                  and transaction["transactionType"] is None
                  and auto.single.parse_api_timestamp(transaction["createdAt"]) == auto.single.parse_api_timestamp(fill["filledAt"])
                  and all(transaction.get(k) is None for k in ("outstandingAdvanceAfter", "componentId", "reason", "collateralDelta")),
                  "Transaction contradicts the known BUY or introduces unexplained cash effects")
    positions = [r for r in account["positions"] if scanner.numeric_id(r["exchangeId"]) == eid]
    pilot.require(len(positions) == 1 and not any(scanner.numeric_id(r["exchangeId"]) in target["exchanges"]
                  for r in before["account"]["positions"])
                  and probe.inventory(account, eid) == probe.inventory(before["account"], eid),
                  "Other holdings changed or the pair has unexpected exposure")
    holding = positions[0]
    if rounding:
        pilot.require(probe.account_reader.one_no_buy_cost_matches(holding, price), "POSITION_COST_ROUNDING_MISMATCH")
    pilot.require(scanner.numeric_id(holding["marketId"]) == mid and holding["quantity"] == -1
                  and holding["settled"] is False and Decimal(str(holding["avgCost"])) == price
                  and abs(Decimal(str(holding["costBasis"])) - price) <= pilot_account.BALANCE_DELTA_TOLERANCE,
                  "Holding is not exactly the known one-share NO fill")
    if holding.get("lots") is not None:
        lots = holding["lots"]
        pilot.require(len(lots) == 1 and lots[0]["side"] == "NO" and lots[0]["quantity"] == 1
                      and Decimal(str(lots[0]["entryPrice"])) == price, "Holding lot differs from the known fill")
    recent_orders = [r for r in orders if auto.single.parse_api_timestamp(r["createdAt"]) >= stamp]
    pilot.require(len(recent_orders) == 1 and recent_orders[0] == order,
                  "Another order exists or all-order history disagrees; no leg-two recovery allowed")
    cash = pilot.amount(account["tournament"]["myBalance"])
    debit = pilot.amount(before["account"]["tournament"]["myBalance"]) - cash
    pilot.require(debit >= 0 and abs(debit - price) <= pilot_account.BALANCE_DELTA_TOLERANCE,
                  "Observed account debit differs from the historical model beyond rounding tolerance")
    return {"fill_notional": str(price), "reported_balance_debit": str(debit),
            "conservative_allocation_charge": str(max(price, debit + pilot_account.BALANCE_DELTA_TOLERANCE)),
            "accounting_policy": auto.POLICY, "formally_verified": False,
            "rounding_note": "Displayed balance/cost basis are rounded; no separate fee observed, not a fee proof.",
            "observations": {"isolated_execution_reconciled": True, "execution_and_inventory_verified": True}}


def proposed_checkpoint(checkpoint, key, record):
    target, _, _ = recovery_target(key)
    proposed = copy.deepcopy(checkpoint)
    attempt = proposed["autonomous_execution"]["attempts"][key]
    first = attempt["legs"][0]
    first.update(intent=copy.deepcopy(record["observed_intent"]), activity=copy.deepcopy(record["snapshot"]["order_activity"][0]),
                 review=copy.deepcopy(record["review"]))
    attempt["after"][0] = copy.deepcopy(record["snapshot"])
    attempt["reads"].extend(copy.deepcopy(record["reads"]))
    attempt["filled_leg1_recovery"] = copy.deepcopy(record)
    attempt["stages"].extend([{"state": pilot.HALTED, "at": checkpoint["updated_at"]},
                              {"state": pilot.LEG2_RECHECK, "at": record["recovered_at"]}])
    attempt.update(state=pilot.LEG2_RECHECK, halt_reason="", halted_from=None)
    exposure = proposed["live_exposures"][key]
    charge = record["review"]["conservative_allocation_charge"]
    exposure.update(confirmed_quantities=["1", "0"], confirmed_costs=[str(target["price"]), "0"],
                    possible_additional_quantities=["0", "1"], capital_charge=charge,
                    execution_status="EXECUTING", accounting_buffer=str(auto.ACCOUNTING_BUFFER))
    proposed["accounted_pair_costs"][key] = charge
    proposed.update(state=pilot.LEG2_RECHECK, manual_review_required=False, review_reason="",
                    last_reconciled_account_cash=str(pilot.amount(record["snapshot"]["account"]["tournament"]["myBalance"])))
    auto.recalculate_budget(proposed)
    pilot.refresh_totals(proposed)
    return proposed


def validate_recovered(checkpoint, key):
    """Keep recovery proof and retired key immutable through later completion."""
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    target, kind, reason = recovery_target(key)
    record = attempt["filled_leg1_recovery"]
    fields = {"version", "kind", "recovered_at", "prior_checkpoint_hash",
                  "prior_revision", "prior_updated_at", "prior_halt_reason", "original_intent", "original_receipt",
                  "observed_intent", "snapshot", "orders", "reads", "review", "submission_status",
                  "retired_idempotency_key", "resume_consumed_at"}
    if kind == "FILLED_LEG1_POSITION_COST_ROUNDING":
        fields.add("original_leg1_evidence")
        pilot.require(set(record["original_leg1_evidence"]) == {"activity", "review", "after"}
                      and record["original_leg1_evidence"]["review"]["issues"] == [
                          "POSITION_COST_SEMANTICS_UNVERIFIED",
                          "Rounded balances and ledger proximity do not prove receipt-linked fee-inclusive debit"],
                      "Original rounding halt evidence is missing")
    pilot.require(set(record) == fields
                  and record["version"] == 1 and record["kind"] == kind
                  and record["prior_halt_reason"] == reason and record["submission_status"] == "RETIRED_NEVER_RETRY"
                  and len(record["prior_checkpoint_hash"]) == 64 and type(record["prior_revision"]) is int
                  and record["retired_idempotency_key"] == attempt["legs"][0]["intent"]["request"]["idempotencyKey"]
                  and record["original_intent"]["request"] == attempt["legs"][0]["intent"]["request"]
                  and record["original_receipt"] == attempt["legs"][0]["receipt"]
                  and attempt["legs"][0]["intent"] == record["observed_intent"]
                  and attempt["legs"][0]["review"] == record["review"] and attempt["after"][0] == record["snapshot"],
                  "Filled-leg recovery evidence/key changed")
    pilot.require(auto.single.parse_api_timestamp(record["recovered_at"]) >= auto.single.parse_api_timestamp(record["prior_updated_at"])
                  and record["reads"] and all(r.get("http_status") == 200 and r.get("credential_redacted") is False for r in record["reads"]),
                  "Invalid recovery timestamp or raw GET evidence")
    archived = copy.deepcopy(attempt)
    archived["legs"][0]["intent"] = record["original_intent"]
    pilot.require(prove_fill(archived, record["snapshot"], record["orders"], record["observed_intent"], target) == record["review"],
                  "Archived recovery proof is inconsistent")
    if record["resume_consumed_at"] is not None:
        pilot.require(auto.single.parse_api_timestamp(record["resume_consumed_at"]) >= auto.single.parse_api_timestamp(record["recovered_at"]),
                      "Resume permission predates recovery")
    pilot.require(not attempt["legs"][1]["post_attempted"] or record["resume_consumed_at"] is not None,
                  "Recovered second leg lacks explicit consumed resume permission")


def validate_transition(previous, proposed):
    key = previous["autonomous_execution"]["active_attempt"]
    attempt = eligible_attempt(previous, key)
    target, kind, _ = recovery_target(key)
    record = proposed["autonomous_execution"]["attempts"][key]["filled_leg1_recovery"]
    pilot.require(record["prior_checkpoint_hash"] == pilot.snapshot_hash(previous)
                  and record["prior_revision"] == previous["revision"] and record["prior_updated_at"] == previous["updated_at"]
                  and record["original_intent"] == attempt["legs"][0]["intent"]
                  and record["original_receipt"] == attempt["legs"][0]["receipt"] and record["resume_consumed_at"] is None,
                  "Recovery does not bind the current halt and original request/receipt")
    if kind == "FILLED_LEG1_POSITION_COST_ROUNDING":
        pilot.require(record["original_leg1_evidence"] == {"activity": attempt["legs"][0]["activity"],
                      "review": attempt["legs"][0]["review"], "after": attempt["after"][0]},
                      "Original first-leg review, fills or account evidence changed")
    pilot.require(record["review"] == prove_fill(attempt, record["snapshot"], record["orders"], record["observed_intent"], target)
                  and proposed == proposed_checkpoint(previous, key, record), "Recovery contains an unrelated state change")
    model = sum((sum(map(pilot.amount, e["confirmed_costs"])) for e in proposed["live_exposures"].values()), Decimal(0))
    returned = sum((pilot.amount(p["exit_proceeds"]) for p in proposed.get("autonomous_positions", {}).values()), Decimal(0))
    cash = pilot.amount(proposed["last_reconciled_account_cash"])
    pilot.require(abs(pilot.amount(proposed["autonomous_execution"]["reference_cash"]) - cash - model + returned)
                  <= pilot_account.BALANCE_DELTA_TOLERANCE and cash - Decimal(".01") >= pilot.amount(proposed["untouchable_cash_reserve"]),
                  "Cumulative account balance or untouchable reserve is inconsistent")
    pilot_account.check_fresh(record["snapshot"]["freshness"]["started_monotonic"])


def require_resume(checkpoint, key):
    pilot.require(checkpoint["state"] == pilot.LEG2_RECHECK and not checkpoint["manual_review_required"]
                  and checkpoint["autonomous_execution"]["active_attempt"] == key,
                  "Only the explicitly recovered active first leg may resume")
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    pilot.require("filled_leg1_recovery" in attempt, "Missing filled-leg recovery evidence")
    validate_recovered(checkpoint, key)
    pilot.require(attempt["filled_leg1_recovery"]["resume_consumed_at"] is None
                  and attempt["legs"][1]["intent"] is None and not attempt["legs"][1]["post_attempted"],
                  "Resume already consumed or second-leg intent exists; never replay")


def recover(session, key, apply=False):
    with readonly_pilot_lock() as path:
        checkpoint = pilot._read_checkpoint_locked(path)
        attempt = eligible_attempt(checkpoint, key)
        target, kind, _ = recovery_target(key)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        reads = probe.EvidenceReads(session)  # Memory only until explicit --apply.
        get_only = SettlementGets(reads)  # GET only; no order adapter is exposed.
        pilot.read_live_pair(get_only, attempt["authorization"])
        started = time.monotonic()
        orders = read_orders(get_only, checkpoint["tournament_id"])
        observed = copy.deepcopy(attempt["legs"][0]["intent"])
        observed["state"], observed["observation"] = auto.single.observe_test(get_only, observed)
        snapshot = pilot_account.read_snapshot(get_only, attempt["authorization"]["tournament_slug"], checkpoint,
                                               started=started, order_ids=[target["order"]])
        pilot.require(read_orders(get_only, checkpoint["tournament_id"]) == orders, "Order history changed during recovery")
        review = prove_fill(attempt, snapshot, orders, observed, target)
        record = {"version": 1, "kind": kind, "recovered_at": datetime.now(timezone.utc).isoformat(),
                  "prior_checkpoint_hash": pilot.snapshot_hash(checkpoint), "prior_revision": checkpoint["revision"],
                  "prior_updated_at": checkpoint["updated_at"], "prior_halt_reason": checkpoint["review_reason"],
                  "original_intent": copy.deepcopy(attempt["legs"][0]["intent"]), "original_receipt": copy.deepcopy(attempt["legs"][0]["receipt"]),
                  "observed_intent": observed, "snapshot": snapshot, "orders": orders, "reads": reads.reads, "review": review,
                  "submission_status": "RETIRED_NEVER_RETRY", "retired_idempotency_key": observed["request"]["idempotencyKey"],
                  "resume_consumed_at": None}
        if kind == "FILLED_LEG1_POSITION_COST_ROUNDING":
            record["original_leg1_evidence"] = copy.deepcopy({"activity": attempt["legs"][0]["activity"],
                "review": attempt["legs"][0]["review"], "after": attempt["after"][0]})
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
                proposed = pilot._save_checkpoint_locked(proposed, path, filled_recovery=True)
            finally:
                pilot._state_lock_owners.pop(path, None)
        fields = ("state", "revision", "confirmed_cumulative_debits", "allocated_cash_remaining", "reserved_unconfirmed_capital",
                  "quarantine_reserve", "calculated_remaining_allocation", "last_reconciled_account_cash")
        after = {f: proposed[f] for f in fields}
        if not apply:
            after["revision"] += 1
        return {"result": "RECOVERED" if apply else "DRY_RUN_PASS", "attempt": key,
                "order_id": target["order"], "fill_id": target["fill"], "accounting": review,
                "before": {f: checkpoint[f] for f in fields}, "after": after,
                "exposure_after": proposed["live_exposures"][key], "retired_idempotency_key": record["retired_idempotency_key"],
                "leg_two_intent": None, "orders_submitted": 0, "state_written": apply, "get_requests": len(reads.reads),
                "continuation": "Separate explicit --resume-recovered-leg1 command; ordinary startup halts unfinished execution"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", required=True, help="Exact diagnosed active attempt ID")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="GET-only preview (default)")
    mode.add_argument("--apply", action="store_true", help="Persist only reconciliation; never submit leg two")
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
        reason = str(error) if isinstance(error, (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked, auto.single.TestError)) else "GET reconciliation unavailable; halt retained"
        print("RECOVERY_BLOCKED | " + reason)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
