"""Supervised one-share competition test. Preparation and checks never place orders."""
import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import requests
from dotenv import load_dotenv

from account_reader import account_error_reason, read_account
from order_preview import account_risk, ceil_buy_limit
from paper_trader import MAX_CAPITAL_PER_RACE, MAX_CAPITAL_PER_TRADE, is_finite_number, parse_api_timestamp
from price_reader import (
    API_BASE_URL, API_ERRORS, MIN_LIQUIDITY, REQUEST_TIMEOUT,
    check_context, fetch_json, get_best_prices, get_exchange_id, numeric_id,
)

STATE_PATH = Path(__file__).resolve().with_name("account_test.json")
STATES = {"PREPARED", "SUBMITTING", "UNKNOWN", "ACCEPTED", "NOOP",
          "CANCEL_REQUESTED", "CANCEL_UNKNOWN", "OBSERVED_TERMINAL"}
# Only documented, fixed codes may be saved. Never save an exception message,
# arbitrary response text, URL, headers, or the secret used to authenticate.
SAFE_PLACEMENT_CODES = {"VALIDATION_ERROR", "TERMS_NOT_ACKNOWLEDGED",
                        "REQUEST_IN_FLIGHT", "RESIDENCE_UPDATE_REQUIRED"}
DIAGNOSTIC_KINDS = {"timeout", "connection-error", "request-error", "http-response", "invalid-receipt"}


class TestError(ValueError):
    """A fixed failure reason; stop and reload saved state before another action."""


def require(condition, reason):
    if not condition:
        raise TestError(reason)


def approval_hash(intent):
    # Bind approval to the immutable request, market and competition slug.
    # State, observations and order ID may change without changing approval.
    material = {key: intent[key] for key in ("market_id", "tournament_slug", "request")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate_intent(intent):
    require(isinstance(intent, dict) and type(intent["version"]) is int and intent["version"] == 1,
            "Invalid test journal version")
    require(isinstance(intent["market_id"], str), "Invalid test market identity")
    numeric_id(intent["market_id"])
    require(isinstance(intent["tournament_slug"], str)
            and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", intent["tournament_slug"]), "Invalid test competition slug")
    body = intent["request"]
    require(isinstance(body, dict) and set(body) == {
        "idempotencyKey", "exchangeId", "side", "action", "quantity", "price", "tournamentId", "expirationDate"},
        "Invalid test request fields")
    require(isinstance(body["exchangeId"], str), "Invalid test exchange identity")
    numeric_id(body["exchangeId"])
    require(body["action"] == "buy" and body["side"] in ("yes", "no")
            and type(body["quantity"]) is int and body["quantity"] == 1, "Only one-share limit buys are supported")
    price = body["price"]
    require(is_finite_number(price) and .005 <= price <= .995
            and Decimal(str(price)) % Decimal(".005") == 0, "Invalid test limit price")
    require(isinstance(body["tournamentId"], str) and str(UUID(body["tournamentId"])) == body["tournamentId"],
            "A test must have explicit competition scope")
    require(isinstance(body["idempotencyKey"], str)
            and re.fullmatch(r"account-test-[0-9a-f]{32}", body["idempotencyKey"]), "Invalid saved test key")
    parse_api_timestamp(body["expirationDate"])
    parse_api_timestamp(intent["created_at"])
    require(intent["approval"] == approval_hash(intent) and intent["state"] in STATES,
            "Test approval or state is invalid")
    diagnostic = intent.get("placement_diagnostic")
    if diagnostic is not None:
        require(intent["state"] == "UNKNOWN" and isinstance(diagnostic, dict)
                and set(diagnostic) == {"kind", "http_status", "api_code"}
                and diagnostic["kind"] in DIAGNOSTIC_KINDS,
                "Invalid placement diagnostic")
        status = diagnostic["http_status"]
        require(status is None or (type(status) is int and 100 <= status <= 599),
                "Invalid saved HTTP status")
        require(diagnostic["api_code"] is None or diagnostic["api_code"] in SAFE_PLACEMENT_CODES,
                "Unrecognized saved API error code")
    order_id = intent["order_id"]
    require(order_id is None or (type(order_id) is int and order_id > 0), "Invalid saved order ID")
    if intent["state"] in {"PREPARED", "SUBMITTING", "UNKNOWN", "NOOP"}:
        require(order_id is None, "Unconfirmed test contains an order ID")
    else:
        require(order_id is not None, "Confirmed test has no order ID")
    observation = intent["observation"]
    if intent["state"] in {"PREPARED", "SUBMITTING", "UNKNOWN"}:
        require(observation is None, "An unconfirmed test contains observations")
    else:
        require(isinstance(observation, dict)
                and set(observation).issubset({"placement_quantity", "placement_cost", "filled_quantity",
                                              "filled_cost", "open", "terminal_reason"}), "Invalid saved observation")
        for quantity_key, cost_key in (("placement_quantity", "placement_cost"), ("filled_quantity", "filled_cost")):
            require((quantity_key in observation) == (cost_key in observation), "Incomplete saved fill observation")
            if quantity_key in observation:
                quantity, cost = observation[quantity_key], observation[cost_key]
                require(is_finite_number(quantity) and is_finite_number(cost)
                        and 0 <= quantity <= 1 and 0 <= cost <= quantity * price + 1e-9,
                        "Invalid saved fill quantity or cost")
        require("placement_quantity" in observation, "A confirmed test has no placement observation")
        if "filled_quantity" in observation:
            require(type(observation.get("open")) is bool
                    and (observation.get("terminal_reason") is None or isinstance(observation["terminal_reason"], str))
                    and observation["filled_quantity"] + 1e-9 >= observation["placement_quantity"]
                    and observation["filled_cost"] + 1e-9 >= observation["placement_cost"], "Invalid saved cumulative observation")
            require(observation["open"] or intent["state"] == "OBSERVED_TERMINAL", "Closed observations need a terminal state")
        if intent["state"] == "OBSERVED_TERMINAL":
            require("filled_quantity" in observation and observation["open"] is False, "Terminal observation is incomplete")
        if intent["state"] == "NOOP":
            require(observation["placement_quantity"] == observation["placement_cost"] == 0, "A no-op contains fills")


def save_intent(intent, path, new=False):
    """New files are exclusive; updates replace atomically after flushing to disk."""
    temporary = None
    try:
        validate_intent(intent)
        path = Path(path)
        # Exclusive creation blocks overwriting another test, even a finished one.
        if new:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(intent, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        else:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(intent, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise TestError("Cannot save test journal; stop without further orders") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def load_intent(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            intent = json.load(stream)
        validate_intent(intent)
        return intent
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise TestError("Cannot restore test journal; never reset an unresolved test") from error


def commit_state(intent, path, state, **changes):
    proposed = copy.deepcopy(intent)
    proposed.update(changes, state=state)
    save_intent(proposed, path)
    intent.clear()
    intent.update(proposed)


def read_test_inputs(session, market_id, side, slug):
    """Fresh scoped GETs; a single directional test has no pair payout claim."""
    require(side in ("yes", "no"), "Choose YES or NO")
    started = time.monotonic()
    account = read_account(session, slug)
    tournament_id = account["tournament"]["id"]
    market_id = numeric_id(market_id)
    market = fetch_json(session, f"{API_BASE_URL}/markets/{market_id}", params={"tournamentId": tournament_id})
    require(numeric_id(market["id"]) == market_id and market["status"] == "open", "The selected market is wrong or closed")
    check_context(market["contexts"], tournament_id)
    exchange_id = get_exchange_id(market)
    risk = account_risk(account, [exchange_id])
    book = get_best_prices(session, market, tournament_id)
    require(book is not None, "The selected market has no executable book")
    book_side = "ask" if side == "yes" else "bid"
    quote = book[book_side]
    require(is_finite_number(quote) and 0 < quote < 1
            and is_finite_number(book[book_side + "_quantity"])
            and book[book_side + "_quantity"] >= MIN_LIQUIDITY, "The selected side needs at least 50 visible shares")
    price = ceil_buy_limit(quote if side == "yes" else float(Decimal(1) - Decimal(str(quote))))
    require(0 <= time.monotonic() - started <= 15
            and 0 <= time.monotonic() - book["received_at"] <= 5
            and -5 <= time.time() - book["quoted_at"] <= 5, "Account or book observations became stale")
    return account, exchange_id, price, risk


def check_risk(risk, price):
    # The single share is much smaller than the project's existing capital caps.
    # Existing account exposure still counts; no paper balance is substituted.
    require(price <= MAX_CAPITAL_PER_TRADE
            and risk["race_exposure_upper_bound"] + price <= MAX_CAPITAL_PER_RACE + 1e-9,
            "Existing account exposure leaves no room within the risk limits")
    require(price <= risk["available_cash"] + 1e-9, "Actual cash minus pending order reserves cannot cover the test")


def prepare_test(session, market_id, side, slug="midterm-elections"):
    account, exchange_id, price, risk = read_test_inputs(session, market_id, side, slug)
    check_risk(risk, price)
    now = datetime.now(timezone.utc)
    body = {"idempotencyKey": "account-test-" + uuid4().hex, "exchangeId": exchange_id,
            "side": side, "action": "buy", "quantity": 1, "price": price,
            "tournamentId": account["tournament"]["id"],
            "expirationDate": (now + timedelta(minutes=15)).isoformat(timespec="seconds")}
    intent = {"version": 1, "market_id": numeric_id(market_id), "tournament_slug": slug,
              "created_at": now.isoformat(timespec="seconds"), "request": body,
              "state": "PREPARED", "order_id": None, "observation": None}
    intent["approval"] = approval_hash(intent)
    validate_intent(intent)
    return intent


def require_approval(intent, approval):
    validate_intent(intent)
    require(isinstance(approval, str) and approval == intent["approval"], "Exact saved order approval is required")


def safe_placement_diagnostic(response, error):
    """Keep useful failure categories without retaining raw server or key data."""
    if response is not None:
        status = response.status_code
        status = status if type(status) is int and 100 <= status <= 599 else None
        kind = "invalid-receipt" if status == 200 else "http-response"
    else:
        status = None
        kind = ("timeout" if isinstance(error, requests.Timeout) else
                "connection-error" if isinstance(error, requests.ConnectionError) else "request-error")
    code = None
    if response is not None and status != 200:
        try:
            payload = response.json()
            server_error = payload.get("error") if isinstance(payload, dict) else None
            candidate = server_error.get("code") if isinstance(server_error, dict) else None
            if isinstance(candidate, str) and candidate in SAFE_PLACEMENT_CODES:
                code = candidate
        except (ValueError, TypeError):
            pass
    return {"kind": kind, "http_status": status, "api_code": code}


def print_placement_diagnostic(intent):
    if intent["state"] != "UNKNOWN":
        return
    diagnostic = intent.get("placement_diagnostic")
    if diagnostic is None:
        print("No placement error diagnostic was recorded for this earlier attempt.")
    else:
        print(f"Placement diagnostic: {diagnostic['kind']} | HTTP {diagnostic['http_status']} | "
              f"documented API code {diagnostic['api_code']}")


def submit_test(session, intent, path, approval, recover=False):
    """Exactly one POST per invocation. Uncertain replies never trigger retries.

    GET lists have no client key and cannot prove that a missing order never ran.
    No replay is implemented without a documented deduplication retention window.
    """
    require_approval(intent, approval)
    require(not recover, "Receipt replay is disabled; an unknown placement needs manual reconciliation")
    require(intent["state"] == "PREPARED", "This test was already attempted; never submit a replacement")
    body = intent["request"]
    if not recover:
        require(parse_api_timestamp(body["expirationDate"]).timestamp() > time.time(), "The prepared order has expired")
        account, exchange_id, current_price, risk = read_test_inputs(
            session, intent["market_id"], body["side"], intent["tournament_slug"])
        require(account["tournament"]["id"] == body["tournamentId"] and exchange_id == body["exchangeId"],
                "The prepared market or competition identity changed")
        require(current_price <= body["price"] + 1e-12, "The current quote exceeds the approved limit; no order sent")
        check_risk(risk, body["price"])
    # Check again after the GET preflight; the approved expiry must not pass
    # during those reads. The request body itself is never refreshed or changed.
    require(parse_api_timestamp(body["expirationDate"]).timestamp() > time.time(), "The prepared order has expired")
    commit_state(intent, path, "SUBMITTING")
    response = None
    try:
        response = session.post(f"{API_BASE_URL}/orders", json=copy.deepcopy(body),
                                timeout=REQUEST_TIMEOUT, allow_redirects=False)
        require(response.status_code == 200, "The placement reply is unconfirmed")
        result = response.json()
        require(numeric_id(result["exchangeId"]) == body["exchangeId"]
                and result["action"] == "buy" and result["side"] == body["side"]
                and type(result["quantity"]) is int and result["quantity"] == 1
                and result["price"] == body["price"] and type(result["open"]) is bool,
                "The placement receipt conflicts with the approved intent")
        traded, cost, remaining = result["quantityTraded"], result["totalCost"], result["remainingQuantity"]
        require(all(is_finite_number(value) for value in (traded, cost, remaining))
                and 0 <= traded <= 1 and 0 <= remaining <= 1 and traded + remaining <= 1 + 1e-9
                and 0 <= cost <= traded * body["price"] + 1e-9, "Invalid placement quantities or cost")
        require((remaining > 0 and math.isclose(traded + remaining, 1, rel_tol=0, abs_tol=1e-9))
                if result["open"] else remaining == 0, "Placement open state conflicts with its remainder")
        fill_price = result["fillPrice"]
        require(fill_price is None if traded == 0 else is_finite_number(fill_price) and 0 <= fill_price <= body["price"],
                "Placement fill price conflicts with the limit")
        order_id = result["orderId"]
        require(order_id is None or (type(order_id) is int and order_id > 0), "Invalid placement order ID")
        require(order_id is not None or (not result["open"] and traded == cost == remaining == 0),
                "An unidentified order may have filled")
    except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError) as error:
        commit_state(intent, path, "UNKNOWN", placement_diagnostic=safe_placement_diagnostic(response, error))
        raise TestError("Order outcome is UNKNOWN; do not create another test or key") from error
    # If this save fails, SUBMITTING remains on disk and blocks a fresh order.
    commit_state(intent, path, "NOOP" if order_id is None else "ACCEPTED", order_id=order_id,
                 observation={"placement_quantity": traded, "placement_cost": cost})
    return intent


def read_order(session, intent):
    require(intent["order_id"] is not None, "No confirmed order ID; GET reads cannot recover the lost receipt")
    body, order_id = intent["request"], intent["order_id"]
    order = fetch_json(session, f"{API_BASE_URL}/orders/{order_id}")
    require(type(order["id"]) is int and order["id"] == order_id
            and numeric_id(order["exchangeId"]) == body["exchangeId"]
            and order["tournamentId"] == body["tournamentId"]
            and order["side"] == body["side"] and order["action"] == "buy"
            and order["priceLimit"] == body["price"] and type(order["open"]) is bool,
            "Order details conflict with the approved scope or identity")
    require(is_finite_number(order["quantity"]) and 0 <= order["quantity"] <= 1, "Invalid order quantity")
    require(not (intent["state"] == "OBSERVED_TERMINAL" and order["open"]), "A terminal order reopened in reporting")
    require(parse_api_timestamp(order["expirationDate"]) == parse_api_timestamp(body["expirationDate"]),
            "Order expiry conflicts with the saved intent")
    return order


def check_test(session, intent, path):
    """Read specific order and every fill page; a cancelled share remains held."""
    validate_intent(intent)
    order = read_order(session, intent)
    body, order_id = intent["request"], intent["order_id"]
    params = {"limit": 200}
    seen_cursors, seen_fills = set(), set()
    quantity, cost, lifecycle = 0, 0, None
    sign = 1 if body["side"] == "yes" else -1
    for _ in range(50):
        payload = fetch_json(session, f"{API_BASE_URL}/orders/{order_id}/fills", params=params.copy())
        require(type(payload["orderId"]) is int and payload["orderId"] == order_id
                and numeric_id(payload["exchangeId"]) == body["exchangeId"]
                and payload["tournamentId"] == body["tournamentId"]
                and payload["coverage"]["complete"] is True and isinstance(payload["data"], list),
                "Fill history has invalid scope or coverage")
        total, average = payload["totalQuantityFilled"], payload["avgFillPrice"]
        require(is_finite_number(total) and 0 <= sign * total <= 1
                and (average is None if total == 0 else is_finite_number(average) and 0 <= average <= body["price"]),
                "Invalid cumulative fill history")
        require(lifecycle is None or lifecycle == (total, average), "Fill history changed during pagination; check again")
        lifecycle = (total, average)
        for fill in payload["data"]:
            require(type(fill["id"]) is int and fill["id"] > 0 and fill["id"] not in seen_fills
                    and fill["side"] == body["side"] and is_finite_number(fill["quantity"])
                    and 0 < sign * fill["quantity"] <= 1 and is_finite_number(fill["price"])
                    and 0 <= fill["price"] <= body["price"], "Invalid or duplicate fill")
            seen_fills.add(fill["id"])
            parse_api_timestamp(fill["filledAt"])
            quantity += sign * fill["quantity"]
            cost += sign * fill["quantity"] * fill["price"]
        pagination = payload["pagination"]
        require(type(pagination["hasMore"]) is bool, "Invalid fill pagination")
        if not pagination["hasMore"]:
            break
        cursor = pagination["nextCursor"]
        require(isinstance(cursor, str) and bool(cursor) and cursor not in seen_cursors, "Invalid fill cursor")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
    else:
        raise TestError("Fill pagination is incomplete")
    require(math.isclose(quantity, sign * total, rel_tol=0, abs_tol=1e-9)
            and math.isclose(cost, quantity * (average or 0), rel_tol=0, abs_tol=1e-9),
            "Fill pages do not match lifecycle totals")
    prior = intent["observation"] or {}
    require(quantity + 1e-9 >= prior.get("filled_quantity", prior.get("placement_quantity", 0))
            and cost + 1e-9 >= prior.get("filled_cost", prior.get("placement_cost", 0)),
            "Reporting has not caught up with previously confirmed fills")
    prior_quantity = prior.get("filled_quantity", prior.get("placement_quantity", 0))
    prior_cost = prior.get("filled_cost", prior.get("placement_cost", 0))
    require(cost - prior_cost <= (quantity - prior_quantity) * body["price"] + 1e-9,
            "Confirmed fill cost changed without matching new shares")
    if intent["state"] == "OBSERVED_TERMINAL":
        require(math.isclose(quantity, prior_quantity, rel_tol=0, abs_tol=1e-9)
                and math.isclose(cost, prior_cost, rel_tol=0, abs_tol=1e-9),
                "Closed reporting history changed; inspect the account manually")
    if order["open"]:
        require(quantity + order["quantity"] <= 1 + 1e-9, "Order remainder conflicts with fill history")
        require(intent["state"] != "OBSERVED_TERMINAL", "A terminal order reopened in reporting")
    else:
        require(is_finite_number(order["quantityFilled"])
                and math.isclose(order["quantityFilled"], quantity, rel_tol=0, abs_tol=1e-9),
                "Closed order and fills disagree; check again")
    observation = {**prior, "filled_quantity": quantity, "filled_cost": cost,
                   "open": order["open"], "terminal_reason": order.get("terminalReasonCode")}
    # Preserve an uncertain cancel until GET explicitly reports the order closed.
    state = intent["state"] if order["open"] else "OBSERVED_TERMINAL"
    commit_state(intent, path, state, observation=observation)
    return intent


def cancel_test(session, intent, path, approval):
    require_approval(intent, approval)
    order = read_order(session, intent)
    if not order["open"]:
        return check_test(session, intent, path)
    commit_state(intent, path, "CANCEL_REQUESTED")
    try:
        response = session.delete(f"{API_BASE_URL}/orders/{intent['order_id']}",
                                  timeout=REQUEST_TIMEOUT, allow_redirects=False)
        require(response.status_code in (200, 409), "Cancellation is unconfirmed")
        if response.status_code == 200:
            result = response.json()
            require(type(result["orderId"]) is int and result["orderId"] == intent["order_id"]
                    and result["tournamentId"] == intent["request"]["tournamentId"], "Cancellation scope conflicts")
    except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError) as error:
        commit_state(intent, path, "CANCEL_UNKNOWN")
        raise TestError("Cancellation outcome is UNKNOWN; preserve held shares and check this order") from error
    return check_test(session, intent, path)


def print_intent(intent):
    print(f"SUPERVISED ACCOUNT TEST | {intent['state']} | order ID: {intent['order_id']}")
    print_placement_diagnostic(intent)
    print(json.dumps(intent["request"], indent=2, allow_nan=False))
    print(f"Approval fingerprint: {intent['approval']}")
    print(f"Contract spend cap: {intent['request']['price']:.3f} SUSQies for one share; fees unverified.")
    print("A fill creates a held share. Cancelling remainder does not refund that share. This is a directional mechanics test.")
    if intent["observation"]:
        print(json.dumps(intent["observation"], indent=2, allow_nan=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "show", "submit", "check", "cancel"))
    parser.add_argument("--market", help="Market ID for preparation")
    parser.add_argument("--side", choices=("yes", "no"), help="Outcome for preparation")
    parser.add_argument("--tournament", default="midterm-elections")
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--approve", help="Exact saved approval fingerprint for a write operation")
    args = parser.parse_args()
    if args.action == "prepare" and (args.market is None or args.side is None):
        parser.error("Preparation requires --market and --side")
    try:
        if args.action == "prepare":
            require(not args.state.exists(), "A test journal already exists; do not overwrite or reset it")
            require(not STATE_PATH.with_name("paired_account_test").exists(),
                    "A paired test journal exists; resolve it before preparing another account test")
        else:
            intent = load_intent(args.state)
            if args.action == "show":
                print_intent(intent)
                return 0
            if args.action in ("submit", "cancel"):
                require_approval(intent, args.approve)
        load_dotenv(Path(__file__).resolve().with_name(".env"))
        api_key = os.getenv("SIG_API_KEY")
        require(bool(api_key), "Set SIG_API_KEY locally before the account test")
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {api_key}"})
            if args.action == "prepare":
                intent = prepare_test(session, args.market, args.side, args.tournament)
                save_intent(intent, args.state, new=True)
            elif args.action == "submit":
                submit_test(session, intent, args.state, args.approve)
            elif args.action == "check":
                check_test(session, intent, args.state)
            else:
                cancel_test(session, intent, args.state, args.approve)
        print_intent(intent)
        print("Paper portfolio and scanner were not used. Preparation/show/check do not submit or cancel orders.")
        return 0
    except TestError as error:
        print(f"Account test stopped: {error}")
    except API_ERRORS as error:
        print(f"Account test stopped: {account_error_reason(error)}. State remains unresolved; no automatic retry.")
    except KeyboardInterrupt:
        print("Account test interrupted. Restore the journal before any further operation.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
