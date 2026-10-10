"""Fresh, GET-only pilot account observations. Never authorize or submit orders.

The public API currently does not establish an exact trade-debit/fee model.
Reading all available records is useful, but cannot turn that ambiguity into
permission to trade. ACCOUNTING_MODEL_UNVERIFIED therefore remains a hard gate.
"""
import argparse
import copy
import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

import account_reader
import config
import price_reader as scanner
from order_preview import MAX_ACCOUNT_READ_AGE
from paper_trader import parse_api_timestamp

BALANCE_UNAVAILABLE = "ACCOUNT_BALANCE_UNAVAILABLE"
POSITIONS_UNAVAILABLE = "POSITIONS_UNAVAILABLE"
ORDERS_UNAVAILABLE = "OPEN_ORDERS_UNAVAILABLE"
RECONCILIATION_UNAVAILABLE = "FILL_TRANSACTION_RECONCILIATION_UNAVAILABLE"
STALE = "STALE_ACCOUNT_DATA"
INCONSISTENT = "ACCOUNT_STATE_INCONSISTENT"
INSUFFICIENT_BALANCE = "INSUFFICIENT_ACCOUNT_BALANCE"
ACCOUNTING_UNVERIFIED = "ACCOUNTING_MODEL_UNVERIFIED"
HISTORICAL_POLICY = "HISTORICALLY_RECONCILED_ZERO_EXTRA_FEE_ASSUMPTION"
# Same conservative delta bound as the supervised review: up to one cent of
# error in each two-decimal balance reading. This is NOT proof of zero fees.
BALANCE_DELTA_TOLERANCE = Decimal(".02")
MAX_HISTORY_PAGES = 50
MAX_RECENT_ORDERS = 50
API_REFERENCE = "https://sig.thesuper.market/api/v1/docs"


class AccountReadinessBlocked(ValueError):
    """Only fixed local reasons are displayed; never HTTP bodies or credentials."""

    def __init__(self, code, reason):
        self.code = code
        super().__init__(f"{code} | {reason}")


def require(condition, code, reason):
    if not condition:
        raise AccountReadinessBlocked(code, reason)


def check_fresh(started):
    age = time.monotonic() - started
    require(0 <= age <= MAX_ACCOUNT_READ_AGE, STALE,
            "Account read exceeded the existing 15-second freshness window")


def read_current_account(session, slug="midterm-elections", allow_inactive=False):
    """Reuse the existing validators, retaining which essential read failed."""
    class Reads:
        stage = BALANCE_UNAVAILABLE

        def get(self, url, **kwargs):
            self.stage = (POSITIONS_UNAVAILABLE if url.endswith("/portfolio/positions") else
                          ORDERS_UNAVAILABLE if url.endswith("/orders") else BALANCE_UNAVAILABLE)
            return session.get(url, **kwargs)

    reads = Reads()
    try:
        return account_reader.read_account(reads, slug, allow_inactive=allow_inactive)
    except scanner.API_ERRORS as error:
        raise AccountReadinessBlocked(reads.stage, "Required account response unavailable or invalid") from error


def get_data(session, path, params, started):
    check_fresh(started)
    try:
        result = scanner.fetch_json(session, scanner.API_BASE_URL + path, params=params)
    except AccountReadinessBlocked:
        raise
    except scanner.API_ERRORS as error:
        raise AccountReadinessBlocked(RECONCILIATION_UNAVAILABLE,
                                      "Required fill/order/transaction read failed; no retry") from error
    check_fresh(started)
    return result


def fill_identity(row):
    # A trade ID is shared by counterparties; it is NOT an order ID. Two
    # different orders can legitimately have fills with the same trade ID.
    return row["id"], row.get("orderId")


def validate_fill(row, portfolio=True):
    require(type(row["id"]) is int and row["id"] > 0, RECONCILIATION_UNAVAILABLE, "Invalid fill ID")
    if portfolio:
        require(type(row["orderId"]) is int and row["orderId"] > 0,
                RECONCILIATION_UNAVAILABLE, "Recent fill has no receipt; never infer an order ID")
        scanner.numeric_id(row["marketId"])
        scanner.numeric_id(row["exchangeId"])
    quantity = account_reader.require_number(row["quantity"])
    price = account_reader.require_number(row["price"])
    require(row["side"] in {"yes", "no"} and quantity != 0 and
            (quantity > 0) == (row["side"] == "yes") and 0 <= price <= 1,
            RECONCILIATION_UNAVAILABLE, "Invalid signed quantity or side-relative fill price")
    return parse_api_timestamp(row["filledAt"])


def validate_transaction(row, tournament_id):
    require(isinstance(row["event_id"], str) and bool(row["event_id"]) and
            row["tournamentId"] == tournament_id and row["event_type"] in
            {"trade", "settlement", "deposit", "fee", "market_creation"},
            RECONCILIATION_UNAVAILABLE, "Invalid or incorrectly scoped transaction")
    account_reader.require_number(row["quantity"])
    if row["event_type"] == "trade":
        require(row["amount"] is None, RECONCILIATION_UNAVAILABLE,
                "Trade debit schema changed; accounting needs review")
        require(0 <= account_reader.require_number(row["price"]) <= 1,
                RECONCILIATION_UNAVAILABLE, "Trade price unavailable")
        scanner.numeric_id(row["exchangeId"])
        scanner.numeric_id(row["marketId"])
    else:
        # Signed amount is documented for ordinary non-trade cash events.
        # ALL events instead report collateralDelta: never sum them as cash.
        account_reader.require_number(row["amount"])
    return parse_api_timestamp(row["createdAt"])


def canonical_transaction(row):
    """Keep execution/cash evidence, excluding only known mutable display data.

    Use an exclusion list so new order/fill links or accounting fields are
    checked automatically. Unknown fields are never silently discarded.
    """
    require(isinstance(row, dict) and isinstance(row.get("event_id"), str)
            and bool(row["event_id"]), RECONCILIATION_UNAVAILABLE, "Missing transaction identity")
    validate_transaction(row, row["tournamentId"])
    result = {key: value for key, value in row.items()
              if key not in {"currentPrice", "marketTitle", "marketImage"}}
    # Z and UTC offsets describe the same immutable execution timestamp.
    result["createdAt"] = parse_api_timestamp(row["createdAt"]).astimezone(timezone.utc).isoformat()
    json.dumps(result, allow_nan=False)  # Malformed new accounting fields fail closed too.
    return result


def canonical_transaction_history(rows):
    """Compare by unique event ID; API list order is not an economic event.

    Changing which event belongs to which ID still fails, as does a duplicate
    even when both copies happen to contain identical financial values.
    """
    require(isinstance(rows, list), RECONCILIATION_UNAVAILABLE, "Invalid transaction history")
    result = {}
    for row in rows:
        canonical = canonical_transaction(row)
        key = canonical["event_id"]
        require(key not in result, RECONCILIATION_UNAVAILABLE, "Duplicate transaction identity")
        result[key] = canonical
    return result


def transaction_histories_equal(before, after):
    """Malformed, duplicated or economically changed history fails closed."""
    try:
        return canonical_transaction_history(before) == canonical_transaction_history(after)
    except scanner.API_ERRORS:
        return False


def account_execution_state(account):
    """Remove position marks, retaining cash, entry cost, lots and settlement."""
    result = copy.deepcopy(account)
    for position in result["positions"]:
        for key in ("currentPrice", "marketValue", "unrealizedPnl", "unrealizedPnlPct"):
            position.pop(key, None)
    result["positions"].sort(key=lambda p: scanner.numeric_id(p["exchangeId"]))
    for key in ("totalMarketValue", "totalUnrealizedPnl"):
        result["summary"].pop(key, None)
    return result


def read_history(session, path, started, validate, identity, since=None):
    """Read the whole relevant window, or the full lifecycle for a known order.

    Empty/incomplete pages and repeated cursors never mean zero exposure. A
    coverage flag describes the server's page walk, not atomic account state.
    """
    rows, seen, cursors = [], set(), set()
    params, previous_time, previous_sequence = {"limit": 200}, None, None
    head, first_payload = None, None
    for _ in range(MAX_HISTORY_PAGES):
        payload = get_data(session, path, params.copy(), started)
        require(isinstance(payload, dict) and isinstance(payload["data"], list) and
                payload["coverage"]["complete"] is True,
                RECONCILIATION_UNAVAILABLE, "History coverage is incomplete")
        sequence = payload["coverage"].get("projectedThroughSequence")
        require(sequence is None or (type(sequence) is int and sequence >= 0 and
                (previous_sequence is None or sequence >= previous_sequence)),
                RECONCILIATION_UNAVAILABLE, "History reporting projection regressed")
        previous_sequence = sequence
        if first_payload is None:
            first_payload, head = payload, payload["data"][:1]
        past_window = False
        for row in payload["data"]:
            timestamp = validate(row)
            require(timestamp <= datetime.now(timezone.utc) + timedelta(seconds=5),
                    RECONCILIATION_UNAVAILABLE, "History timestamp is unexpectedly in the future")
            require(previous_time is None or timestamp <= previous_time,
                    RECONCILIATION_UNAVAILABLE, "History is not newest first")
            previous_time = timestamp
            key = identity(row)
            require(key not in seen, RECONCILIATION_UNAVAILABLE, "History has a repeated event")
            seen.add(key)
            if since is None or timestamp >= since:
                rows.append(row)
            else:
                past_window = True
        pagination = payload["pagination"]
        require(type(pagination["hasMore"]) is bool, RECONCILIATION_UNAVAILABLE, "Invalid history pagination")
        if not pagination["hasMore"] or past_window:
            return rows, first_payload, head
        cursor = pagination["nextCursor"]
        require(isinstance(cursor, str) and cursor and cursor not in cursors and payload["data"],
                RECONCILIATION_UNAVAILABLE, "Missing/repeated history cursor or empty continuing page")
        cursors.add(cursor)
        params["cursor"] = cursor
    raise AccountReadinessBlocked(RECONCILIATION_UNAVAILABLE, "History page budget exhausted")


def read_order_activity(session, order_id, recent_fills, account, started):
    """Look up actual receipts; no synthetic intent, idempotency key or retry."""
    order = get_data(session, f"/orders/{order_id}", None, started)
    tid = account["tournament"]["id"]
    require(order["id"] == order_id and type(order["id"]) is int and order["tournamentId"] == tid and
            type(order["open"]) is bool and order["action"] in {"buy", "sell"} and order["side"] in {"yes", "no"},
            RECONCILIATION_UNAVAILABLE, "Order receipt identity/scope is invalid")
    exchange_id = scanner.numeric_id(order["exchangeId"])
    require(account_reader.require_number(order["quantity"], nonnegative=True) >= 0,
            RECONCILIATION_UNAVAILABLE, "Invalid order remainder")
    parse_api_timestamp(order["createdAt"])
    if order["expirationDate"] is not None:
        parse_api_timestamp(order["expirationDate"])
    if order["priceLimit"] is not None:
        require(0 <= account_reader.require_number(order["priceLimit"]) <= 1,
                RECONCILIATION_UNAVAILABLE, "Invalid order limit")
    lifecycle = []

    def validate(row):
        require(row["side"] == order["side"], RECONCILIATION_UNAVAILABLE, "Order/fill side differs")
        return validate_fill(row, portfolio=False)

    # Check lifecycle metadata on EVERY page, not just the first.
    class FillReads:
        def get(self, url, **kwargs):
            response = session.get(url, **kwargs)
            if response.status_code == 200:
                p = response.json()
                require(p["orderId"] == order_id and scanner.numeric_id(p["exchangeId"]) == exchange_id and
                        p["tournamentId"] == tid, RECONCILIATION_UNAVAILABLE, "Order-fill scope differs")
                totals = p["totalQuantityFilled"], p["avgFillPrice"]
                require(not lifecycle or totals == lifecycle[0], RECONCILIATION_UNAVAILABLE,
                        "Order fills changed during pagination")
                lifecycle.append(totals)
            return response

    fills, _, _ = read_history(FillReads(), f"/orders/{order_id}/fills", started, validate, lambda r: r["id"])
    quantity = sum((Decimal(str(r["quantity"])) for r in fills), Decimal(0))
    notional = sum((abs(Decimal(str(r["quantity"]))) * Decimal(str(r["price"])) for r in fills), Decimal(0))
    total, average = lifecycle[0]
    require(abs(quantity - Decimal(str(account_reader.require_number(total)))) <= Decimal(".000000001") and
            ((not fills and average is None) or (fills and
             abs(notional / abs(quantity) - Decimal(str(account_reader.require_number(average)))) <= Decimal(".000000001"))),
            RECONCILIATION_UNAVAILABLE, "Fill rows disagree with lifecycle totals")
    if not order["open"]:
        require(order["quantityFilled"] is not None and
                abs(abs(quantity) - Decimal(str(account_reader.require_number(order["quantityFilled"], True))))
                <= Decimal(".000000001") and abs(quantity) <= Decimal(str(order["quantity"])) + Decimal(".000000001"),
                RECONCILIATION_UNAVAILABLE, "Closed order disagrees with filled quantity")
    active = next((r for r in account["orders"] if r["id"] == order_id), None)
    require(order["open"] == (active is not None) and (active is None or
            all(order[k] == active[k] for k in active)), INCONSISTENT, "Open-order inventory changed during the read")
    by_id = {r["id"]: r for r in fills}
    for row in recent_fills:
        if row["orderId"] == order_id:
            other = by_id.get(row["id"])
            require(scanner.numeric_id(row["exchangeId"]) == exchange_id and other is not None and
                    all(row[k] == other[k] for k in ("side", "quantity", "filledAt")) and
                    abs(Decimal(str(row["price"])) - Decimal(str(other["price"]))) <= Decimal(".000000001"),
                    RECONCILIATION_UNAVAILABLE, "Portfolio and order fills disagree")
    return {"order": order, "fills": fills, "fill_notional": str(notional)}


def read_snapshot(session, slug="midterm-elections", checkpoint=None, initial_account=None, started=None,
                  order_ids=None, allow_inactive=False):
    """Bracket history reads with account/head rereads; return only complete data.

    This is a checked sequential observation, not a transactional API snapshot.
    Cached tournament data can be up to two seconds old. Local age starts BEFORE
    the first GET, and is checked again when the snapshot is actually used.
    """
    started = time.monotonic() if started is None else started
    observed = datetime.now(timezone.utc)
    since = observed - timedelta(days=1)
    if checkpoint is not None:
        since = min(since, parse_api_timestamp(checkpoint["created_at"]))
    try:
        account = read_current_account(session, slug, allow_inactive) if initial_account is None else copy.deepcopy(initial_account)
        require(account["tournament"]["slug"] == slug, INCONSISTENT, "Account slug changed")
        base = f"/tournaments/{slug}/portfolio"
        def recent_fill(row):
            timestamp = parse_api_timestamp(row["filledAt"])
            if timestamp >= since:
                validate_fill(row)
            return timestamp

        def recent_transaction(row):
            timestamp = parse_api_timestamp(row["createdAt"])
            if timestamp >= since:
                validate_transaction(row, account["tournament"]["id"])
            return timestamp

        fills, fill_page, fill_head = read_history(session, base + "/fills", started, recent_fill, fill_identity, since)
        transactions, transaction_page, transaction_head = read_history(
            session, base + "/transactions", started, recent_transaction, lambda row: row["event_id"], since)
        # Omit the type=trade filter deliberately: fees, deposits, settlement
        # and collateral events can change account cash even without new fills.
        # Autonomous v0.1 only needs lifecycle receipts for the order it just
        # placed. Inspecting every historical receipt on every cycle eventually
        # exceeds both the freshness window and the read budget. Other callers
        # retain the original full-history behavior by leaving this as None.
        order_ids = sorted({r["orderId"] for r in fills} | {r["id"] for r in account["orders"]}) if order_ids is None else list(order_ids)
        require(len(order_ids) <= MAX_RECENT_ORDERS, RECONCILIATION_UNAVAILABLE,
                "Too many recent receipts for a fresh complete read")
        activity = [read_order_activity(session, oid, fills, account, started) for oid in order_ids]
        final_account = read_current_account(session, slug, allow_inactive)
        require(account_execution_state(final_account) == account_execution_state(account),
                INCONSISTENT, "Balance, holdings, orders or quarantine changed during the read")
        for path, old_head in ((base + "/fills", fill_head), (base + "/transactions", transaction_head)):
            latest = get_data(session, path, {"limit": 200}, started)
            same_head = (transaction_histories_equal(latest["data"][:1], old_head)
                         if path.endswith("/transactions") else latest["data"][:1] == old_head)
            require(latest["coverage"]["complete"] is True and same_head,
                    INCONSISTENT, "Account activity changed during the read")
        check_fresh(started)
        return {"account": account, "recent_fills": fills, "recent_transactions": transactions,
                "order_activity": activity, "history_since": since.isoformat(), "data_complete": True,
                "freshness": {"started_monotonic": started, "completed_monotonic": time.monotonic(),
                              "observed_at": observed.isoformat(), "max_age_seconds": MAX_ACCOUNT_READ_AGE,
                              "documented_account_cache_seconds": 2},
                "coverage": {"fills": fill_page["coverage"], "transactions": transaction_page["coverage"]}}
    except AccountReadinessBlocked:
        raise
    except scanner.API_ERRORS as error:
        raise AccountReadinessBlocked(RECONCILIATION_UNAVAILABLE, "Account history unavailable or malformed") from error


def accounting_model(snapshot=None):
    """Known documented facts, without treating absent fees as a zero-fee rule.

    No configuration switch or generic boolean can mark this model verified.
    An exact, receipt-linked debit authority and a fee bound are still missing.
    """
    transactions = [] if snapshot is None else snapshot["recent_transactions"]
    return {"status": ACCOUNTING_UNVERIFIED, "verified": False, "source": API_REFERENCE,
            "balance_field": "tournament.myBalance (rounded to two decimals)",
            "fill_notional": "abs(quantity) * price; side-relative price, no fee field",
            "transaction_cash_field": "amount for non-trade events; null for trades; collateralDelta for ALL events",
            "placement_totalCost": "fill notional, not guaranteed cash debit",
            "all_effectiveEntryCost": "authoritative ALL entry cash debit when present in the placement response",
            "confirmed_pilot_debit_field": None,
            "idempotency": {"supported": True, "request_field": "idempotencyKey",
                            "required": True, "same_resolved_payload": "replay stored response",
                            "different_resolved_payload": "HTTP 409", "retention": "not documented",
                            "get_lookup_by_key": "not documented", "automatic_post_retry": False},
            "alternative_accounting_evidence": {
                "admin_ledger": "/dmm/tournaments/{slug}/transactions (admin only)",
                "admin_cash_fields": "MONEY amount and balanceAfter; COLLATERAL balanceAfter is advance, not cash",
                "admin_order_link": "no order/client-key field documented",
                "account_balance": "GET /account uses default tournament/global scope; exact precision unspecified",
                "collateral": "GET /portfolio/collateral?tournamentId=... exposes advances, not per-order cash debits"},
            "fee_policy_evidence": "Official docs exclude real-money fees; an exact SUSQie charge bound is not established",
            "observed_fee_events": sum(r["event_type"] == "fee" for r in transactions),
            "reason": "No documented receipt-linked all-in cash debit or guaranteed trading-fee bound; do not infer zero fees"}


def assess_snapshot(snapshot, checkpoint, exchange_ids, capital, quantity=1):
    """Assess structural consistency/risk even when fee verification blocks use."""
    # Import locally to keep the existing pilot state/risk logic as the sole
    # owner of allocation accounting; this module does not write pilot state.
    import live_pilot as pilot
    check_fresh(snapshot["freshness"]["started_monotonic"])
    require(snapshot["data_complete"] is True, RECONCILIATION_UNAVAILABLE, "Incomplete account snapshot")
    failures, risk = [], None
    account = snapshot["account"]
    try:
        pilot.validate_checkpoint(checkpoint)
        cash, proposed = pilot.amount(account["tournament"]["myBalance"]), pilot.amount(capital)
        floor = pilot.amount(checkpoint["untouchable_cash_reserve"])
        if cash < floor + proposed:
            failures.append({"code": INSUFFICIENT_BALANCE, "reason": "Account cash cannot cover the pair without spending reserve"})
        if checkpoint["tournament_id"] != account["tournament"]["id"] or cash != pilot.amount(checkpoint["last_reconciled_account_cash"]):
            failures.append({"code": INCONSISTENT, "reason": "Actual cash/scope differs from durable pilot accounting"})
        # Gross debit records from older diagnostics are not receipt-linked cash
        # proof. Do not adopt them as authoritative accounting after restart.
        if set(checkpoint["accounted_pair_costs"]) != set(checkpoint["live_exposures"]):
            failures.append({"code": INCONSISTENT, "reason": "Durable debits lack matching saved live exposure"})
        risk = pilot.pilot_risk(account, exchange_ids, capital, quantity, checkpoint)
    except scanner.API_ERRORS as error:
        reason = str(error) if isinstance(error, pilot.PilotBlocked) else "Account risk/overlap checks failed"
        code = INCONSISTENT if any(word in reason for word in ("disagree", "checkpoint", "cash changed")) else "LIVE_ACCOUNT_RISK_FAILED"
        failures.append({"code": code, "reason": reason})
    model = accounting_model(snapshot)
    failures.append({"code": model["status"], "reason": model["reason"]})
    return {"ready": False, "account_checks_passed": len(failures) == 1, "failures": failures,
            "risk": risk, "accounting": model, "freshness": snapshot["freshness"],
            "live_eligible": False, "submission_enabled": False}


def require_verified_accounting():
    """Reconciliation must not label a notional charge an exact cash debit."""
    model = accounting_model()
    require(model["verified"] is True, model["status"], model["reason"])


def assess_autonomous_snapshot(snapshot, checkpoint, exchange_ids, capital, quantity=1):
    """Separate assumption-based policy; never change the ordinary verified gate.

    Only the named historical model can replace ACCOUNTING_MODEL_UNVERIFIED
    in this assessment. The coordinator must reconcile every leg and cumulative
    balance immediately, and permanently halt on the first discrepancy.
    """
    result = assess_snapshot(snapshot, checkpoint, exchange_ids, capital, quantity)
    result["failures"] = [row for row in result["failures"] if row["code"] != ACCOUNTING_UNVERIFIED]
    if result["accounting"]["observed_fee_events"]:
        result["failures"].append({"code": "ACCOUNTING_MODEL_MISMATCH", "reason": "Fee events contradict the pilot assumption"})
    result["accounting"] = {**result["accounting"], "policy": HISTORICAL_POLICY,
                            "formally_verified": False, "balance_delta_tolerance": str(BALANCE_DELTA_TOLERANCE),
                            "requires_immediate_reconciliation": True, "mismatch_action": "HALTED_MANUAL_REVIEW"}
    result["ready"] = not result["failures"]
    # Passing this local assessment never enables submission.
    return result


def require_ready(assessment):
    if assessment["failures"]:
        first = assessment["failures"][0]
        raise AccountReadinessBlocked(first["code"], first["reason"])
    require(assessment["ready"] is True, ACCOUNTING_UNVERIFIED, "No verified account readiness result")


def main():
    """Read-only audit works with an empty LIVE allowlist and no saved baseline.

    Never initialize/repair state here. Take the existing exclusive lock before
    loading an optional checkpoint. With no baseline, report that as a blocker.
    """
    import live_pilot as pilot
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tournament", default="midterm-elections")
    args = parser.parse_args()
    try:
        with pilot.pilot_lock():
            checkpoint = pilot.load_checkpoint() if pilot.ALLOCATION_PATH.exists() else None
            load_dotenv(Path(__file__).resolve().with_name(".env"))
            key = os.getenv("SIG_API_KEY")
            require(bool(key), BALANCE_UNAVAILABLE, "Local API key unavailable")
            with requests.Session() as session:
                session.headers.update({"Authorization": f"Bearer {key}"})
                snapshot = read_snapshot(session, args.tournament, checkpoint)
            model = accounting_model(snapshot)
            print(json.dumps({"mode": config.LIVE_PILOT_DISABLED,
                "balance": snapshot["account"]["tournament"]["myBalance"],
                "positions": len(snapshot["account"]["positions"]), "open_orders": len(snapshot["account"]["orders"]),
                "recent_fills": len(snapshot["recent_fills"]), "recent_transactions": len(snapshot["recent_transactions"]),
                "freshness": snapshot["freshness"], "accounting": model,
                "quarantine_reserve": snapshot["account"]["quarantine_reserve"],
                "conservative_contract_exposure": sum(r["costBasis"] for r in snapshot["account"]["positions"] if r["quantity"])
                    + sum(r["quantity"] for r in snapshot["account"]["orders"]) + snapshot["account"]["quarantine_reserve"],
                "durable_baseline": "present" if checkpoint else "not initialized",
                "pilot_state": checkpoint["state"] if checkpoint else "not initialized",
                "manual_review_reason": checkpoint["review_reason"] if checkpoint else None,
                "configured_allocation": config.LIVE_ALLOCATION,
                "saved_allocation_remaining": checkpoint["calculated_remaining_allocation"] if checkpoint else None,
                "usable_live_capital": 0,  # Accounting/activation gates are closed.
                "live_eligible": False, "submission_enabled": False}, indent=2))
            return 1  # Data can be healthy, but the accounting gate is unverified.
    except (AccountReadinessBlocked, pilot.PilotBlocked) as error:
        print(f"BLOCKED | {error}")
        return 1
    except KeyboardInterrupt:
        print("Stopped read-only account audit; no orders submitted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
