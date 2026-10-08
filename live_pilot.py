"""Disabled pilot diagnostics and accounting. No order preparation or submission.

This module reads existing account/journal data and saves pilot accounting.
It never saves an execution intent, allocates an idempotency key, or calls an
exchange write endpoint. Even selecting LIVE_PILOT cannot enable submission.
"""
import argparse
import copy
import fcntl
import json
import os
import re
import time
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
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
from paper_trader import parse_api_timestamp, validate_paper_market_context

ALLOCATION_PATH = Path(__file__).resolve().with_name("live_pilot_allocation.json")
UNCERTAIN_STATES = {"SUBMITTING", "UNKNOWN", "CANCEL_REQUESTED", "CANCEL_UNKNOWN", "EXECUTING", "CANCELLING"}
DISABLED, READY, EXECUTING, HALTED = "DISABLED", "READY", "EXECUTING", "HALTED_MANUAL_REVIEW"
PILOT_STATES = {DISABLED, READY, EXECUTING, HALTED}
# Reentry is allowed only for the same process/thread already holding this lock.
# Independent processes/threads still have to acquire the OS lock themselves.
_state_lock_owners = {}


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


@contextmanager
def pilot_lock(path=None):
    """Hold the scanner's nonblocking flock pattern for the whole pilot operation."""
    path = Path(ALLOCATION_PATH if path is None else path).resolve()
    owner = (os.getpid(), threading.get_ident())
    if _state_lock_owners.get(path) == owner:
        yield path
        return
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        stream = lock_path.open("a")
    except OSError as error:
        raise PilotBlocked("Pilot state lock unavailable; stop for manual review") from error
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise PilotBlocked("Another live-pilot process is running or the state lock is unavailable") from error
        _state_lock_owners[path] = owner
        try:
            # A new operation/process cannot resume a previously unfinished
            # operation. Reentrant calls during the same locked run skip this.
            if path.exists():
                _load_checkpoint_locked(path)
            yield path
        finally:
            _state_lock_owners.pop(path, None)
        # Close releases the OS lock, including on exceptions/Ctrl+C. Never
        # unlink the lock file: two different lock-file inodes could defeat it.


def refresh_totals(checkpoint):
    """Calculate derived fields before saving; loading never repairs bad totals."""
    debits = sum((amount(cost) for cost in checkpoint["accounted_pair_costs"].values()), Decimal(0))
    confirmed = reserved = Decimal(0)
    for exposure in checkpoint["live_exposures"].values():
        confirmed += sum((amount(cost) for cost in exposure["confirmed_costs"]), Decimal(0))
        reserved += sum((amount(quantity) * amount(price) for quantity, price in
                         zip(exposure["possible_additional_quantities"], exposure["limit_prices"])), Decimal(0))
    checkpoint["confirmed_cumulative_debits"] = str(debits)
    checkpoint["reserved_unconfirmed_capital"] = str(reserved)
    checkpoint["total_live_exposure"] = str(confirmed + reserved + amount(checkpoint["quarantine_reserve"]))
    checkpoint["calculated_remaining_allocation"] = str(max(Decimal(0),
        amount(checkpoint["allocated_cash_remaining"]) - reserved - amount(checkpoint["quarantine_reserve"])))


def allocation_checkpoint(account):
    """Construct accounting data in memory ONLY; never initialize/reset on startup.

    Only initialize_checkpoint persists a new baseline. Normal startup must load
    the existing file; recreating a baseline would replenish losses from reserve.
    """
    cash = amount(account["tournament"]["myBalance"])
    allocated = min(cash, Decimal(config.LIVE_ALLOCATION))
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    checkpoint = {"version": 2, "tournament_id": account["tournament"]["id"],
            "initial_account_cash": str(cash), "untouchable_cash_reserve": str(cash - allocated),
            "allocated_cash_remaining": str(allocated), "last_reconciled_account_cash": str(cash),
            "accounted_pair_costs": {}, "manual_review_required": False, "review_reason": "",
            "configured_allocation": str(config.LIVE_ALLOCATION), "live_exposures": {},
            "quarantine_reserve": str(amount(quarantine.reserved_cost(account["tournament"]["id"]))),
            "state": DISABLED, "revision": 0, "created_at": now, "updated_at": now}
    refresh_totals(checkpoint)
    return checkpoint


def validate_checkpoint(checkpoint):
    """Never let a corrupt/restarted checkpoint create new allocation capacity."""
    require(isinstance(checkpoint, dict) and set(checkpoint) == {
        "version", "tournament_id", "initial_account_cash", "untouchable_cash_reserve",
        "allocated_cash_remaining", "last_reconciled_account_cash", "accounted_pair_costs",
        "manual_review_required", "review_reason", "configured_allocation", "confirmed_cumulative_debits",
        "live_exposures", "quarantine_reserve", "reserved_unconfirmed_capital", "total_live_exposure",
        "calculated_remaining_allocation", "state", "revision", "created_at", "updated_at"},
        "Invalid pilot allocation checkpoint")
    require(type(checkpoint["version"]) is int and checkpoint["version"] == 2
            and isinstance(checkpoint["tournament_id"], str)
            and str(UUID(checkpoint["tournament_id"])) == checkpoint["tournament_id"],
            "Invalid pilot allocation identity")
    require(amount(checkpoint["configured_allocation"]) == config.LIVE_ALLOCATION,
            "Configured pilot allocation must remain 5000")
    require(checkpoint["state"] in PILOT_STATES and type(checkpoint["revision"]) is int
            and checkpoint["revision"] >= 0, "Invalid pilot state/revision")
    created, updated = (parse_api_timestamp(checkpoint[key]) for key in ("created_at", "updated_at"))
    require(created.tzinfo is not None and updated.tzinfo is not None and updated >= created,
            "Invalid pilot timestamp metadata")
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
    require(checkpoint["manual_review_required"] == (checkpoint["state"] == HALTED)
            and (bool(checkpoint["review_reason"].strip()) if checkpoint["manual_review_required"]
                 else checkpoint["review_reason"] == ""), "Pilot halt state/reason disagree")
    require(isinstance(checkpoint["live_exposures"], dict), "Invalid saved pilot exposure")
    seen_exchanges = set()
    for fingerprint, exposure in checkpoint["live_exposures"].items():
        require(isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                and isinstance(exposure, dict) and set(exposure) == {
                    "market_ids", "exchange_ids", "position_type", "confirmed_quantities", "confirmed_costs",
                    "possible_additional_quantities", "limit_prices", "execution_status"}, "Invalid saved pilot exposure")
        require(exposure["position_type"] == "NO-PAIR", "Unsupported pilot exposure direction")
        for key in ("market_ids", "exchange_ids"):
            ids = exposure[key]
            require(isinstance(ids, list) and len(ids) == 2 and len(set(ids)) == 2
                    and all(isinstance(value, str) and scanner.numeric_id(value) == value for value in ids),
                    "Invalid saved pilot instrument IDs")
        require(not seen_exchanges.intersection(exposure["exchange_ids"]), "Duplicate saved pilot exchange exposure")
        seen_exchanges.update(exposure["exchange_ids"])
        for key in ("confirmed_quantities", "confirmed_costs", "possible_additional_quantities", "limit_prices"):
            require(isinstance(exposure[key], list) and len(exposure[key]) == 2, "Incomplete saved pilot exposure")
        for quantity, cost, pending, price in zip(exposure["confirmed_quantities"], exposure["confirmed_costs"],
                                                 exposure["possible_additional_quantities"], exposure["limit_prices"]):
            q, c, p, limit = map(amount, (quantity, cost, pending, price))
            require(q + p <= 1 and Decimal(".005") <= limit <= Decimal(".995")
                    and c <= q * limit + Decimal(".000000001"), "Inconsistent saved pilot exposure quantities/costs")
        known_cost = sum((amount(cost) for cost in exposure["confirmed_costs"]), Decimal(0))
        require(known_cost == amount(costs.get(fingerprint, 0)), "Saved pilot exposure and confirmed debit disagree")
        require(exposure["execution_status"] in {EXECUTING, "RECONCILED_PAIR", "AWAITING_MANUAL_SECOND_LEG", "MANUAL_REVIEW"},
                "Invalid saved execution status")
        if exposure["execution_status"] == "RECONCILED_PAIR":
            require(list(map(amount, exposure["confirmed_quantities"])) == [1, 1]
                    and list(map(amount, exposure["possible_additional_quantities"])) == [0, 0],
                    "Reconciled saved pair is incomplete")
        else:
            require(checkpoint["state"] in {EXECUTING, HALTED}, "Unfinished exposure cannot be READY or DISABLED")
    calculated = copy.deepcopy(checkpoint)
    refresh_totals(calculated)
    require(all(amount(checkpoint[key]) == amount(calculated[key]) for key in (
        "confirmed_cumulative_debits", "reserved_unconfirmed_capital", "total_live_exposure",
        "calculated_remaining_allocation")), "Saved pilot totals are inconsistent")


def _read_checkpoint_locked(path):
    """Preserve corrupt bytes for review; never rebuild or repair a budget."""
    def unique_fields(fields):
        result = {}
        for key, value in fields:
            require(key not in result, "Duplicate pilot state field")
            result[key] = value
        return result

    try:
        checkpoint = json.loads(path.read_text(), object_pairs_hook=unique_fields)
        validate_checkpoint(checkpoint)
        require(checkpoint["revision"] >= 1, "Unpersisted pilot state revision")
        return checkpoint
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        raise PilotBlocked("HALTED_MANUAL_REVIEW: pilot allocation checkpoint missing or invalid; manual review required") from error


def _save_checkpoint_locked(checkpoint, path, new=False):
    """Validate then fsync/replace/fsync-directory while holding one stable lock."""
    require(_state_lock_owners.get(path) == (os.getpid(), threading.get_ident()),
            "Pilot state mutation requires the exclusive process lock")
    proposed = copy.deepcopy(checkpoint)
    validate_checkpoint(proposed)
    if new:
        require(not path.exists() and proposed["revision"] == 0, "Pilot state already exists; never reset its allocation")
    else:
        previous = _read_checkpoint_locked(path)
        require(proposed["revision"] == previous["revision"], "Stale pilot state revision; reload and review")
        for key in ("configured_allocation", "tournament_id", "initial_account_cash", "untouchable_cash_reserve", "created_at"):
            require(proposed[key] == previous[key], "Immutable pilot allocation baseline changed")
        require(not previous["manual_review_required"] or proposed["state"] == HALTED,
                "A persistent manual-review halt cannot be cleared automatically")
        for fingerprint, cost in previous["accounted_pair_costs"].items():
            require(fingerprint in proposed["accounted_pair_costs"]
                    and amount(proposed["accounted_pair_costs"][fingerprint]) >= amount(cost),
                    "Confirmed pilot debits cannot disappear or decrease")
        for fingerprint, exposure in previous["live_exposures"].items():
            require(fingerprint in proposed["live_exposures"], "Saved live exposure cannot be forgotten")
            current = proposed["live_exposures"][fingerprint]
            require(all(current[key] == exposure[key] for key in ("market_ids", "exchange_ids", "position_type", "limit_prices")),
                    "Saved live exposure identity changed")
            require(all(amount(after) >= amount(before) for key in ("confirmed_quantities", "confirmed_costs")
                        for before, after in zip(exposure[key], current[key])), "Confirmed pilot fills cannot decrease")
        require(amount(proposed["quarantine_reserve"]) >= amount(previous["quarantine_reserve"]),
                "Saved quarantine reserve cannot disappear")
    proposed["revision"] += 1
    proposed["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    validate_checkpoint(proposed)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(proposed, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        single.sync_directory(path.parent)
    except (OSError, ValueError, TypeError) as error:
        raise PilotBlocked("Pilot state write failed; stop for manual review without further action") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return proposed


def _load_checkpoint_locked(path):
    checkpoint = _read_checkpoint_locked(path)
    if checkpoint["state"] == EXECUTING:
        checkpoint = halted_result(checkpoint, "Restart found unfinished execution; manual review required")["checkpoint"]
        checkpoint = _save_checkpoint_locked(checkpoint, path)
    return checkpoint


def load_checkpoint(path=None):
    """Load under the process lock; a restart latches unfinished execution HALTED."""
    with pilot_lock(path) as state_path:
        return _read_checkpoint_locked(state_path)


def initialize_checkpoint(account, path=None):
    """Explicit local initialization only. Never replace an existing baseline."""
    with pilot_lock(path) as state_path:
        require(not state_path.exists(), "Pilot state already exists; never reset its allocation")
        checkpoint = allocation_checkpoint(account)
        require_execution_clear(checkpoint)
        return _save_checkpoint_locked(checkpoint, state_path, new=True)


def save_checkpoint(checkpoint, path=None):
    """Atomically persist a checked update; stale snapshots cannot overwrite it."""
    with pilot_lock(path) as state_path:
        return _save_checkpoint_locked(checkpoint, state_path)


def require_execution_clear(checkpoint):
    """New uncertainty halts globally; the existing frozen quarantine is separate."""
    validate_checkpoint(checkpoint)
    require(not checkpoint["manual_review_required"], "Pilot halted for manual review; no further live trading")
    require(checkpoint["state"] != EXECUTING, "Unfinished pilot execution requires manual review before another pair")
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
    positions = {scanner.numeric_id(row["exchangeId"]): row for row in account["positions"]}
    stored_holdings = Decimal(0)
    for exposure in checkpoint["live_exposures"].values():
        for market_id, exchange_id, held_quantity, held_cost in zip(exposure["market_ids"], exposure["exchange_ids"],
                                                         exposure["confirmed_quantities"], exposure["confirmed_costs"]):
            q, c = amount(held_quantity), amount(held_cost)
            stored_holdings += c
            if q:
                row = positions.get(exchange_id)
                require(row is not None and scanner.numeric_id(row["marketId"]) == market_id
                        and abs(Decimal(str(row["quantity"])) + q) <= Decimal(".000000001")
                        and abs(amount(row["costBasis"]) - c) <= Decimal(".00000001")
                        and row["settled"] is False, "Actual holdings disagree with persisted pilot exposure; manual review required")
    holdings = max(amount(risk["existing_holdings_cost_basis"]), stored_holdings)
    # Include external orders as well as saved possible pilot fills. They can
    # coexist. A known order may be reserved twice conservatively, never omitted.
    pending = (amount(risk["existing_order_reserve"]) + amount(checkpoint["reserved_unconfirmed_capital"])
               + max(amount(risk["quarantine_reserve"]), amount(checkpoint["quarantine_reserve"])))
    exposure = holdings + pending
    # Account cash above this fixed budget can NEVER increase bot capacity.
    available = min(amount(checkpoint["allocated_cash_remaining"]), cash - reserve_floor,
                    max(Decimal(0), Decimal(config.LIVE_ALLOCATION) - holdings)) - pending
    capital = amount(proposed_capital)
    require(type(quantity) is int and quantity == config.MAX_LIVE_QUANTITY_PER_LEG,
            "Pilot permits exactly one contract per leg")
    require(capital > 0 and capital <= config.MAX_LIVE_CAPITAL_PER_TRADE, "Pilot per-trade capital limit exceeded")
    require(exposure + capital <= config.MAX_TOTAL_LIVE_EXPOSURE, "Pilot total exposure limit exceeded")
    require(max(amount(risk["race_exposure_upper_bound"]), exposure) + capital <= config.MAX_LIVE_CAPITAL_PER_RACE,
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
    """Construct a latched halt; the locked coordinator persists it before return."""
    updated = copy.deepcopy(checkpoint)
    updated["state"], updated["manual_review_required"] = HALTED, True
    updated["review_reason"] = updated["review_reason"] or reason
    refresh_totals(updated)
    return {"checkpoint": updated, "status": "MANUAL_REVIEW", "reason": updated["review_reason"],
            "observed_legs": legs, "live_eligible": False, "submission_enabled": False}


def _observe_pilot_pair(session, pair, legs, checkpoint, on_observation):
    """Existing GET-only reconciliation, coordinated by the durable state wrapper."""
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
                on_observation(observed)  # Persist each confirmed leg before reading the next.
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


def exposure_record(pair, legs, status):
    """Save numeric inventory/reserves, never an executable request or order key."""
    quantities, costs, pending, limits = [], [], [], []
    for leg, material in zip(legs, pair["requests"]):
        single.validate_intent(leg)
        require(all(leg[key] == material[key] for key in material), "Pilot exposure journal identity mismatch")
        observation = leg["observation"] or {}
        quantity = amount(paired.filled_quantity(leg))
        cost = amount(observation.get("filled_cost", observation.get("placement_cost", 0)))
        quantities.append(str(quantity))
        costs.append(str(cost))
        # Reserve unattempted and uncertain remainders conservatively. This is
        # accounting only; it cannot cause the unattempted order to be sent.
        pending.append(str(Decimal(0) if leg["state"] in {"OBSERVED_TERMINAL", "NOOP"} else 1 - quantity))
        limits.append(str(amount(leg["request"]["price"])))
    return {"market_ids": pair["market_context"]["market_ids"],
            "exchange_ids": pair["market_context"]["exchange_ids"], "position_type": "NO-PAIR",
            "confirmed_quantities": quantities, "confirmed_costs": costs,
            "possible_additional_quantities": pending, "limit_prices": limits, "execution_status": status}


def checkpoint_with_exposure(checkpoint, pair, legs, status):
    updated = copy.deepcopy(checkpoint)
    fingerprint = pair["approval"]
    exposure = exposure_record(pair, legs, status)
    previous = updated["live_exposures"].get(fingerprint)
    if previous:
        # A cached legacy journal can lag our persisted fresh fill observations.
        # Preserve known inventory rather than crediting a reporting regression.
        for key in ("confirmed_quantities", "confirmed_costs"):
            exposure[key] = [str(max(amount(before), amount(after)))
                             for before, after in zip(previous[key], exposure[key])]
        exposure["possible_additional_quantities"] = [str(min(amount(pending), 1 - amount(quantity)))
            for pending, quantity in zip(exposure["possible_additional_quantities"], exposure["confirmed_quantities"])]
    cost = sum(map(amount, exposure["confirmed_costs"]), Decimal(0))
    prior_cost = amount(updated["accounted_pair_costs"].get(fingerprint, 0))
    require(cost >= prior_cost, "Confirmed pilot cost regressed; manual review required")
    updated["live_exposures"][fingerprint] = exposure
    if cost or fingerprint in updated["accounted_pair_costs"]:
        updated["accounted_pair_costs"][fingerprint] = str(cost)
    updated["allocated_cash_remaining"] = str(amount(updated["allocated_cash_remaining"]) - (cost - prior_cost))
    refresh_totals(updated)
    return updated


def reconcile_pilot(session, pair, legs, checkpoint=None, path=None):
    """Persist progress/halts under the process lock; never modify order journals."""
    with pilot_lock(path) as state_path:
        current = _read_checkpoint_locked(state_path)
        if current["manual_review_required"]:
            return halted_result(current, "Existing manual-review halt remains in force")
        if checkpoint is not None:
            require(checkpoint == current, "Stale pilot checkpoint; reload before reconciliation")
        try:
            paired.validate_pair(pair)
            require(pair["policy"] != paired.CONDITIONAL_POLICY, "Conditional settlement cannot enter the pilot")
            require(pair["market_context"]["tournament_id"] == current["tournament_id"], "Pilot reconciliation scope mismatch")
            require(len(legs) == 2, "Pilot reconciliation requires two leg journals")
            if pair["approval"] not in current["accounted_pair_costs"]:
                require(amount(pair["starting_cash"]) == amount(current["last_reconciled_account_cash"]),
                        "Pair baseline is not bound to the pilot checkpoint")
            current["state"] = EXECUTING
            current = checkpoint_with_exposure(current, pair, legs, EXECUTING)
            current = _save_checkpoint_locked(current, state_path)  # Durable before any GETs.

            def persist_observation(observed):
                nonlocal current
                current = checkpoint_with_exposure(current, pair, observed, EXECUTING)
                current = _save_checkpoint_locked(current, state_path)

            result = _observe_pilot_pair(session, pair, legs, current, persist_observation)
            observed = result.get("observed_legs") or legs
            status = result["status"]
            current = checkpoint_with_exposure(current, pair, observed, status)
            current["last_reconciled_account_cash"] = result["checkpoint"]["last_reconciled_account_cash"]
            if status == "MANUAL_REVIEW":
                current = halted_result(current, result["reason"])["checkpoint"]
            else:
                current["state"] = EXECUTING if status == "AWAITING_MANUAL_SECOND_LEG" else DISABLED
            current = _save_checkpoint_locked(current, state_path)
            result["checkpoint"] = current
            return result
        except (KeyboardInterrupt, *scanner.API_ERRORS) as error:
            # If persistence itself is unavailable, this write also fails closed.
            # The already durable EXECUTING marker will halt the next startup.
            result = halted_result(current, "Pilot reconciliation interrupted or invalid; manual review required")
            result["checkpoint"] = _save_checkpoint_locked(result["checkpoint"], state_path)
            if isinstance(error, KeyboardInterrupt):
                raise
            return result


def consider_second_leg(session, pair, reconciliation, manually_supervised=False, restarted=True):
    """Only inspect a second leg inside the same still-locked supervised run."""
    require(manually_supervised is True and restarted is False,
            "Second leg requires a fresh explicit supervised decision; restart cannot continue it")
    with pilot_lock() as state_path:
        current = _read_checkpoint_locked(state_path)
        require(not current["manual_review_required"], "Pilot halted for manual review")
        require(current == reconciliation["checkpoint"], "Second-leg diagnostic has a stale pilot checkpoint")
        try:
            return _consider_second_leg(session, pair, reconciliation, manually_supervised, restarted)
        except scanner.API_ERRORS:
            halted = halted_result(current, "Second-leg checks failed; manual review required")["checkpoint"]
            _save_checkpoint_locked(halted, state_path)
            raise


def _consider_second_leg(session, pair, reconciliation, manually_supervised=False, restarted=True):
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
    print("Activation blockers: explicit account baseline initialization, pilot-scoped settlement authorization, "
          "and an explicitly gated supervised executor. None is enabled by this checker.")


def print_checkpoint(checkpoint):
    print(f"Pilot state: {checkpoint['state']} | version {checkpoint['version']} | revision {checkpoint['revision']}")
    print(f"Allocation {checkpoint['configured_allocation']} | confirmed cumulative debits "
          f"{checkpoint['confirmed_cumulative_debits']} | remaining after saved reserves "
          f"{checkpoint['calculated_remaining_allocation']}")
    print(f"Live exposure {checkpoint['total_live_exposure']} | uncertain/unattempted reserve "
          f"{checkpoint['reserved_unconfirmed_capital']} | quarantine reserve {checkpoint['quarantine_reserve']}")
    if checkpoint["manual_review_required"]:
        print(f"HALTED_MANUAL_REVIEW | {checkpoint['review_reason']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=(config.PAPER, config.LIVE_PILOT_DISABLED, config.LIVE_PILOT),
                        default=config.LIVE_PILOT_DISABLED)
    parser.add_argument("--dem-market")
    parser.add_argument("--rep-market")
    parser.add_argument("--initialize-state", action="store_true",
                        help="Explicitly save a new DISABLED account baseline using GETs only; never overwrite")
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
    diagnostic = args.dem_market is not None or args.rep_market is not None
    if diagnostic and (args.dem_market is None or args.rep_market is None):
        parser.error("Read-only candidate diagnostics require both market IDs")
    if args.initialize_state and diagnostic:
        parser.error("Initialize the disabled baseline separately from candidate diagnostics")
    try:
        # One exclusive lock covers load, all GET observations, and all writes.
        # Paper scanning uses its own unchanged portfolio and scanner lock.
        with pilot_lock() as state_path:
            if args.initialize_state:
                require(not state_path.exists(), "Pilot state already exists; never reset its allocation")
            else:
                checkpoint = _read_checkpoint_locked(state_path)
                print_checkpoint(checkpoint)
                if not diagnostic:
                    return 1 if checkpoint["manual_review_required"] else 0
                require(not checkpoint["manual_review_required"], "Pilot halted for manual review; no further live trading")
                ids = [scanner.numeric_id(args.dem_market), scanner.numeric_id(args.rep_market)]
                configured_approval(ids)
            load_dotenv()
            api_key = os.getenv("SIG_API_KEY")
            require(bool(api_key), "Set SIG_API_KEY locally before GET-only diagnostics")
            with requests.Session() as session:
                session.headers.update({"Authorization": f"Bearer {api_key}"})
                if args.initialize_state:
                    checkpoint = initialize_checkpoint(read_account(session, "midterm-elections"))
                    print_checkpoint(checkpoint)
                else:
                    try:
                        result = audit_candidate(session, ids, checkpoint)
                        checkpoint["state"] = READY  # Diagnostic only; still cannot submit.
                        checkpoint = _save_checkpoint_locked(checkpoint, state_path)
                        print(json.dumps(result, indent=2))
                    except (KeyboardInterrupt, *scanner.API_ERRORS) as error:
                        checkpoint = halted_result(checkpoint, "Pilot readiness/account checks failed; manual review required")["checkpoint"]
                        _save_checkpoint_locked(checkpoint, state_path)
                        if isinstance(error, KeyboardInterrupt):
                            raise
                        raise
    except KeyboardInterrupt:
        print("Stopped; any unfinished execution remains blocked for manual review.")
        return 130
    except scanner.API_ERRORS as error:
        print(f"BLOCKED | {str(error) if isinstance(error, PilotBlocked) else 'Read-only pilot observations unavailable or invalid'}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
