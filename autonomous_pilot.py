"""Autonomous v0.1: scan -> buy -> hold -> sell/settle -> record P&L -> repeat.

The exact intents, keys, receipts and observations share the pilot allocation's
atomic write and exclusive lock. A crash never resumes a submission. Only fake
HTTP is used by the tests. The single autonomous enable switch is off by default.
"""
import copy
import argparse
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from pathlib import Path

import requests
from dotenv import load_dotenv

import account_test as single
import config
import execution_quarantine as quarantine
import live_pilot as pilot
import live_settlement as live
import pilot_account
import price_reader as scanner
import supervised_accounting_probe as probe
from order_preview import PreviewBlocked, ceil_buy_limit, executable_buy_limit, executable_limits, require_preview

MIN_EDGE = Decimal("0.005")
MAX_DETERIORATION = Decimal(".005")
# Entry needs positive edge. After an actual first fill, v0.1 permits
# break-even completion, retaining the same one-tick deterioration limits.
MIN_COMPLETION_EDGE = Decimal("0")
EARLY_EXIT_PROCEEDS = Decimal("1.010")
# A successful observation may differ from notional by .02; the two rounded
# balances can themselves hide .02. Reserve .04 before either possible POST.
# This guards rounding under the assumption; it is NOT a documented fee cap.
ACCOUNTING_BUFFER = 2 * pilot_account.BALANCE_DELTA_TOLERANCE
POLICY = pilot_account.HISTORICAL_POLICY
TRANSITIONS = {
    pilot.LEG1_SUBMITTING: {pilot.LEG1_RECONCILING},
    pilot.LEG1_RECONCILING: {pilot.LEG2_RECHECK},
    pilot.LEG2_RECHECK: {pilot.LEG2_SUBMITTING},
    pilot.LEG2_SUBMITTING: {pilot.FINAL_RECONCILING},
    pilot.FINAL_RECONCILING: {pilot.READY},
}


def require(condition, reason):
    pilot.require(condition, reason)


def missing_trade_scope(evidence):
    """Recognize only the saved, definite permission rejection; no raw printing."""
    try:
        import json
        error = json.loads(evidence["body"])["error"]
        return (evidence["http_status"] == 403 and evidence["credential_redacted"] is False
                and error["code"] == "INSUFFICIENT_SCOPES"
                and "trade" in error["details"]["required"] and "trade" in error["details"]["missing"])
    except (ValueError, TypeError, KeyError):
        return False


def require_trade_scope_response(evidence):
    require(not missing_trade_scope(evidence),
            "HTTP 403 INSUFFICIENT_SCOPES: API key missing required trade scope; "
            "confirm permission in platform API-key settings; no submission retry")


def require_submission(mode=config.LIVE_PILOT):
    """One autonomous switch, off by default; legacy diagnostics stay separate."""
    require(mode == config.LIVE_PILOT and config.AUTONOMOUS_LIVE_PILOT_ENABLED,
            "Autonomous LIVE_PILOT submission is disabled")
    require(MIN_EDGE == Decimal("0.005") and MIN_COMPLETION_EDGE == 0 and config.LIVE_ALLOCATION == 5000
            and (config.MAX_LIVE_CAPITAL_PER_TRADE, config.MAX_LIVE_CAPITAL_PER_RACE,
                 config.MAX_TOTAL_LIVE_EXPOSURE, config.MAX_LIVE_QUANTITY_PER_LEG) == (50, 100, 500, 1),
            "Autonomous hard limits changed; review required")


def autonomous_authorization(ids, tournament_id):
    approval = live.configured_authorization(ids, tournament_id, execution_mode=live.AUTONOMOUS_MODE)
    live.require(approval["execution_mode"] == live.AUTONOMOUS_MODE
                 and approval["verification_route"] in {live.MACHINE, live.MANUAL_AUTONOMOUS},
                 "Explicit machine or distinct manual autonomous settlement permission is required")
    return approval


def observed_autonomous_limits(books, account_started):
    """One fixed 0.5% autonomous policy, after shared freshness/depth/tick checks.

    The legacy preview and paper classification have independent policies.
    Neither chooses the threshold for this autonomous execution coordinator.
    No float epsilon or runtime threshold override is accepted here.
    """
    result = executable_limits("NO-PAIR", books, account_started, quantity=1, minimum_depth=1)
    _, _, cost, edge = result
    require_preview(cost <= Decimal("1.000") and edge > 0, "Autonomous pair must have positive ordinary edge")
    require_preview(edge >= MIN_EDGE, "Tick-rounded limits fall below the 0.5% autonomous edge requirement")
    return result


def fresh_candidate(session, ids, checkpoint):
    """GET-only detection/preflight; never make an intent or infer permission."""
    pilot.require_execution_clear(checkpoint)
    approval = autonomous_authorization(ids, checkpoint["tournament_id"])
    markets, allowed, context = pilot.read_live_pair(session, approval)
    require("NO-PAIR" in allowed, "Settlement does not verify the NO pair")
    snapshot = pilot_account.read_snapshot(session, approval["tournament_slug"], checkpoint, order_ids=[])
    books = [scanner.get_best_prices(session, market, approval["tournament_id"]) for market in markets]
    prices, quantity, cost, edge = observed_autonomous_limits(books, snapshot["freshness"]["started_monotonic"])
    second_cap = min(Decimal(".995"), Decimal(str(prices[1])) + MAX_DETERIORATION)
    reserve = Decimal(str(prices[0])) + second_cap + ACCOUNTING_BUFFER
    assessment = pilot_account.assess_autonomous_snapshot(snapshot, checkpoint, approval["exchange_ids"], reserve, quantity)
    pilot_account.require_ready(assessment)
    require(not snapshot["account"]["orders"], "Open orders prevent isolated autonomous accounting")
    live.revalidate_authorization(approval)
    return {"authorization": approval, "markets": markets, "settlement": context, "before": snapshot,
            "books": books, "prices": prices, "edge": str(edge), "second_cap": str(second_cap),
            "reserve": str(reserve), "risk": assessment["risk"], "accounting_policy": POLICY,
            "authorization_tier": live.authorization_tier(approval),
            "formally_verified": False, "submission_enabled": False}


def active_attempt(checkpoint):
    journal = checkpoint["autonomous_execution"]
    return journal["attempts"][journal["active_attempt"]]


def validate_journal(checkpoint):
    """Validate keys, stage/exposure links and completion evidence on every load."""
    journal = checkpoint["autonomous_execution"]
    require(isinstance(journal, dict) and set(journal) == {
        "version", "policy", "reference_cash", "attempts", "active_attempt"}
        and type(journal["version"]) is int and journal["version"] == 1 and journal["policy"] == POLICY,
        "Invalid autonomous journal/accounting policy")
    pilot.amount(journal["reference_cash"])
    require(isinstance(journal["attempts"], dict), "Invalid autonomous history")
    active = journal["active_attempt"]
    require(active is None or active in journal["attempts"], "Missing active autonomous execution")
    keys = set()
    fields = {"state", "created_at", "authorization", "initial_prices", "initial_edge", "before", "after",
              "legs", "reads", "halt_reason", "halted_from", "completion", "stages"}
    for fingerprint, attempt in journal["attempts"].items():
        require(isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                and isinstance(attempt, dict) and set(attempt) in
                (fields, fields | {"rejection_recovery"}, fields | {"filled_leg1_recovery"}),
                "Invalid autonomous execution record")
        approval = attempt["authorization"]
        live.validate_authorization(approval)
        require(approval["execution_mode"] == live.AUTONOMOUS_MODE
                and approval["verification_route"] in {live.MACHINE, live.MANUAL_AUTONOMOUS},
                "Saved execution lacks explicit autonomous authorization")
        exposure = checkpoint["live_exposures"].get(fingerprint)
        require(exposure is not None and "capital_charge" in exposure
                and exposure["market_ids"] == approval["market_ids"] and exposure["exchange_ids"] == approval["exchange_ids"],
                "Autonomous execution/exposure binding changed")
        prices = attempt["initial_prices"]
        require(isinstance(prices, list) and len(prices) == 2 and all(ceil_buy_limit(p) == p for p in prices),
                "Invalid saved initial limits")
        edge = Decimal("1.000") - sum(map(lambda p: Decimal(str(p)), prices))
        require(edge == Decimal(attempt["initial_edge"]) and edge >= MIN_EDGE
                and list(map(pilot.amount, exposure["limit_prices"])) == [Decimal(str(prices[0])),
                    min(Decimal(".995"), Decimal(str(prices[1])) + MAX_DETERIORATION)],
                "Initial economics or reserved price caps changed")
        require(len(attempt["legs"]) == len(attempt["after"]) == 2 and isinstance(attempt["reads"], list)
                and isinstance(attempt["halt_reason"], str) and isinstance(attempt["stages"], list), "Incomplete execution evidence")
        single.parse_api_timestamp(attempt["created_at"])
        require(attempt["before"]["data_complete"] is True and
                attempt["before"]["account"]["tournament"]["id"] == approval["tournament_id"] == checkpoint["tournament_id"],
                "Saved before-account scope changed")
        for index, leg in enumerate(attempt["legs"]):
            require(isinstance(leg, dict) and set(leg) == {
                "intent", "post_attempted", "placement_response", "receipt", "activity", "review"}
                and type(leg["post_attempted"]) is bool, "Invalid autonomous leg")
            intent = leg["intent"]
            require(intent is not None or not leg["post_attempted"], "Possible POST lacks a saved intent/key")
            if intent is not None:
                single.validate_intent(intent)
                body = intent["request"]
                require(intent["market_id"] == approval["market_ids"][index]
                        and intent["tournament_slug"] == approval["tournament_slug"]
                        and body["exchangeId"] == approval["exchange_ids"][index] and body["side"] == "no"
                        and body["tournamentId"] == approval["tournament_id"]
                        and Decimal(str(body["price"])) <= pilot.amount(exposure["limit_prices"][index]),
                        "Saved autonomous intent exceeds its approved identity/cap")
                require(body["idempotencyKey"] not in keys, "Duplicate durable idempotency key; never submit")
                keys.add(body["idempotencyKey"])
                require(leg["post_attempted"] or intent["state"] == "PREPARED", "Unsubmitted intent has advanced")
                observation = intent["observation"] or {}
                if "filled_quantity" in observation:
                    require(abs(Decimal(str(observation["filled_quantity"])) - pilot.amount(exposure["confirmed_quantities"][index]))
                            <= Decimal(".000000001") and
                            abs(Decimal(str(observation["filled_cost"])) - pilot.amount(exposure["confirmed_costs"][index]))
                            <= Decimal(".000000001"), "Saved order observation and exposure disagree")
            if index == 1 and leg["post_attempted"]:
                first = attempt["legs"][0]
                require(first["review"] is not None and first["review"]["observations"]["isolated_execution_reconciled"] is True
                        and first["intent"]["state"] == "OBSERVED_TERMINAL"
                        and first["intent"]["observation"]["filled_quantity"] == 1,
                        "Leg two was attempted without fully reconciled leg one")
        if fingerprint == active:
            require(attempt["state"] == checkpoint["state"] and checkpoint["state"] in pilot.EXECUTION_STATES | {pilot.HALTED},
                    "Active execution/pilot stages disagree")
        else:
            require(attempt["state"] in {pilot.READY, pilot.REJECTED_RETIRED}, "Unfinished execution was forgotten")
        require(("rejection_recovery" in attempt) == (attempt["state"] == pilot.REJECTED_RETIRED),
                "Recovery evidence requires a retired rejection")
        if attempt["state"] == pilot.REJECTED_RETIRED:
            from recover_rejected_attempt import validate_retired
            validate_retired(checkpoint, fingerprint)
        if "filled_leg1_recovery" in attempt:
            from recover_filled_leg1 import validate_recovered
            validate_recovered(checkpoint, fingerprint)
        if attempt["state"] == pilot.READY:
            require(exposure["execution_status"] == "RECONCILED_PAIR" and pilot.amount(exposure["accounting_buffer"]) == 0
                    and all(leg["post_attempted"] and leg["intent"]["state"] == "OBSERVED_TERMINAL"
                            and leg["intent"]["observation"]["filled_quantity"] == 1
                            and leg["review"]["observations"]["isolated_execution_reconciled"] is True
                            for leg in attempt["legs"]) and all(s is not None for s in attempt["after"]),
                    "READY lacks full two-leg reconciliation")
        if attempt["state"] == pilot.HALTED:
            require(bool(attempt["halt_reason"]) and checkpoint["manual_review_required"], "Halt evidence was cleared")
    require(not any("capital_charge" in value and key not in journal["attempts"]
                    for key, value in checkpoint["live_exposures"].items()), "Autonomous exposure lost its journal")
    exit_active = any(p["status"] == "EXITING" for p in checkpoint.get("autonomous_positions", {}).values())
    require(active is not None or checkpoint["state"] not in pilot.EXECUTION_STATES or exit_active,
            "Execution stage has no active journal")
    if active is None:
        notional = sum((sum(map(pilot.amount, checkpoint["live_exposures"][key]["confirmed_costs"]))
                        for key in journal["attempts"]), Decimal(0))
        returned = sum((pilot.amount(p["exit_proceeds"]) for p in checkpoint.get("autonomous_positions", {}).values()), Decimal(0))
        require(abs(pilot.amount(journal["reference_cash"]) - pilot.amount(checkpoint["last_reconciled_account_cash"]) - notional + returned)
                <= pilot_account.BALANCE_DELTA_TOLERANCE, "Completed autonomous accounting baseline is inconsistent")


def validate_update(previous, proposed, rejected_recovery=False, filled_recovery=False):
    """No deletion, key reuse, changed request or clearing a historical halt."""
    old, new = previous["autonomous_execution"], proposed.get("autonomous_execution")
    require(new is not None and old["policy"] == new["policy"] and old["reference_cash"] == new["reference_cash"],
            "Autonomous accounting history cannot disappear/reset")
    for fingerprint, attempt in old["attempts"].items():
        current = new["attempts"].get(fingerprint)
        require(current is not None and all(current[k] == attempt[k] for k in
                ("created_at", "authorization", "initial_prices", "initial_edge", "before")), "Execution binding/history changed")
        require(attempt["state"] not in {pilot.READY, pilot.REJECTED_RETIRED} or current == attempt,
                "Completed/retired execution evidence changed")
        require(attempt["state"] != pilot.HALTED or current["state"] == pilot.HALTED
                or rejected_recovery and current["state"] == pilot.REJECTED_RETIRED
                or filled_recovery and current["state"] == pilot.LEG2_RECHECK, "Autonomous halt cannot clear")
        old_recovery, new_recovery = attempt.get("filled_leg1_recovery"), current.get("filled_leg1_recovery")
        if old_recovery is not None:
            require(new_recovery is not None, "Filled-leg recovery audit cannot disappear")
            old_record, new_record = copy.deepcopy(old_recovery), copy.deepcopy(new_recovery)
            old_consumed, new_consumed = old_record.pop("resume_consumed_at"), new_record.pop("resume_consumed_at")
            require(old_record == new_record and (old_consumed == new_consumed or
                    old_consumed is None and new_consumed is not None and
                    previous["state"] == proposed["state"] == pilot.LEG2_RECHECK),
                    "Filled-leg recovery evidence or consumed resume permission changed")
        else:
            require(new_recovery is None or filled_recovery, "Filled-leg recovery requires its dedicated verified transition")
        require(current["stages"][:len(attempt["stages"])] == attempt["stages"], "Execution stage history cannot be erased")
        for prior_leg, current_leg in zip(attempt["legs"], current["legs"]):
            require(not prior_leg["post_attempted"] or current_leg["post_attempted"], "A possible POST was forgotten")
            if prior_leg["intent"] is not None:
                require(current_leg["intent"] is not None and prior_leg["intent"]["request"] == current_leg["intent"]["request"],
                        "A persisted idempotency key/request changed")
            if prior_leg["receipt"] is not None:
                require(current_leg["receipt"] == prior_leg["receipt"], "Original placement receipt changed")
    for key, old_position in previous.get("autonomous_positions", {}).items():
        position = proposed.get("autonomous_positions", {}).get(key)
        require(position is not None, "Position/P&L history cannot disappear")
        for field in ("pair", "market_ids", "exchange_ids", "quantity", "entry_timestamp", "actual_entry_prices", "entry_edge"):
            require(position[field] == old_position[field], "Recorded entry changed")
        require(old_position["status"] != "CLOSED" or position == old_position, "Closed P&L history changed")
        require(all(Decimal(q) <= Decimal(old_q) for q, old_q in zip(position["remaining_quantities"], old_position["remaining_quantities"]))
                and Decimal(position["allocation_credit"]) >= Decimal(old_position["allocation_credit"]),
                "Position quantities/credits regressed")
        old_exit = old_position["exit_execution"]
        if old_exit:
            require(position["exit_execution"] is not None, "Exit intent cannot disappear")
            for old_leg, leg in zip(old_exit["legs"], position["exit_execution"]["legs"]):
                require(old_leg["request"] is None or leg["request"] == old_leg["request"], "Exit idempotency key/request changed")
                require(not old_leg["post_attempted"] or leg["post_attempted"], "Possible sale was forgotten")


def persist(state):
    state["checkpoint"] = pilot.save_checkpoint(state["checkpoint"])


def stage(state, next_state):
    cp = state["checkpoint"]
    require(next_state in TRANSITIONS.get(cp["state"], set()), "Invalid autonomous transition")
    cp["state"] = next_state
    attempt = active_attempt(cp)
    attempt["state"] = next_state
    attempt["stages"].append({"state": next_state, "at": datetime.now(timezone.utc).isoformat()})


def halt(state, reason):
    # Reload the last successful write: an interrupted disk write must not
    # promote in-memory fills or clear a persisted possible POST.
    saved = pilot._read_checkpoint_locked(pilot.ALLOCATION_PATH.resolve())
    state["checkpoint"] = pilot.halted_result(saved, reason)["checkpoint"]
    persist(state)
    return {"state": pilot.HALTED, "reason": state["checkpoint"]["review_reason"], "submission_enabled": False}


class EvidenceReads(probe.EvidenceReads):
    """Reuse secret-redacted GET collection, flushing each response to pilot state."""
    def __init__(self, session, state):
        super().__init__(session)
        self.state = state

    def get(self, url, **kwargs):
        try:
            return super().get(url, **kwargs)
        finally:
            if self.state.get("checkpoint", {}).get("autonomous_execution", {}).get("active_attempt") is not None:
                active_attempt(self.state["checkpoint"])["reads"] = copy.deepcopy(self.reads)
                persist(self.state)


def new_intent(checkpoint, approval, index, price):
    key = "account-test-" + uuid4().hex  # The existing strict one-share schema.
    keys = {leg["intent"]["request"]["idempotencyKey"] for attempt in checkpoint["autonomous_execution"]["attempts"].values()
            for leg in attempt["legs"] if leg["intent"] is not None}
    keys |= exit_keys(checkpoint)
    frozen = quarantine.load_quarantine()
    if frozen:
        _, legs = pilot.paired.load_pair(quarantine.SOURCE_DIR)
        keys |= {leg["request"]["idempotencyKey"] for leg in legs}
    require(key not in keys, "Duplicate idempotency key; no retry or replacement key")
    now = datetime.now(timezone.utc)
    body = {"idempotencyKey": key, "exchangeId": approval["exchange_ids"][index], "side": "no", "action": "buy",
            "quantity": 1, "price": price, "tournamentId": approval["tournament_id"],
            "expirationDate": (now + timedelta(seconds=30)).isoformat(timespec="milliseconds")}
    intent = {"version": 1, "market_id": approval["market_ids"][index], "tournament_slug": approval["tournament_slug"],
              "created_at": now.isoformat(), "request": body, "state": "PREPARED", "order_id": None, "observation": None}
    intent["approval"] = single.approval_hash(intent)
    single.validate_intent(intent)
    return intent


def begin(state, candidate, reads):
    cp, approval = state["checkpoint"], candidate["authorization"]
    journal = cp.setdefault("autonomous_execution", {"version": 1, "policy": POLICY,
        "reference_cash": cp["last_reconciled_account_cash"], "attempts": {}, "active_attempt": None})
    require(journal["active_attempt"] is None, "Prior autonomous execution requires review")
    fingerprint = hashlib.sha256(uuid4().hex.encode()).hexdigest()
    require(fingerprint not in journal["attempts"], "Duplicate execution identity")
    legs = [{"intent": None, "post_attempted": False, "placement_response": None,
             "receipt": None, "activity": None, "review": None} for _ in range(2)]
    journal["active_attempt"] = fingerprint
    journal["attempts"][fingerprint] = {"state": pilot.LEG1_SUBMITTING, "created_at": datetime.now(timezone.utc).isoformat(),
        "authorization": copy.deepcopy(approval), "initial_prices": candidate["prices"], "initial_edge": candidate["edge"],
        "before": copy.deepcopy(candidate["before"]), "after": [None, None], "legs": legs, "reads": copy.deepcopy(reads),
        "halt_reason": "", "halted_from": None, "completion": None,
        "stages": [{"state": pilot.READY, "at": datetime.now(timezone.utc).isoformat()},
                   {"state": pilot.LEG1_SUBMITTING, "at": datetime.now(timezone.utc).isoformat()}]}
    cp["state"] = pilot.LEG1_SUBMITTING
    cp["live_exposures"][fingerprint] = {"market_ids": approval["market_ids"], "exchange_ids": approval["exchange_ids"],
        "position_type": "NO-PAIR", "confirmed_quantities": ["0", "0"], "confirmed_costs": ["0", "0"],
        "possible_additional_quantities": ["1", "1"], "limit_prices": [str(candidate["prices"][0]), candidate["second_cap"]],
        "execution_status": pilot.EXECUTING, "capital_charge": "0", "accounting_buffer": str(ACCOUNTING_BUFFER)}
    cp["accounted_pair_costs"][fingerprint] = "0"
    legs[0]["intent"] = new_intent(cp, approval, 0, candidate["prices"][0])
    pilot.refresh_totals(cp)
    persist(state)  # Complete before evidence, both reserves and leg-one key.


def submit_once(session, reads, state, index, quote_check):
    """One write-ahead marker, ONE POST, raw receipt persisted before parsing."""
    require_submission()
    cp = state["checkpoint"]
    require(cp["state"] == (pilot.LEG1_SUBMITTING if index == 0 else pilot.LEG2_SUBMITTING), "Wrong submission stage")
    leg = active_attempt(cp)["legs"][index]
    require(not leg["post_attempted"] and leg["intent"]["state"] == "PREPARED", "No submission retries are permitted")
    live.revalidate_authorization(active_attempt(cp)["authorization"])
    quarantine.require_unblocked_markets(active_attempt(cp)["authorization"]["market_ids"])
    quarantine.require_unblocked_exchanges(active_attempt(cp)["authorization"]["exchange_ids"])
    require(single.parse_api_timestamp(leg["intent"]["request"]["expirationDate"]) > datetime.now(timezone.utc), "Saved order expired")
    leg["post_attempted"], leg["intent"]["state"] = True, "SUBMITTING"
    persist(state)  # If fsync fails, there is NO POST.
    require_submission()
    live.revalidate_authorization(active_attempt(state["checkpoint"])["authorization"])
    pilot.require_external_execution_clear()
    pilot.require_initial_evidence_clear(pilot.ALLOCATION_PATH.resolve())
    quarantine.require_unblocked_markets(active_attempt(state["checkpoint"])["authorization"]["market_ids"])
    quarantine.require_unblocked_exchanges(active_attempt(state["checkpoint"])["authorization"]["exchange_ids"])
    quote_check()  # Recheck AFTER the final fsync/authorization checks.
    leg = active_attempt(state["checkpoint"])["legs"][index]
    response = session.post(scanner.API_BASE_URL + "/orders", json=copy.deepcopy(leg["intent"]["request"]),
                            timeout=scanner.REQUEST_TIMEOUT, allow_redirects=False)
    leg["placement_response"] = reads.response_evidence(response)
    persist(state)
    require(not leg["placement_response"]["credential_redacted"], "Unexpected credential reflection; review preserved evidence")
    require_trade_scope_response(leg["placement_response"])
    response.raise_for_status()
    require(response.status_code == 200, "Ambiguous order response; never retry")
    receipt = response.json()
    require(isinstance(receipt, dict), "Missing order receipt; never infer an order ID")
    leg = active_attempt(state["checkpoint"])["legs"][index]
    leg["receipt"] = copy.deepcopy(receipt)
    persist(state)
    body = leg["intent"]["request"]
    require(type(receipt.get("orderId")) is int and receipt["orderId"] > 0 and receipt.get("exchangeId") == body["exchangeId"]
            and receipt.get("side") == "no" and receipt.get("action") == "buy"
            and type(receipt.get("quantity")) is int and receipt["quantity"] == 1
            and probe.money(receipt["price"]) == probe.money(body["price"]), "Receipt conflicts with saved intent")
    leg = active_attempt(state["checkpoint"])["legs"][index]
    leg["intent"].update(state="ACCEPTED", order_id=receipt["orderId"], observation={
        "placement_quantity": receipt["quantityTraded"], "placement_cost": receipt["totalCost"]})
    single.validate_intent(leg["intent"])
    stage(state, pilot.LEG1_RECONCILING if index == 0 else pilot.FINAL_RECONCILING)
    persist(state)


def record_fills(state, index):
    cp = state["checkpoint"]
    fingerprint = cp["autonomous_execution"]["active_attempt"]
    exposure, leg = cp["live_exposures"][fingerprint], active_attempt(cp)["legs"][index]
    observed = leg["intent"]["observation"]
    q, cost = Decimal(str(observed["filled_quantity"])), Decimal(str(observed["filled_cost"]))
    exposure["confirmed_quantities"][index], exposure["confirmed_costs"][index] = str(q), str(cost)
    exposure["possible_additional_quantities"][index] = str(Decimal(0) if observed["open"] is False else 1 - q)
    update_charge(state, sum(map(pilot.amount, exposure["confirmed_costs"])))
    persist(state)  # Known partial/full inventory survives later GET failures.


def update_charge(state, charge):
    cp = state["checkpoint"]
    fingerprint = cp["autonomous_execution"]["active_attempt"]
    exposure = cp["live_exposures"][fingerprint]
    previous = pilot.amount(cp["accounted_pair_costs"][fingerprint])
    charge = max(previous, charge)
    exposure["capital_charge"] = cp["accounted_pair_costs"][fingerprint] = str(charge)
    recalculate_budget(cp)
    pilot.refresh_totals(cp)


def wait_for_account_cache():
    """The official tournament balance may be cached for two seconds.

    Wait once after a known receipt before reading post-trade account state.
    This never repeats a POST, and unexplained state still halts reconciliation.
    """
    time.sleep(2.1)


def reconcile_leg(reads, state, index):
    wait_for_account_cache()
    cp = state["checkpoint"]
    leg = active_attempt(cp)["legs"][index]
    leg["intent"]["state"], leg["intent"]["observation"] = single.observe_test(reads, copy.deepcopy(leg["intent"]))
    # GET callbacks replace the checkpoint object. Apply the observation to the
    # newest object rather than overwriting it with an older captured reference.
    observation = copy.deepcopy(leg["intent"])
    active_attempt(state["checkpoint"])["legs"][index]["intent"] = observation
    record_fills(state, index)
    cp = state["checkpoint"]
    attempt, leg = active_attempt(cp), active_attempt(cp)["legs"][index]
    require(leg["intent"]["state"] == "OBSERVED_TERMINAL" and leg["intent"]["observation"]["filled_quantity"] == 1
            and leg["receipt"]["open"] is False and leg["receipt"]["remainingQuantity"] == 0
            and leg["receipt"]["quantityTraded"] == 1, "Partial, resting, rejected or unknown leg requires manual review")
    after = pilot_account.read_snapshot(reads, attempt["authorization"]["tournament_slug"], cp,
                                        order_ids=[leg["intent"]["order_id"]])
    attempt = active_attempt(state["checkpoint"])
    attempt["after"][index] = copy.deepcopy(after)
    leg = attempt["legs"][index]
    activity = next((item for item in after["order_activity"] if item["order"]["id"] == leg["intent"]["order_id"]), None)
    before = attempt["before"] if index == 0 else attempt["after"][0]
    leg["activity"] = activity
    leg["review"] = probe.review_observation(before, after, leg["receipt"], activity,
                                            leg["intent"]["market_id"], leg["intent"]["request"]["exchangeId"])
    # Reuse the execution/inventory/ledger checks, but not the diagnostic
    # probe's demand to PROVE an upper debit <=1 from rounded balances. A .990
    # first fill is a valid autonomous entry; its rounding buffer and the live
    # allocation/50-per-trade caps were already reserved. No other issue is waived.
    diagnostic_issues = {"ONE_SUSQIE_DEBIT_CAP_NOT_PROVEN",
                         "Rounded balances and ledger proximity do not prove receipt-linked fee-inclusive debit"}
    observations = leg["review"]["observations"]
    if set(leg["review"]["issues"]) == diagnostic_issues and observations["execution_and_inventory_verified"] \
            and observations["notional_consistent_with_rounded_balance"] and Decimal(observations["reported_balance_debit"]) >= 0:
        observations["isolated_execution_reconciled"] = True
        leg["review"]["autonomous_policy_note"] = "Diagnostic probe debit proof excluded; reserved live allocation/caps apply."
    persist(state)
    require(leg["review"]["observations"]["isolated_execution_reconciled"] is True,
            "ACCOUNTING_MODEL_MISMATCH or incomplete isolated reconciliation; manual review required")
    cp = state["checkpoint"]
    current_cash = probe.money(after["account"]["tournament"]["myBalance"])
    journal = cp["autonomous_execution"]
    model_total = sum((sum(map(pilot.amount, cp["live_exposures"][key]["confirmed_costs"])) for key in journal["attempts"]), Decimal(0))
    model_total -= sum((pilot.amount(p["exit_proceeds"]) for p in cp.get("autonomous_positions", {}).values()), Decimal(0))
    require(abs((pilot.amount(journal["reference_cash"]) - current_cash) - model_total)
            <= pilot_account.BALANCE_DELTA_TOLERANCE,
            "ACCOUNTING_MODEL_MISMATCH: cumulative account balance differs from historical model")
    attempt = active_attempt(cp)
    notional = sum(map(pilot.amount, cp["live_exposures"][journal["active_attempt"]]["confirmed_costs"]))
    debit = probe.money(attempt["before"]["account"]["tournament"]["myBalance"]) - current_cash
    require(abs(debit - notional) <= pilot_account.BALANCE_DELTA_TOLERANCE and debit >= 0,
            "ACCOUNTING_MODEL_MISMATCH: pair balance differs from fill notional")
    # Charge the upper rounded balance debit, never replenish on price gains,
    # and retain both precise fill notional and observed balance as evidence.
    update_charge(state, max(notional, debit + pilot_account.BALANCE_DELTA_TOLERANCE))
    cp["last_reconciled_account_cash"] = str(current_cash)
    require(current_cash - Decimal(".01") >= pilot.amount(cp["untouchable_cash_reserve"]), "Untouchable reserve floor endangered")
    persist(state)
    return after


def completion_limits(attempt, book, account_started):
    """Complete at non-negative edge, losing at most one tick of initial edge."""
    # Leg one is an actual fill, so validate just the remaining authoritative
    # book. Do not substitute a hypothetical price for the filled first leg.
    pilot_account.check_fresh(account_started)
    price = Decimal(str(executable_buy_limit("NO-PAIR", book, minimum_depth=1)))
    actual_first = Decimal(str(attempt["legs"][0]["intent"]["observation"]["filled_cost"]))
    edge = Decimal("1.000") - actual_first - price
    required = max(MIN_COMPLETION_EDGE, Decimal(attempt["initial_edge"]) - MAX_DETERIORATION)
    require(price <= Decimal(str(attempt["initial_prices"][1])) + MAX_DETERIORATION
            and edge >= required, "Leg two quote deteriorated beyond the one-tick / non-negative completion rule")
    return float(price), {"actual_leg1_notional": str(actual_first), "leg2_limit": str(price),
                          "pair_notional": str(actual_first + price), "edge": str(edge), "minimum_edge": str(required),
                          "book": copy.deepcopy(book)}


def prepare_second_leg(reads, state):
    cp = state["checkpoint"]
    approval = active_attempt(cp)["authorization"]
    markets, allowed, _ = pilot.read_live_pair(reads, approval)
    require("NO-PAIR" in allowed, "Settlement became invalid before leg two")
    cp = state["checkpoint"]
    # A fresh full account read must match reconciled leg-one inventory/history.
    after = pilot_account.read_snapshot(reads, approval["tournament_slug"], cp, order_ids=[])
    attempt = active_attempt(state["checkpoint"])
    previous = attempt["after"][0]
    require(probe.money(after["account"]["tournament"]["myBalance"]) == probe.money(previous["account"]["tournament"]["myBalance"])
            and probe.inventory(after["account"], "") == probe.inventory(previous["account"], "")
            and not after["account"]["orders"] and after["recent_fills"] == previous["recent_fills"]
            and after["recent_transactions"] == previous["recent_transactions"], "Account changed before leg two")
    book = scanner.get_best_prices(reads, markets[1], approval["tournament_id"])
    cp = state["checkpoint"]
    attempt = active_attempt(cp)
    price, completion = completion_limits(attempt, book, after["freshness"]["started_monotonic"])
    fingerprint = cp["autonomous_execution"]["active_attempt"]
    risk_checkpoint = copy.deepcopy(cp)
    # Replace our unsubmitted leg-two reservation with the proposed debit for
    # the risk calculation only; do not double-count or alter durable reserves.
    risk_checkpoint["live_exposures"][fingerprint]["possible_additional_quantities"][1] = "0"
    risk_checkpoint["live_exposures"][fingerprint]["accounting_buffer"] = "0"
    pilot.refresh_totals(risk_checkpoint)
    assessment = pilot_account.assess_autonomous_snapshot(after, risk_checkpoint, [approval["exchange_ids"][1]],
                                                         Decimal(str(price)) + ACCOUNTING_BUFFER, 1)
    pilot_account.require_ready(assessment)
    live.revalidate_authorization(approval)
    attempt["completion"] = completion
    attempt["legs"][1]["intent"] = new_intent(cp, approval, 1, price)
    stage(state, pilot.LEG2_SUBMITTING)
    persist(state)
    completion_limits(active_attempt(state["checkpoint"]), book, after["freshness"]["started_monotonic"])
    return book, after["freshness"]["started_monotonic"]


def finish_second_leg(session, state, reads):
    """Shared continuation, reachable only after a reconciled first leg."""
    book, account_started = prepare_second_leg(reads, state)
    submit_once(session, reads, state, 1, lambda: completion_limits(active_attempt(state["checkpoint"]), book, account_started))
    after = reconcile_leg(reads, state, 1)
    single.observe_test(reads, copy.deepcopy(active_attempt(state["checkpoint"])["legs"][0]["intent"]))
    pilot_account.check_fresh(after["freshness"]["started_monotonic"])
    cp = state["checkpoint"]
    fingerprint = cp["autonomous_execution"]["active_attempt"]
    cp["live_exposures"][fingerprint].update(execution_status="RECONCILED_PAIR", accounting_buffer="0")
    stage(state, pilot.READY)
    cp["autonomous_execution"]["active_attempt"] = None
    record_position(cp, fingerprint, after)
    pilot.refresh_totals(cp)
    persist(state)
    return {"state": pilot.READY, "execution_id": fingerprint, "accounting_policy": POLICY,
            "formally_verified": False, "capital_charge": cp["accounted_pair_costs"][fingerprint]}


def _execute_locked(session, ids, checkpoint):
    state = {"checkpoint": checkpoint}
    reads = EvidenceReads(session, state)
    try:
        fresh_candidate(reads, ids, checkpoint)  # Detect, then repeat ALL gates.
        candidate = fresh_candidate(reads, ids, state["checkpoint"])
        state["checkpoint"]["state"] = pilot.READY
        persist(state)  # Activation/readiness does not create an intent/key.
        begin(state, candidate, reads.reads)
        observed_autonomous_limits(candidate["books"], candidate["before"]["freshness"]["started_monotonic"])
        submit_once(session, reads, state, 0, lambda: observed_autonomous_limits(
            candidate["books"], candidate["before"]["freshness"]["started_monotonic"]))
        reconcile_leg(reads, state, 0)
        stage(state, pilot.LEG2_RECHECK)
        persist(state)
        return finish_second_leg(session, state, reads)
    except (OSError, *scanner.API_ERRORS) as error:
        # Fixed local policy reasons are useful; raw HTTP errors/bodies/headers
        # must not be printed or copied into a halt reason.
        safe = (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked, live.LiveSettlementBlocked,
                PreviewBlocked, single.TestError, probe.ProbeBlocked)
        reason = str(error) if isinstance(error, safe) else "API or storage unavailable/ambiguous; no submission retry"
        return halt(state, reason)
    except BaseException:
        # Ctrl+C and injected crashes preserve the halt if storage works. A
        # failed halt write leaves the last in-flight stage for restart to halt.
        halt(state, "Autonomous execution interrupted; no automatic retry or restart submission")
        raise


def exit_keys(checkpoint):
    """Sale keys share the entry key namespace; none can be reused."""
    return {leg["request"]["idempotencyKey"] for p in checkpoint.get("autonomous_positions", {}).values()
            if p["exit_execution"] for leg in p["exit_execution"]["legs"] if leg["request"]}


def recalculate_budget(checkpoint):
    # Recycle confirmed principal, not profits or unrelated account deposits.
    # Gross entry debits remain in the history forever, including after closes.
    initial = min(pilot.amount(checkpoint["initial_account_cash"]), Decimal(config.LIVE_ALLOCATION))
    debits = sum(map(pilot.amount, checkpoint["accounted_pair_costs"].values()), Decimal(0))
    credits = sum((pilot.amount(p["allocation_credit"]) for p in checkpoint.get("autonomous_positions", {}).values()), Decimal(0))
    checkpoint["allocated_cash_remaining"] = str(min(initial, initial - debits + credits))
    pilot.refresh_totals(checkpoint)


def record_position(checkpoint, key, snapshot):
    """Entry receipts remain immutable; this separate record tracks the holding."""
    attempt = checkpoint["autonomous_execution"]["attempts"][key]
    prices = checkpoint["live_exposures"][key]["confirmed_costs"]  # Exactly one share each.
    checkpoint.setdefault("autonomous_positions", {})[key] = {
        "pair": attempt["authorization"]["pair_name"],
        "market_ids": attempt["authorization"]["market_ids"], "exchange_ids": attempt["authorization"]["exchange_ids"],
        "quantity": 1, "entry_timestamp": attempt["created_at"], "actual_entry_prices": prices[:],
        "entry_edge": str(Decimal("1.000") - sum(map(Decimal, prices))),
        "status": "OPEN", "remaining_quantities": ["1", "1"], "leg_proceeds": ["0", "0"],
        "exit_timestamp": None, "exit_reason": None, "exit_proceeds": "0", "realized_pnl": None,
        "allocation_credit": "0", "settled_legs": [False, False], "settlement_event_ids": [],
        "exit_execution": None, "last_snapshot": copy.deepcopy(snapshot),
        "notes": [f"Quoted entry edge {attempt['initial_edge']}; actual completed edge "
                  f"{Decimal('1.000') - sum(map(Decimal, prices))}."]}


def validate_positions(checkpoint):
    """Basic restart checks: binding, quantities, cash credits, P&L and keys."""
    positions = checkpoint["autonomous_positions"]
    require(isinstance(positions, dict), "Invalid persistent autonomous positions")
    keys = {leg["intent"]["request"]["idempotencyKey"] for a in checkpoint["autonomous_execution"]["attempts"].values()
            for leg in a["legs"] if leg["intent"]}
    executing = 0
    for key, p in positions.items():
        attempt = checkpoint["autonomous_execution"]["attempts"].get(key)
        require(attempt is not None and attempt["state"] == pilot.READY
                and p["market_ids"] == attempt["authorization"]["market_ids"]
                and p["exchange_ids"] == attempt["authorization"]["exchange_ids"] and p["quantity"] == 1,
                "Position differs from its completed entry")
        require(p["status"] in {"OPEN", "EXITING", "CLOSED"}
                and len(p["remaining_quantities"]) == len(p["leg_proceeds"]) == len(p["settled_legs"]) == 2
                and all(0 <= pilot.amount(q) <= 1 for q in p["remaining_quantities"])
                and all(type(v) is bool for v in p["settled_legs"]), "Invalid held quantities/state")
        proceeds = sum(map(pilot.amount, p["leg_proceeds"]))
        require(pilot.amount(p["exit_proceeds"]) == proceeds and pilot.amount(p["allocation_credit"]) <=
                min(proceeds, pilot.amount(checkpoint["accounted_pair_costs"][key])), "Invalid recycled principal")
        prices = checkpoint["live_exposures"][key]["confirmed_costs"]
        require(p["actual_entry_prices"] == prices and Decimal(p["entry_edge"]) == Decimal("1.000") - sum(map(Decimal, prices)),
                "Position entry economics changed")
        single.parse_api_timestamp(p["entry_timestamp"])
        if p["status"] == "CLOSED":
            require(all(Decimal(q) == 0 for q in p["remaining_quantities"]) and
                    p["exit_reason"] in {"EARLY_EXIT", "SETTLEMENT"} and
                    Decimal(p["realized_pnl"]) == proceeds - sum(map(Decimal, prices)), "Invalid closed P&L")
            single.parse_api_timestamp(p["exit_timestamp"])
            require((p["exit_reason"] == "SETTLEMENT" and all(p["settled_legs"])) or
                    (p["exit_reason"] == "EARLY_EXIT" and p["exit_execution"] is not None and
                     all(Decimal(leg["filled_quantity"]) == 1 for leg in p["exit_execution"]["legs"])),
                    "Close lacks settlement or full-sale evidence")
        if p["exit_execution"]:
            for leg in p["exit_execution"]["legs"]:
                body = leg["request"]
                require(not leg["post_attempted"] or body is not None, "Possible sale lacks saved intent")
                if body:
                    require(body["idempotencyKey"] not in keys and body["action"] == "sell" and body["side"] == "no"
                            and body["quantity"] == 1 and body["tournamentId"] == checkpoint["tournament_id"]
                            and Decimal(".005") <= Decimal(str(body["price"])) <= Decimal(".995")
                            and Decimal(str(body["price"])) % Decimal(".005") == 0, "Invalid/duplicate saved sale key")
                    keys.add(body["idempotencyKey"])
        executing += p["status"] == "EXITING"
    require(executing <= 1 and (not executing or checkpoint["state"] in {pilot.EXECUTING, pilot.HALTED}),
            "Overlapping or forgotten sale execution")


def scoped_markets(session, approval):
    """Read identity/status even after closure; an expected closure is not a halt."""
    markets = []
    for mid, eid in zip(approval["market_ids"], approval["exchange_ids"]):
        m = scanner.fetch_json(session, scanner.API_BASE_URL + f"/markets/{mid}",
                               params={"tournamentId": approval["tournament_id"]})
        scanner.check_context(m["contexts"], approval["tournament_id"])
        require(scanner.numeric_id(m["id"]) == mid and scanner.get_exchange_id(m) == eid
                and m["status"] in {"open", "closed", "settled"}, "Held market identity/mapping changed")
        markets.append(m)
    return markets


def close_position(p, reason):
    p.update(status="CLOSED", exit_reason=reason, exit_timestamp=datetime.now(timezone.utc).isoformat(),
             realized_pnl=str(Decimal(p["exit_proceeds"]) - sum(map(Decimal, p["actual_entry_prices"]))))


def credit_position(checkpoint, key, index, proceeds, observed_credit):
    p = checkpoint["autonomous_positions"][key]
    p["leg_proceeds"][index] = str(Decimal(p["leg_proceeds"][index]) + proceeds)
    p["exit_proceeds"] = str(sum(map(Decimal, p["leg_proceeds"])))
    # Rounded balances can overstate the cash credit by .02. Retain that
    # allowance until enough cash has actually returned. Never recycle profits.
    credit = max(Decimal(0), min(proceeds, observed_credit - pilot_account.BALANCE_DELTA_TOLERANCE))
    p["allocation_credit"] = str(min(pilot.amount(checkpoint["accounted_pair_costs"][key]),
                                     Decimal(p["allocation_credit"]) + credit))
    recalculate_budget(checkpoint)


def check_cash_model(checkpoint, snapshot):
    cash = probe.money(snapshot["account"]["tournament"]["myBalance"])
    journal = checkpoint.get("autonomous_execution")
    if journal:
        buys = sum((sum(map(pilot.amount, checkpoint["live_exposures"][key]["confirmed_costs"]))
                    for key in journal["attempts"]), Decimal(0))
        returns = sum((Decimal(p["exit_proceeds"]) for p in checkpoint.get("autonomous_positions", {}).values()), Decimal(0))
        require(abs(pilot.amount(journal["reference_cash"]) - buys + returns - cash)
                <= pilot_account.BALANCE_DELTA_TOLERANCE, "Unexplained account cash change; manual review required")
    else:
        require(cash == pilot.amount(checkpoint["last_reconciled_account_cash"]), "Unexplained baseline cash change")
    require(cash >= pilot.amount(checkpoint["untouchable_cash_reserve"]), "Untouchable reserve endangered")
    checkpoint["last_reconciled_account_cash"] = str(cash)


def held_quantity(account, exchange_id):
    row = next((r for r in account["positions"] if scanner.numeric_id(r["exchangeId"]) == exchange_id), None)
    return Decimal(0) if row is None or row["settled"] else -Decimal(str(row["quantity"]))


def process_settlements(session, state, snapshot):
    """Official settlement amount is cash; closure alone never means payout.

    Process all held pairs against one snapshot before saving: several markets
    can settle together, and their credits must explain the combined cash delta.
    """
    cp = state["checkpoint"]
    reports = []
    for key, p in cp.get("autonomous_positions", {}).items():
        if p["status"] != "OPEN":
            continue
        approval = cp["autonomous_execution"]["attempts"][key]["authorization"]
        old_ids = {r["event_id"] for r in p["last_snapshot"]["recent_transactions"]} | set(p["settlement_event_ids"])
        new_settlements = [r for r in snapshot["recent_transactions"] if r["event_type"] == "settlement"
                           and r["event_id"] not in old_ids and r["exchangeId"] in p["exchange_ids"]]
        # HOLD needs no separate settlement-market reread. Fetch official
        # lifecycle status when there is an actual payout to reconcile; quotes
        # below already recheck every open pair's identity/status once per cycle.
        markets = scoped_markets(session, approval) if new_settlements else None
        for i, (mid, eid) in enumerate(zip(p["market_ids"], p["exchange_ids"])):
            events = [r for r in new_settlements if r["exchangeId"] == eid and r["marketId"] == mid]
            if events:
                require(not p["settled_legs"][i] and len(events) == 1 and markets[i]["status"] == "settled"
                        and held_quantity(snapshot["account"], eid) == 0,
                        "Ambiguous settlement/position change; manual review required")
                event = events[0]
                require(not (event.get("transactionType") or "").startswith("ALL_"), "Collateral is not a settlement cash payout")
                payout = probe.money(event["amount"])
                require(0 <= payout <= 1, "Unexpected one-contract settlement payout")
                p["remaining_quantities"][i], p["settled_legs"][i] = "0", True
                p["settlement_event_ids"].append(event["event_id"])
                p["notes"].append(f"Settlement {event['event_id']} market {mid}: credited {payout} SUSQies.")
                credit_position(cp, key, i, payout, payout)
            require(held_quantity(snapshot["account"], eid) == Decimal(p["remaining_quantities"][i]),
                    "Actual holding differs from recorded position without a reconciled settlement")
            row = next((r for r in snapshot["account"]["positions"] if r["exchangeId"] == eid), None)
            require(row is None or row["marketId"] == mid, "Holding market/exchange mapping changed")
        p["last_snapshot"] = copy.deepcopy(snapshot)
        if all(p["settled_legs"]):
            close_position(p, "SETTLEMENT")
            reports.append({"pair": p["pair"], "action": "SETTLEMENT", "realized_pnl": p["realized_pnl"]})
    check_cash_model(cp, snapshot)
    recalculate_budget(cp)
    persist(state)
    return reports


def exit_quote(session, approval, started, markets=None):
    """NO executable bid is 1 - YES ask. Round DOWN to an executable sell tick."""
    markets = scoped_markets(session, approval) if markets is None else markets
    require(all(m["status"] == "open" for m in markets), "Held market is closed; await settlement")
    books = [scanner.get_best_prices(session, m, approval["tournament_id"]) for m in markets]
    pilot_account.check_fresh(started)
    # Shared validator checks age, version and the YES ask's executable depth.
    prices = [Decimal("1.000") - Decimal(str(executable_buy_limit("YES-PAIR", b, minimum_depth=1))) for b in books]
    require(all(Decimal(".005") <= p <= Decimal(".995") for p in prices), "No executable sell limit")
    return prices, books


def execute_exit_locked(session, state, key, before):
    """Sell held shares sequentially; uncertainty stops here with durable evidence."""
    cp = state["checkpoint"]
    p = cp["autonomous_positions"][key]
    approval = cp["autonomous_execution"]["attempts"][key]["authorization"]
    initial, _ = exit_quote(session, approval, before["freshness"]["started_monotonic"])
    if sum(initial) < EARLY_EXIT_PROCEEDS:
        return {"pair": p["pair"], "action": "HOLD", "exit_value": str(sum(initial))}
    p["status"], cp["state"] = "EXITING", pilot.EXECUTING
    p["exit_execution"] = {"before": copy.deepcopy(before), "legs": [
        {"request": None, "post_attempted": False, "response": None, "receipt": None, "after": None,
         "filled_quantity": "0", "proceeds": "0"} for _ in range(2)]}
    persist(state)
    current = before
    reads = probe.EvidenceReads(session)
    for i, eid in enumerate(approval["exchange_ids"]):
        require_submission()
        live.revalidate_authorization(approval)
        quarantine.require_unblocked_markets(approval["market_ids"])
        quarantine.require_unblocked_exchanges(approval["exchange_ids"])
        # SELL beyond an owned NO share can be canonicalized to a new YES BUY.
        # Never permit that: exactly one owned, unsettled NO share backs each leg.
        require(not current["account"]["orders"] and held_quantity(current["account"], eid) == 1,
                "Sale is not fully backed or an open order overlaps it")
        prices, _ = exit_quote(session, approval, current["freshness"]["started_monotonic"])
        p = state["checkpoint"]["autonomous_positions"][key]
        completed = Decimal(p["exit_proceeds"]) + prices[i] if i else sum(prices)
        require(completed >= EARLY_EXIT_PROCEEDS and prices[i] >= initial[i] - MAX_DETERIORATION,
                "Exit quote deteriorated; preserve remaining holding for review")
        new_key = "autonomous-exit-" + uuid4().hex
        used = exit_keys(state["checkpoint"]) | {leg["intent"]["request"]["idempotencyKey"]
                for a in state["checkpoint"]["autonomous_execution"]["attempts"].values() for leg in a["legs"] if leg["intent"]}
        require(new_key not in used, "Duplicate sale idempotency key; no retry")
        body = {"idempotencyKey": new_key, "exchangeId": eid, "side": "no", "action": "sell", "quantity": 1,
                "price": float(prices[i]), "tournamentId": approval["tournament_id"],
                "expirationDate": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="milliseconds")}
        leg = p["exit_execution"]["legs"][i]
        leg["request"], leg["post_attempted"] = body, True
        persist(state)  # Key, exact body and possible POST survive a crash.
        require_submission()
        # Fresh account + price immediately before submission, after fsync.
        fresh = pilot_account.read_snapshot(reads, approval["tournament_slug"], state["checkpoint"], order_ids=[])
        require(held_quantity(fresh["account"], eid) == 1 and not fresh["account"]["orders"]
                and probe.inventory(fresh["account"], "") == probe.inventory(current["account"], "")
                and fresh["account"]["tournament"]["myBalance"] == current["account"]["tournament"]["myBalance"],
                "Account changed before sale")
        latest, _ = exit_quote(reads, approval, fresh["freshness"]["started_monotonic"])
        live.revalidate_authorization(approval)
        require(latest[i] >= Decimal(str(body["price"])) and
                (Decimal(p["exit_proceeds"]) + latest[i] if i else sum(latest)) >= EARLY_EXIT_PROCEEDS,
                "Executable exit disappeared before submission")
        response = session.post(scanner.API_BASE_URL + "/orders", json=copy.deepcopy(body),
                                timeout=scanner.REQUEST_TIMEOUT, allow_redirects=False)
        leg = state["checkpoint"]["autonomous_positions"][key]["exit_execution"]["legs"][i]
        leg["response"] = reads.response_evidence(response)
        persist(state)
        require_trade_scope_response(leg["response"])
        response.raise_for_status()
        require(response.status_code == 200 and not leg["response"]["credential_redacted"], "Ambiguous sale response; no retry")
        receipt = response.json()
        leg = state["checkpoint"]["autonomous_positions"][key]["exit_execution"]["legs"][i]
        leg["receipt"] = copy.deepcopy(receipt)
        persist(state)
        require(type(receipt.get("orderId")) is int and receipt["orderId"] > 0 and
                all(receipt.get(k) == body[k] for k in ("exchangeId", "side", "action", "quantity", "price")),
                "Sale receipt does not match the held-share intent")
        wait_for_account_cache()
        after = pilot_account.read_snapshot(reads, approval["tournament_slug"], state["checkpoint"], order_ids=[receipt["orderId"]])
        leg = state["checkpoint"]["autonomous_positions"][key]["exit_execution"]["legs"][i]
        leg["after"] = copy.deepcopy(after)
        activity = after["order_activity"][0]
        quantity = sum((abs(Decimal(str(f["quantity"]))) for f in activity["fills"]), Decimal(0))
        proceeds = Decimal(activity["fill_notional"])
        leg["filled_quantity"], leg["proceeds"] = str(quantity), str(proceeds)
        state["checkpoint"]["autonomous_positions"][key]["remaining_quantities"][i] = str(1 - quantity)
        pilot.refresh_totals(state["checkpoint"])
        persist(state)  # Preserve a known partial sale before stopping.
        require(quantity == 1 and receipt["quantityTraded"] == 1 and receipt["open"] is False and
                receipt["remainingQuantity"] == 0 and activity["order"]["open"] is False and
                activity["order"]["action"] == "sell" and activity["order"]["side"] == "no" and
                activity["order"]["exchangeId"] == eid and activity["order"]["priceLimit"] == body["price"] and
                single.same_order_expiry(activity["order"]["expirationDate"], body["expirationDate"]) and
                all(Decimal(str(f["price"])) >= Decimal(str(body["price"])) for f in activity["fills"])
                and abs(proceeds - probe.money(receipt["totalCost"])) <= Decimal(".000000001"),
                "Partial, resting or inconsistent sale; manual review required")
        delta = probe.money(after["account"]["tournament"]["myBalance"]) - probe.money(current["account"]["tournament"]["myBalance"])
        old_events = {r["event_id"] for r in current["recent_transactions"]}
        events = [r for r in after["recent_transactions"] if r["event_id"] not in old_events]
        require(abs(delta - proceeds) <= pilot_account.BALANCE_DELTA_TOLERANCE and
                held_quantity(after["account"], eid) == 0 and not after["account"]["orders"]
                and probe.inventory(current["account"], eid) == probe.inventory(after["account"], eid)
                and len(events) == 1 and events[0]["event_type"] == "trade" and events[0]["exchangeId"] == eid
                and events[0]["orderType"] == "SELL" and abs(Decimal(str(events[0]["quantity"]))) == 1
                and abs(probe.money(events[0]["price"]) - proceeds) <= Decimal(".000000001"),
                "Sale cash/position/ledger reconciliation is ambiguous")
        credit_position(state["checkpoint"], key, i, proceeds, delta)
        check_cash_model(state["checkpoint"], after)
        persist(state)
        current = after
    p = state["checkpoint"]["autonomous_positions"][key]
    close_position(p, "EARLY_EXIT")
    p["last_snapshot"] = copy.deepcopy(current)
    p["notes"].append("Both backed NO sells reconciled against fills, ledger, holdings and cash.")
    state["checkpoint"]["state"] = pilot.READY
    recalculate_budget(state["checkpoint"])
    persist(state)
    return {"pair": p["pair"], "action": "EARLY_EXIT", "realized_pnl": p["realized_pnl"]}


def manage_positions_locked(session, checkpoint):
    require_submission()  # Disabled mode cannot stage an exit or touch state.
    state = {"checkpoint": checkpoint}
    try:
        pilot.require_execution_clear(checkpoint)
        if not any(p["status"] == "OPEN" for p in checkpoint.get("autonomous_positions", {}).values()):
            return {"state": checkpoint["state"], "positions": [], "checkpoint": checkpoint}
        snapshot = pilot_account.read_snapshot(session, checkpoint.get("baseline_snapshot", {}).get("account", {}).get(
            "tournament", {}).get("slug", "midterm-elections"), checkpoint, order_ids=[], allow_inactive=True)
        reports = process_settlements(session, state, snapshot)
        if snapshot["account"]["tournament"]["status"] != "active":
            return {"state": state["checkpoint"]["state"], "positions": reports,
                    "checkpoint": state["checkpoint"], "account_active": False}
        for key, p in state["checkpoint"].get("autonomous_positions", {}).items():
            if p["status"] != "OPEN" or any(p["settled_legs"]):
                continue
            approval = state["checkpoint"]["autonomous_execution"]["attempts"][key]["authorization"]
            markets = scoped_markets(session, approval)
            if not all(m["status"] == "open" for m in markets):
                reports.append({"pair": p["pair"], "action": "HOLD", "reason": "Closed; awaiting settlement ledger"})
                continue
            try:
                prices, _ = exit_quote(session, approval, snapshot["freshness"]["started_monotonic"], markets)
            except (PreviewBlocked, scanner.DataValidationError):
                reports.append({"pair": p["pair"], "action": "HOLD", "reason": "No fresh executable exit"})
                continue
            if sum(prices) >= EARLY_EXIT_PROCEEDS:
                reports.append(execute_exit_locked(session, state, key, snapshot))
                break  # One sale execution at a time; reread next cycle.
            reports.append({"pair": p["pair"], "action": "HOLD", "exit_value": str(sum(prices))})
        return {"state": state["checkpoint"]["state"], "positions": reports, "checkpoint": state["checkpoint"]}
    except (OSError, *scanner.API_ERRORS) as error:
        safe = (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked, live.LiveSettlementBlocked, PreviewBlocked)
        return halt(state, str(error) if isinstance(error, safe) else "Position account/execution unavailable; manual review required")
    except BaseException:
        halt(state, "Position execution interrupted; never resume a sale automatically")
        raise


def execute_candidate(session, ids, mode=config.LIVE_PILOT):
    require_submission(mode)  # No reads, intents or keys while disabled.
    with pilot.pilot_lock() as path:
        cp = pilot._read_checkpoint_locked(path)
        pilot.require_execution_clear(cp)
        pilot.require_initial_evidence_clear(path)
        return _execute_locked(session, ids, cp)


def best_candidate(session, checkpoint):
    """Scan only exact authorized pairs; edge, then executable depth decide rank."""
    occupied = {eid for key, e in checkpoint["live_exposures"].items()
                if checkpoint.get("autonomous_positions", {}).get(key, {}).get("status") != "CLOSED"
                for eid, q, pending in zip(e["exchange_ids"], e["confirmed_quantities"], e["possible_additional_quantities"])
                if Decimal(q) or Decimal(pending)}
    candidates = []
    for approval in live.autonomous_authorizations():
        if occupied.intersection(approval["exchange_ids"]):
            continue
        try:
            if not all(m["status"] == "open" for m in scoped_markets(session, approval)):
                continue  # Normal closure/settlement is not changed settlement wording.
            candidates.append(fresh_candidate(session, approval["market_ids"], checkpoint))
        except (PreviewBlocked, pilot.PilotBlocked):
            continue  # Price/depth/duplicate/capacity: no execution has started.
        except pilot_account.AccountReadinessBlocked as error:
            if error.code == "LIVE_ACCOUNT_RISK_FAILED":
                continue
            raise
    if not candidates:
        return None
    return max(candidates, key=lambda c: (Decimal(c["edge"]), min(b["bid_quantity"] for b in c["books"])))


def cycle_locked(session, checkpoint):
    """One serial cycle: reconcile holds, select one entry, then manage it."""
    require_submission()
    managed = manage_positions_locked(session, checkpoint)
    if managed["state"] == pilot.HALTED:
        return managed
    cp = managed["checkpoint"]
    # An exit already used this cycle's execution slot. No overlapping orders.
    if managed.get("account_active") is False or any(p["action"] == "EARLY_EXIT" for p in managed["positions"]):
        return managed
    try:
        candidate = best_candidate(session, cp)
        if candidate is None:
            return managed
        # Candidate ranking grants no permission to use old prices: the existing
        # entry coordinator repeats all checks and persists intent before POST.
        entered = _execute_locked(session, candidate["authorization"]["market_ids"], cp)
        if entered["state"] == pilot.HALTED:
            return entered
        managed = manage_positions_locked(session, pilot._read_checkpoint_locked(pilot.ALLOCATION_PATH.resolve()))
        managed["entry"] = entered
        return managed
    except (OSError, *scanner.API_ERRORS) as error:
        safe = (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked, live.LiveSettlementBlocked)
        return halt({"checkpoint": cp}, str(error) if isinstance(error, safe) else "Readiness unavailable; no orders retried")


def run(session):
    """Foreground 15-second loop. One process lock covers entries and exits."""
    require_submission()
    with pilot.pilot_lock() as path:
        while True:
            require_submission()
            started = time.monotonic()
            cp = pilot._read_checkpoint_locked(path)
            pilot.require_execution_clear(cp)
            pilot.require_initial_evidence_clear(path)
            result = cycle_locked(session, cp)
            if result["state"] == pilot.HALTED:
                print(f"HALTED_MANUAL_REVIEW | {result['reason']}")
                return result
            if result.get("entry"):
                print(f"OPEN | {result['entry']['execution_id']} | quantity 1 per leg")
            for report in result["positions"]:
                if report["action"] != "HOLD":
                    print(f"{report['action']} | {report['pair']} | realized P&L {report['realized_pnl']} SUSQies")
            time.sleep(max(0, scanner.SCAN_INTERVAL - (time.monotonic() - started)))


def resume_recovered_leg1(session, attempt_id):
    """Explicit one-use continuation; ordinary restart still halts execution.

    This command can submit ONLY a newly rechecked leg two. The first leg's
    original key/request stays terminal and is never passed to submit_once.
    """
    import threading
    from recover_filled_leg1 import require_resume
    from senate_readiness_watcher import readonly_pilot_lock
    require_submission()
    with readonly_pilot_lock() as path:
        cp = pilot._read_checkpoint_locked(path)  # No automatic restart mutation.
        require_resume(cp, attempt_id)
        pilot.require_external_execution_clear()
        pilot.require_initial_evidence_clear(path)
        require(path not in pilot._state_lock_owners, "Another operation owns pilot state")
        pilot._state_lock_owners[path] = (os.getpid(), threading.get_ident())
        state = {"checkpoint": cp}
        try:
            active_attempt(cp)["filled_leg1_recovery"]["resume_consumed_at"] = datetime.now(timezone.utc).isoformat()
            persist(state)  # Consume BEFORE reads/intent/POST; crash cannot replay.
            reads = EvidenceReads(session, state)
            reads.reads = copy.deepcopy(active_attempt(state["checkpoint"])["reads"])
            return finish_second_leg(session, state, reads)
        except (OSError, *scanner.API_ERRORS) as error:
            safe = (pilot.PilotBlocked, pilot_account.AccountReadinessBlocked, live.LiveSettlementBlocked,
                    PreviewBlocked, single.TestError, probe.ProbeBlocked)
            return halt(state, str(error) if isinstance(error, safe) else "Continuation unavailable/ambiguous; no submission retry")
        except BaseException:
            halt(state, "Recovered execution interrupted; no automatic continuation or retry")
            raise
        finally:
            pilot._state_lock_owners.pop(path, None)


def main(argv=()):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-recovered-leg1", metavar="ATTEMPT_ID",
                        help="Explicitly continue one GET-reconciled filled first leg; may submit leg two")
    args = parser.parse_args(argv)
    if not config.AUTONOMOUS_LIVE_PILOT_ENABLED:
        print("DISABLED | Set AUTONOMOUS_LIVE_PILOT_ENABLED only after explicit review")
        return 0  # No credentials, reads, state writes or intents while disabled.
    try:
        require_submission()
        load_dotenv(Path(__file__).resolve().with_name(".env"))  # Read only.
        key = os.getenv("SIG_API_KEY")
        require(bool(key), "SIG_API_KEY is unavailable")
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {key}"})
            result = resume_recovered_leg1(session, args.resume_recovered_leg1) if args.resume_recovered_leg1 else run(session)
            if args.resume_recovered_leg1:
                print(f"{result['state']} | {result.get('execution_id', result.get('reason', ''))}")
        return 1 if result and result["state"] == pilot.HALTED else 0
    except KeyboardInterrupt:
        print("Stopped. Any interrupted execution remains halted for review.")
        return 130
    except (pilot.PilotBlocked, *scanner.API_ERRORS):
        print("Stopped: account, authorization or persistent state requires review.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
