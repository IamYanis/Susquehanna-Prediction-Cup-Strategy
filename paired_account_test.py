"""Supervised one-pair NO test. Default commands do not submit orders."""
import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import requests
from dotenv import load_dotenv

import execution_quarantine as quarantine
import account_test as single
from account_reader import account_error_reason, read_account
from order_preview import PreviewBlocked, account_risk, conditional_proposal, preview_pair, read_selected_pair
from paper_trader import validate_market_context
from price_reader import (
    API_BASE_URL, API_ERRORS, MIN_EDGE, DataValidationError, fetch_json,
    get_election_rule, get_exchange_id, get_pair_rules, numeric_id,
)

STATE_DIR = Path(__file__).resolve().with_name("paired_account_test")
STATES = {"PREPARED", "EXECUTING", "UNKNOWN", "RESTING", "UNMATCHED",
          "COMPLETE", "CLOSED_NO_FILL", "CANCELLING"}
VERIFIED_POLICY = "verified-no-pair-first-full-fill-then-second"
CONDITIONAL_POLICY = "conditional-ri-no-pair-first-full-fill-then-second"
# This opt-in is limited to the particular one-pair test the user requested.
# It does not relax the scanner, the paper trader, or other races' eligibility.
CONDITIONAL_MARKETS = ["387", "388"]
CONDITIONAL_EXCHANGES = ["1076", "1077"]
CONDITIONAL_RACE = ["62978", "98108", "2026-11-03", "General", "Party Winner"]
CONDITIONAL_SPEND_CAP = Decimal("0.970")
CONDITIONAL_ASSUMPTIONS = [
    "Ordinary payout calculations assume exclusive Democratic/Republican winner affiliations.",
    "Missing engine relationship evidence is accepted for this one-pair test only.",
    "Exceptional or uncoordinated settlement/refunds can cause losses; fees are unverified.",
    "Sequential execution can leave one acquired share; cancellation cannot undo a fill.",
]


def leg_path(directory, index):
    return Path(directory) / f"leg_{index + 1}.json"


def approval_hash(pair):
    # Approval covers both immutable bodies, their settlement evidence and the
    # sequencing/cancellation policy. Mutable observations stay in leg journals.
    material = {key: pair[key] for key in ("version", "policy", "requests", "market_context")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate_pair(pair):
    single.require(isinstance(pair, dict) and type(pair["version"]) is int and pair["version"] == 1,
                   "Invalid pair journal version")
    single.require(pair["policy"] in {VERIFIED_POLICY, CONDITIONAL_POLICY},
                   "Unsupported pair settlement or execution policy")
    single.require(pair["state"] in STATES and pair["approval"] == approval_hash(pair),
                   "Invalid pair state or approval")
    if "starting_cash" in pair:
        single.require(single.is_finite_number(pair["starting_cash"]) and pair["starting_cash"] >= 0,
                       "Invalid saved pre-submission cash")
    materials = pair["requests"]
    single.require(isinstance(materials, list) and len(materials) == 2, "Exactly two saved legs are required")
    for material in materials:
        single.require(isinstance(material, dict) and set(material) == {"market_id", "tournament_slug", "request"},
                       "Invalid pair request material")
        # Reuse the stricter single-order schema without inventing fake order IDs.
        intent = dict(material, version=1, created_at=pair["created_at"], state="PREPARED",
                      order_id=None, observation=None)
        intent["approval"] = single.approval_hash(intent)
        single.validate_intent(intent)
        single.require(material["request"]["side"] == "no", "This paired test supports NO buys only")
    bodies = [material["request"] for material in materials]
    single.require(bodies[0]["exchangeId"] != bodies[1]["exchangeId"]
                   and materials[0]["market_id"] != materials[1]["market_id"]
                   and materials[0]["tournament_slug"] == materials[1]["tournament_slug"]
                   and bodies[0]["tournamentId"] == bodies[1]["tournamentId"]
                   and bodies[0]["idempotencyKey"] != bodies[1]["idempotencyKey"],
                   "Pair legs must have different identities and keys but share scope")
    cost = sum(Decimal(str(body["price"])) for body in bodies)
    single.require(Decimal(1) - cost >= Decimal(str(MIN_EDGE)), "Pair limits fail the minimum edge policy")
    context = pair["market_context"]
    if pair["policy"] == CONDITIONAL_POLICY:
        validate_conditional_context(context)
        single.require(cost <= CONDITIONAL_SPEND_CAP, "Conditional pair exceeds its 0.970 contract-cost cap")
        single.require(all(material["tournament_slug"] == "midterm-elections" for material in materials),
                       "Conditional test is limited to the midterm competition")
    else:
        # Conditional observations never go through the paper strategy's
        # evidence checker as if they were verified normal-payout evidence.
        validate_market_context({"market_context": context, "position_type": "NO-PAIR", "cost_per_pair": float(cost)})
    single.require(context["market_ids"] == [material["market_id"] for material in materials]
                   and context["exchange_ids"] == [body["exchangeId"] for body in bodies]
                   and context["tournament_id"] == bodies[0]["tournamentId"]
                   and context["leg_prices"] == [body["price"] for body in bodies],
                   "Pair bodies disagree with saved settlement or price evidence")


def validate_conditional_context(context):
    """Validate the accepted conditional policy without inventing a relationship."""
    single.require(isinstance(context, dict) and set(context) == {
        "mode", "tournament_id", "market_ids", "exchange_ids", "race_key",
        "settlement_fingerprint", "relationship_verified", "assumptions", "leg_prices", "book_versions"},
        "Invalid conditional evidence fields")
    single.require(context["mode"] == "conditional-ri-no-pair"
                   and context["market_ids"] == CONDITIONAL_MARKETS
                   and context["exchange_ids"] == CONDITIONAL_EXCHANGES
                   and context["race_key"] == CONDITIONAL_RACE
                   and context["assumptions"] == CONDITIONAL_ASSUMPTIONS
                   and type(context["relationship_verified"]) is bool,
                   "Conditional policy is limited to the reviewed Rhode Island pair and assumptions")
    single.require(isinstance(context["settlement_fingerprint"], str)
                   and re.fullmatch(r"[0-9a-f]{64}", context["settlement_fingerprint"]),
                   "Conditional observations have no valid fingerprint")
    versions = context["book_versions"]
    single.require(isinstance(versions, list) and len(versions) == 2, "Both book versions are required")
    for version in versions:
        single.require(isinstance(version, dict) and type(version["sequence"]) is int
                       and version["sequence"] >= 0, "Invalid saved conditional book version")
        single.parse_api_timestamp(version["at"])


def conditional_preview(session, account, democrat_id, republican_id, started):
    """Turn read-only observations into explicitly conditional preparation data.

    This function creates no order IDs or execution keys and makes only GETs.
    The caller later saves fresh keys and requires exact approval to submit.
    """
    proposal = conditional_proposal(session, account, democrat_id, republican_id, started)
    single.require(Decimal(str(proposal["max_new_spend"])) <= CONDITIONAL_SPEND_CAP,
                   "Conditional pair exceeds its 0.970 contract-cost cap")
    context = {"mode": "conditional-ri-no-pair", "tournament_id": proposal["tournament_id"],
               "market_ids": [leg["market_id"] for leg in proposal["legs"]],
               "exchange_ids": [leg["exchange_id"] for leg in proposal["legs"]],
               "race_key": proposal["race_key"], "settlement_fingerprint": proposal["evidence_fingerprint"],
               "relationship_verified": proposal["relationship_verified"],
               "assumptions": list(CONDITIONAL_ASSUMPTIONS),
               "leg_prices": [leg["price"] for leg in proposal["legs"]],
               "book_versions": copy.deepcopy(proposal["book_versions"])}
    validate_conditional_context(context)
    legs = [{"exchangeId": leg["exchange_id"], "side": "no", "action": "buy", "quantity": 1,
             "price": leg["price"], "tournamentId": context["tournament_id"]} for leg in proposal["legs"]]
    return {"market_context": context, "request": {"legs": legs}}


def save_pair(pair, directory):
    """Save the controller intent before writes; every leg has its own journal."""
    temporary = None
    try:
        quarantine.require_writable_path(directory)
        validate_pair(pair)
        directory = Path(directory)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".pair.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(pair, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "pair.json")
        single.sync_directory(directory)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise single.TestError("Cannot save pair journal; stop without further account writes") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def load_pair(directory):
    try:
        with (Path(directory) / "pair.json").open(encoding="utf-8") as stream:
            pair = json.load(stream)
        validate_pair(pair)
        legs = [single.load_intent(leg_path(directory, index)) for index in range(2)]
        for material, intent in zip(pair["requests"], legs):
            single.require(material == {key: intent[key] for key in material},
                           "A leg changed after the pair was approved")
        if pair["state"] == "PREPARED":
            single.require(all(leg["state"] == "PREPARED" for leg in legs), "A prepared pair already has an attempted leg")
        if pair["state"] in {"COMPLETE", "UNMATCHED", "CLOSED_NO_FILL"}:
            single.require(classify(legs) == pair["state"], "Pair terminal state conflicts with the leg journals")
        return pair, legs
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise single.TestError("Cannot restore pair journals; never reset an unresolved pair") from error


@contextmanager
def operation_lock(directory):
    """Exclusive file prevents two processes from submitting the same pair.

    A killed process can leave the lock behind. We do not guess that it is stale
    or remove it automatically; confirm that the process stopped before recovery.
    """
    path = Path(directory) / ".operation.lock"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        raise single.TestError("Pair is locked or unavailable; inspect it before another operation") from error
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        # A cleanup failure leaves a lock and stops subsequent writes safely.
        try:
            path.unlink()
        except OSError:
            pass


def set_state(pair, directory, state):
    proposed = dict(pair, state=state)
    save_pair(proposed, directory)
    pair.update(proposed)


def prepare_pair(session, directory, democrat_id, republican_id, slug="midterm-elections", conditional=False):
    quarantine.require_writable_path(directory)
    quarantine.require_unblocked_markets([democrat_id, republican_id])
    single.require(not Path(directory).exists(), "Pair directory already exists; preserve every prior attempt")
    single.require(not single.STATE_PATH.exists(), "A single-order test journal exists; resolve it before preparing a pair")
    single.require(type(conditional) is bool, "Preparation policy must be explicit")
    if conditional:
        single.require([str(democrat_id), str(republican_id)] == CONDITIONAL_MARKETS
                       and slug == "midterm-elections", "Conditional preparation is limited to markets 387/388")
    started = time.monotonic()
    account = read_account(session, slug)
    if conditional:
        preview = conditional_preview(session, account, democrat_id, republican_id, started)
    else:
        preview = preview_pair(session, account, democrat_id, republican_id, "NO-PAIR", started, quantity=1)
    now = datetime.now(timezone.utc)
    expiry = (now + timedelta(minutes=15)).isoformat(timespec="seconds")
    legs = []
    for market_id, leg in zip(preview["market_context"]["market_ids"], preview["request"]["legs"]):
        body = dict(leg, idempotencyKey="account-test-" + uuid4().hex, expirationDate=expiry)
        intent = {"version": 1, "market_id": market_id, "tournament_slug": slug,
                  "created_at": now.isoformat(timespec="seconds"), "request": body,
                  "state": "PREPARED", "order_id": None, "observation": None}
        intent["approval"] = single.approval_hash(intent)
        single.validate_intent(intent)
        legs.append(intent)
    policy = CONDITIONAL_POLICY if conditional else VERIFIED_POLICY
    pair = {"version": 1, "policy": policy, "state": "PREPARED",
            "created_at": now.isoformat(timespec="seconds"), "market_context": copy.deepcopy(preview["market_context"]),
            "requests": [{key: leg[key] for key in ("market_id", "tournament_slug", "request")} for leg in legs]}
    pair["approval"] = approval_hash(pair)
    validate_pair(pair)
    # Exclusive directory creation refuses replacement, including after a crash
    # during preparation. An incomplete directory must never become a new test.
    try:
        Path(directory).mkdir(mode=0o700)
        single.sync_directory(Path(directory).parent)
    except OSError as error:
        raise single.TestError("Cannot create a new pair directory; nothing submitted") from error
    for index, leg in enumerate(legs):
        single.save_intent(leg, leg_path(directory, index), new=True)
    save_pair(pair, directory)
    return pair, legs


def require_approval(pair, approval):
    single.require(isinstance(approval, str) and approval == pair["approval"], "Exact saved pair approval is required")


def preflight_pair(session, pair):
    """Recheck both instruments, settlement fingerprint, cash and approved spend."""
    started = time.monotonic()
    materials = pair["requests"]
    quarantine.require_unblocked_markets([material["market_id"] for material in materials])
    account = read_account(session, materials[0]["tournament_slug"])
    if pair["policy"] == CONDITIONAL_POLICY:
        preview = conditional_preview(session, account, materials[0]["market_id"], materials[1]["market_id"], started)
    else:
        preview = preview_pair(session, account, materials[0]["market_id"], materials[1]["market_id"],
                               "NO-PAIR", started, quantity=1)
    current = preview["market_context"]
    original = pair["market_context"]
    single.require(all(current[key] == original[key] for key in
                       ("tournament_id", "market_ids", "exchange_ids", "settlement_fingerprint")),
                   "Pair identity or settlement evidence changed")
    if pair["policy"] == CONDITIONAL_POLICY:
        single.require(all(current[key] == original[key] for key in
                           ("mode", "race_key", "relationship_verified", "assumptions")),
                       "Conditional settlement policy or observations changed")
    for material, draft in zip(materials, preview["request"]["legs"]):
        single.require(draft["price"] <= material["request"]["price"], "A quote exceeds its approved limit")
        single.require(single.parse_api_timestamp(material["request"]["expirationDate"]).timestamp() > time.time(),
                       "An approved pair order expired")
    # Use the approved maximum, even if today's quote is cheaper.
    risk = account_risk(account, original["exchange_ids"])
    single.check_risk(risk, float(sum(Decimal(str(material["request"]["price"])) for material in materials)))
    return account["tournament"]["myBalance"]


def filled_quantity(leg):
    observation = leg["observation"] or {}
    return observation.get("filled_quantity", observation.get("placement_quantity", 0))


def classify(legs):
    if any(leg["state"] in {"SUBMITTING", "UNKNOWN", "CANCEL_UNKNOWN", "CANCEL_REQUESTED"} for leg in legs):
        return "UNKNOWN"
    if any((leg["observation"] or {}).get("open") is True for leg in legs):
        return "RESTING"
    if any(leg["state"] == "ACCEPTED" for leg in legs):
        return "UNKNOWN"
    quantities = [filled_quantity(leg) for leg in legs]
    if all(math.isclose(quantity, 1, rel_tol=0, abs_tol=1e-9) for quantity in quantities):
        return "COMPLETE"
    return "UNMATCHED" if any(quantities) else "CLOSED_NO_FILL"


def reconcile_pair_account(session, pair, legs):
    """Match confirmed fills to actual inventory and cash before proceeding.

    Preparation forbids holdings/orders on either exchange, so these fills must
    account for all selected inventory. Unrelated trading or lagging reporting
    can break the cash comparison; stop rather than crediting guessed proceeds.
    """
    single.require(all(leg["state"] in {"PREPARED", "NOOP", "OBSERVED_TERMINAL"} for leg in legs),
                   "Unconfirmed orders prevent exact account reconciliation")
    single.require("starting_cash" in pair, "No saved cash baseline; reconcile this legacy attempt manually")
    started = time.monotonic()
    account = read_account(session, pair["requests"][0]["tournament_slug"])
    single.require(account["tournament"]["id"] == pair["market_context"]["tournament_id"],
                   "Reconciliation account scope changed")
    positions = {numeric_id(row["exchangeId"]): row for row in account["positions"]}
    total_cost = 0
    for leg in legs:
        exchange_id = leg["request"]["exchangeId"]
        quantity = filled_quantity(leg)
        cost = (leg["observation"] or {}).get("filled_cost", 0)
        holding = positions.get(exchange_id)
        single.require(holding is None or (numeric_id(holding["marketId"]) == leg["market_id"]
                                           and holding["settled"] is False),
                       "Selected holding identity or settlement changed")
        single.require(math.isclose(holding["quantity"] if holding else 0, -quantity, rel_tol=0, abs_tol=1e-9)
                       and math.isclose(holding["costBasis"] if holding else 0, cost, rel_tol=0, abs_tol=1e-8),
                       "Account inventory or cost disagrees with confirmed NO fills")
        single.require(not any(numeric_id(order["exchangeId"]) == exchange_id for order in account["orders"]),
                       "A selected exchange still has an open order")
        total_cost += cost
    single.require(math.isclose(account["tournament"]["myBalance"], pair["starting_cash"] - total_cost,
                                rel_tol=0, abs_tol=1e-8),
                   "Account cash disagrees with confirmed spend; inspect other activity or reporting")
    single.require(0 <= time.monotonic() - started <= 15, "Account reconciliation became stale")


def recheck_second_leg_settlement(session, pair):
    """A full first fill does not authorize buying after settlement rules change."""
    original = pair["market_context"]
    markets = read_selected_pair(session, original["tournament_id"], *original["market_ids"])
    single.require([get_exchange_id(market) for market in markets] == original["exchange_ids"],
                   "Pair exchange identity changed after the first leg")
    try:
        allowed, current = get_pair_rules(session, *markets, original["tournament_id"])
        single.require("NO-PAIR" in allowed, "Second leg has no verified normal-payout coverage")
        fingerprint = current["settlement_fingerprint"]
    except DataValidationError as error:
        if (pair["policy"] != CONDITIONAL_POLICY
                or str(error) != "No active relationship verifies this pair's normal payout"):
            raise
        # Reproduce the existing conditional observation fingerprint. Missing
        # evidence remains an accepted assumption, never a verified guarantee.
        rules = [get_election_rule(session, market, party, original["tournament_id"])
                 for market, party in zip(markets, ("Democratic", "Republican"))]
        fingerprint = hashlib.sha256(json.dumps(
            ["conditional-rule-analysis", rules], sort_keys=True, allow_nan=False
        ).encode()).hexdigest()
    single.require(fingerprint == original["settlement_fingerprint"],
                   "Settlement evidence changed after the first leg; second leg blocked")


def record_pair_outcome(session, pair, legs, directory):
    outcome = classify(legs)
    if outcome in {"COMPLETE", "UNMATCHED", "CLOSED_NO_FILL"}:
        reconcile_pair_account(session, pair, legs)
    set_state(pair, directory, outcome)


def submit_pair(session, directory, approval):
    quarantine.require_writable_path(directory)
    with operation_lock(directory):
        pair, legs = load_pair(directory)
        require_approval(pair, approval)
        single.require(pair["state"] == "PREPARED", "This pair was already attempted; do not replay or replace it")
        single.require(not single.STATE_PATH.exists(), "A single-order journal exists; resolve it before this pair")
        pair["starting_cash"] = preflight_pair(session, pair)
        set_state(pair, directory, "EXECUTING")
        try:
            for index, leg in enumerate(legs):
                if index == 1:
                    recheck_second_leg_settlement(session, pair)
                    reconcile_pair_account(session, pair, legs)
                path = leg_path(directory, index)
                single.submit_test(session, leg, path, leg["approval"])
                if leg["order_id"] is not None:
                    single.check_test(session, leg, path)
                    # The approval covers cancelling only these known remaining
                    # orders. It never authorizes selling an acquired share.
                    if (leg["observation"] or {}).get("open"):
                        single.cancel_test(session, leg, path, leg["approval"])
                if leg["state"] != "OBSERVED_TERMINAL" or not math.isclose(filled_quantity(leg), 1, rel_tol=0, abs_tol=1e-9):
                    # Do not submit leg two unless leg one is definitely full.
                    # A late full fill during cancellation can still pass here.
                    break
            record_pair_outcome(session, pair, legs, directory)
        except API_ERRORS as error:
            # A leg save may have failed after an accepted order: do not rely on
            # the in-memory ticket or infer that an exception means no execution.
            set_state(pair, directory, "UNKNOWN")
            raise single.TestError("Pair stopped with an unresolved outcome; restore journals and check known orders") from error
        return load_pair(directory)


def check_pair(session, directory):
    quarantine.require_writable_path(directory)
    with operation_lock(directory):
        pair, legs = load_pair(directory)
        if pair["state"] == "PREPARED":
            return pair, legs
        try:
            for index, leg in enumerate(legs):
                if leg["order_id"] is not None:
                    single.check_test(session, leg, leg_path(directory, index))
            record_pair_outcome(session, pair, legs, directory)
        except API_ERRORS as error:
            set_state(pair, directory, "UNKNOWN")
            raise single.TestError("Pair reporting is incomplete; preserve both leg journals") from error
        return load_pair(directory)


def diagnose_pair(session, directory):
    """Read account and recent trades without modifying any execution journal.

    The documented audit trail is newest first. We validate that ordering and
    page coverage before stopping at the preparation-time boundary. A trade
    event is evidence of account activity, not a receipt for our particular key.
    Empty history must never release an uncertain order for another submission.
    """
    pair, legs = load_pair(directory)
    account = read_account(session, pair["requests"][0]["tournament_slug"])
    tournament_id = pair["requests"][0]["request"]["tournamentId"]
    single.require(account["tournament"]["id"] == tournament_id, "Diagnostic account scope changed")
    since = single.parse_api_timestamp(pair["created_at"])
    slug = pair["requests"][0]["tournament_slug"]
    params = {"type": "trade", "limit": 200}
    seen_cursors, seen_ids, recent = set(), set(), []
    previous_time = None
    complete = False
    for _ in range(50):
        payload = fetch_json(session, f"{API_BASE_URL}/tournaments/{slug}/portfolio/transactions", params=params.copy())
        single.require(payload["coverage"]["complete"] is True and isinstance(payload["data"], list),
                       "Trade audit coverage is incomplete; outcome remains unknown")
        reached_boundary = False
        for row in payload["data"]:
            event_id = row["event_id"]
            single.require(isinstance(event_id, str) and event_id and event_id not in seen_ids
                           and row["tournamentId"] == tournament_id and row["event_type"] == "trade",
                           "Trade audit has duplicate events or unexpected scope")
            seen_ids.add(event_id)
            at = single.parse_api_timestamp(row["createdAt"])
            single.require(previous_time is None or at <= previous_time, "Trade audit ordering changed")
            previous_time = at
            if at < since:
                reached_boundary = True
                continue
            exchange_id = numeric_id(row["exchangeId"])
            single.require(single.is_finite_number(row["quantity"])
                           and single.is_finite_number(row["price"]) and 0 <= row["price"] <= 1,
                           "Trade audit contains invalid amounts")
            if exchange_id in pair["market_context"]["exchange_ids"]:
                # Print only numeric amounts/validated IDs and our timestamp.
                # Descriptions, titles and other arbitrary server text are omitted.
                recent.append({"exchange_id": exchange_id, "quantity": row["quantity"],
                               "price": row["price"], "at": at.isoformat()})
        pagination = payload["pagination"]
        single.require(type(pagination["hasMore"]) is bool, "Trade audit pagination is invalid")
        if reached_boundary or not pagination["hasMore"]:
            complete = True
            break
        cursor = pagination["nextCursor"]
        single.require(isinstance(cursor, str) and cursor and cursor not in seen_cursors,
                       "Trade audit cursor is invalid or repeated")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
    single.require(complete, "Trade audit page limit reached; outcome remains unknown")
    print(f"GET-ONLY DIAGNOSIS | saved controller {pair['state']}")
    print(f"Current account: {sum(position['quantity'] != 0 for position in account['positions'])} nonzero holdings | "
          f"{len(account['orders'])} open orders")
    print(f"Selected-exchange trade events since preparation: {len(recent)} | recent audit coverage complete")
    for row in recent:
        print(json.dumps(row, allow_nan=False))
    for leg in legs:
        single.print_placement_diagnostic(leg)
    print("History does not identify the original execution key. Empty results cannot prove non-execution.")
    print("No orders submitted, cancelled or replayed. Execution journals were not changed.")
    return pair, legs


def cancel_pair(session, directory, approval):
    quarantine.require_writable_path(directory)
    with operation_lock(directory):
        pair, legs = load_pair(directory)
        require_approval(pair, approval)
        single.require(pair["state"] != "PREPARED", "No pair orders have been attempted")
        set_state(pair, directory, "CANCELLING")
        try:
            for index, leg in enumerate(legs):
                if leg["order_id"] is not None:
                    single.cancel_test(session, leg, leg_path(directory, index), leg["approval"])
            record_pair_outcome(session, pair, legs, directory)
        except API_ERRORS as error:
            set_state(pair, directory, "UNKNOWN")
            raise single.TestError("Pair cancellation is unresolved; held shares remain held") from error
        return load_pair(directory)


def print_pair(pair, legs):
    quarantine.print_quarantine(pair["market_context"]["tournament_id"])
    maximum = sum(Decimal(str(material["request"]["price"])) for material in pair["requests"])
    print(f"SUPERVISED ONE-PAIR TEST | {pair['state']} | maximum contract spend {maximum:.3f} SUSQies; fees unverified")
    if pair["policy"] == CONDITIONAL_POLICY:
        print("CONDITIONAL SETTLEMENT POLICY | not a verified arbitrage")
        evidence = "present" if pair["market_context"]["relationship_verified"] else "MISSING"
        print(f"Engine ordinary-payout relationship at preparation: {evidence}")
        for assumption in pair["market_context"]["assumptions"]:
            print(assumption)
        print(f"Conditional gain if both fill at their limits and exactly one selected party wins: {Decimal(1) - maximum:.3f}")
        print(f"Both NO shares losing can lose {maximum:.3f} SUSQies before unverified fees.")
    else:
        print("Saved normal-payout evidence was verified at preparation and is rechecked before submission.")
    print("Exceptional refunds and execution remain risks.")
    print("Approval covers up to two one-share BUY NO orders, plus cancellation of their unfilled remainders.")
    print("First leg must be fully confirmed before second. Held shares are never automatically sold or refunded.")
    for index, leg in enumerate(legs):
        observation = leg["observation"] or {}
        cost = observation.get("filled_cost", observation.get("placement_cost", 0))
        print(f"Leg {index + 1} | {leg['state']} | order {leg['order_id']} | "
              f"confirmed acquired shares {filled_quantity(leg)} | confirmed contract cost {cost} SUSQies")
        if leg["state"] not in {"PREPARED", "NOOP", "OBSERVED_TERMINAL"}:
            print(f"Execution is unresolved: total exposure may reach 1 NO share and "
                  f"{leg['request']['price']:.3f} SUSQies contract cost. Confirmed zero is not proof of no fill.")
        single.print_placement_diagnostic(leg)
        print(json.dumps(leg["request"], indent=2, allow_nan=False))
    print(f"Pair approval fingerprint: {pair['approval']}")
    if pair["state"] == "UNMATCHED":
        print("Unmatched exposure remains. Inspect the account; cancellation cannot undo acquired shares.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "show", "submit", "check", "diagnose", "cancel", "quarantine"))
    parser.add_argument("--dem-market")
    parser.add_argument("--rep-market")
    parser.add_argument("--tournament", default="midterm-elections")
    parser.add_argument("--approve", help="Full saved pair fingerprint for submission or cancellation")
    parser.add_argument("--conditional", action="store_true",
                        help="Preparation only: accept conditional settlement risks for the capped Rhode Island pair")
    args = parser.parse_args()
    if args.conditional and args.action != "prepare":
        parser.error("--conditional is a preparation policy; submission uses the saved policy and exact approval")
    if args.action == "prepare" and (args.dem_market is None or args.rep_market is None):
        parser.error("Preparation requires both market IDs")
    try:
        if args.action == "quarantine":
            quarantine.create_quarantine()
            quarantine.print_quarantine()
            print("Local quarantine saved. Original journals unchanged; no API requests made.")
            return 0
        # The original pair stays frozen. Only one fixed additional active pair
        # is permitted, and only while its quarantine evidence verifies intact.
        args.state = quarantine.active_pair_directory(STATE_DIR)
        quarantine.print_quarantine()
        if args.action == "diagnose" and quarantine.load_quarantine():
            args.state = STATE_DIR  # This command is GET-only and changes no journal.
        if args.action == "show" and not args.state.exists() and quarantine.load_quarantine():
            print("No active pair prepared. Unrelated verified pairs may undergo readiness checks.")
            return 0
        if args.action == "prepare":
            single.require(not args.state.exists(), "Pair directory already exists; preserve it")
        else:
            pair, legs = load_pair(args.state)
            if args.action == "show":
                print_pair(pair, legs)
                return 0
            if args.action in {"submit", "cancel"}:
                require_approval(pair, args.approve)
        load_dotenv(Path(__file__).resolve().with_name(".env"))
        api_key = os.getenv("SIG_API_KEY")
        single.require(bool(api_key), "Set SIG_API_KEY locally before using the paired test")
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {api_key}"})
            if args.action == "prepare":
                pair, legs = prepare_pair(session, args.state, args.dem_market, args.rep_market,
                                          args.tournament, conditional=args.conditional)
            elif args.action == "submit":
                pair, legs = submit_pair(session, args.state, args.approve)
            elif args.action == "check":
                pair, legs = check_pair(session, args.state)
            elif args.action == "diagnose":
                diagnose_pair(session, args.state)
                return 0
            else:
                pair, legs = cancel_pair(session, args.state, args.approve)
        print_pair(pair, legs)
        return 0
    except single.TestError as error:
        print(f"Paired test stopped: {error}")
    except PreviewBlocked as error:
        print(f"Paired preparation or preflight blocked: {error}. No new account orders submitted.")
    except API_ERRORS as error:
        print(f"Paired test stopped: {account_error_reason(error)}. No automatic retry.")
    except OSError:
        print("Paired test journal is unavailable; preserve it and inspect the account.")
    except KeyboardInterrupt:
        print("Paired test interrupted. Restore all journals before further account writes.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
