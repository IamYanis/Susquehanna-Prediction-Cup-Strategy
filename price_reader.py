"""Repeated, paper-only scanner. Run with --once for a single scan."""
import argparse
import fcntl
import hashlib
import json
import re
import math
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import requests
import paper_trader
from dotenv import load_dotenv
from paper_trader import (
    PortfolioError,
    execute_paper_trade,
    load_portfolio,
    parse_api_timestamp,
    print_portfolio_summary,
)

# Keep the separate supervised live-order policy unchanged. Paper scanning uses
# the four levels below, rather than borrowing this live-order minimum.
MIN_EDGE = 0.02
WATCH_EDGE = Decimal("0.005")
PAPER_TRADE_EDGE = Decimal("0.010")
STRONG_PAPER_TRADE_EDGE = Decimal("0.020")
MIN_LIQUIDITY = 50
MAX_TRADE_QUANTITY = 100
SCAN_INTERVAL = 15
REQUEST_TIMEOUT = 10
# Compare with the last reported values so small changes can accumulate.
MATERIAL_EDGE_CHANGE = 0.01
MATERIAL_QUANTITY_CHANGE = 10
API_BASE_URL = "https://sig.thesuper.market/api/v1"
MARKETS_URL = f"{API_BASE_URL}/markets"
# The documented ordinary-account budget is 100 reads/minute across all keys.
# 0.75 seconds between starts uses at most about 80/minute in this process.
READ_REQUEST_SPACING = 0.75
# A broken server can return endlessly changing cursors. Never approve from a
# partial walk or spend the whole run on an unbounded discovery request.
MAX_DISCOVERY_PAGES = 50
_last_request_started = None
_read_cooldown_until = 0
APPROVED_SETTLEMENTS_PATH = Path(__file__).resolve().with_name("approved_settlements.json")


class RateLimitError(requests.RequestException):
    """End the current scan without treating unobserved races as disappeared."""


class DataValidationError(ValueError):
    """A short, fixed explanation that is safe to show without request details."""


class ScannerLockError(Exception):
    """The scanner cannot safely obtain exclusive use of its paper portfolio."""


@contextmanager
def scanner_lock():
    """Hold a non-blocking OS lock beside the shared paper portfolio."""
    lock_path = paper_trader.PORTFOLIO_PATH.with_name(".paper_scanner.lock")
    try:
        lock_file = lock_path.open("a")
    except OSError as error:
        raise ScannerLockError("Cannot open the paper scanner lock; scanner did not start.") from error
    # Closing this descriptor releases the OS lock on return, Ctrl+C or an
    # exception. The OS also releases it if the process is forcibly stopped.
    with lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ScannerLockError("Another paper scanner instance is already running; exiting.") from error
        except OSError as error:
            raise ScannerLockError("Cannot acquire the paper scanner lock; scanner did not start.") from error
        # Leave the empty file in place after exit. Removing it could let another
        # process lock a different file while a running scanner holds this one.
        yield


def fetch_json(session, url, params=None):
    """Use read-only requests to one API host, with pacing and safe failures."""
    global _last_request_started, _read_cooldown_until

    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc != "sig.thesuper.market"
            or not parsed.path.startswith("/api/v1/")):
        raise DataValidationError("Request is outside the SIG competition API")
    now = time.monotonic()
    if now < _read_cooldown_until:
        raise RateLimitError("API read cooldown is still active")
    if _last_request_started is not None:
        delay = READ_REQUEST_SPACING - (now - _last_request_started)
        if delay > 0:
            time.sleep(delay)
    _last_request_started = time.monotonic()
    # Refuse redirects so the key never follows an unexpected endpoint change.
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT, allow_redirects=False)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "60")
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 60
        if not math.isfinite(delay) or delay <= 0:
            delay = 60
        _read_cooldown_until = time.monotonic() + delay
        raise RateLimitError("API read rate limit reached")
    if 300 <= response.status_code < 400:
        raise DataValidationError("Unexpected API redirect")
    response.raise_for_status()
    return response.json()


def numeric_id(value):
    """Only documented positive numeric market/exchange IDs go into URLs."""
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise DataValidationError("Invalid market or exchange ID")
    return str(value)


def get_tournament(session, slug="midterm-elections"):
    """Resolve the competition slug to the UUID required by scoped API reads."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise DataValidationError("Invalid tournament slug")
    tournament = fetch_json(session, f"{API_BASE_URL}/tournaments/{slug}")
    if not isinstance(tournament["id"], str):
        raise DataValidationError("Invalid tournament ID")
    tournament_id = str(UUID(tournament["id"]))
    if (tournament["slug"] != slug or tournament["status"] != "active"
            or tournament["currencyName"] not in ("SUSQie", "SUSQies")):
        raise DataValidationError("Expected an active SUSQies competition")
    return tournament_id


def fetch_pages(session, url, params):
    """Read all cursor pages; an incomplete or looping response is unknown."""
    items = []
    seen_cursors, seen_ids = set(), set()
    params = params.copy()
    for _ in range(MAX_DISCOVERY_PAGES):
        payload = fetch_json(session, url, params=params)
        if not isinstance(payload, dict):
            raise DataValidationError("Invalid paginated API response")
        # Some engine-backed listings declare projection coverage. If they say
        # it is incomplete, even a final cursor page is not usable evidence.
        if "coverage" in payload:
            coverage = payload["coverage"]
            if not isinstance(coverage, dict) or coverage.get("complete") is not True:
                raise DataValidationError("Discovery coverage is incomplete")
        data, pagination = payload["data"], payload["pagination"]
        if not isinstance(data, list) or type(pagination["hasMore"]) is not bool:
            raise DataValidationError("Invalid paginated API response")
        for item in data:
            if not isinstance(item, dict):
                raise DataValidationError("Invalid discovery item")
            identity = item.get("id")
            if type(identity) not in (str, int) or not str(identity):
                raise DataValidationError("Discovery item has no valid identity")
            identity = str(identity).lower()
            if identity in seen_ids:
                # Conflicting active/inactive versions must not let the first
                # copy approve settlement from an inconsistent graph snapshot.
                raise DataValidationError("Discovery contains repeated identities")
            seen_ids.add(identity)
        items.extend(data)
        if not pagination["hasMore"]:
            return items
        cursor = pagination["nextCursor"]
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise DataValidationError("Missing or repeated API cursor")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
    raise DataValidationError("Discovery page limit reached; coverage is incomplete")


def check_context(contexts, tournament_id):
    """Explicit scoped reads must describe the same competition, not public books."""
    if (not isinstance(contexts, list) or len(contexts) != 1
            or contexts[0]["type"] != "tournament"
            or contexts[0]["tournament"]["id"] != tournament_id
            or contexts[0]["tournament"]["isOngoingPlay"] is not False):
        raise DataValidationError("Unexpected competition context")


def get_races(session, tournament_id):
    """Return title candidates AND every listed ID, including unmatched markets."""
    # Keep closed markets in discovery so a status change is distinguishable
    # from an incomplete page or an unverified pair.
    markets = fetch_pages(session, MARKETS_URL,
                          {"tournamentId": tournament_id, "status": "any", "limit": 100})
    parties = {"Democratic": {}, "Republican": {}}
    seen_ids = set()
    for market in markets:
        market_id = numeric_id(market["id"])
        title = market["title"]
        if not isinstance(title, str) or market_id in seen_ids:
            raise DataValidationError("Invalid or repeated market in discovery")
        seen_ids.add(market_id)
        check_context(market["contexts"], tournament_id)
        if market["status"] not in ("open", "closed", "settled"):
            raise DataValidationError("Unknown market lifecycle status")
        for party in parties:
            prefix = f"Will the {party} Party win the "
            if title.startswith(prefix):
                race = title[len(prefix):].removesuffix("?").strip()
                if not race or race in parties[party]:
                    raise DataValidationError("Ambiguous party market title")
                parties[party][race] = market
    races = {
        race: (market, parties["Republican"][race])
        for race, market in parties["Democratic"].items()
        if race in parties["Republican"]
    }
    return races, seen_ids


def get_exchange_id(market):
    """Restrict this strategy to a single binary exchange, never exchanges[0]."""
    exchanges = market["exchanges"]
    if (market["isComposite"] is not False or market["isMultiOutcome"] is not False
            or not isinstance(exchanges, list) or len(exchanges) != 1
            or exchanges[0]["option"] not in (None, "YES")):
        raise DataValidationError("Expected one simple binary YES exchange")
    return numeric_id(exchanges[0]["id"])


def get_election_rule(session, market, party, tournament_id):
    """Use structured resolution data, rejecting freeform/chamber contracts."""
    market_id = numeric_id(market["id"])
    payload = fetch_json(session, f"{API_BASE_URL}/markets/{market_id}/nodes",
                         params={"tournamentId": tournament_id})
    if numeric_id(payload["market_id"]) != market_id:
        raise DataValidationError("Wrong market resolution tree")
    check_context(payload["contexts"], tournament_id)
    root = payload["root"]
    if (root["node_type"] != "contract" or root["contract_type"] != "Election Outcome"
            or root["settled_with"] is not None):
        raise DataValidationError("Unreviewed or already settled contract type")
    details = root["contract_details"]
    if (details["resolutionType"] != "Party Winner" or details["raceStage"] != "General"
            or details["winnerName"] != f"{party} Party"):
        raise DataValidationError("Unreviewed election resolution rules")
    # The API passes these fields through rather than guaranteeing their shape.
    # Reject absent identifiers instead of falling back to the title.
    key = (numeric_id(details["raceId"]), numeric_id(details["stageId"]),
           details["electionDate"], details["raceStage"], details["resolutionType"])
    if not isinstance(key[2], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", key[2]):
        raise DataValidationError("Missing election date")
    datetime.strptime(key[2], "%Y-%m-%d")
    settlement_date = root["settlement_date"]
    if not isinstance(settlement_date, str):
        raise DataValidationError("Missing settlement date")
    parse_api_timestamp(settlement_date)
    return key, root


def get_pair_rules(session, democrat, republican, tournament_id, relationships=None):
    """Require the engine's active relationship, not a guessed payout from titles."""
    democrat_exchange = get_exchange_id(democrat)
    republican_exchange = get_exchange_id(republican)
    if democrat_exchange == republican_exchange or democrat["id"] == republican["id"]:
        raise DataValidationError("Pair legs must be different exchanges and markets")
    # A scan shares one complete relationship listing across all candidates.
    # Standalone calls can still fetch the evidence for this exchange alone.
    if relationships is None:
        relationships = fetch_pages(session, f"{API_BASE_URL}/relationships",
                                    {"tournamentId": tournament_id,
                                     "exchangeId": democrat_exchange, "limit": 200})
    wanted = {(democrat_exchange, numeric_id(democrat["id"])),
              (republican_exchange, numeric_id(republican["id"]))}
    evidence = []
    allowed = set()
    for relationship in relationships:
        if relationship["type"] != "mutually_exclusive" or relationship["status"] != "active":
            continue
        if (not isinstance(relationship["id"], str)
                or type(relationship["version"]) is not int or relationship["version"] <= 0
                or type(relationship["isExhaustive"]) is not bool
                or not isinstance(relationship["nodes"], list)):
            raise DataValidationError("Invalid relationship evidence")
        UUID(relationship["id"])
        members = set()
        for node in relationship["nodes"]:
            if node["outcome"] == "YES" and node["role"] == "member":
                members.add((numeric_id(node["exchangeId"]), numeric_id(node["marketId"])))
        if not wanted.issubset(members):
            continue
        allowed.add("NO-PAIR")
        # Exhaustiveness of a larger group does NOT prove that just D/R cover it.
        if (relationship["isExhaustive"] is True and members == wanted
                and len(relationship["nodes"]) == 2):
            allowed.add("YES-PAIR")
        evidence.append({"id": relationship["id"], "version": relationship["version"],
                         "isExhaustive": relationship["isExhaustive"],
                         "members": sorted(members)})
    if not allowed:
        raise DataValidationError("No active relationship verifies this pair's normal payout")
    # Read the resolution trees only when relationship evidence could approve a
    # pair. An empty engine graph therefore does not cost two calls per race.
    democrat_key, democrat_rule = get_election_rule(session, democrat, "Democratic", tournament_id)
    republican_key, republican_rule = get_election_rule(session, republican, "Republican", tournament_id)
    if (democrat_key != republican_key
            or democrat_rule["settlement_date"] != republican_rule["settlement_date"]):
        raise DataValidationError("Party contracts describe different races or stages")
    # Ignore changing indicative prices when fingerprinting settlement evidence.
    evidence.sort(key=lambda relationship: relationship["id"])
    fingerprint = hashlib.sha256(json.dumps(
        [democrat_rule, republican_rule, evidence], sort_keys=True
    ).encode()).hexdigest()
    context = {"tournament_id": tournament_id,
               "market_ids": [numeric_id(democrat["id"]), numeric_id(republican["id"])],
               "exchange_ids": [democrat_exchange, republican_exchange],
               "race_key": list(democrat_key),
               "settlement_fingerprint": fingerprint, "relationships": evidence,
               "payout_condition": "ordinary_binary_settlement; refunds are separate"}
    return allowed, context


def get_best_prices(session, market, tournament_id):
    """Read one explicitly scoped exchange and validate its identity and quote age."""
    exchange_id = get_exchange_id(market)
    book = fetch_json(session, f"{API_BASE_URL}/exchanges/{exchange_id}/orderbook",
                      params={"tournamentId": tournament_id, "depth": 1})
    received_at = time.monotonic()
    if (numeric_id(book["exchangeId"]) != exchange_id
            or numeric_id(book["marketId"]) != numeric_id(market["id"])):
        raise DataValidationError("Wrong market/exchange order book")
    bids, asks = book["bids"], book["asks"]
    if not isinstance(bids, list) or not isinstance(asks, list):
        raise DataValidationError("Invalid book sides")
    version = book["asOf"]
    if (not isinstance(version, dict) or type(version["sequence"]) is not int
            or version["sequence"] < 0 or not isinstance(version["at"], str)):
        raise DataValidationError("Book has no authoritative quote version")
    quoted_at = parse_api_timestamp(version["at"])
    if quoted_at.tzinfo is None or not -5 <= time.time() - quoted_at.timestamp() <= 5:
        raise DataValidationError("Book timestamp is stale or invalid")
    if not bids and not asks:
        return None
    result = {"received_at": received_at, "quoted_at": quoted_at.timestamp(), "version": version}
    for levels, name in ((bids, "bid"), (asks, "ask")):
        result[name], result[name + "_quantity"] = None, 0
        if not levels:
            continue
        level = levels[0]
        if isinstance(level["price"], bool) or isinstance(level["quantity"], bool):
            raise DataValidationError("Invalid book numbers")
        price, quantity = float(level["price"]), float(level["quantity"])
        if not math.isfinite(price) or not 0 <= price <= 1:
            raise DataValidationError("Invalid price")
        if not math.isfinite(quantity) or quantity < 0:
            raise DataValidationError("Invalid quantity")
        result[name], result[name + "_quantity"] = price, quantity
    if result["bid"] is not None and result["ask"] is not None and result["bid"] > result["ask"]:
        raise DataValidationError("Crossed order book")
    return result


def classify_edge(edge):
    """Classify profit / minimum ordinary payout, not return on purchase cost."""
    if isinstance(edge, bool) or not isinstance(edge, (int, float, Decimal)):
        raise DataValidationError("Invalid opportunity edge")
    value = Decimal(str(edge))
    if not value.is_finite():
        raise DataValidationError("Invalid opportunity edge")
    if value < WATCH_EDGE:
        return "IGNORE"
    if value < PAPER_TRADE_EDGE:
        return "WATCH"
    if value < STRONG_PAPER_TRADE_EDGE:
        return "PAPER TRADE"
    return "STRONG PAPER TRADE"


def find_opportunities(democrat, republican, allowed_positions=()):
    """Calculate only the pair types supported by current relationship evidence."""
    if democrat is None or republican is None:
        return {}
    now = time.monotonic()
    for book in (democrat, republican):
        if "received_at" in book and now - book["received_at"] > 5:
            raise DataValidationError("Pair quotes are too far apart in time")
        if "quoted_at" in book and not -5 <= time.time() - book["quoted_at"] <= 5:
            raise DataValidationError("Pair snapshot is stale at evaluation time")
    opportunities = {}
    for position_type, side in (("YES-PAIR", "ask"), ("NO-PAIR", "bid")):
        if position_type not in allowed_positions or democrat[side] is None or republican[side] is None:
            continue
        # Round each executable buy price upward to the venue's tick. Decimal
        # keeps an exact 0.010 edge on the PAPER TRADE boundary.
        quotes = [Decimal(str(book[side])) for book in (democrat, republican)]
        if position_type == "NO-PAIR":
            quotes = [Decimal(1) - quote for quote in quotes]
        tick = Decimal("0.005")
        prices = [(quote / tick).to_integral_value(rounding=ROUND_CEILING) * tick for quote in quotes]
        cost = sum(prices)
        edge = Decimal(1) - cost
        classification = classify_edge(edge)
        available = min(democrat[side + "_quantity"], republican[side + "_quantity"])
        if classification != "IGNORE" and available >= MIN_LIQUIDITY:
            opportunities[position_type] = {
                "cost_per_pair": float(cost), "profit_per_pair": float(edge),
                "quantity": min(math.floor(available), MAX_TRADE_QUANTITY),
                "classification": classification,
            }
    return opportunities


def report_race(race, current, previous, paper_trade=True):
    """Update only successfully observed races; disappearance never closes a trade."""
    for position_type in ("YES-PAIR", "NO-PAIR"):
        key = (race, position_type)
        old, new = previous.get(key), current.get(position_type)
        if new is None:
            if old is not None:
                print(f"DISAPPEARED | {race} | {position_type}")
                del previous[key]
            continue
        changed = old is not None and (
            abs(new["profit_per_pair"] - old["profit_per_pair"]) + 1e-12 >= MATERIAL_EDGE_CHANGE
            or abs(new["quantity"] - old["quantity"]) >= MATERIAL_QUANTITY_CHANGE
            or new.get("market_context", {}).get("settlement_fingerprint")
            != old.get("market_context", {}).get("settlement_fingerprint")
            or classify_edge(new["profit_per_pair"]) != classify_edge(old["profit_per_pair"])
        )
        if old is None or changed:
            event = "APPEARED" if old is None else "CHANGED"
            print(f"{event} | {race} | {position_type} | "
                  f"{classify_edge(new['profit_per_pair'])} | "
                  f"edge {new['profit_per_pair']:.2%} | quantity {new['quantity']:g}")
            previous[key] = new.copy()
            if paper_trade and classify_edge(new["profit_per_pair"]) in {"PAPER TRADE", "STRONG PAPER TRADE"}:
                execute_paper_trade(race=race, position_type=position_type, **new)


API_ERRORS = (requests.RequestException, ValueError, KeyError, TypeError, IndexError, OverflowError)


def api_error_reason(error):
    """Show our validation messages or generic failures, never raw HTTP errors."""
    if isinstance(error, DataValidationError):
        return str(error)
    if isinstance(error, requests.RequestException):
        return "API request failed"
    return "Malformed API data"


def load_approved_settlements():
    """Read the explicit paper allowlist each cycle; never generate approvals.

    Missing or malformed configuration permits no new paper trades. Loading it
    every cycle also makes removing an approval effective on the next scan.
    """
    try:
        data = json.loads(APPROVED_SETTLEMENTS_PATH.read_text())
        if (not isinstance(data, dict) or set(data) != {"version", "allowed_mode", "pairs"}
                or type(data["version"]) is not int or data["version"] != 1
                or data["allowed_mode"] != "paper-only" or not isinstance(data["pairs"], list)):
            raise ValueError
        required = {"pair_name", "tournament_id", "market_ids", "exchange_ids", "relationship_type",
                    "position_types", "max_quantity", "approved_at", "approval_note"}
        seen = set()
        for approval in data["pairs"]:
            if (not isinstance(approval, dict) or not required.issubset(approval)
                    or set(approval) - required - {"manual_approval"}
                    or any(not isinstance(approval[key], str) or not approval[key].strip()
                           for key in ("pair_name", "approval_note"))
                    or approval["relationship_type"] != "mutually_exclusive"
                    or not isinstance(approval["tournament_id"], str)
                    or str(UUID(approval["tournament_id"])) != approval["tournament_id"]
                    or type(approval["max_quantity"]) is not int
                    or not 1 <= approval["max_quantity"] <= MAX_TRADE_QUANTITY):
                raise ValueError
            for key in ("market_ids", "exchange_ids"):
                ids = approval[key]
                if (not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2
                        or any(not isinstance(item, str) or numeric_id(item) != item for item in ids)):
                    raise ValueError
            positions = approval["position_types"]
            if (not isinstance(positions, list) or not positions or len(set(positions)) != len(positions)
                    or not set(positions).issubset({"YES-PAIR", "NO-PAIR"})
                    or parse_api_timestamp(approval["approved_at"]).tzinfo is None):
                raise ValueError
            identity = (approval["tournament_id"], tuple(sorted(approval["market_ids"])))
            if identity in seen:
                raise ValueError
            seen.add(identity)
            if "manual_approval" in approval:
                manual = approval["manual_approval"]
                if (approval["max_quantity"] != 1 or positions != ["NO-PAIR"]
                        or not isinstance(manual, dict) or set(manual) != {
                            "evidence_hash", "proposition", "source_refs", "limitations"}
                        or not isinstance(manual["evidence_hash"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", manual["evidence_hash"])
                        or not isinstance(manual["proposition"], str) or not manual["proposition"].strip()
                        or any(not isinstance(manual[key], list) or not manual[key]
                               or any(not isinstance(item, str) or not item.strip() for item in manual[key])
                               for key in ("source_refs", "limitations"))):
                    raise ValueError
        return data["pairs"]
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise DataValidationError("Approved settlement configuration is missing or invalid") from error


def check_approved_pair(approval, markets, tournament_id):
    """Bind approval to the discovered instruments, never to the displayed name."""
    if ([numeric_id(market["id"]) for market in markets] != approval["market_ids"]
            or [get_exchange_id(market) for market in markets] != approval["exchange_ids"]
            or tournament_id != approval["tournament_id"]):
        raise DataValidationError("Approved market/exchange identity changed")
    for market in markets:
        check_context(market["contexts"], tournament_id)


def manual_paper_context(session, tournament_id, approval, explicit_hash=None):
    """Recheck official evidence against persistent, previously approved facts.

    The existing narrow evidence reader still enforces the reviewed Senate
    wording. This function creates paper metadata, never an execution journal.
    """
    from paired_account_test import MANUAL_POLICY, read_manual_evidence

    try:
        _, record = read_manual_evidence(session, tournament_id, *approval["market_ids"])
    except (ValueError, KeyError, TypeError, IndexError) as error:
        raise DataValidationError("Approved official settlement evidence changed or is invalid") from error
    expected = approval["manual_approval"]
    if (record["market_ids"] != approval["market_ids"]
            or record["exchange_ids"] != approval["exchange_ids"]
            or any(record[key] != value for key, value in expected.items())
            or (explicit_hash is not None and explicit_hash != expected["evidence_hash"])):
        raise DataValidationError("Approved settlement evidence does not match the current official evidence")
    # Retain the original human approval time rather than approving anew each scan.
    record["approved_at"] = approval["approved_at"]
    return {"mode": MANUAL_POLICY, "paper_only": True, "tournament_id": tournament_id,
            "market_ids": record["market_ids"], "exchange_ids": record["exchange_ids"],
            "settlement_fingerprint": record["evidence_hash"], "manual_approval": record}


def scan_once(session, previous, tournament_id, paper_trade=True, manual_approval=None):
    """Failures mean unknown, rather than falsely reporting disappearance."""
    try:
        approvals = load_approved_settlements()
    except DataValidationError as error:
        print(f"INVALID / NEEDS REVALIDATION | {error} | no paper trades this cycle")
        return
    try:
        races, listed_ids = get_races(session, tournament_id)
        relationships = fetch_pages(session, f"{API_BASE_URL}/relationships",
                                    {"tournamentId": tournament_id, "limit": 200})
    except RateLimitError:
        print("API read limit reached; retaining previous observations until the cooldown ends.")
        return
    except API_ERRORS:
        # Do not print raw exceptions: request details could contain credentials.
        print("Market list unavailable or invalid; retaining previous observations.")
        return
    print(f"Matched {len(races)} title candidates in the selected competition.")
    import execution_quarantine as quarantine

    # Titles remain discovery hints. Only the exact ordered market IDs from the
    # explicit configuration can authorize analysis or a new paper position.
    scoped = [approval for approval in approvals if approval["tournament_id"] == tournament_id]
    discovered = {tuple(numeric_id(market["id"]) for market in markets) for markets in races.values()}
    for approval in scoped:
        if tuple(approval["market_ids"]) not in discovered:
            reason = ("approved market ID missing" if not set(approval["market_ids"]).issubset(listed_ids)
                      else "approved market IDs no longer form the expected party pair")
            print(f"INVALID / NEEDS REVALIDATION | {approval['pair_name']} | {reason}")
    approved_by_ids = {tuple(approval["market_ids"]): approval for approval in scoped}
    unavailable = {}
    unapproved_count = 0
    for race, (democrat_market, republican_market) in races.items():
        try:
            markets = (democrat_market, republican_market)
            ids = [numeric_id(market["id"]) for market in markets]
            approval = approved_by_ids.get(tuple(ids))
            if approval is None:
                unapproved_count += 1
                continue
            try:
                check_approved_pair(approval, markets, tournament_id)
            except API_ERRORS:
                print(f"INVALID / NEEDS REVALIDATION | {approval['pair_name']} | market/exchange mapping changed")
                continue
            if democrat_market["status"] != "open" or republican_market["status"] != "open":
                report_race(race, {}, previous, paper_trade=paper_trade)
                continue
            quarantine.require_unblocked_markets(ids)
            quarantine.require_unblocked_exchanges(approval["exchange_ids"])
            manual = False
            machine_missing = False
            try:
                allowed, context = get_pair_rules(session, democrat_market, republican_market,
                                                  tournament_id, relationships=relationships)
            except DataValidationError as error:
                if ("manual_approval" not in approval
                        or str(error) != "No active relationship verifies this pair's normal payout"):
                    raise
                machine_missing = True
            if "manual_approval" in approval:
                # Even if the graph now verifies this pair, a changed approved
                # rule/hash cannot silently bypass the recorded manual review.
                manual_context = manual_paper_context(session, tournament_id, approval, manual_approval)
                if machine_missing:
                    context, allowed, manual = manual_context, {"NO-PAIR"}, True
            allowed = set(allowed).intersection(approval["position_types"])
            quarantine.require_unblocked_exchanges(context.get("exchange_ids", []))
            democrat = get_best_prices(session, democrat_market, tournament_id)
            republican = get_best_prices(session, republican_market, tournament_id)
            current = find_opportunities(democrat, republican, allowed)
            for position_type, opportunity in current.items():
                opportunity["market_context"] = dict(context)
                opportunity["market_context"]["book_versions"] = [democrat["version"], republican["version"]]
                side = "ask" if position_type == "YES-PAIR" else "bid"
                opportunity["market_context"]["leg_prices"] = [
                    float((price / Decimal("0.005")).to_integral_value(rounding=ROUND_CEILING) * Decimal("0.005"))
                    for price in [Decimal(str(book[side])) if side == "ask"
                                  else Decimal(1) - Decimal(str(book[side])) for book in (democrat, republican)]
                ]
                opportunity["quantity"] = min(opportunity["quantity"], approval["max_quantity"])
                if manual:
                    print(f"SETTLEMENT | {approval['pair_name']} | explicitly approved manual evidence | one paper pair")
        except RateLimitError:
            print("API read limit reached; pausing reads and retaining previous observations.")
            return
        except API_ERRORS as error:
            reason = api_error_reason(error)
            if isinstance(error, DataValidationError) and reason.startswith("Approved "):
                print(f"INVALID / NEEDS REVALIDATION | {approval['pair_name']} | {reason}")
                continue
            unavailable[reason] = unavailable.get(reason, 0) + 1
            continue
        report_race(race, current, previous, paper_trade=paper_trade)
    if unapproved_count:
        print(f"NOT APPROVED | {unapproved_count} pairs | absent from approved_settlements.json | no paper trades")
    for reason, count in unavailable.items():
        print(f"UNVERIFIED / UNAVAILABLE | {count} pairs | {reason} | retaining previous observations")
    # A title rename or missing counterpart is unknown while either old market
    # still exists. Only omission of both IDs from the complete list is removal.
    for race in {key[0] for key in previous} - races.keys():
        old_positions = [old for key, old in previous.items() if key[0] == race]
        market_ids = {market_id for old in old_positions
                      for market_id in old.get("market_context", {}).get("market_ids", [])}
        if len(market_ids) == 2 and market_ids.isdisjoint(listed_ids):
            report_race(race, {}, previous, paper_trade=paper_trade)
        else:
            print(f"UNVERIFIED | {race} | party pairing changed; retaining previous observations")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Scan once, then exit")
    parser.add_argument("--audit-only", action="store_true", help="Inspect API data once without changing paper files")
    parser.add_argument("--tournament", default="midterm-elections", help="Competition slug (default: midterm-elections)")
    parser.add_argument("--manual-settlement-approval",
                        help="Optional one-shot cross-check of the configured manual settlement evidence hash")
    args = parser.parse_args()
    if args.manual_settlement_approval is not None and not (args.once or args.audit_only):
        parser.error("Manual settlement approval requires a supervised --once or --audit-only paper scan")
    try:
        # Acquire before loading cash/positions or repairing the paper CSV, and
        # retain exclusive ownership until the complete scanner run returns.
        with scanner_lock():
            return _run_scanner(args)
    except ScannerLockError as error:
        print(f"Scanner stopped: {error}")
        return 1
    except KeyboardInterrupt:
        # Also handle Ctrl+C during startup, before the scan loop's own handler.
        print("\nScanner stopped.")
        return 0


def _run_scanner(args):
    """Existing paper scanner behavior, called only while holding its lock."""
    load_dotenv()
    api_key = os.getenv("SIG_API_KEY")
    if not api_key:
        print("Set SIG_API_KEY in your local .env before running the scanner.")
        return 1
    # Load saved holdings before fetching markets or opening any paper trades.
    try:
        restored = load_portfolio() if not args.audit_only else False
    except PortfolioError as error:
        print(f"Scanner stopped: {error}")
        return 1
    if not args.audit_only:
        print("Saved paper portfolio loaded." if restored else "No saved portfolio; starting with 5,000 paper SUSQies.")
    previous = {}
    if args.audit_only:
        print("Read-only API audit started. No paper trades or portfolio/log writes.")
    else:
        print("Paper-only scanner started. Ctrl+C stops it. Paper positions are saved locally.")
    print("Paper levels: <0.5% IGNORE | 0.5% WATCH | 1.0% PAPER TRADE | 2.0% STRONG PAPER TRADE")
    print("Settlement verification is mandatory; projected profit assumes ordinary settlement.")
    print("Only pairs listed in approved_settlements.json can open paper positions; evidence is rechecked each cycle.")
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {api_key}"})
            try:
                tournament_id = get_tournament(session, args.tournament)
            except API_ERRORS:
                print("Competition metadata unavailable or invalid; scanner stopped.")
                return 1
            print(f"Competition: {args.tournament} | {tournament_id}")
            while True:
                started = time.monotonic()
                print(f"\nScan {datetime.now().astimezone().isoformat(timespec='seconds')}")
                scan_once(session, previous, tournament_id, paper_trade=not args.audit_only,
                          manual_approval=args.manual_settlement_approval)
                if not args.audit_only:
                    print_portfolio_summary()
                if args.once or args.audit_only:
                    break
                # Aim for 15 seconds between starts; never overlap scans.
                time.sleep(max(0, SCAN_INTERVAL - (time.monotonic() - started)))
    except PortfolioError as error:
        print(f"\nScanner stopped: {error}")
        if not args.audit_only:
            print_portfolio_summary()
        return 1
    except KeyboardInterrupt:
        print("\nScanner stopped.")
        if not args.audit_only:
            print_portfolio_summary()
    return 0


if __name__ == "__main__":
    # Lazy imports of the existing evidence tools must share this scanner's
    # request-pacing state, rather than importing a second copy of this file.
    sys.modules.setdefault("price_reader", sys.modules[__name__])
    raise SystemExit(main())
