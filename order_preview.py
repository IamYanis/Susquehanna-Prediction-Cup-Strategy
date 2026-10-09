"""Prepare a local order preview from GET reads. Never submit or cancel orders."""
import argparse
import copy
import hashlib
import json
import math
import os
import time
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from uuid import UUID, uuid4

import requests
from dotenv import load_dotenv

import execution_quarantine as quarantine
from account_reader import account_error_reason, read_account, require_number, validate_positions
from paper_trader import (
    MAX_CAPITAL_PER_RACE, MAX_CAPITAL_PER_TRADE, is_finite_number,
    parse_api_timestamp, validate_market_context,
)
from price_reader import (
    API_BASE_URL, API_ERRORS, MAX_TRADE_QUANTITY, MIN_EDGE, MIN_LIQUIDITY,
    DataValidationError, check_context, fetch_json, find_opportunities, get_best_prices,
    get_election_rule, get_exchange_id, get_pair_rules, numeric_id,
)

PRICE_TICK = Decimal("0.005")
# Measure from the START of the account reads. A long paginated read can itself
# be too old, even if its final response arrived just now. This is local policy.
MAX_ACCOUNT_READ_AGE = 15
ORDER_PATH = "/api/v1/orders/multi-leg"


class PreviewBlocked(ValueError):
    """A fixed local policy reason, safe to display without headers or raw bodies."""


def require_preview(condition, message):
    if not condition:
        raise PreviewBlocked(message)


def ceil_buy_limit(price):
    """Round UP so the buy limit still reaches the observed quote.

    Decimal avoids a binary float accidentally turning an exact tick into the
    next one. Limits of 0 or 1 are excluded: 1 is a market buy in this API.
    """
    require_preview(is_finite_number(price) and 0 < price < 1,
                    "A preview needs positive limit prices below 1")
    rounded = (Decimal(str(price)) / PRICE_TICK).to_integral_value(rounding=ROUND_CEILING) * PRICE_TICK
    require_preview(PRICE_TICK <= rounded <= Decimal("0.995"),
                    "A rounded buy limit would become a market order")
    return float(rounded)


def account_risk(account, exchange_ids):
    """Use actual holdings and unreserved orders, with conservative upper bounds.

    Selling YES can acquire NO, and an order can cross a position through zero.
    Reserving 1 per remaining share covers either side/action without guessing
    netting or crediting future sale proceeds. This is a contract-cost reserve;
    it is not an exchange cash reservation or a verified fee allowance.
    """
    tournament = account["tournament"]
    tournament_id = tournament["id"]
    require_preview(isinstance(tournament_id, str) and str(UUID(tournament_id)) == tournament_id,
                    "The account has no valid competition identity")
    require_preview(tournament["status"] == "active"
                    and tournament["currencyName"] in ("SUSQie", "SUSQies")
                    and tournament["isPendingEnrolment"] is False,
                    "The preview needs an enrolled active competition account")
    cash = require_number(tournament["myBalance"], nonnegative=True)
    positions, _ = validate_positions({"positions": account["positions"], "summary": account["summary"]})
    require_preview(isinstance(exchange_ids, (list, tuple)) and 1 <= len(exchange_ids) <= 2,
                    "One or two selected exchanges are required")
    selected = {numeric_id(exchange_id) for exchange_id in exchange_ids}
    require_preview(len(selected) == len(exchange_ids), "The selected exchanges must be different")
    quarantine.require_unblocked_exchanges(selected)
    quarantined = quarantine.reserved_cost(tournament_id)
    holdings_cost = 0
    for position in positions:
        if position["quantity"] == 0:
            continue
        require_preview(numeric_id(position["exchangeId"]) not in selected,
                        "A selected exchange already has a holding; duplicate or netting exposure blocks the preview")
        # Reported costBasis is already a nonnegative cost for signed YES/NO
        # holdings. Valuation prices and unrealized P&L do not release capital.
        holdings_cost += position["costBasis"]
    orders = account["orders"]
    require_preview(isinstance(orders, list), "Open orders are unknown")
    reserve, seen_ids = 0, set()
    for order in orders:
        require_preview(isinstance(order, dict) and type(order["id"]) is int
                        and order["id"] > 0 and order["id"] not in seen_ids,
                        "Open orders have invalid or repeated identities")
        seen_ids.add(order["id"])
        require_preview(order["tournamentId"] == tournament_id and order["open"] is True
                        and order["side"] in ("yes", "no") and order["action"] in ("buy", "sell"),
                        "Open orders have unknown scope or direction")
        remainder = require_number(order["quantity"])
        require_preview(remainder > 0, "An open order has invalid remaining quantity")
        if order["priceLimit"] is not None:
            require_preview(0 <= require_number(order["priceLimit"]) <= 1,
                            "An open order has an invalid limit")
        parse_api_timestamp(order["createdAt"])
        if order["expirationDate"] is not None:
            parse_api_timestamp(order["expirationDate"])
        require_preview(numeric_id(order["exchangeId"]) not in selected,
                        "A selected exchange already has an open order; overlapping exposure blocks the preview")
        # Even an apparently expired order remains reserved until it is actually
        # absent from a complete open-order read. We never cancel it here.
        reserve += remainder
    require_preview(is_finite_number(holdings_cost) and is_finite_number(reserve),
                    "Account exposure is too large or invalid")
    return {"reported_cash": cash, "existing_order_reserve": reserve,
            "quarantine_reserve": quarantined,
            "existing_holdings_cost_basis": holdings_cost, "available_cash": cash - reserve - quarantined,
            # No title-based race mapping: every existing instrument might be
            # in this race. This bound is restrictive but cannot undercount it.
            # Add the full uncertain cost even if visible holdings/orders might
            # already include it. Without a receipt we cannot safely net it out.
            "race_exposure_upper_bound": holdings_cost + reserve + quarantined}


def validate_request_body(body):
    """Local checks for our narrow subset of the documented multi-leg schema.

    The API has no participant GET endpoint that validates hypothetical orders.
    Passing this function does not prove server acceptance or simultaneous fills.
    """
    require_preview(isinstance(body, dict) and set(body) == {"idempotencyKey", "legs"},
                    "The order preview contains unsupported request fields")
    key, legs = body["idempotencyKey"], body["legs"]
    require_preview(isinstance(key, str) and key.startswith("preview-") and 1 <= len(key) <= 255
                    and isinstance(legs, list) and len(legs) == 2,
                    "Invalid preview key or leg count")
    identities, tournament_ids, quantities, sides = set(), set(), set(), set()
    for leg in legs:
        require_preview(isinstance(leg, dict) and set(leg) == {
            "exchangeId", "side", "action", "quantity", "price", "tournamentId"},
            "The order preview contains unsupported leg fields")
        require_preview(isinstance(leg["exchangeId"], str), "Exchange IDs must be numeric strings")
        identities.add(numeric_id(leg["exchangeId"]))
        require_preview(leg["action"] == "buy" and leg["side"] in ("yes", "no")
                        and type(leg["quantity"]) is int and 1 <= leg["quantity"] <= MAX_TRADE_QUANTITY,
                        "Preview legs must be bounded integer-quantity buys")
        tournament_id = leg["tournamentId"]
        require_preview(isinstance(tournament_id, str) and str(UUID(tournament_id)) == tournament_id,
                        "Each preview leg needs an explicit competition UUID")
        price = leg["price"]
        require_preview(is_finite_number(price) and PRICE_TICK <= Decimal(str(price)) <= Decimal("0.995")
                        and Decimal(str(price)) % PRICE_TICK == 0,
                        "Preview buy prices must be limit prices on the 0.005 tick")
        tournament_ids.add(tournament_id)
        quantities.add(leg["quantity"])
        sides.add(leg["side"])
    require_preview(len(identities) == 2 and len(tournament_ids) == len(quantities) == len(sides) == 1,
                    "The two preview legs must share scope, side and quantity")


def executable_buy_limit(position_type, book, minimum_depth=MIN_LIQUIDITY):
    """Validate ONE authoritative executable book for either pair leg."""
    require_preview(position_type in ("YES-PAIR", "NO-PAIR") and isinstance(book, dict), "Unknown pair/book type")
    require_preview(is_finite_number(book["received_at"]) and is_finite_number(book["quoted_at"])
                    and 0 <= time.monotonic() - book["received_at"] <= 5
                    and -5 <= time.time() - book["quoted_at"] <= 5,
                    "A selected book is stale or has no authoritative timestamp")
    book_side = "ask" if position_type == "YES-PAIR" else "bid"
    quote, depth = book[book_side], book[book_side + "_quantity"]
    require_preview(is_finite_number(quote) and 0 < quote < 1
                    and is_finite_number(depth) and depth >= minimum_depth,
                    "The selected book side has insufficient depth or no positive limit quote")
    own_side_price = quote if position_type == "YES-PAIR" else float(Decimal(1) - Decimal(str(quote)))
    require_preview(abs(parse_api_timestamp(book["version"]["at"]).timestamp() - book["quoted_at"]) < 1e-6,
                    "Book version and quote timestamps disagree")
    return ceil_buy_limit(own_side_price)


def executable_limits(position_type, books, account_started, quantity=None, minimum_depth=MIN_LIQUIDITY):
    """Validate quotes and size, then price tick-rounded limits with Decimal.

    This helper does not authorize a trade or choose an edge policy. The normal
    live checker and the separate supervised probe apply their own fixed gate.
    Freshness, depth, quantity and quote-version checks are shared by both.
    """
    require_preview(position_type in ("YES-PAIR", "NO-PAIR"), "Unknown pair type")
    require_preview(is_finite_number(account_started)
                    and 0 <= time.monotonic() - account_started <= MAX_ACCOUNT_READ_AGE,
                    "Account observations are too old; repeat the preview")
    require_preview(isinstance(books, (list, tuple)) and len(books) == 2
                    and all(isinstance(book, dict) for book in books),
                    "Both selected books need executable depth")
    book_side = "ask" if position_type == "YES-PAIR" else "bid"
    prices = [executable_buy_limit(position_type, book, minimum_depth) for book in books]
    available = min(book[book_side + "_quantity"] for book in books)
    if quantity is None:
        quantity = min(math.floor(available), MAX_TRADE_QUANTITY)
    require_preview(type(quantity) is int and 1 <= quantity <= MAX_TRADE_QUANTITY
                    and quantity <= available, "Requested quantity is invalid or exceeds observed depth")
    pair_cost = sum(Decimal(str(price)) for price in prices)
    edge = Decimal("1.000") - pair_cost
    return prices, quantity, pair_cost, edge


def observed_limits(position_type, books, account_started, quantity=None):
    """Keep the existing live policy fixed at 2%; no lower-threshold override."""
    result = executable_limits(position_type, books, account_started, quantity)
    # Preserve the existing scanner eligibility check for normal live previews.
    # The probe does not borrow the paper scanner's opportunity classification.
    opportunities = find_opportunities(books[0], books[1], {position_type})
    require_preview(position_type in opportunities, "The selected pair has no qualifying observed edge")
    require_preview(result[3] >= Decimal(str(MIN_EDGE)),
                    "Tick-rounded limits fall below the 2% edge requirement")
    return result


def build_preview(account, position_type, market_context, books, account_started, quantity=None):
    """Build a two-leg draft after validating settlement evidence and account risk."""
    prices, quantity, pair_cost, edge = observed_limits(position_type, books, account_started, quantity)
    context = copy.deepcopy(market_context)
    context["leg_prices"] = prices
    context["book_versions"] = [copy.deepcopy(book["version"]) for book in books]
    validate_market_context({"market_context": context, "position_type": position_type,
                             "cost_per_pair": float(pair_cost)})
    require_preview(context["tournament_id"] == account["tournament"]["id"],
                    "Market evidence belongs to another competition account")
    risk = account_risk(account, context["exchange_ids"])
    capital = float(pair_cost * quantity)
    require_preview(capital <= MAX_CAPITAL_PER_TRADE, "The draft exceeds the per-trade capital limit")
    require_preview(risk["race_exposure_upper_bound"] + capital <= MAX_CAPITAL_PER_RACE + 1e-9,
                    "The conservative race exposure bound exceeds 150; structured exposure mapping is required")
    require_preview(capital <= risk["available_cash"] + 1e-9,
                    "Reported cash minus existing open-order reserves cannot cover the draft")
    risk["race_exposure_after_upper_bound"] = risk["race_exposure_upper_bound"] + capital
    body = {"idempotencyKey": "preview-" + uuid4().hex,
            "legs": [{"exchangeId": exchange_id, "side": position_type.split("-")[0].lower(),
                      "action": "buy", "quantity": quantity, "price": price,
                      "tournamentId": context["tournament_id"]}
                     for exchange_id, price in zip(context["exchange_ids"], prices)]}
    validate_request_body(body)
    return {"mode": "preview-only", "submission_enabled": False, "endpoint_path": ORDER_PATH,
            "request": body, "position_type": position_type, "quantity": quantity,
            "max_new_spend": capital, "ordinary_edge": float(edge),
            "conditional_projected_profit": float(edge * quantity), "risk": risk,
            "book_versions": context["book_versions"], "market_context": context}


def read_selected_pair(session, tournament_id, democrat_market_id, republican_market_id):
    """Confirm market scope and titles before reading rules or executable quotes."""
    ids = [numeric_id(democrat_market_id), numeric_id(republican_market_id)]
    require_preview(ids[0] != ids[1], "Select two different markets")
    quarantine.require_unblocked_markets(ids)
    markets, races = [], []
    for market_id, party in zip(ids, ("Democratic", "Republican")):
        market = fetch_json(session, f"{API_BASE_URL}/markets/{market_id}",
                            params={"tournamentId": tournament_id})
        require_preview(numeric_id(market["id"]) == market_id and market["status"] == "open",
                        "A selected market is wrong or no longer open")
        check_context(market["contexts"], tournament_id)
        get_exchange_id(market)
        prefix = f"Will the {party} Party win the "
        require_preview(isinstance(market["title"], str) and market["title"].startswith(prefix),
                        "Selected markets must identify the Democratic and Republican candidates")
        races.append(market["title"][len(prefix):].removesuffix("?").strip())
        markets.append(market)
    require_preview(bool(races[0]) and races[0] == races[1], "Selected market titles describe different races")
    require_preview(get_exchange_id(markets[0]) != get_exchange_id(markets[1]),
                    "Select two different exchanges")
    return markets


def preview_pair(session, account, democrat_market_id, republican_market_id,
                 position_type, account_started, quantity=None):
    """Read exactly one selected pair; use the same settlement gate as the scanner."""
    tournament_id = account["tournament"]["id"]
    markets = read_selected_pair(session, tournament_id, democrat_market_id, republican_market_id)
    # No manual override or unsupported guessed payout: absent relationships
    # remain blocked. Rule trees also establish the shared structured race key.
    try:
        allowed, context = get_pair_rules(session, markets[0], markets[1], tournament_id)
    except DataValidationError as error:
        # Only this known literal becomes a policy explanation. Never display
        # arbitrary API exception strings, which may contain credentials.
        if str(error) == "No active relationship verifies this pair's normal payout":
            raise PreviewBlocked("No active relationship verifies this pair; settlement approval is required") from error
        raise
    require_preview(position_type in allowed, "The requested pair type has no verified ordinary settlement coverage")
    account_risk(account, context["exchange_ids"])
    books = [get_best_prices(session, market, tournament_id) for market in markets]
    return build_preview(account, position_type, context, books, account_started, quantity)


def conditional_proposal(session, account, democrat_market_id, republican_market_id, account_started):
    """Analyze one NO share per leg without creating any executable order intent.

    Matching election rules establish which race we are discussing. They do not
    prove mutual exclusivity or coordinated refunds. Missing relationship
    evidence is recorded explicitly; the strict execution tools still reject it.
    """
    tournament_id = account["tournament"]["id"]
    markets = read_selected_pair(session, tournament_id, democrat_market_id, republican_market_id)
    exchange_ids = [get_exchange_id(market) for market in markets]
    risk = account_risk(account, exchange_ids)
    try:
        allowed, context = get_pair_rules(session, markets[0], markets[1], tournament_id)
        require_preview("NO-PAIR" in allowed, "No ordinary NO-pair coverage was verified")
        relationship_verified = True
        race_key = context["race_key"]
        evidence_fingerprint = context["settlement_fingerprint"]
    except DataValidationError as error:
        # Only this specific missing-evidence condition allows further analysis.
        # Network failures, malformed evidence and mismatched scopes still stop.
        if str(error) != "No active relationship verifies this pair's normal payout":
            raise
        relationship_verified = False
        rules = [get_election_rule(session, market, party, tournament_id)
                 for market, party in zip(markets, ("Democratic", "Republican"))]
        require_preview(rules[0][0] == rules[1][0]
                        and rules[0][1]["settlement_date"] == rules[1][1]["settlement_date"],
                        "Party contracts describe different races or stages")
        race_key = list(rules[0][0])
        # Pin the actual structured rules for a later supervised preparation.
        # This fingerprint identifies observations; it does not verify payouts.
        evidence_fingerprint = hashlib.sha256(json.dumps(
            ["conditional-rule-analysis", rules], sort_keys=True, allow_nan=False
        ).encode()).hexdigest()
    books = [get_best_prices(session, market, tournament_id) for market in markets]
    prices, quantity, cost, edge = observed_limits("NO-PAIR", books, account_started, quantity=1)
    capital = float(cost)
    require_preview(capital <= MAX_CAPITAL_PER_TRADE, "The proposal exceeds the per-trade capital limit")
    require_preview(risk["race_exposure_upper_bound"] + capital <= MAX_CAPITAL_PER_RACE + 1e-9,
                    "The conservative race exposure bound exceeds 150; structured exposure mapping is required")
    require_preview(capital <= risk["available_cash"] + 1e-9,
                    "Reported cash minus existing open-order reserves cannot cover the proposal")
    risk["race_exposure_after_upper_bound"] = risk["race_exposure_upper_bound"] + capital
    # All gains assume BOTH shares fill at their displayed limits. Refund cases
    # additionally assume the complete acquisition cost is refundable. Neither
    # assumption is verified by this numerical plan; fees are not modeled.
    scenarios = [
        ("Only Democratic or only Republican party wins", float(edge)),
        ("Neither party wins", float(Decimal(2) - cost)),
        ("Both party markets resolve YES", -capital),
        ("Both shares refunded at acquisition cost", 0.0),
        ("Democratic NO refunded; Republican NO loses", -prices[1]),
        ("Republican NO refunded; Democratic NO loses", -prices[0]),
        ("Only Democratic NO fills and later loses", -prices[0]),
        ("Only Republican NO fills and later loses", -prices[1]),
    ]
    # Deliberately omit a request body, idempotency key, approval hash and
    # market_context: this result cannot be loaded by the execution handler.
    return {"mode": "conditional-proposal", "submission_enabled": False,
            "execution_approved": False, "quantity": quantity, "position_type": "NO-PAIR",
            "relationship_verified": relationship_verified, "race_key": race_key,
            "tournament_id": tournament_id, "evidence_fingerprint": evidence_fingerprint,
            "book_versions": [copy.deepcopy(book["version"]) for book in books],
            "legs": [{"market_id": numeric_id(market["id"]), "exchange_id": exchange_id,
                      "price": price, "depth": book["bid_quantity"], "quote_at": book["version"]["at"]}
                     for market, exchange_id, price, book in zip(markets, exchange_ids, prices, books)],
            "max_new_spend": capital, "conditional_projected_profit": float(edge),
            "risk": risk, "scenarios": scenarios}


def print_conditional_proposal(proposal):
    """Display the numerical plan and its assumptions, without an order payload."""
    print("CONDITIONAL PROPOSAL | 1 NO share per leg | EXECUTION NOT APPROVED")
    quarantine.print_quarantine(proposal["tournament_id"])
    for party, leg in zip(("Democratic", "Republican"), proposal["legs"]):
        print(f"{party} NO | market {leg['market_id']} | exchange {leg['exchange_id']} | "
              f"limit {leg['price']:.3f} | depth {leg['depth']:g} | quote {leg['quote_at']}")
    print(f"Maximum contract spend / loss before unverified fees: {proposal['max_new_spend']:.3f} SUSQies")
    risk = proposal["risk"]
    print(f"Account checks passed | available after open-order reserves {risk['available_cash']:.2f} | "
          f"exposure after proposal (account-wide upper bound) {risk['race_exposure_after_upper_bound']:.3f} / {MAX_CAPITAL_PER_RACE}")
    evidence = "present" if proposal["relationship_verified"] else "MISSING; strict execution remains blocked"
    print(f"Engine ordinary-payout relationship: {evidence}")
    print("Conditional gains/losses at these limits; refunds assume full acquisition-cost reimbursement:")
    for scenario, gain in proposal["scenarios"]:
        print(f"  {scenario}: {gain:+.3f} SUSQies")
    print("The first two cases assume ordinary binary settlement. Matching rules do not prove exclusive party outcomes.")
    print("Quotes do not guarantee fills. Cancelling a remaining order cannot undo a filled share.")
    print("Account and book reads are sequential observations, not an atomic executable snapshot.")
    print("No order body, execution key, approval fingerprint, or execution journal was created.")


def print_preview(preview):
    """Print local diagnostics and the draft body, never credential headers."""
    risk = preview["risk"]
    quarantine.print_quarantine(preview["market_context"]["tournament_id"])
    print(f"PREVIEW READY | {preview['position_type']} | {preview['quantity']} shares per leg")
    print(f"Reported cash: {risk['reported_cash']:.2f} | existing order reserve: {risk['existing_order_reserve']:.2f}")
    print(f"Quarantine reserve: {risk['quarantine_reserve']:.3f} | "
          f"available after all reserves: {risk['available_cash']:.3f} | maximum new spend: {preview['max_new_spend']:.2f}")
    print(f"Race exposure after draft (account-wide upper bound): {risk['race_exposure_after_upper_bound']:.2f} / {MAX_CAPITAL_PER_RACE}")
    print(f"Projected profit at ordinary settlement, before unverified fees/refunds: {preview['conditional_projected_profit']:.2f}")
    print(f"Draft body for {preview['endpoint_path']} (display only):")
    print(json.dumps(preview["request"], indent=2, allow_nan=False))
    print("The preview key is temporary, not a saved execution intent. No server acceptance was checked.")
    print("Account and book reads are sequential observations, not an atomic executable snapshot.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dem-market", required=True, help="Democratic market ID")
    parser.add_argument("--rep-market", required=True, help="Republican market ID")
    parser.add_argument("--position", choices=("YES-PAIR", "NO-PAIR"), default="NO-PAIR")
    parser.add_argument("--quantity", type=int, help="Shares per leg (1..100); default uses available depth up to 100")
    parser.add_argument("--conditional-proposal", action="store_true",
                        help="Read-only one-pair NO analysis, even without relationship evidence; never an order draft")
    parser.add_argument("--tournament", default="midterm-elections", help="Competition slug")
    args = parser.parse_args()
    if args.conditional_proposal and (args.position != "NO-PAIR" or args.quantity not in (None, 1)):
        print("CONDITIONAL PROPOSAL BLOCKED | Select NO-PAIR and exactly one share per leg.")
        return 1
    # Load the secret only at the CLI boundary; never put it in a draft or log.
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    api_key = os.getenv("SIG_API_KEY")
    if not api_key:
        print("Set SIG_API_KEY in your local .env before reading an order preview.")
        return 1
    print("PREVIEW ONLY | GET requests; order submission and cancellation are disabled.")
    try:
        numeric_id(args.dem_market)
        numeric_id(args.rep_market)
        if args.quantity is not None:
            require_preview(1 <= args.quantity <= MAX_TRADE_QUANTITY, "Requested quantity must be 1..100")
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {api_key}"})
            account_started = time.monotonic()
            account = read_account(session, args.tournament)
            print(f"Account read complete | reported cash {account['tournament']['myBalance']:.2f} | "
                  f"nonzero holdings {sum(position['quantity'] != 0 for position in account['positions'])} | "
                  f"open orders {len(account['orders'])}")
            if args.conditional_proposal:
                preview = conditional_proposal(session, account, args.dem_market, args.rep_market, account_started)
            else:
                preview = preview_pair(session, account, args.dem_market, args.rep_market,
                                       args.position, account_started, args.quantity)
        if args.conditional_proposal:
            print_conditional_proposal(preview)
        else:
            print_preview(preview)
    except PreviewBlocked as error:
        print(f"PREVIEW BLOCKED | {error}. No order draft approved.")
        return 1
    except API_ERRORS as error:
        # Exception strings can contain a URL, response body or credential.
        # Only the reader's fixed, safe reason is displayed.
        print(f"PREVIEW BLOCKED | {account_error_reason(error)}. Eligibility or account state is unknown.")
        return 1
    except KeyboardInterrupt:
        print("Preview stopped before completion; no orders submitted.")
        return 1
    print("No orders submitted or cancelled. No paper portfolio, CSV, or account snapshot was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
