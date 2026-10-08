"""Disabled pilot diagnostics and accounting. No order preparation or submission.

This module reads existing account/journal data and returns validation results.
It never saves an execution intent, allocates an idempotency key, or calls an
exchange write endpoint. Even selecting LIVE_PILOT cannot enable submission.
"""
import argparse
import copy
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import requests
from dotenv import load_dotenv

import config
import account_test as single
import paired_account_test as paired
import execution_quarantine as quarantine
import price_reader as scanner
from account_reader import read_account
from order_preview import account_risk, observed_limits, read_selected_pair
from paper_trader import validate_paper_market_context

ALLOCATION_PATH = Path(__file__).resolve().with_name("live_pilot_allocation.json")
UNCERTAIN_STATES = {"SUBMITTING", "UNKNOWN", "CANCEL_REQUESTED", "CANCEL_UNKNOWN", "EXECUTING", "CANCELLING"}


class PilotBlocked(ValueError):
    """A fixed local explanation; never include a raw HTTP error or credential."""


def require(condition, message):
    if not condition:
        raise PilotBlocked(message)


def amount(value):
    """Checkpoint money uses decimal strings to preserve exact debits in JSON."""
    require(not isinstance(value, bool) and isinstance(value, (int, float, Decimal, str)), "Invalid pilot amount")
    if isinstance(value, str):
        require(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value), "Invalid pilot amount")
    result = Decimal(str(value))
    require(result.is_finite() and result >= 0, "Invalid pilot amount")
    return result


def require_live_submission(mode):
    """Fail closed before any network access, even if a flag is accidentally set."""
    require(mode == config.LIVE_PILOT and config.LIVE_PILOT_SUBMISSION_ENABLED,
            "LIVE_PILOT order submission is disabled")
    raise PilotBlocked("No pilot submission adapter is installed; order submission remains impossible")


def allocation_checkpoint(account):
    """Construct accounting data in memory ONLY; never initialize/reset on startup.

    Future activation must explicitly persist this once under an exclusive lock.
    Recreating a baseline on each scan would replenish losses from the reserve.
    """
    cash = amount(account["tournament"]["myBalance"])
    allocated = min(cash, Decimal(config.LIVE_ALLOCATION))
    return {"version": 1, "tournament_id": account["tournament"]["id"],
            "initial_account_cash": str(cash), "untouchable_cash_reserve": str(cash - allocated),
            "allocated_cash_remaining": str(allocated), "last_reconciled_account_cash": str(cash),
            "accounted_pair_costs": {}, "manual_review_required": False, "review_reason": ""}


def validate_checkpoint(checkpoint):
    """Never let a corrupt/restarted checkpoint create new allocation capacity."""
    require(isinstance(checkpoint, dict) and set(checkpoint) == {
        "version", "tournament_id", "initial_account_cash", "untouchable_cash_reserve",
        "allocated_cash_remaining", "last_reconciled_account_cash", "accounted_pair_costs",
        "manual_review_required", "review_reason"}, "Invalid pilot allocation checkpoint")
    require(type(checkpoint["version"]) is int and checkpoint["version"] == 1
            and isinstance(checkpoint["tournament_id"], str)
            and str(UUID(checkpoint["tournament_id"])) == checkpoint["tournament_id"],
            "Invalid pilot allocation identity")
    initial = amount(checkpoint["initial_account_cash"])
    allocated = min(initial, Decimal(config.LIVE_ALLOCATION))
    require(amount(checkpoint["untouchable_cash_reserve"]) == initial - allocated,
            "Untouchable reserve changed")
    costs = checkpoint["accounted_pair_costs"]
    require(isinstance(costs, dict), "Invalid pilot accounted costs")
    for fingerprint, cost in costs.items():
        require(isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                and amount(cost) <= config.MAX_LIVE_CAPITAL_PER_TRADE, "Invalid pilot accounted trade")
    spent = sum((amount(cost) for cost in costs.values()), Decimal(0))
    require(amount(checkpoint["allocated_cash_remaining"]) == allocated - spent,
            "Pilot allocation does not match confirmed debits")
    amount(checkpoint["last_reconciled_account_cash"])
    require(type(checkpoint["manual_review_required"]) is bool
            and isinstance(checkpoint["review_reason"], str), "Invalid pilot review state")


def load_checkpoint(path=None):
    """Read only. A missing checkpoint is a blocker, never a fresh 5,000 reset."""
    try:
        checkpoint = json.loads(Path(ALLOCATION_PATH if path is None else path).read_text())
        validate_checkpoint(checkpoint)
        return checkpoint
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise PilotBlocked("Pilot allocation checkpoint missing or invalid; no live eligibility") from error


def require_execution_clear(checkpoint):
    """New uncertainty halts globally; the existing frozen quarantine is separate."""
    validate_checkpoint(checkpoint)
    require(not checkpoint["manual_review_required"], "Pilot halted for manual review; no further live trading")
    quarantined = quarantine.load_quarantine()  # Verifies frozen source journal hashes.
    if single.STATE_PATH.exists():
        single.load_intent(single.STATE_PATH)
        raise PilotBlocked("Existing single-order journal requires manual review before pilot use")
    directory = quarantine.ACTIVE_DIR if quarantined else quarantine.SOURCE_DIR
    if directory.exists():
        pair, legs = paired.load_pair(directory)
        if pair["state"] in UNCERTAIN_STATES or any(leg["state"] in UNCERTAIN_STATES for leg in legs):
            raise PilotBlocked("Uncertain execution journal halts all further pilot trading")
        # Legacy journals have no pilot allocation binding. Do not adopt or replay them.
        raise PilotBlocked("Existing paired journal requires manual review before pilot use")


def pilot_risk(account, exchange_ids, proposed_capital, quantity, checkpoint):
    """Apply pilot limits in addition to the existing account/overlap checks.

    All account holdings/orders count conservatively. The existing reader has
    no authoritative race attribution, so all exposure also bounds this race.
    """
    validate_checkpoint(checkpoint)
    require(not checkpoint["manual_review_required"], "Pilot halted for manual review; no further live trading")
    require(checkpoint["tournament_id"] == account["tournament"]["id"], "Pilot allocation belongs to another account scope")
    risk = account_risk(account, exchange_ids)
    cash = amount(risk["reported_cash"])
    require(cash == amount(checkpoint["last_reconciled_account_cash"]),
            "Account cash changed since reconciliation; manual review required")
    reserve_floor = amount(checkpoint["untouchable_cash_reserve"])
    require(cash >= reserve_floor, "Account cash is below the untouchable reserve; manual review required")
    holdings = amount(risk["existing_holdings_cost_basis"])
    pending = amount(risk["existing_order_reserve"]) + amount(risk["quarantine_reserve"])
    exposure = holdings + pending
    # Account cash above this fixed budget can NEVER increase bot capacity.
    available = min(amount(checkpoint["allocated_cash_remaining"]), cash - reserve_floor,
                    max(Decimal(0), Decimal(config.LIVE_ALLOCATION) - holdings)) - pending
    capital = amount(proposed_capital)
    require(type(quantity) is int and quantity == config.MAX_LIVE_QUANTITY_PER_LEG,
            "Pilot permits exactly one contract per leg")
    require(capital > 0 and capital <= config.MAX_LIVE_CAPITAL_PER_TRADE, "Pilot per-trade capital limit exceeded")
    require(exposure + capital <= config.MAX_TOTAL_LIVE_EXPOSURE, "Pilot total exposure limit exceeded")
    require(amount(risk["race_exposure_upper_bound"]) + capital <= config.MAX_LIVE_CAPITAL_PER_RACE,
            "Pilot per-race capital limit exceeded (account-wide conservative bound)")
    require(capital <= available, "Insufficient remaining pilot allocation; reserve is untouchable")
    return {**risk, "live_allocation": config.LIVE_ALLOCATION, "untouchable_cash_reserve": float(reserve_floor),
            "allocation_remaining_after_reserves": float(available), "max_new_capital": float(capital),
            "total_exposure_after": float(exposure + capital)}


def configured_approval(market_ids, tournament_id=None):
    matches = [entry for entry in scanner.load_approved_settlements()
               if entry["market_ids"] == market_ids
               and (tournament_id is None or entry["tournament_id"] == tournament_id)]
    require(len(matches) == 1, "Pair is not explicitly approved in the settlement configuration")
    return matches[0]


def read_settlement(session, markets, tournament_id, approval):
    """Reuse current ID, rule, evidence-hash and quarantine revalidation."""
    scanner.check_approved_pair(approval, markets, tournament_id)
    quarantine.require_unblocked_markets(approval["market_ids"])
    quarantine.require_unblocked_exchanges(approval["exchange_ids"])
    missing_graph = False
    try:
        allowed, context = scanner.get_pair_rules(session, *markets, tournament_id)
    except scanner.DataValidationError as error:
        if ("manual_approval" not in approval
                or str(error) != "No active relationship verifies this pair's normal payout"):
            raise
        missing_graph = True
    if "manual_approval" in approval:
        manual = scanner.manual_paper_context(session, tournament_id, approval)
        if missing_graph:
            allowed, context = {"NO-PAIR"}, manual
    return set(allowed).intersection(approval["position_types"]), context


def audit_candidate(session, market_ids, checkpoint, slug="midterm-elections", position_type="NO-PAIR", quantity=1):
    """GET-only assessment. No request body, order key, staged trade or journal."""
    configured_approval(market_ids)  # Reject unlisted pairs before any API read.
    require_execution_clear(checkpoint)
    started = time.monotonic()
    account = read_account(session, slug)
    tournament_id = account["tournament"]["id"]
    approval = configured_approval(market_ids, tournament_id)
    require(position_type in approval["position_types"] and type(quantity) is int
            and quantity == 1 and quantity <= approval["max_quantity"], "Pair direction or quantity is not approved")
    markets = read_selected_pair(session, tournament_id, *market_ids)
    allowed, context = read_settlement(session, markets, tournament_id, approval)
    require(position_type in allowed, "Settlement relationship is unverified or invalid")
    books = [scanner.get_best_prices(session, market, tournament_id) for market in markets]
    prices, _, cost, edge = observed_limits(position_type, books, started, quantity=quantity)
    context = {**context, "leg_prices": prices, "book_versions": [book["version"] for book in books]}
    validate_paper_market_context({"market_context": context, "position_type": position_type,
                                  "quantity": quantity, "cost_per_pair": float(cost)})
    risk = pilot_risk(account, approval["exchange_ids"], cost, quantity, checkpoint)
    return {"mode": config.LIVE_PILOT_DISABLED, "live_eligible": False, "submission_enabled": False,
            "checks_passed": True, "pair": approval["pair_name"], "market_ids": market_ids,
            "exchange_ids": approval["exchange_ids"], "position": position_type, "quantity_per_leg": quantity,
            "prices": prices, "capital": float(cost), "edge": float(edge), "risk": risk,
            "settlement_route": "manual-paper-approval" if "manual_approval" in context else "machine-verified",
            "blockers": ["Pilot submission is disabled and has no adapter",
                         "approved_settlements.json currently authorizes PAPER only"]}


def halted_result(checkpoint, reason, legs=None):
    """Return a latched halt for future persistence; never clear it automatically."""
    updated = copy.deepcopy(checkpoint)
    updated["manual_review_required"], updated["review_reason"] = True, reason
    return {"checkpoint": updated, "status": "MANUAL_REVIEW", "reason": reason,
            "observed_legs": legs, "live_eligible": False, "submission_enabled": False}


def reconcile_pilot(session, pair, legs, checkpoint):
    """Fresh order/fill/account reconciliation, without changing real journals.

    The returned checkpoint must eventually be persisted before any subsequent
    action. This disabled scaffolding has no writer or submission continuation.
    """
    validate_checkpoint(checkpoint)
    if checkpoint["manual_review_required"]:
        return halted_result(checkpoint, "Existing manual-review halt remains in force")
    observed = copy.deepcopy(legs)
    try:
        paired.validate_pair(pair)
        require(pair["policy"] != paired.CONDITIONAL_POLICY, "Unverified conditional settlement cannot enter the pilot")
        require(pair["market_context"]["tournament_id"] == checkpoint["tournament_id"], "Reconciliation scope mismatch")
        require(len(observed) == 2, "Reconciliation requires exactly two legs")
        if pair["state"] in UNCERTAIN_STATES:
            return halted_result(checkpoint, "Uncertain controller state; no retries or automatic recovery", observed)
        for leg, material in zip(observed, pair["requests"]):
            single.validate_intent(leg)
            require(all(leg[key] == material[key] for key in material), "Leg journal identity mismatch")
            if leg["state"] in UNCERTAIN_STATES:
                return halted_result(checkpoint, "Uncertain leg state; no retries or automatic recovery", observed)
            if leg["state"] not in {"PREPARED", "NOOP"}:
                leg["state"], leg["observation"] = single.observe_test(session, leg)
        account = paired.reconcile_pair_account(session, pair, observed)
        fingerprint = pair["approval"]
        prior_cost = amount(checkpoint["accounted_pair_costs"].get(fingerprint, 0))
        if fingerprint not in checkpoint["accounted_pair_costs"]:
            require(amount(pair["starting_cash"]) == amount(checkpoint["last_reconciled_account_cash"]),
                    "Pair baseline is not bound to the pilot allocation checkpoint")
        total_cost = sum((amount((leg["observation"] or {}).get("filled_cost", 0)) for leg in observed), Decimal(0))
        require(total_cost >= prior_cost, "Previously confirmed fill cost disappeared")
        updated = copy.deepcopy(checkpoint)
        updated["accounted_pair_costs"][fingerprint] = str(total_cost)
        updated["allocated_cash_remaining"] = str(amount(updated["allocated_cash_remaining"]) - (total_cost - prior_cost))
        updated["last_reconciled_account_cash"] = str(amount(account["tournament"]["myBalance"]))
        validate_checkpoint(updated)
        quantities = [paired.filled_quantity(leg) for leg in observed]
        if quantities == [1, 1]:
            status = "RECONCILED_PAIR"
        elif quantities == [1, 0] and observed[1]["state"] == "PREPARED":
            status = "AWAITING_MANUAL_SECOND_LEG"
        else:
            return halted_result(updated, "Partial, rejected or one-sided pair requires manual review", observed)
        return {"checkpoint": updated, "status": status, "observed_legs": observed,
                "pair_approval": pair["approval"],
                "account": account, "observed_at": time.monotonic(), "confirmed_quantities": quantities,
                "confirmed_cost": float(total_cost), "live_eligible": False, "submission_enabled": False}
    except scanner.API_ERRORS:
        # Includes timeout, 429, incomplete/stale fill pages and account mismatch.
        return halted_result(checkpoint, "Order/fill/account reconciliation uncertain; stop for manual review", observed)


def consider_second_leg(session, pair, reconciliation, manually_supervised=False, restarted=True):
    """Read-only decision only: no continuation or submission after a restart."""
    require(manually_supervised is True and restarted is False,
            "Second leg requires a fresh explicit supervised decision; restart cannot continue it")
    require(reconciliation["status"] == "AWAITING_MANUAL_SECOND_LEG"
            and reconciliation["confirmed_quantities"] == [1, 0], "Leg one must be fully reconciled first")
    paired.validate_pair(pair)
    require(reconciliation["pair_approval"] == pair["approval"], "Reconciliation belongs to a different pair")
    first, second = reconciliation["observed_legs"]
    single.validate_intent(first)
    single.validate_intent(second)
    require(first["state"] == "OBSERVED_TERMINAL" and paired.filled_quantity(first) == 1
            and second["state"] == "PREPARED", "Partial or uncertain leg one blocks leg two")
    require(0 <= time.monotonic() - reconciliation["observed_at"] <= 15, "Reconciled order/account state is stale")
    checkpoint = reconciliation["checkpoint"]
    require(not checkpoint["manual_review_required"], "Pilot halted for manual review")
    context = pair["market_context"]
    approval = configured_approval(context["market_ids"], context["tournament_id"])
    markets = read_selected_pair(session, context["tournament_id"], *context["market_ids"])
    allowed, current = read_settlement(session, markets, context["tournament_id"], approval)
    require("NO-PAIR" in allowed and current["settlement_fingerprint"] == context["settlement_fingerprint"],
            "Second-leg settlement evidence changed; needs revalidation")
    books = [scanner.get_best_prices(session, market, context["tournament_id"]) for market in markets]
    prices, _, _, _ = observed_limits("NO-PAIR", books, reconciliation["observed_at"], quantity=1)
    second_price = amount(prices[1])
    first_cost = amount(reconciliation["observed_legs"][0]["observation"]["filled_cost"])
    require(second_price <= amount(pair["requests"][1]["request"]["price"]), "Quote moved above the original second-leg limit")
    require(Decimal(1) - first_cost - second_price >= Decimal(str(scanner.MIN_EDGE)),
            "Actual first fill plus current second quote fails the existing live edge")
    require(first_cost + second_price <= config.MAX_LIVE_CAPITAL_PER_TRADE, "Pilot paired capital limit exceeded")
    risk = pilot_risk(reconciliation["account"], [context["exchange_ids"][1]], second_price, 1, checkpoint)
    return {"checks_passed": True, "second_leg_price": float(second_price), "risk": risk,
            "live_eligible": False, "submission_enabled": False}


def print_policy():
    print("Mode: LIVE_PILOT_DISABLED | default scanner: PAPER | submission: IMPOSSIBLE")
    print(f"Allocation {config.LIVE_ALLOCATION} | trade {config.MAX_LIVE_CAPITAL_PER_TRADE} | "
          f"race {config.MAX_LIVE_CAPITAL_PER_RACE} | total exposure {config.MAX_TOTAL_LIVE_EXPOSURE} | "
          f"quantity/leg {config.MAX_LIVE_QUANTITY_PER_LEG}")
    print("Existing live edge remains 2%; quote freshness and depth checks are unchanged.")
    print("Activation blockers: persisted allocation initialization/halt handling, pilot-scoped settlement authorization, "
          "and an explicitly gated supervised executor. None is enabled by this checker.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=(config.PAPER, config.LIVE_PILOT_DISABLED, config.LIVE_PILOT),
                        default=config.LIVE_PILOT_DISABLED)
    parser.add_argument("--dem-market")
    parser.add_argument("--rep-market")
    args = parser.parse_args()
    print_policy()
    if args.mode == config.LIVE_PILOT:
        try:
            require_live_submission(args.mode)
        except PilotBlocked as error:
            print(f"BLOCKED | {error}")
            return 1
    if args.mode == config.PAPER:
        print("Run price_reader.py for the unchanged paper scanner.")
        return 0
    if args.dem_market is None and args.rep_market is None:
        return 0  # Local policy report: no API key, account read or state creation.
    if args.dem_market is None or args.rep_market is None:
        parser.error("Read-only candidate diagnostics require both market IDs")
    try:
        ids = [scanner.numeric_id(args.dem_market), scanner.numeric_id(args.rep_market)]
        configured_approval(ids)
        checkpoint = load_checkpoint()
        require_execution_clear(checkpoint)
        load_dotenv()
        api_key = os.getenv("SIG_API_KEY")
        require(bool(api_key), "Set SIG_API_KEY locally before GET-only diagnostics")
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {api_key}"})
            print(json.dumps(audit_candidate(session, ids, checkpoint), indent=2))
    except scanner.API_ERRORS as error:
        print(f"BLOCKED | {str(error) if isinstance(error, PilotBlocked) else 'Read-only pilot observations unavailable or invalid'}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
