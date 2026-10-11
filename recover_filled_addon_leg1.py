"""GET-only recovery of Colorado's reviewed, filled first add-on leg.

Default --dry-run leaves the halt untouched. --apply writes one audited local
transition under the exclusive lock. It never prepares or submits leg two.
The existing explicit first-leg continuation remains a separate command.
"""
import copy
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal

import account_reader
import autonomous_pilot as auto
import live_pilot as pilot
import live_settlement as live
import pilot_account
import price_reader as scanner
import recover_filled_leg1 as filled
import supervised_accounting_probe as probe
from recover_completed_pair import history_delta, fill_identity, transaction_identity
from recover_rejected_attempt import read_orders
from recover_settlement_halt import SettlementGets
from senate_readiness_watcher import readonly_pilot_lock

TARGET = {"attempt": "c163a240772836f934fe35fc42ad2f467d46bfca05949e9cb400aa4c637f8c50",
          "parent": "7d4dcd813328d03afef241e8baba01e8117ccf0bf924d19885105a2f4c724623",
          "revision": 577, "markets": ["256", "257"], "exchanges": ["945", "946"],
          "order": 30406346, "fill": 127219604, "transaction": "engine-127219609",
          "key": "account-test-3e4b1f1ddadf4656858651919f4ea99b", "price": Decimal(".075"),
          "baseline_quantity": 3, "baseline_costs": [Decimal(".225"), Decimal("2.700")],
          "cash": Decimal("20566.40")}
KIND = "FILLED_ADDON_LEG1_FRESHNESS"
HALT_REASON = "STALE_ACCOUNT_DATA | Account read exceeded the existing 15-second freshness window"


def eligible_attempt(checkpoint, key):
    """No generic halt override: bind this exact incomplete marginal unit."""
    pilot.validate_checkpoint(checkpoint)
    journal = checkpoint.get("autonomous_execution", {})
    pilot.require(key == TARGET["attempt"] and journal.get("active_attempt") == key
                  and checkpoint["revision"] == TARGET["revision"] and checkpoint["state"] == pilot.HALTED
                  and checkpoint["manual_review_required"] and checkpoint["review_reason"] == HALT_REASON,
                  "Not the reviewed Colorado first-add-on-leg freshness halt")
    attempt = journal["attempts"][key]
    first, second = attempt["legs"]
    parent = checkpoint["autonomous_positions"][TARGET["parent"]]
    baseline = TARGET["baseline_quantity"]
    pilot.require(attempt["state"] == pilot.HALTED and attempt["halted_from"] == pilot.LEG1_RECONCILING
                  and attempt["halt_reason"] == HALT_REASON and "filled_leg1_recovery" not in attempt
                  and attempt["authorization"]["market_ids"] == TARGET["markets"]
                  and attempt["authorization"]["exchange_ids"] == TARGET["exchanges"]
                  and attempt["addon"]["position_id"] == TARGET["parent"]
                  and attempt["addon"]["baseline_quantity"] == parent["quantity"] == baseline
                  and parent["remaining_quantities"] == [str(baseline)] * 2 and parent["status"] == "OPEN"
                  and list(map(Decimal, attempt["addon"]["baseline_leg_costs"])) == TARGET["baseline_costs"]
                  and auto.position_leg_costs(checkpoint, TARGET["parent"]) == TARGET["baseline_costs"]
                  and parent["total_entry_cost"] == str(sum(TARGET["baseline_costs"]))
                  and attempt["authorization"] == journal["attempts"][TARGET["parent"]]["authorization"],
                  "Completed position/add-on binding differs from the reviewed baseline")
    pilot.require(first["post_attempted"] and first["placement_response"]["http_status"] == 200
                  and first["placement_response"]["credential_redacted"] is False and first["receipt"] is not None
                  and first["intent"]["request"]["idempotencyKey"] == TARGET["key"]
                  and first["intent"]["order_id"] == TARGET["order"]
                  and first["intent"]["state"] == "OBSERVED_TERMINAL"
                  and first["intent"]["observation"]["filled_quantity"] == 1
                  and Decimal(str(first["intent"]["observation"]["filled_cost"])) == TARGET["price"]
                  and first["intent"]["observation"]["open"] is False
                  and first["activity"] is None and first["review"] is None and attempt["after"] == [None, None]
                  and attempt["completion"] is None and not second["post_attempted"]
                  and all(second[k] is None for k in ("intent", "receipt", "placement_response", "activity", "review")),
                  "Unexpected first-leg evidence or second-leg execution; retain halt")
    exposure = checkpoint["live_exposures"][key]
    pilot.require(exposure["execution_status"] == "MANUAL_REVIEW"
                  and list(map(Decimal, exposure["confirmed_quantities"])) == [1, 0]
                  and list(map(Decimal, exposure["confirmed_costs"])) == [TARGET["price"], 0]
                  and list(map(Decimal, exposure["possible_additional_quantities"])) == [0, 0]
                  and Decimal(exposure["capital_charge"]) == TARGET["price"]
                  and Decimal(exposure["accounting_buffer"]) == auto.ACCOUNTING_BUFFER
                  and all(k == key or a["state"] in {pilot.READY, pilot.REJECTED_RETIRED}
                          for k, a in journal["attempts"].items())
                  and all(p["status"] == "OPEN" and p["exit_execution"] is None
                          for p in checkpoint["autonomous_positions"].values()),
                  "Another unfinished execution/sale or unexpected exposure prevents recovery")
    return attempt


def prove_fill(attempt, snapshot, orders, observed):
    """Prove one new fill while protecting every previously held share/event."""
    first, before = attempt["legs"][0], attempt["before"]
    account_reader.validate_positions(before["account"])
    account_reader.validate_positions(snapshot["account"])
    mid, eid = TARGET["markets"][0], TARGET["exchanges"][0]
    price, baseline = TARGET["price"], TARGET["baseline_quantity"]
    pilot.require(observed == first["intent"] and observed["request"]["quantity"] == 1
                  and observed["market_id"] == mid and observed["request"]["exchangeId"] == eid
                  and observed["request"]["action"] == "buy" and observed["request"]["side"] == "no"
                  and Decimal(str(observed["request"]["price"])) == price,
                  "Original filled request/observation changed; never retry it")
    account = snapshot["account"]
    duration = snapshot["freshness"]["completed_monotonic"] - snapshot["freshness"]["started_monotonic"]
    stamp = auto.single.parse_api_timestamp(observed["created_at"])
    pilot.require(snapshot["data_complete"] is True and all(snapshot["coverage"][k]["complete"] is True
                  for k in ("fills", "transactions")) and 0 <= duration <= pilot_account.MAX_ACCOUNT_READ_AGE
                  and auto.single.parse_api_timestamp(snapshot["history_since"]) <= stamp
                  and auto.single.parse_api_timestamp(snapshot["freshness"]["observed_at"]) >= stamp
                  and auto.single.parse_api_timestamp(before["freshness"]["observed_at"]) <= stamp
                  and account["tournament"]["id"] == attempt["authorization"]["tournament_id"]
                  and account["tournament"]["slug"] == attempt["authorization"]["tournament_slug"]
                  and not account["orders"] and not before["account"]["orders"]
                  and account["quarantine_reserve"] == before["account"]["quarantine_reserve"],
                  "Account scope, freshness, orders or quarantine differs")
    pilot.require(len(snapshot["order_activity"]) == 1, "Exactly the known first order must be reconciled")
    activity = snapshot["order_activity"][0]
    auto.single.verify_order_intent(activity["order"], observed)
    pilot_account.check_order_activity(activity, snapshot["recent_fills"], account)
    pilot.require(activity["order"]["id"] == TARGET["order"] and activity["order"]["open"] is False
                  and activity["order"]["quantityFilled"] == 1 and len(activity["fills"]) == 1
                  and activity["fills"][0]["id"] == TARGET["fill"]
                  and activity["fills"][0]["quantity"] == -1 and activity["fills"][0]["side"] == "no"
                  and Decimal(activity["fill_notional"]) == price
                  and probe.ordinary_cash_receipt(first["receipt"]["all"], price),
                  "Known full ordinary BUY fill or receipt accounting differs")
    receipt = first["receipt"]
    pilot.require(receipt["orderId"] == TARGET["order"] and scanner.numeric_id(receipt["exchangeId"]) == eid
                  and receipt["action"] == "buy" and receipt["side"] == "no"
                  and receipt["quantity"] == receipt["quantityTraded"] == 1
                  and receipt["open"] is False and receipt["remainingQuantity"] == 0
                  and all(Decimal(str(receipt[k])) == price for k in ("price", "fillPrice", "totalCost")),
                  "Original receipt differs from the one-contract buy")
    new_fills = history_delta(before["recent_fills"], snapshot["recent_fills"], "id", fill_identity)
    trades = history_delta(before["recent_transactions"], snapshot["recent_transactions"], "event_id", transaction_identity)
    pilot.require(len(new_fills) == len(trades) == 1 and new_fills[0]["id"] == TARGET["fill"]
                  and new_fills[0]["orderId"] == TARGET["order"] and trades[0]["event_id"] == TARGET["transaction"],
                  "Missing, duplicate or additional financial activity; retain halt")
    pilot.require(scanner.numeric_id(new_fills[0]["marketId"]) == mid and new_fills[0]["side"] == "no"
                  and new_fills[0]["quantity"] == -1, "Portfolio fill market/side/quantity differs")
    trade = trades[0]
    pilot.require(trade["event_type"] == "trade" and trade["orderType"] == "BUY"
                  and scanner.numeric_id(trade["marketId"]) == mid and scanner.numeric_id(trade["exchangeId"]) == eid
                  and trade["quantity"] == -1 and Decimal(str(trade["price"])) == price
                  and auto.single.parse_api_timestamp(trade["createdAt"]) ==
                      auto.single.parse_api_timestamp(activity["fills"][0]["filledAt"])
                  and all(trade.get(k) is None for k in ("amount", "transactionType", "collateralDelta",
                                                        "outstandingAdvanceAfter", "componentId", "reason")),
                  "Known transaction introduces contradictory debit/collateral effects")
    recent_orders = [o for o in orders if auto.single.parse_api_timestamp(o["createdAt"]) >= stamp]
    pilot.require(recent_orders == [activity["order"]], "Another order exists after this attempt")
    # Compare all other instruments' costs/lots too, not only their quantities.
    old_other, new_other = copy.deepcopy(before["account"]), copy.deepcopy(account)
    for other in (old_other, new_other):
        other["positions"] = [p for p in other["positions"] if scanner.numeric_id(p["exchangeId"]) != eid]
        # The full portfolio total includes the expected new selected share.
        # Compare remaining instruments by their individual cost/lots instead.
        other["summary"].pop("totalCostBasis", None)
    new_other["tournament"]["myBalance"] = old_other["tournament"]["myBalance"]
    pilot.require(pilot_account.account_execution_state(old_other) == pilot_account.account_execution_state(new_other),
                  "Colorado leg two, Alaska or New Hampshire changed")
    selected = [p for p in account["positions"] if scanner.numeric_id(p["exchangeId"]) == eid]
    pilot.require(len(selected) == 1 and scanner.numeric_id(selected[0]["marketId"]) == mid
                  and selected[0]["settled"] is False
                  and account_reader.no_holding_cost_matches(selected[0], baseline + 1, TARGET["baseline_costs"][0] + price),
                  "Expected exactly the existing three shares plus one known NO fill")
    original = next(p for p in before["account"]["positions"] if scanner.numeric_id(p["exchangeId"]) == eid)
    pilot.require(account_reader.no_holding_cost_matches(original, baseline, TARGET["baseline_costs"][0]),
                  "Saved managed holding baseline changed")
    if original.get("lots") is not None:
        lots = selected[0].get("lots", [])
        pilot.require(len(lots) == len(original["lots"]) + 1 and all(p in lots for p in original["lots"]),
                      "Original held lots changed")
    # The exact order/fill/ledger/incremental holding checks above cover the
    # ordinary one-leg proof. Use UTC chronology for recovery: monotonic clocks
    # in this process cannot be compared with the old bot's saved clock.
    cash = pilot.amount(account["tournament"]["myBalance"])
    debit = pilot.amount(before["account"]["tournament"]["myBalance"]) - cash
    pilot.require(cash == TARGET["cash"] and debit >= 0 and abs(debit - price) <= pilot_account.BALANCE_DELTA_TOLERANCE,
                  "Current cash differs from the reviewed rounded debit")
    return {"fill_notional": str(price), "reported_balance_debit": str(debit),
            "conservative_allocation_charge": str(max(price, debit + pilot_account.BALANCE_DELTA_TOLERANCE)),
            "accounting_policy": auto.POLICY, "formally_verified": False,
            "observations": {"isolated_execution_reconciled": True, "execution_and_inventory_verified": True}}


def validate_recovered(checkpoint, key):
    """The existing one-use resume guard revalidates this archived proof."""
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    record = attempt["filled_leg1_recovery"]
    pilot.require(key == TARGET["attempt"] and set(record) == {"version", "kind", "recovered_at", "prior_checkpoint_hash",
                  "prior_revision", "prior_updated_at", "prior_halt_reason", "original_intent", "original_receipt",
                  "original_leg1_evidence", "observed_intent", "snapshot", "orders", "reads", "review",
                  "submission_status", "retired_idempotency_key", "resume_consumed_at"}
                  and record["version"] == 1 and record["kind"] == KIND and record["prior_revision"] == TARGET["revision"]
                  and record["prior_halt_reason"] == HALT_REASON and len(record["prior_checkpoint_hash"]) == 64
                  and record["submission_status"] == "RETIRED_NEVER_RETRY" and record["retired_idempotency_key"] == TARGET["key"]
                  and record["original_intent"] == record["observed_intent"] == attempt["legs"][0]["intent"]
                  and record["original_receipt"] == attempt["legs"][0]["receipt"]
                  and record["original_leg1_evidence"] == {"activity": None, "review": None, "after": None}
                  and attempt["legs"][0]["activity"] == record["snapshot"]["order_activity"][0]
                  and attempt["legs"][0]["review"] == record["review"] and attempt["after"][0] == record["snapshot"],
                  "Filled add-on recovery/request/key evidence changed")
    parse = auto.single.parse_api_timestamp
    pilot.require(parse(record["recovered_at"]) >= parse(record["prior_updated_at"])
                  and record["reads"] and all(r.get("http_status") == 200 and r.get("credential_redacted") is False
                                            for r in record["reads"])
                  and record["review"] == prove_fill(attempt, record["snapshot"], record["orders"], record["observed_intent"]),
                  "Archived filled-add-on proof is inconsistent")
    if record["resume_consumed_at"] is not None:
        pilot.require(parse(record["resume_consumed_at"]) >= parse(record["recovered_at"]), "Continuation predates recovery")
    pilot.require(not attempt["legs"][1]["post_attempted"] or record["resume_consumed_at"] is not None,
                  "Second leg lacks a consumed explicit continuation permission")


def validate_transition(previous, proposed):
    key = previous["autonomous_execution"]["active_attempt"]
    attempt = eligible_attempt(previous, key)
    record = proposed["autonomous_execution"]["attempts"][key]["filled_leg1_recovery"]
    pilot.require(record["prior_checkpoint_hash"] == pilot.snapshot_hash(previous)
                  and record["prior_revision"] == previous["revision"] and record["prior_updated_at"] == previous["updated_at"]
                  and record["original_intent"] == attempt["legs"][0]["intent"]
                  and record["original_receipt"] == attempt["legs"][0]["receipt"] and record["resume_consumed_at"] is None
                  and record["review"] == prove_fill(attempt, record["snapshot"], record["orders"], record["observed_intent"])
                  and proposed == filled.proposed_checkpoint(previous, key, record), "Recovery contains an unrelated state change")
    model = sum((sum(map(Decimal, e["confirmed_costs"])) for e in proposed["live_exposures"].values()), Decimal(0))
    returned = sum((Decimal(p["exit_proceeds"]) for p in proposed["autonomous_positions"].values()), Decimal(0))
    cash = Decimal(proposed["last_reconciled_account_cash"])
    pilot.require(abs(Decimal(proposed["autonomous_execution"]["reference_cash"]) - cash - model + returned)
                  <= pilot_account.BALANCE_DELTA_TOLERANCE and cash - Decimal(".01") >= Decimal(proposed["untouchable_cash_reserve"]),
                  "Cumulative accounting or reserve differs")
    pilot_account.check_fresh(record["snapshot"]["freshness"]["started_monotonic"])


def recover(session, key, apply=False):
    with readonly_pilot_lock() as path:
        checkpoint = pilot._read_checkpoint_locked(path)
        attempt = eligible_attempt(checkpoint, key)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        reads = probe.EvidenceReads(session)  # No flush/journal writes in this GET-only collector.
        gets = SettlementGets(reads)
        pilot.read_live_pair(gets, attempt["authorization"])
        orders = read_orders(gets, checkpoint["tournament_id"])
        observed, activity = copy.deepcopy(attempt["legs"][0]["intent"]), {}
        observed["state"], observed["observation"] = auto.single.observe_test(gets, observed, activity=activity)
        snapshot = pilot_account.read_snapshot(gets, attempt["authorization"]["tournament_slug"], checkpoint, order_ids=[])
        snapshot["order_activity"] = [activity]
        pilot.require(read_orders(gets, checkpoint["tournament_id"]) == orders, "Order history changed during recovery")
        review = prove_fill(attempt, snapshot, orders, observed)
        record = {"version": 1, "kind": KIND, "recovered_at": datetime.now(timezone.utc).isoformat(),
                  "prior_checkpoint_hash": pilot.snapshot_hash(checkpoint), "prior_revision": checkpoint["revision"],
                  "prior_updated_at": checkpoint["updated_at"], "prior_halt_reason": checkpoint["review_reason"],
                  "original_intent": copy.deepcopy(attempt["legs"][0]["intent"]),
                  "original_receipt": copy.deepcopy(attempt["legs"][0]["receipt"]),
                  "original_leg1_evidence": {"activity": None, "review": None, "after": None},
                  "observed_intent": observed, "snapshot": snapshot, "orders": orders, "reads": reads.reads, "review": review,
                  "submission_status": "RETIRED_NEVER_RETRY", "retired_idempotency_key": TARGET["key"], "resume_consumed_at": None}
        proposed = filled.proposed_checkpoint(checkpoint, key, record)
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
                proposed = pilot._save_checkpoint_locked(proposed, path, filled_recovery=True)
            finally:
                pilot._state_lock_owners.pop(path, None)
        fields = ("state", "revision", "manual_review_required", "confirmed_cumulative_debits", "reserved_unconfirmed_capital",
                  "quarantine_reserve", "calculated_remaining_allocation", "last_reconciled_account_cash")
        after = {f: proposed[f] for f in fields}
        if not apply:
            after["revision"] += 1
        return {"result": "RECOVERED" if apply else "DRY_RUN_PASS", "attempt": key,
                "before": {f: checkpoint[f] for f in fields}, "after": after, "accounting": review,
                "exposure_after": proposed["live_exposures"][key], "managed_quantity_after": proposed["autonomous_positions"][TARGET["parent"]]["quantity"],
                "retired_idempotency_key": TARGET["key"], "leg_two_intent": None, "orders_submitted": 0,
                "state_written": apply, "get_requests": len(reads.reads), "resume_consumed_at": None,
                "continuation_command": ".venv/bin/python autonomous_pilot.py --resume-recovered-leg1 " + key}


if __name__ == "__main__":
    # Reuse the existing safe CLI: explicit --apply, default dry-run, read-only
    # .env loading, redacted errors. Its recovery dispatch recognizes this ID.
    raise SystemExit(filled.main())
