"""Offline two-leg execution exercises. No API, network, or credentials are used."""
import argparse
import copy
import json
import math
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from paper_trader import STARTING_BALANCE, MAX_CAPITAL_PER_TRADE, MAX_CAPITAL_PER_RACE, is_finite_number

MAX_QUANTITY = 100
MIN_EDGE = .02
VERSION = 1
PENDING = {"planned", "submitting", "open", "partial", "unknown", "cancel_pending"}
TERMINAL = {"filled", "cancelled", "rejected", "skipped"}
SCENARIOS = ("complete", "partial", "second-leg-failure", "cancellation-race",
             "lost-submit-ack", "cancellation-timeout", "restart", "venue-unavailable")


class SimulationError(Exception):
    """Halt the exercise; discard controller objects and restore before reuse."""


class SimulatedReject(Exception):
    """The fake venue definitely rejected an operation."""


class SimulatedTimeout(Exception):
    """An absent reply does not tell us whether an operation happened."""


class SimulatedCrash(Exception):
    """Stop after a venue change, before the controller receives its reply."""


def _check(condition, message):
    if not condition:
        raise SimulationError(message)


def _price(value):
    _check(is_finite_number(value) and 0 < value < 1, "Invalid simulated share price")


def _quantity(value, allow_zero=False):
    _check(type(value) is int and (0 if allow_zero else 1) <= value <= MAX_QUANTITY,
           "Quantity must be an integer within the simulation cap")


def create_journal(starting_cash=STARTING_BALANCE):
    return {"version": VERSION, "starting_cash": starting_cash, "cash": starting_cash,
            "reconciled": False, "attempts": []}


def create_venue(starting_cash=STARTING_BALANCE):
    return {"version": VERSION, "starting_cash": starting_cash, "cash": starting_cash, "orders": {}}


def pending_exposure(journal):
    """Reserve the maximum additional spend locally, even when a reply is lost.

    This is our conservative planning reserve, not a claim that Cup orders
    reserve cash. Known fill costs have already been deducted from journal cash.
    """
    return sum((attempt["target_quantity"] - leg["filled_quantity"]) * leg["limit_price"]
               for attempt in journal["attempts"] for leg in attempt["legs"] if leg["status"] in PENDING)


def pair_status(attempt):
    legs = attempt["legs"]
    if any(leg["status"] in {"submitting", "unknown", "cancel_pending"} for leg in legs):
        return "UNKNOWN"
    quantities = [leg["filled_quantity"] for leg in legs]
    if quantities == [attempt["target_quantity"]] * 2:
        return "COMPLETE"
    if all(leg["status"] in TERMINAL for leg in legs):
        if quantities[0] != quantities[1]:
            return "UNHEDGED"
        return "PARTIAL_PAIR" if quantities[0] else "ABORTED"
    if attempt["aborting"]:
        return "CANCELLING"
    return "PREPARED" if all(leg["status"] == "planned" for leg in legs) else "WORKING"


def _attempt(journal, attempt_id):
    matches = [attempt for attempt in journal["attempts"] if attempt["id"] == attempt_id]
    _check(len(matches) == 1, "Unknown simulation attempt")
    return matches[0]


def _risk(attempt):
    return sum(leg["filled_cost"] + (attempt["target_quantity"] - leg["filled_quantity"])
               * leg["limit_price"] * (leg["status"] in PENDING) for leg in attempt["legs"])


def validate_journal(journal):
    """Check cash, intent, exposure, and identity before trusting a saved journal."""
    _check(isinstance(journal, dict) and type(journal["version"]) is int
           and journal["version"] == VERSION, "Invalid execution journal version")
    _check(is_finite_number(journal["starting_cash"]) and journal["starting_cash"] > 0
           and is_finite_number(journal["cash"]) and journal["cash"] >= -1e-6,
           "Invalid execution cash")
    _check(type(journal["reconciled"]) is bool and isinstance(journal["attempts"], list),
           "Invalid execution journal structure")
    ids, clients, duplicates = set(), set(), set()
    race_risk, instrument_risk = {}, {}
    unfinished = []
    spent = 0
    for attempt_index, attempt in enumerate(journal["attempts"]):
        _check(isinstance(attempt, dict) and isinstance(attempt["id"], str)
               and re.fullmatch(r"[0-9a-f]{32}", attempt["id"]) and attempt["id"] not in ids,
               "Invalid or repeated attempt ID")
        ids.add(attempt["id"])
        _check(isinstance(attempt["race"], str) and bool(attempt["race"].strip())
               and attempt["position_type"] in {"YES-PAIR", "NO-PAIR"}
               and type(attempt["aborting"]) is bool, "Invalid attempt metadata")
        _quantity(attempt["target_quantity"])
        _check(isinstance(attempt["legs"], list) and len(attempt["legs"]) == 2, "An attempt needs two legs")
        exchanges = []
        for index, leg in enumerate(attempt["legs"]):
            _check(isinstance(leg, dict) and leg["client_order_id"] == f"{attempt['id']}-{index}"
                   and leg["client_order_id"] not in clients, "Invalid or repeated client order key")
            clients.add(leg["client_order_id"])
            _check(isinstance(leg["exchange_id"], str) and bool(leg["exchange_id"].strip())
                   and leg["side"] == attempt["position_type"].split("-")[0]
                   and leg["status"] in PENDING | TERMINAL
                   and type(leg["cancel_requested"]) is bool, "Invalid leg metadata")
            exchanges.append(leg["exchange_id"])
            _price(leg["limit_price"])
            _quantity(leg["filled_quantity"], allow_zero=True)
            _check(leg["filled_quantity"] <= attempt["target_quantity"]
                   and is_finite_number(leg["filled_cost"]) and leg["filled_cost"] >= 0
                   and leg["filled_cost"] <= leg["filled_quantity"] * leg["limit_price"] + 1e-6,
                   "Invalid cumulative fill")
            _check((leg["filled_quantity"] == 0) == (leg["filled_cost"] == 0), "Fill quantity and cost disagree")
            if leg["status"] in {"planned", "submitting", "open", "rejected", "skipped"}:
                _check(leg["filled_quantity"] == 0, "Unfilled leg contains a fill")
            if leg["status"] == "filled":
                _check(leg["filled_quantity"] == attempt["target_quantity"], "Filled leg is incomplete")
            if leg["status"] == "partial":
                _check(0 < leg["filled_quantity"] < attempt["target_quantity"], "Invalid partial fill")
            if leg["cancel_requested"]:
                _check(attempt["aborting"], "Cancellation needs a durable abort intent")
            spent += leg["filled_cost"]
        _check(exchanges[0] != exchanges[1], "Pair legs must be different instruments")
        pair_cost = sum(leg["limit_price"] for leg in attempt["legs"])
        _check(pair_cost <= 1 - MIN_EDGE + 1e-12, "Synthetic pair fails the minimum edge")
        _check(attempt["target_quantity"] * pair_cost <= MAX_CAPITAL_PER_TRADE + 1e-6,
               "Trade capital limit exceeded")
        instruments = tuple(sorted(exchanges))
        exposure = _risk(attempt)
        # Check both race text and instruments so renaming cannot bypass limits.
        for mapping, key in ((race_risk, attempt["race"]), (instrument_risk, instruments)):
            mapping[key] = mapping.get(key, 0) + exposure
            _check(mapping[key] <= MAX_CAPITAL_PER_RACE + 1e-6, "Race capital limit exceeded")
        if exposure > 0:
            for key in ((attempt["race"], attempt["position_type"]), (instruments, attempt["position_type"])):
                _check(key not in duplicates, "Duplicate instrument or race position")
                duplicates.add(key)
        if pair_status(attempt) not in {"COMPLETE", "PARTIAL_PAIR", "ABORTED"}:
            unfinished.append(attempt_index)
    # Restore must enforce the same rule as preparing a new pair: an unfinished
    # or unhedged attempt blocks all later attempts, including in saved files.
    _check(not unfinished or unfinished == [len(journal["attempts"]) - 1],
           "An unfinished or unhedged pair blocks later attempts")
    _check(math.isclose(journal["cash"], journal["starting_cash"] - spent, abs_tol=1e-6, rel_tol=0),
           "Cash does not match known fills")
    _check(journal["cash"] - pending_exposure(journal) >= -1e-6, "Pending orders exceed cash")


def validate_venue(venue):
    """The fake venue is independently durable; fills are the cash ledger."""
    _check(isinstance(venue, dict) and type(venue["version"]) is int and venue["version"] == VERSION,
           "Invalid fake venue version")
    _check(is_finite_number(venue["starting_cash"]) and venue["starting_cash"] > 0
           and is_finite_number(venue["cash"]) and venue["cash"] >= -1e-6
           and isinstance(venue["orders"], dict), "Invalid fake venue structure")
    fill_ids, order_ids = set(), set()
    spent = 0
    for client_key, order in venue["orders"].items():
        _check(isinstance(order, dict) and order["client_order_id"] == client_key
               and isinstance(client_key, str) and re.fullmatch(r"[0-9a-f]{32}-[01]", client_key),
               "Invalid fake client key")
        _check(type(order["id"]) is int and order["id"] > 0 and order["id"] not in order_ids,
               "Invalid or repeated fake order ID")
        order_ids.add(order["id"])
        _check(isinstance(order["exchange_id"], str) and bool(order["exchange_id"].strip())
               and order["side"] in {"YES", "NO"} and order["status"] in {"open", "filled", "cancelled"}
               and isinstance(order["fills"], list), "Invalid fake order")
        _quantity(order["quantity"])
        _price(order["limit_price"])
        quantity = 0
        for fill in order["fills"]:
            _check(isinstance(fill, dict) and isinstance(fill["id"], str) and bool(fill["id"])
                   and fill["id"] not in fill_ids, "Invalid or repeated fill ID")
            fill_ids.add(fill["id"])
            _quantity(fill["quantity"])
            _price(fill["price"])
            _check(fill["price"] <= order["limit_price"] + 1e-12, "Fill exceeded its buy limit")
            quantity += fill["quantity"]
            spent += fill["quantity"] * fill["price"]
        _check(quantity <= order["quantity"] and (order["status"] == "filled") == (quantity == order["quantity"]),
               "Order status and fill quantity disagree")
    _check(math.isclose(venue["cash"], venue["starting_cash"] - spent, abs_tol=1e-6, rel_tol=0),
           "Fake venue cash does not match fills")


def _save(snapshot, path, validate):
    """Save atomically; failure never commits a proposed in-memory change."""
    temporary_path = None
    try:
        validate(snapshot)
        path = Path(path)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary_path = Path(stream.name)
            json.dump(snapshot, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise SimulationError("Cannot save simulation state; execution stopped") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _load(path, validate):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            snapshot = json.load(stream)
        validate(snapshot)
        return snapshot
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise SimulationError("Cannot restore simulation state; files left untouched") from error


def save_journal(journal, path):
    _save(journal, path, validate_journal)


def save_venue(venue, path):
    _save(venue, path, validate_venue)


def load_journal(path):
    return _load(path, validate_journal)


def load_venue(path):
    return _load(path, validate_venue)


def _commit(state, path, proposed, save):
    save(proposed, path)
    state.clear()
    state.update(proposed)


def prepare_pair(journal, journal_path, race, position_type, quantity, prices,
                 exchange_ids=("demo-dem", "demo-rep")):
    """Reserve both legs' worst-case cost and persist stable keys before sending."""
    validate_journal(journal)
    _check(not journal["attempts"] or journal["reconciled"], "Reconciliation is required before another pair")
    _check(all(pair_status(attempt) in {"COMPLETE", "PARTIAL_PAIR", "ABORTED"}
               for attempt in journal["attempts"]), "An unfinished or unhedged pair blocks new pairs")
    _check(isinstance(prices, (list, tuple)) and len(prices) == 2
           and isinstance(exchange_ids, (list, tuple)) and len(exchange_ids) == 2, "Two prices and instruments are required")
    _check(isinstance(position_type, str) and position_type in {"YES-PAIR", "NO-PAIR"}, "Invalid pair type")
    attempt_id = uuid4().hex
    attempt = {"id": attempt_id, "race": race, "position_type": position_type,
               "target_quantity": quantity, "aborting": False, "legs": []}
    for index in range(2):
        attempt["legs"].append({"client_order_id": f"{attempt_id}-{index}", "exchange_id": exchange_ids[index],
                                "side": position_type.split("-")[0], "limit_price": prices[index],
                                "status": "planned", "filled_quantity": 0, "filled_cost": 0,
                                "cancel_requested": False})
    proposed = copy.deepcopy(journal)
    proposed["attempts"].append(attempt)
    _commit(journal, journal_path, proposed, save_journal)
    return attempt_id


def fill_order(venue, venue_path, client_order_id, fill_id, quantity, price):
    """Apply an actual fake fill once; repeats never spend cash twice."""
    validate_venue(venue)
    _quantity(quantity)
    _price(price)
    _check(isinstance(fill_id, str) and bool(fill_id), "Invalid fill ID")
    _check(client_order_id in venue["orders"], "Cannot fill an unknown order")
    order = venue["orders"][client_order_id]
    matches = [fill for fill in order["fills"] if fill["id"] == fill_id]
    if matches:
        _check(matches[0] == {"id": fill_id, "quantity": quantity, "price": price}, "Conflicting duplicate fill")
        return False
    _check(order["status"] == "open", "Cannot fill a terminal order")
    proposed = copy.deepcopy(venue)
    changed = proposed["orders"][client_order_id]
    changed["fills"].append({"id": fill_id, "quantity": quantity, "price": price})
    if sum(fill["quantity"] for fill in changed["fills"]) == changed["quantity"]:
        changed["status"] = "filled"
    proposed["cash"] -= quantity * price
    _commit(venue, venue_path, proposed, save_venue)
    return True


def _same_intent(attempt, leg, order):
    return (order["client_order_id"] == leg["client_order_id"] and order["exchange_id"] == leg["exchange_id"]
            and order["side"] == leg["side"] and order["quantity"] == attempt["target_quantity"]
            and order["limit_price"] == leg["limit_price"])


def _submit_order(venue, venue_path, attempt, leg, script):
    """A fake idempotent venue; this is NOT an implementation of the Cup API."""
    key = leg["client_order_id"]
    if key in venue["orders"]:
        _check(_same_intent(attempt, leg, venue["orders"][key]), "Client key reused for a different intent")
        return
    if script.get("reject"):
        raise SimulatedReject()
    if script.get("timeout_before_accept"):
        raise SimulatedTimeout()
    proposed = copy.deepcopy(venue)
    proposed["orders"][key] = {"id": len(proposed["orders"]) + 1, "client_order_id": key,
                               "exchange_id": leg["exchange_id"], "side": leg["side"],
                               "quantity": attempt["target_quantity"], "limit_price": leg["limit_price"],
                               "status": "open", "fills": []}
    _commit(venue, venue_path, proposed, save_venue)
    if script.get("fill_quantity", 0):
        fill_order(venue, venue_path, key, key + ":initial", script["fill_quantity"],
                   script.get("fill_price", leg["limit_price"]))
    if script.get("crash_after_accept"):
        raise SimulatedCrash()
    if script.get("lose_ack"):
        raise SimulatedTimeout()


def reconcile(journal, journal_path, venue, attempt_id, available=True):
    """Replace cumulative observations rather than adding the same fills again.

    Our fake venue returns its entire authoritative history. An eventually
    consistent real API cannot be assumed to prove absence in this way.
    """
    validate_journal(journal)
    _attempt(journal, attempt_id)
    proposed = copy.deepcopy(journal)
    proposed["reconciled"] = False
    _commit(journal, journal_path, proposed, save_journal)
    if not available:
        raise SimulatedTimeout()
    validate_venue(venue)
    _check(venue["starting_cash"] == journal["starting_cash"], "Simulation accounts have different starting cash")
    known_keys = {leg["client_order_id"] for attempt in journal["attempts"] for leg in attempt["legs"]}
    _check(set(venue["orders"]).issubset(known_keys), "Orphan venue order has no durable intent")
    proposed = copy.deepcopy(journal)
    for attempt in proposed["attempts"]:
        for leg in attempt["legs"]:
            order = venue["orders"].get(leg["client_order_id"])
            if order is None:
                _check(leg["filled_quantity"] == 0 and leg["status"] not in {"open", "partial", "filled", "cancelled"},
                       "A previously observed order disappeared")
                if leg["status"] in {"submitting", "unknown", "cancel_pending"}:
                    leg["status"] = "skipped" if attempt["aborting"] else "planned"
                continue
            _check(_same_intent(attempt, leg, order) and leg["status"] not in {"rejected", "skipped"},
                   "Venue order conflicts with durable intent")
            # A terminal fake venue response is final. Restoring an older or
            # rewritten snapshot must never turn cancelled remainder into orders.
            if leg["status"] in {"filled", "cancelled"}:
                _check(order["status"] == leg["status"], "A terminal order changed status")
            quantity = sum(fill["quantity"] for fill in order["fills"])
            cost = sum(fill["quantity"] * fill["price"] for fill in order["fills"])
            if leg["status"] in {"filled", "cancelled"}:
                _check(quantity == leg["filled_quantity"], "A terminal order changed its fills")
            _check(quantity >= leg["filled_quantity"] and cost + 1e-6 >= leg["filled_cost"], "Observed fills went backwards")
            # Only newly filled shares can add cost. This also requires the cost
            # to stay unchanged when the cumulative quantity stays unchanged.
            new_quantity = quantity - leg["filled_quantity"]
            new_cost = cost - leg["filled_cost"]
            _check(new_cost <= new_quantity * leg["limit_price"] + 1e-6,
                   "Confirmed fill costs changed or new fills exceeded the limit")
            leg["filled_quantity"], leg["filled_cost"] = quantity, cost
            if order["status"] != "open":
                leg["status"] = order["status"]
            elif leg["cancel_requested"]:
                leg["status"] = "cancel_pending"
            else:
                leg["status"] = "partial" if quantity else "open"
    proposed["cash"] = proposed["starting_cash"] - sum(leg["filled_cost"]
        for attempt in proposed["attempts"] for leg in attempt["legs"])
    _check(math.isclose(proposed["cash"], venue["cash"], rel_tol=0, abs_tol=1e-6), "Venue and observed cash disagree")
    proposed["reconciled"] = True
    _commit(journal, journal_path, proposed, save_journal)
    return pair_status(_attempt(journal, attempt_id))


def submit_pair(journal, journal_path, venue, venue_path, attempt_id, scripts=None, available=True):
    """Send each saved intent only after reconciliation; uncertain A stops B."""
    scripts = [{}, {}] if scripts is None else scripts
    _check(isinstance(scripts, (list, tuple)) and len(scripts) == 2
           and all(isinstance(script, dict) for script in scripts), "Two fake submission scripts are required")
    reconcile(journal, journal_path, venue, attempt_id, available=available)
    if _attempt(journal, attempt_id)["aborting"]:
        return request_cancellation(journal, journal_path, venue, venue_path, attempt_id)
    for index in range(2):
        attempt = _attempt(journal, attempt_id)
        leg = attempt["legs"][index]
        if leg["status"] != "planned":
            continue
        proposed = copy.deepcopy(journal)
        _attempt(proposed, attempt_id)["legs"][index]["status"] = "submitting"
        proposed["reconciled"] = False
        _commit(journal, journal_path, proposed, save_journal)
        try:
            _submit_order(venue, venue_path, attempt, leg, scripts[index])
        except SimulatedReject:
            proposed = copy.deepcopy(journal)
            changed = _attempt(proposed, attempt_id)
            changed["legs"][index]["status"] = "rejected"
            changed["aborting"] = True
            _commit(journal, journal_path, proposed, save_journal)
            return request_cancellation(journal, journal_path, venue, venue_path, attempt_id)
        except SimulatedTimeout:
            proposed = copy.deepcopy(journal)
            _attempt(proposed, attempt_id)["legs"][index]["status"] = "unknown"
            _commit(journal, journal_path, proposed, save_journal)
            return "UNKNOWN"
        # A crash or disk failure propagates: never continue to the second leg.
        reconcile(journal, journal_path, venue, attempt_id)
    return pair_status(_attempt(journal, attempt_id))


def _cancel_order(venue, venue_path, key, script):
    order = venue["orders"][key]
    if order["status"] != "open":
        return
    if script.get("reject"):
        raise SimulatedReject()
    if script.get("timeout_before_cancel"):
        raise SimulatedTimeout()
    if script.get("late_fill_quantity", 0):
        fill_order(venue, venue_path, key, key + ":cancel-late", script["late_fill_quantity"],
                   script.get("late_fill_price", order["limit_price"]))
    proposed = copy.deepcopy(venue)
    if proposed["orders"][key]["status"] == "open":
        proposed["orders"][key]["status"] = "cancelled"
    _commit(venue, venue_path, proposed, save_venue)
    if script.get("lose_ack"):
        raise SimulatedTimeout()


def request_cancellation(journal, journal_path, venue, venue_path, attempt_id,
                         cancel_scripts=None, available=True):
    """Cancel remaining orders; already bought shares and their costs survive."""
    cancel_scripts = [{}, {}] if cancel_scripts is None else cancel_scripts
    _check(isinstance(cancel_scripts, (list, tuple)) and len(cancel_scripts) == 2
           and all(isinstance(script, dict) for script in cancel_scripts), "Two fake cancellation scripts are required")
    reconcile(journal, journal_path, venue, attempt_id, available=available)
    proposed = copy.deepcopy(journal)
    changed = _attempt(proposed, attempt_id)
    changed["aborting"] = True
    for leg in changed["legs"]:
        if leg["status"] == "planned":
            leg["status"] = "skipped"
    _commit(journal, journal_path, proposed, save_journal)
    uncertain = False
    for index in range(2):
        leg = _attempt(journal, attempt_id)["legs"][index]
        if leg["status"] in TERMINAL:
            continue
        proposed = copy.deepcopy(journal)
        changed = _attempt(proposed, attempt_id)["legs"][index]
        changed["cancel_requested"], changed["status"] = True, "cancel_pending"
        proposed["reconciled"] = False
        _commit(journal, journal_path, proposed, save_journal)
        try:
            _cancel_order(venue, venue_path, leg["client_order_id"], cancel_scripts[index])
        except (SimulatedReject, SimulatedTimeout):
            uncertain = True
            # Continue cancelling the other leg: this reduces possible exposure.
    if uncertain:
        return "UNKNOWN"
    return reconcile(journal, journal_path, venue, attempt_id)


def print_summary(journal, attempt_id):
    attempt = _attempt(journal, attempt_id)
    quantities = [leg["filled_quantity"] for leg in attempt["legs"]]
    print(f"Status: {pair_status(attempt)} | reconciled: {journal['reconciled']}")
    for index, leg in enumerate(attempt["legs"], 1):
        print(f"  Leg {index}: {leg['status']} | filled {leg['filled_quantity']}/{attempt['target_quantity']} "
              f"| cost {leg['filled_cost']:.2f}")
    print(f"Known cash: {journal['cash']:.2f} | possible additional spend: {pending_exposure(journal):.2f}")
    print(f"Matched pairs: {min(quantities)} | unmatched shares: {abs(quantities[0] - quantities[1])}")
    if pair_status(attempt) in {"COMPLETE", "PARTIAL_PAIR"} and journal["reconciled"]:
        projected = min(quantities) - sum(leg["filled_cost"] for leg in attempt["legs"])
        print(f"Synthetic projected profit at normal settlement: {projected:.2f}")
    else:
        print("No completed-pair profit claimed; unfinished or unmatched exposure blocks new pairs.")


def run_scenario(name, directory):
    """Use invented prices and fills, not a currently approved market."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    journal_path, venue_path = directory / "execution_state.json", directory / "simulated_venue.json"
    journal, venue = create_journal(), create_venue()
    save_journal(journal, journal_path)
    save_venue(venue, venue_path)
    attempt_id = prepare_pair(journal, journal_path, "Synthetic race", "YES-PAIR", 100, [.4, .5])
    print(f"\nSCENARIO: {name}")
    full = [{"fill_quantity": 100}, {"fill_quantity": 100}]
    if name == "complete":
        submit_pair(journal, journal_path, venue, venue_path, attempt_id, full)
    elif name == "partial":
        submit_pair(journal, journal_path, venue, venue_path, attempt_id,
                    [{"fill_quantity": 60}, {"fill_quantity": 40}])
        print_summary(journal, attempt_id)
        for index, remaining in ((0, 40), (1, 60)):
            leg = _attempt(journal, attempt_id)["legs"][index]
            fill_order(venue, venue_path, leg["client_order_id"], f"later-{index}", remaining, leg["limit_price"])
        reconcile(journal, journal_path, venue, attempt_id)
    elif name == "second-leg-failure":
        submit_pair(journal, journal_path, venue, venue_path, attempt_id,
                    [{"fill_quantity": 60}, {"reject": True}])
    elif name in {"cancellation-race", "cancellation-timeout"}:
        submit_pair(journal, journal_path, venue, venue_path, attempt_id,
                    [{"fill_quantity": 40}, {"fill_quantity": 40}])
        scripts = [{"late_fill_quantity": 20}, {}] if name == "cancellation-race" else [{"lose_ack": True}, {}]
        request_cancellation(journal, journal_path, venue, venue_path, attempt_id, scripts)
        if name == "cancellation-timeout":
            print_summary(journal, attempt_id)
            journal, venue = load_journal(journal_path), load_venue(venue_path)
            reconcile(journal, journal_path, venue, attempt_id)
    elif name in {"lost-submit-ack", "restart"}:
        script = {"fill_quantity": 100, "lose_ack": name == "lost-submit-ack", "crash_after_accept": name == "restart"}
        try:
            submit_pair(journal, journal_path, venue, venue_path, attempt_id, [script, {}])
        except SimulatedCrash:
            print("Simulated crash after the venue accepted leg 1.")
        print_summary(journal, attempt_id)
        journal, venue = load_journal(journal_path), load_venue(venue_path)
        submit_pair(journal, journal_path, venue, venue_path, attempt_id, [{}, {"fill_quantity": 100}])
        print(f"Accepted fake orders after recovery: {len(venue['orders'])} (one per leg)")
    elif name == "venue-unavailable":
        submit_pair(journal, journal_path, venue, venue_path, attempt_id, [{"fill_quantity": 60}, {"lose_ack": True}])
        try:
            reconcile(journal, journal_path, venue, attempt_id, available=False)
        except SimulatedTimeout:
            print("Fake venue unavailable; no new orders or assumptions about missing fills.")
    else:
        raise SimulationError("Unknown scenario")
    print_summary(journal, attempt_id)
    return journal, venue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all",) + SCENARIOS, default="all")
    parser.add_argument("--state-dir", type=Path, help="Keep exercises in a new directory instead of disposable temporary files")
    parser.add_argument("--resume", type=Path, help="Reconcile and resume one saved fake-venue exercise directory")
    args = parser.parse_args()
    if args.resume is not None and args.state_dir is not None:
        parser.error("Use --resume or --state-dir, not both")
    print("OFFLINE execution simulator. Synthetic orders only; no account, API key, or network access.")
    try:
        if args.resume is not None:
            journal_path = args.resume / "execution_state.json"
            venue_path = args.resume / "simulated_venue.json"
            journal, venue = load_journal(journal_path), load_venue(venue_path)
            _check(bool(journal["attempts"]), "Saved simulation has no attempt")
            attempt_id = journal["attempts"][-1]["id"]
            submit_pair(journal, journal_path, venue, venue_path, attempt_id)
            print_summary(journal, attempt_id)
        else:
            names = SCENARIOS if args.scenario == "all" else (args.scenario,)
            with tempfile.TemporaryDirectory(prefix="paper-execution-") as temporary:
                root = args.state_dir if args.state_dir is not None else Path(temporary)
                if args.state_dir is not None:
                    root.mkdir(parents=True, exist_ok=False)
                for name in names:
                    run_scenario(name, root / name)
                if args.state_dir is not None:
                    print(f"Saved simulation directories: {root}")
    except (SimulationError, SimulatedTimeout, SimulatedCrash, OSError) as error:
        print(f"Simulation stopped: {error or type(error).__name__}")
        return 1
    print("Existing paper portfolio and trade log were not used. No real orders were placed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
