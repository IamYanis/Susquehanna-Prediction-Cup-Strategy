"""Inspect the competition account using GET requests only; never place orders."""
import argparse
import math
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import requests
from dotenv import load_dotenv

import execution_quarantine as quarantine
from paper_trader import is_finite_number, parse_api_timestamp
from price_reader import (
    API_BASE_URL,
    API_ERRORS,
    DataValidationError,
    RateLimitError,
    api_error_reason,
    fetch_json,
    numeric_id,
)

# Bound the work even if a faulty server keeps returning new cursors forever.
# Reaching this limit is an incomplete read, never evidence of zero orders.
MAX_ORDER_PAGES = 50
# One cent-displayed cost basis can differ by half a cent. This is separate
# from the 0.02 bound for a DIFFERENCE of two displayed account balances.
POSITION_COST_ROUNDING_TOLERANCE = Decimal("0.005")
POSITION_PRICE_PRECISION = Decimal("0.000000001")


def require_number(value, nonnegative=False):
    """Reject booleans, strings, NaN and infinity before displaying account data."""
    if not is_finite_number(value) or (nonnegative and value < 0):
        raise DataValidationError("Invalid account number")
    return value


def one_no_buy_cost_matches(position, fill_notional):
    """Accept exact cost or half-cent display rounding for ONE known NO buy.

    Average entry cost must still match the actual fill to numeric precision.
    A different quantity, missing data or a non-cent rounded basis fails.
    Instrument identity, fills, ledger and cash remain the caller's checks.
    """
    return no_holding_cost_matches(position, Decimal(1), fill_notional)


def no_holding_cost_matches(position, quantity, total_cost):
    """Match known NO inventory and weighted average against immutable fills.

    A multi-unit position still has only one cent-displayed total cost basis.
    Do not multiply the half-cent display allowance by its quantity.
    """
    try:
        if quantity <= 0 or Decimal(str(require_number(position["quantity"]))) != -quantity:
            return False
        average = Decimal(str(require_number(position["avgCost"], nonnegative=True)))
        basis = Decimal(str(require_number(position["costBasis"], nonnegative=True)))
        if abs(average - total_cost / quantity) > POSITION_PRICE_PRECISION:
            return False
        difference = abs(basis - total_cost)
        return difference <= POSITION_PRICE_PRECISION or (
            basis % Decimal("0.01") == 0 and difference <= POSITION_COST_ROUNDING_TOLERANCE)
    except API_ERRORS:
        return False


def validate_positions(payload):
    """Use the platform's reported values; signed quantity identifies YES or NO."""
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
        raise DataValidationError("Invalid positions response")
    positions = payload["positions"]
    summary = payload["summary"]
    if not isinstance(summary, dict):
        raise DataValidationError("Invalid positions summary")
    seen_exchanges = set()
    for position in positions:
        if not isinstance(position, dict):
            raise DataValidationError("Invalid position row")
        exchange_id = numeric_id(position["exchangeId"])
        numeric_id(position["marketId"])
        if exchange_id in seen_exchanges:
            raise DataValidationError("Duplicate position exchange")
        seen_exchanges.add(exchange_id)
        if not isinstance(position["marketTitle"], str) or type(position["settled"]) is not bool:
            raise DataValidationError("Invalid position metadata")
        quantity = require_number(position["quantity"])
        price = position["currentPrice"]
        # The API permits a missing valuation only for a zero-quantity row.
        if price is None:
            if quantity != 0:
                raise DataValidationError("A holding has no valuation price")
        elif not 0 <= require_number(price) <= 1:
            raise DataValidationError("Invalid position valuation price")
        require_number(position["marketValue"], nonnegative=True)
        require_number(position["costBasis"], nonnegative=True)
        require_number(position["unrealizedPnl"])
    for total, field in (("totalMarketValue", "marketValue"),
                         ("totalCostBasis", "costBasis"),
                         ("totalUnrealizedPnl", "unrealizedPnl")):
        require_number(summary[total], nonnegative=total != "totalUnrealizedPnl")
        # Allow one cent per row for possible rounding in reported values.
        if not math.isclose(sum(position[field] for position in positions), summary[total],
                            rel_tol=1e-9, abs_tol=.01 * max(1, len(positions))):
            raise DataValidationError("Position rows do not match the reported totals")
    return positions, summary


def get_open_orders(session, tournament_id):
    """Follow every orders page with explicit scope and validate the remainder."""
    orders = []
    seen_ids, seen_cursors = set(), set()
    params = {"tournamentId": tournament_id, "status": "open", "limit": 200}
    for _ in range(MAX_ORDER_PAGES):
        payload = fetch_json(session, f"{API_BASE_URL}/orders", params=params.copy())
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise DataValidationError("Invalid open-orders response")
        coverage = payload.get("coverage")
        if coverage is not None:
            if not isinstance(coverage, dict) or coverage.get("complete") is not True:
                raise DataValidationError("Open-orders coverage is incomplete")
            sequence = coverage["projectedThroughSequence"]
            if sequence is not None and (type(sequence) is not int or sequence < 0):
                raise DataValidationError("Invalid order projection sequence")
        for order in payload["data"]:
            if not isinstance(order, dict):
                raise DataValidationError("Invalid order row")
            order_id = order["id"]
            if type(order_id) is not int or order_id <= 0 or order_id in seen_ids:
                raise DataValidationError("Invalid or repeated order ID")
            seen_ids.add(order_id)
            numeric_id(order["exchangeId"])
            if order["tournamentId"] != tournament_id or order["open"] is not True:
                raise DataValidationError("Order belongs to another scope or is not open")
            if order["side"] not in ("yes", "no") or order["action"] not in ("buy", "sell"):
                raise DataValidationError("Unknown order side or action")
            if require_number(order["quantity"]) <= 0:
                raise DataValidationError("Invalid open-order remainder")
            if order["priceLimit"] is not None and not 0 <= require_number(order["priceLimit"]) <= 1:
                raise DataValidationError("Invalid order price limit")
            parse_api_timestamp(order["createdAt"])
            if order["expirationDate"] is not None:
                parse_api_timestamp(order["expirationDate"])
            orders.append(order)
        pagination = payload["pagination"]
        if not isinstance(pagination, dict) or type(pagination["hasMore"]) is not bool:
            raise DataValidationError("Invalid orders pagination")
        if not pagination["hasMore"]:
            return orders
        cursor = pagination["nextCursor"]
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise DataValidationError("Missing or repeated orders cursor")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
    raise DataValidationError("Orders page limit reached; the account read is incomplete")


def read_account(session, slug="midterm-elections", allow_inactive=False):
    """Read the cash, holdings and orders; return a result only when all succeed."""
    # Read-only holding/settlement management can continue after a tournament
    # ends. Entry callers retain the default requirement for an active account.
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise DataValidationError("Invalid tournament slug")
    tournament = fetch_json(session, f"{API_BASE_URL}/tournaments/{slug}")
    if not isinstance(tournament, dict) or not isinstance(tournament.get("id"), str):
        raise DataValidationError("Invalid tournament metadata")
    tournament_id = str(UUID(tournament["id"]))
    if (tournament_id != tournament["id"] or tournament["slug"] != slug
            or not isinstance(tournament["status"], str) or not tournament["status"]
            or (not allow_inactive and tournament["status"] != "active")
            or tournament["currencyName"] not in ("SUSQie", "SUSQies")
            or not isinstance(tournament["name"], str)
            or type(tournament["isPendingEnrolment"]) is not bool):
        raise DataValidationError("Unexpected competition metadata")
    require_number(tournament["myBalance"], nonnegative=True)
    require_number(tournament["initialBalance"], nonnegative=True)
    if tournament["isPendingEnrolment"]:
        # Reading an unenrolled tournament can return its seed balance.
        # Never enrol the user, or pretend that this is an active account.
        raise DataValidationError("Account is pending enrollment; the seed balance is not an enrolled account")
    payload = fetch_json(session, f"{API_BASE_URL}/tournaments/{slug}/portfolio/positions")
    positions, summary = validate_positions(payload)
    orders = get_open_orders(session, tournament_id)
    fields = ("id", "slug", "name", "status", "currencyName", "myBalance", "initialBalance", "isPendingEnrolment")
    return {"tournament": {field: tournament[field] for field in fields},
            "positions": positions, "summary": summary, "orders": orders,
            "quarantine_reserve": quarantine.reserved_cost(tournament_id)}


def print_account_summary(account, details=False):
    """Print observations, without converting them into paper trades or files."""
    tournament, summary = account["tournament"], account["summary"]
    holdings = [position for position in account["positions"] if position["quantity"] != 0]
    orders = account["orders"]
    print(f"Competition: {tournament['name']} | {tournament['slug']} | {tournament['currencyName']}")
    print(f"Cash balance (reported): {tournament['myBalance']:,.2f}")
    print(f"Initial allocation: {tournament['initialBalance']:,.2f}")
    print(f"Nonzero holdings: {len(holdings)}")
    print(f"Position value (reported): {summary['totalMarketValue']:,.2f}")
    print(f"Position cost basis (reported): {summary['totalCostBasis']:,.2f}")
    print(f"Unrealized P&L (reported): {summary['totalUnrealizedPnl']:,.2f}")
    print(f"Open orders: {len(orders)} (all pages read)")
    quarantine.print_quarantine(tournament["id"])
    quarantined = quarantine.reserved_cost(tournament["id"])
    print(f"Cash after pending/quarantine reserves: "
          f"{tournament['myBalance'] - sum(order['quantity'] for order in orders) - quarantined:,.3f}")
    now = datetime.now(timezone.utc)
    expired = sum(order["expirationDate"] is not None
                  and parse_api_timestamp(order["expirationDate"]) <= now for order in orders)
    if expired:
        print(f"Orders past expiry but still reported open: {expired}")
    if details:
        for position in holdings:
            side = "YES" if position["quantity"] > 0 else "NO"
            title = " ".join(position["marketTitle"].split())
            print(f"  HOLDING | market {position['marketId']} | exchange {position['exchangeId']} | "
                  f"{side} {abs(position['quantity']):g} | value {position['marketValue']:.2f} | {title}")
        for order in orders:
            limit = "unspecified" if order["priceLimit"] is None else f"{order['priceLimit']:g}"
            print(f"  ORDER | {order['id']} | exchange {order['exchangeId']} | "
                  f"{order['action']} {order['side'].upper()} | remaining {order['quantity']:g} | limit {limit}")
    print("Resting orders do not reserve cash; their fills can still change this balance.")
    print("These sequential reads are observations, not an atomic account snapshot.")
    print("No orders submitted or cancelled. No account or paper files written.")


def account_error_reason(error):
    """Explain common HTTP failures without printing bodies, URLs or headers."""
    if isinstance(error, quarantine.QuarantineError):
        return str(error)  # Our fixed local messages contain no server/key data.
    if isinstance(error, RateLimitError):
        return "API read cooldown or rate limit; try again after the cooldown"
    if isinstance(error, requests.HTTPError) and error.response is not None:
        reasons = {401: "API authentication failed", 403: "Account read is not permitted",
                   409: "Position valuation is unavailable; holdings are unknown",
                   503: "Authoritative account data is unavailable"}
        return reasons.get(error.response.status_code, "Account API request failed")
    return api_error_reason(error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tournament", default="midterm-elections", help="Competition slug")
    parser.add_argument("--details", action="store_true", help="Also show each holding and open order")
    args = parser.parse_args()
    # Resolve .env beside the script; never print or persist the key.
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    api_key = os.getenv("SIG_API_KEY")
    if not api_key:
        print("Set SIG_API_KEY in your local .env before reading the account.")
        return 1
    print("Read-only competition account check started.")
    try:
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {api_key}"})
            account = read_account(session, args.tournament)
        print_account_summary(account, details=args.details)
    except API_ERRORS as error:
        print(f"Account check stopped: {account_error_reason(error)}. Account state is unknown.")
        return 1
    except KeyboardInterrupt:
        print("Account check stopped before completion; no orders submitted.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
