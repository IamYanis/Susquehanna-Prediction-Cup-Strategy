"""Manual accounting probe: one explicitly confirmed first-leg POST, then stop.

Default/describe/report modes cannot trade. The separate supervised command
requires LIVE settlement permission, a healthy durable baseline and exact TTY
confirmation. Its journal cannot be reused; there is no retry or leg-two path.
LIVE_PILOT and its accounting gate remain disabled/unverified.
"""
import argparse
import copy
import json
import hashlib
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import requests
from dotenv import load_dotenv

import account_reader
import execution_quarantine as quarantine
import live_settlement
import live_pilot as pilot
import account_test as single
import config
from order_preview import executable_limits
import pilot_account
import price_reader as scanner

MODE = "SUPERVISED_ACCOUNTING_PROBE"
QUANTITY_PER_LEG = 1
MAX_PAIR_DEBIT = Decimal("1")
# Diagnostic gross margin; the autonomous coordinator now also requires 0.5%.
# One 0.005 price tick is the smallest positive one-contract ordinary edge.
PROBE_MIN_EDGE = Decimal("0.005")
JOURNAL_PATH = Path(__file__).resolve().with_name("accounting_probe.json")


class ProbeBlocked(ValueError):
    """Fixed reasons only; never print supplied evidence or HTTP headers."""


def require(condition, reason):
    if not condition:
        raise ProbeBlocked(reason)


def require_execution(command_requested=False):
    require(command_requested is True, "The separate supervised probe command is required")
    require(config.LIVE_PILOT_SUBMISSION_ENABLED is False, "LIVE_PILOT must remain disabled during the probe")


def observed_probe_limits(books, account_started):
    """Price exactly one NO per leg; require a positive tick-rounded margin.

    Common quote checks remain mandatory. This is only an ordinary-settlement
    notional check: fees are unverified and no second leg is automatic.
    """
    result = executable_limits("NO-PAIR", books, account_started, quantity=QUANTITY_PER_LEG)
    _, _, cost, edge = result
    require(cost <= MAX_PAIR_DEBIT, "Pair maximum notional exceeds the one-SUSQie probe target")
    require(edge >= PROBE_MIN_EDGE, "Tick-rounded limits fall below the 0.5% probe edge requirement")
    return result


def describe_pair(market_ids, tournament_id=None, slug="midterm-elections"):
    """Return a non-executable design for an explicitly LIVE-authorized pair.

    Listing approval is not fresh settlement verification. A future executor
    must reread official evidence through the existing live settlement gate.
    """
    approval = live_settlement.configured_authorization(market_ids, tournament_id, slug)
    quarantine.require_unblocked_markets(approval["market_ids"])
    quarantine.require_unblocked_exchanges(approval["exchange_ids"])
    return {"mode": MODE, "enabled": False, "live_eligible": False, "submission_enabled": False,
            "live_authorization": copy.deepcopy(approval), "position": "NO-PAIR",
            "quantity_per_leg": QUANTITY_PER_LEG, "maximum_pair_debit_target": str(MAX_PAIR_DEBIT),
            "debit_cap_verified": False,
            "blockers": ["Separate manual command and exact interactive confirmation required",
                         "Fresh settlement/account/risk/quote checks and a durable baseline required",
                         "Fees unverified: the exchange limit caps notional, not an all-in debit"],
            "confirmation": f"PROBE {approval['tournament_id']} NO {','.join(approval['market_ids'])} "
                            f"ONE PER LEG MAX {MAX_PAIR_DEBIT} SUSQie"}


def require_manual_confirmation(design, command_requested, input_stream=None, ask=None):
    """Exact terminal confirmation, separate from settlement authorization."""
    require(command_requested is True, "The separate supervised probe flag is required")
    approval = design["live_authorization"]
    current = describe_pair(approval["market_ids"], approval["tournament_id"], approval["tournament_slug"])
    baseline = design.get("design", design)
    require(current == baseline, "Probe design/authorization changed; review again")
    stream = sys.stdin if input_stream is None else input_stream
    require(stream.isatty(), "Probe confirmation requires an interactive terminal")
    prompt = input if ask is None else ask
    require(prompt(f"Type exactly: {design['confirmation']}\n") == design["confirmation"],
            "Probe was not explicitly confirmed")
    live_settlement.revalidate_authorization(approval)
    require(describe_pair(approval["market_ids"], approval["tournament_id"], approval["tournament_slug"]) == baseline,
            "Probe authorization/quarantine changed during confirmation")


def money(value):
    return Decimal(str(account_reader.require_number(value, nonnegative=True)))


def inventory(account, exclude_exchange):
    """Ignore valuation changes; compare inventory/cost rather than live P&L."""
    return sorted((scanner.numeric_id(r["exchangeId"]), scanner.numeric_id(r["marketId"]),
                   r["quantity"], r["costBasis"], r["settled"], json.dumps(r.get("lots"), sort_keys=True))
                  for r in account["positions"] if scanner.numeric_id(r["exchangeId"]) != exclude_exchange)


def ordinary_cash_receipt(economics, notional):
    """An ALL object can describe an ordinary buy with zero collateral effect.

    Require the full known schema and explicit zeros; absent/inconsistent
    values or linkage must never be treated as an ordinary cash debit.
    Balance/ledger reconciliation remains a separate mandatory check.
    """
    if economics is None:
        return True  # Existing plain receipts do not contain ALL accounting.
    zero_fields = {"collateralSavings", "guaranteedPayoutFloorAfter", "outstandingAdvanceAfter",
                   "collateralRepayment", "redemptionCredit"}
    cost_fields = {"fullNotionalCost", "effectiveEntryCost", "netBuyingPowerImpact"}
    try:
        return (isinstance(economics, dict) and set(economics) == zero_fields | cost_fields | {"relationshipIds", "componentId"}
                and economics["relationshipIds"] == [] and economics["componentId"] is None
                and all(money(economics[k]) == 0 for k in zero_fields)
                and all(money(economics[k]) == notional for k in cost_fields))
    except scanner.API_ERRORS:
        return False


def review_observation(before, after, receipt, activity, market_id, exchange_id):
    """Compare ONE completed leg using supplied full snapshots/actual receipt.

    Return all supplied evidence, including invalid/partial evidence, for a
    future collector to save atomically under the pilot lock. This helper does
    not write files or advance allocation. A matched rounded balance is merely
    a consistency observation; every result remains HALTED/unverified.
    """
    result = {"mode": MODE, "state": "HALTED_MANUAL_REVIEW", "accounting_verified": False,
              "live_eligible": False, "submission_enabled": False,
              "code": pilot_account.ACCOUNTING_UNVERIFIED, "issues": [], "observations": {},
              "evidence": copy.deepcopy({"before": before, "after": after, "placement_receipt": receipt,
                                         "order_activity": activity, "market_id": market_id, "exchange_id": exchange_id})}
    # Populate the report even when later validation rejects a partial/unknown
    # order. Reporting an observed value never labels it a confirmed debit.
    observations = result["observations"]
    observations.update(order_state="UNKNOWN", zero_extra_fee_model="UNKNOWN",
                        execution_and_inventory_verified=False, isolated_execution_reconciled=False)
    for key, snapshot in (("balance_before", before), ("balance_after", after)):
        try:
            observations[key] = str(money(snapshot["account"]["tournament"]["myBalance"]))
        except scanner.API_ERRORS:
            pass
    # /account sometimes exposes more precision, but its default scope and
    # precision are not established as pilot debit authority.
    try:
        left, right = (money(s["supplemental_default_balance"]["balance"]) for s in (before, after))
        observations.update(default_balance_before=str(left), default_balance_after=str(right),
                            default_balance_delta=str(right - left), default_balance_scope_verified=False)
    except scanner.API_ERRORS:
        pass
    try:
        b, a = before["account"], after["account"]
        observations.update(balance_before=str(money(b["tournament"]["myBalance"])),
                            balance_after=str(money(a["tournament"]["myBalance"])),
                            observed_cash_delta=str(money(a["tournament"]["myBalance"]) - money(b["tournament"]["myBalance"])))
        eid = scanner.numeric_id(exchange_id)
        quantities = [sum((Decimal(str(r["quantity"])) for r in account["positions"]
                          if scanner.numeric_id(r["exchangeId"]) == eid), Decimal(0)) for account in (b, a)]
        observations["position_change"] = str(quantities[1] - quantities[0])
        old = {r["event_id"] for r in before["recent_transactions"]}
        new = [r for r in after["recent_transactions"] if r["event_id"] not in old]
        observations["fee_like_transactions"] = copy.deepcopy([r for r in new if
            r["event_type"] == "fee" or "FEE" in (r.get("transactionType") or "").upper()])
        if activity is not None:
            observations["order_state"] = "OPEN" if activity["order"]["open"] else "CLOSED"
            observations["fill_notional"] = str(sum((abs(Decimal(str(r["quantity"]))) * money(r["price"])
                                                      for r in activity["fills"]), Decimal(0)))
    except scanner.API_ERRORS:
        pass
    try:
        mid, eid = scanner.numeric_id(market_id), scanner.numeric_id(exchange_id)
        require(before["data_complete"] is True and after["data_complete"] is True, "Incomplete account evidence")
        for snapshot in (before, after):
            age = snapshot["freshness"]["completed_monotonic"] - snapshot["freshness"]["started_monotonic"]
            require(0 <= age <= pilot_account.MAX_ACCOUNT_READ_AGE, "Snapshot capture was stale")
            account_reader.validate_positions(snapshot["account"])
        require(after["freshness"]["started_monotonic"] >= before["freshness"]["completed_monotonic"],
                "Before/after capture order is ambiguous")
        b, a = before["account"], after["account"]
        tid = b["tournament"]["id"]
        require(tid == a["tournament"]["id"] and b["tournament"]["slug"] == a["tournament"]["slug"],
                "Tournament scope changed")
        require(not b["orders"] and not a["orders"], "Open orders prevent isolated accounting attribution")
        require(not any(scanner.numeric_id(r["exchangeId"]) == eid and r["quantity"] for r in b["positions"]),
                "Existing selected holdings allow netting")
        require(inventory(b, eid) == inventory(a, eid), "Unrelated inventory changed during probe")
        order, fills = activity["order"], activity["fills"]
        require(type(receipt["orderId"]) is int and receipt["orderId"] > 0 and order["id"] == receipt["orderId"] and
                scanner.numeric_id(receipt["exchangeId"]) == eid == scanner.numeric_id(order["exchangeId"]) and
                order["tournamentId"] == tid, "Missing or mismatched receipt; never retry or infer an order ID")
        require(receipt["open"] is False and order["open"] is False and receipt["remainingQuantity"] == 0 and
                receipt["side"] == order["side"] == "no" and receipt["action"] == order["action"] == "buy" and
                type(receipt["quantity"]) is int and receipt["quantity"] == QUANTITY_PER_LEG and
                receipt["quantityTraded"] == order["quantityFilled"] == QUANTITY_PER_LEG,
                "Order is open, partial, rejected or not the one-contract NO buy")
        require(account_reader.require_number(receipt["remainingQuantity"]) == 0 and
                account_reader.require_number(receipt["quantityTraded"]) == 1 and
                account_reader.require_number(order["quantityFilled"]) == 1 and
                account_reader.require_number(order["quantity"]) == 1, "Invalid numeric order quantities")
        limit = money(receipt["price"])
        require(limit == money(order["priceLimit"]) and Decimal(".005") <= limit <= Decimal(".995")
                and limit % Decimal(".005") == 0, "Invalid limit or receipt/order limit differs")
        quantity, notional, seen = Decimal(0), Decimal(0), set()
        for row in fills:
            pilot_account.validate_fill(row, portfolio=False)
            require(row["id"] not in seen and row["side"] == "no" and money(row["price"]) <= limit,
                    "Repeated or incorrectly priced fill")
            seen.add(row["id"])
            quantity += abs(Decimal(str(row["quantity"])))
            notional += abs(Decimal(str(row["quantity"]))) * money(row["price"])
        require(quantity == QUANTITY_PER_LEG and abs(notional - money(receipt["totalCost"])) <= Decimal(".000000001"),
                "Receipt notional and full fills disagree")
        position = next((r for r in a["positions"] if scanner.numeric_id(r["exchangeId"]) == eid), None)
        require(position is not None and scanner.numeric_id(position["marketId"]) == mid and position["settled"] is False
                and Decimal(str(position["quantity"])) == -quantity, "Position does not match completed NO fill")
        cost = money(position["costBasis"])
        observations["execution_and_inventory_verified"] = True
        if abs(cost - notional) > Decimal(".000000001"):
            result["issues"].append("POSITION_COST_SEMANTICS_UNVERIFIED")
        delta = money(b["tournament"]["myBalance"]) - money(a["tournament"]["myBalance"])
        # The docs specify two decimals, but not the rounding mode. Conservatively
        # allow one cent per reading (covering nearest, floor and ceiling), hence
        # two cents on the delta. This consistency bound is not an exact debit
        # authority, and cannot establish zero fees even on a displayed match.
        low, high = delta - Decimal(".02"), delta + Decimal(".02")
        result["observations"].update(fill_notional=str(notional), position_cost=str(cost),
                                       reported_balance_debit=str(delta), possible_balance_debit=[str(low), str(high)],
                                       notional_consistent_with_rounded_balance=low <= notional <= high,
                                       balance_rounding_mode_verified=False,
                                       all_execution_economics=copy.deepcopy(receipt["all"]))
        if not low <= notional <= high:
            result["issues"].append("BALANCE_CHANGE_NOT_EXPLAINED_BY_NOTIONAL")
        if high > MAX_PAIR_DEBIT:
            result["issues"].append("ONE_SUSQIE_DEBIT_CAP_NOT_PROVEN")
        previous = {}
        for row in before["recent_transactions"]:
            pilot_account.validate_transaction(row, tid)
            require(row["event_id"] not in previous, "Duplicate transaction in before-snapshot")
            previous[row["event_id"]] = row
        new = []
        seen_transactions = set()
        for row in after["recent_transactions"]:
            pilot_account.validate_transaction(row, tid)
            require(row["event_id"] not in seen_transactions, "Duplicate transaction event")
            seen_transactions.add(row["event_id"])
            if row["event_id"] in previous:
                require(row == previous[row["event_id"]], "Existing ledger entry changed")
            else:
                new.append(row)
        result["observations"]["new_transactions"] = copy.deepcopy(new)
        if not any(r["event_type"] == "trade" and scanner.numeric_id(r["exchangeId"]) == eid for r in new):
            result["issues"].append("TRADE_LEDGER_RECONCILIATION_UNAVAILABLE")
        if any(r["event_type"] != "trade" or scanner.numeric_id(r["exchangeId"]) != eid for r in new):
            result["issues"].append("LEDGER_ATTRIBUTION_UNVERIFIED")
        if not ordinary_cash_receipt(receipt["all"], notional) or any(
                (r.get("transactionType") or "").startswith("ALL_") for r in new):
            result["issues"].append("COLLATERAL_CASH_EFFECT_UNVERIFIED")
        # An isolated probe must not silently attribute another fill/trade to
        # its receipt. Timestamp proximity alone is not a documented ledger join.
        old_fills = {pilot_account.fill_identity(r): r for r in before["recent_fills"]}
        extra_fills = [r for r in after["recent_fills"] if pilot_account.fill_identity(r) not in old_fills]
        if not extra_fills or any(r["orderId"] != receipt["orderId"] for r in extra_fills) or \
                {r["id"] for r in extra_fills} != seen:
            result["issues"].append("UNRELATED_OR_INCOMPLETE_FILL_ACTIVITY")
        for row in after["recent_fills"]:
            previous_fill = old_fills.get(pilot_account.fill_identity(row))
            if previous_fill is not None:
                require(all(row[k] == previous_fill[k] for k in ("orderId", "marketId", "exchangeId", "side", "quantity", "filledAt"))
                        and abs(money(row["price"]) - money(previous_fill["price"])) <= Decimal(".000000001"),
                        "Existing fill history changed during probe")
        if len(new) != 1 or new[0]["event_type"] != "trade" or (
                scanner.numeric_id(new[0]["exchangeId"]) != eid or
                scanner.numeric_id(new[0]["marketId"]) != mid or
                new[0].get("orderType") != "BUY" or Decimal(str(new[0]["quantity"])) != -quantity or
                abs(money(new[0]["price"]) * quantity - notional) > Decimal(".000000001")):
            result["issues"].append("TRADE_LEDGER_RECONCILIATION_UNAVAILABLE")
        observations["isolated_execution_reconciled"] = not result["issues"] and delta >= 0
        if observations["isolated_execution_reconciled"]:
            observations["zero_extra_fee_model"] = ("MATCH_AT_REPORTED_PRECISION" if
                abs(delta - notional) <= Decimal(".000000001") else "ROUNDING_AMBIGUOUS")
        elif not low <= notional <= high or observations["fee_like_transactions"]:
            observations["zero_extra_fee_model"] = "DIFFERS_OR_UNEXPLAINED"
        result["issues"].append("Rounded balances and ledger proximity do not prove receipt-linked fee-inclusive debit")
    except ProbeBlocked as error:
        result["issues"].append(str(error))
    except scanner.API_ERRORS:
        result["issues"].append("Missing, invalid, ambiguous or partial evidence; manual review required")
    return result


def save_journal(record):
    """Private, atomic evidence writes under the SAME lock as pilot accounting."""
    state_path = Path(pilot.ALLOCATION_PATH).resolve()
    require(pilot._state_lock_owners.get(state_path) == (os.getpid(), threading.get_ident()),
            "Probe evidence requires the exclusive pilot lock")
    target, temporary = Path(JOURNAL_PATH), None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(record, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        single.sync_directory(target.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class EvidenceReads:
    """Retain GET bodies, including reads that later fail validation.

    Headers and credentials are never recorded or printed. After the first
    journal write, every response is flushed before the reader can use it.
    """
    def __init__(self, session):
        self.session, self.reads, self.record = session, [], None

    def response_evidence(self, response):
        body = response.text
        authorization = self.session.headers.get("Authorization")
        key = authorization.removeprefix("Bearer ") if isinstance(authorization, str) else None
        redacted = bool(key and key in body)
        if redacted:
            body = body.replace(key, "[REDACTED CREDENTIAL]")
        return {"http_status": response.status_code, "body": body, "credential_redacted": redacted,
                "received_at": datetime.now(timezone.utc).isoformat(), "received_monotonic": time.monotonic()}

    def get(self, url, **kwargs):
        try:
            response = self.session.get(url, **kwargs)
        except requests.RequestException as error:
            self.reads.append({"path": url.removeprefix(scanner.API_BASE_URL),
                               "params": kwargs.get("params"), "error_kind": type(error).__name__})
            if self.record is not None:
                save_journal(self.record)
            raise
        self.reads.append({"path": url.removeprefix(scanner.API_BASE_URL), "params": kwargs.get("params"),
                           **self.response_evidence(response)})
        if self.record is not None:
            save_journal(self.record)
        require(not self.reads[-1]["credential_redacted"], "Unexpected credential reflection in API response")
        return response


def fresh_preview(session, market_ids, first_market, checkpoint):
    try:
        return _fresh_preview(session, market_ids, first_market, checkpoint)
    except (live_settlement.LiveSettlementBlocked, pilot_account.AccountReadinessBlocked) as error:
        if isinstance(error, pilot_account.AccountReadinessBlocked) or error.code == live_settlement.NEEDS_REVALIDATION:
            pilot.save_checkpoint(pilot.halted_result(pilot.load_checkpoint(), str(error))["checkpoint"])
        raise


def capture_default_balance(session, snapshot):
    """Additional official evidence; never substitute it for scoped pilot cash."""
    payload = scanner.fetch_json(session, scanner.API_BASE_URL + "/account")
    snapshot["supplemental_default_balance"] = {
        "balance": account_reader.require_number(payload["balance"], nonnegative=True),
        "scope_verified": False, "source": scanner.API_BASE_URL + "/account",
        "observed_at": datetime.now(timezone.utc).isoformat()}
    pilot_account.check_fresh(snapshot["freshness"]["started_monotonic"])
    snapshot["freshness"]["completed_monotonic"] = time.monotonic()


def _fresh_preview(session, market_ids, first_market, checkpoint):
    """All existing gates, with one narrow accounting-probe exception.

    Only ACCOUNTING_MODEL_UNVERIFIED may be present. It is disclosed in the
    exact confirmation and is NOT bypassed in ordinary LIVE_PILOT readiness.
    """
    pilot.require_execution_clear(checkpoint)
    design = describe_pair(market_ids, checkpoint["tournament_id"])
    approval = design["live_authorization"]
    require(first_market in approval["market_ids"], "First-leg market must be explicitly selected from this pair")
    before = pilot_account.read_snapshot(session, approval["tournament_slug"], checkpoint)
    capture_default_balance(session, before)
    assessment = pilot_account.assess_snapshot(before, checkpoint, approval["exchange_ids"], MAX_PAIR_DEBIT, 1)
    require([r["code"] for r in assessment["failures"]] == [pilot_account.ACCOUNTING_UNVERIFIED],
            "Account, risk, allocation or freshness checks failed")
    require(not before["account"]["orders"], "Open orders prevent an isolated probe")
    markets, allowed, context = pilot.read_live_pair(session, approval)
    require("NO-PAIR" in allowed, "LIVE settlement verification failed")
    books = [scanner.get_best_prices(session, market, approval["tournament_id"]) for market in markets]
    prices, _, cost, edge = observed_probe_limits(books, before["freshness"]["started_monotonic"])
    live_settlement.revalidate_authorization(approval)
    selected = approval["market_ids"].index(first_market)
    material = {"authorization": approval, "first_market": first_market, "limits": prices,
                "pilot_revision": checkpoint["revision"]}
    binding = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]
    phrase = (f"PROBE BUY 1 NO MARKET {first_market} EXCHANGE {approval['exchange_ids'][selected]} "
              f"TOURNAMENT {approval['tournament_id']} MAX NOTIONAL {prices[selected]:.3f} SUSQies "
              f"FEES UNVERIFIED NO LEG 2 REVIEW {binding}")
    return {"design": design, "live_authorization": approval, "confirmation": phrase,
            "before": before, "pilot_state": copy.deepcopy(checkpoint), "books": books,
            "market_context": context, "prices": prices, "pair_notional": str(cost), "edge": str(edge),
            "selected_index": selected, "expected_maximum_notional": f"{prices[selected]:.3f}",
            "expected_maximum_zero_fee_debit": f"{prices[selected]:.3f}", "all_in_debit_cap_verified": False,
            "risk": assessment["risk"]}


def print_preview(preview):
    approval, index = preview["live_authorization"], preview["selected_index"]
    print(f"Pair: {approval['pair_name']} | markets {','.join(approval['market_ids'])} | exchanges {','.join(approval['exchange_ids'])}")
    print(f"Only first leg: BUY 1 NO in market {approval['market_ids'][index]}; leg two will not be submitted")
    print(f"Maximum notional / expected zero-fee debit: {preview['expected_maximum_notional']} SUSQies")
    print("Fees remain unverified; the exchange limit does not cap additional charges. LIVE_PILOT stays disabled.")
    print("Gates: LIVE authorization + fresh settlement evidence, quarantine, durable state, allocation/risk,")
    print("isolated account/orders/fills/ledger, 15-second account freshness, 5-second books, 0.5% probe edge, depth 50, exact TTY confirmation.")
    print(f"Exact confirmation: {preview['confirmation']}")


def persist_review(record, review=None):
    """Always latch review. Cash debit and holding notional remain separate."""
    checkpoint = pilot.load_checkpoint()
    updated = pilot.halted_result(checkpoint, record.get("halt_reason") or
        "Accounting probe stopped after first leg; one-sided exposure requires manual review")["checkpoint"]
    fingerprint = record["fingerprint"]
    exposure = updated["live_exposures"].get(fingerprint)
    if exposure is not None and review is not None:
        observations = review["observations"]
        if observations.get("execution_and_inventory_verified"):
            index = record["preview"]["selected_index"]
            exposure["confirmed_quantities"][index] = "1"
            exposure["confirmed_costs"][index] = observations["fill_notional"]
            exposure["possible_additional_quantities"][index] = "0"
            exposure["observed_cash_debit"] = None
            if observations.get("isolated_execution_reconciled"):
                # This is the isolated SCOPED reported cash delta, not inferred
                # fill notional or default-account cash. Keep its precision caveat.
                debit = pilot.amount(observations["reported_balance_debit"])
                previous = pilot.amount(updated["accounted_pair_costs"][fingerprint])
                require(debit >= previous, "Previously recorded observed debit cannot decrease")
                exposure["observed_cash_debit"] = str(debit)
                updated["accounted_pair_costs"][fingerprint] = str(debit)
                updated["allocated_cash_remaining"] = str(pilot.amount(updated["allocated_cash_remaining"]) - (debit - previous))
                updated["last_reconciled_account_cash"] = observations["balance_after"]
    if exposure is not None:
        exposure["execution_status"] = "MANUAL_REVIEW"
    pilot.refresh_totals(updated)
    record["pilot_state_after"] = pilot.save_checkpoint(updated)
    record["state"] = pilot.HALTED
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_journal(record)


def execute_first_leg(session, market_ids, first_market, command_requested=False, input_stream=None, ask=None):
    """The sole POST path. No recovery/resubmit function and no second-leg body."""
    require_execution(command_requested)
    with pilot.pilot_lock():
        checkpoint = pilot.load_checkpoint()
        if Path(JOURNAL_PATH).exists():
            pilot.save_checkpoint(pilot.halted_result(checkpoint,
                "Existing accounting-probe journal requires manual review; never resubmit")["checkpoint"])
            raise ProbeBlocked("Existing probe journal blocks another attempt; no replay or new key")
        reads = EvidenceReads(session)
        preview = fresh_preview(reads, market_ids, first_market, checkpoint)
        print_preview(preview)
        require_manual_confirmation(preview, command_requested, input_stream, ask)
        # Human input can take arbitrarily long. Repeat EVERY gate after it and
        # reject changed prices/authorization; never widen a confirmed limit.
        fresh = fresh_preview(reads, market_ids, first_market, checkpoint)
        require(fresh["confirmation"] == preview["confirmation"], "Prices or authorization changed; repeat manual review")
        approval, index = fresh["live_authorization"], fresh["selected_index"]
        request = {"idempotencyKey": f"account-test-{uuid4().hex}", "exchangeId": approval["exchange_ids"][index],
                   "side": "no", "action": "buy", "quantity": 1, "price": fresh["prices"][index],
                   "tournamentId": approval["tournament_id"],
                   "expirationDate": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="milliseconds")}
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        record = {"version": 1, "mode": MODE, "state": "PREPARED", "created_at": datetime.now(timezone.utc).isoformat(),
                  "fingerprint": fingerprint, "preview": fresh, "request": request, "reads": reads.reads,
                  "post_attempted": False, "placement_response": None, "raw_receipt": None,
                  "halt_reason": "", "review": None}
        # Both the exact key/request and all before evidence reach durable disk
        # before any POST. A file already present is never resumed or replaced.
        save_journal(record)
        reads.record = record
        receipt, activity, after = None, None, None
        try:
            executing = copy.deepcopy(checkpoint)
            pending = ["0", "0"]
            pending[index] = "1"
            executing["state"] = pilot.EXECUTING
            executing["live_exposures"][fingerprint] = {
                "market_ids": approval["market_ids"], "exchange_ids": approval["exchange_ids"], "position_type": "NO-PAIR",
                "confirmed_quantities": ["0", "0"], "confirmed_costs": ["0", "0"],
                "possible_additional_quantities": pending, "limit_prices": [str(p) for p in fresh["prices"]],
                "execution_status": pilot.EXECUTING}
            executing["accounted_pair_costs"][fingerprint] = "0"
            pilot.refresh_totals(executing)
            record["pilot_state_before_post"] = pilot.save_checkpoint(executing)
            observed_probe_limits(fresh["books"], fresh["before"]["freshness"]["started_monotonic"])
            live_settlement.revalidate_authorization(approval)
            record["state"], record["post_attempted"] = "SUBMITTING", True
            record["submission_started_at"] = datetime.now(timezone.utc).isoformat()
            save_journal(record)  # No POST if this write/fsync fails.
            try:
                response = session.post(scanner.API_BASE_URL + "/orders", json=request,
                                        timeout=scanner.REQUEST_TIMEOUT, allow_redirects=False)
                record["placement_response"] = reads.response_evidence(response)
                save_journal(record)  # Preserve even error/malformed bodies before parsing.
                require(not record["placement_response"]["credential_redacted"], "Unexpected credential reflection in placement response")
                response.raise_for_status()
                require(response.status_code == 200, "Ambiguous placement HTTP status")
                receipt = response.json()
                record["raw_receipt"] = copy.deepcopy(receipt)
                save_journal(record)
                require(isinstance(receipt, dict) and type(receipt.get("orderId")) is int and receipt["orderId"] > 0,
                        "No confirmed receipt ID; never infer one or replay the POST")
                require(receipt.get("exchangeId") == request["exchangeId"] and receipt.get("side") == "no" and
                        receipt.get("action") == "buy" and type(receipt.get("quantity")) is int and receipt["quantity"] == 1 and
                        money(receipt["price"]) == money(request["price"]), "Placement scope or canonical order differs")
                intent = {"version": 1, "market_id": first_market, "tournament_slug": approval["tournament_slug"],
                          "created_at": record["created_at"], "request": request, "state": "ACCEPTED",
                          "order_id": receipt["orderId"], "observation": {
                              "placement_quantity": receipt["quantityTraded"], "placement_cost": receipt["totalCost"]}}
                intent["approval"] = single.approval_hash(intent)
                state, observation = single.observe_test(reads, intent)
                record["order_observation"] = {"state": state, **observation}
                save_journal(record)
            except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError):
                record["halt_reason"] = "Placement/reconciliation ambiguous, partial or unavailable; never retry"
                persist_review(record)  # Latch uncertainty BEFORE diagnostic GETs.
            # Even a lost receipt needs an account/ledger capture for manual
            # review. These are GETs only; no order ID is guessed from history.
            try:
                after = pilot_account.read_snapshot(reads, approval["tournament_slug"], checkpoint)
                record["after"] = after
                capture_default_balance(reads, after)
                if not record["halt_reason"] and receipt is not None:
                    activity = next((a for a in after["order_activity"] if a["order"]["id"] == receipt["orderId"]), None)
                    if activity is None:
                        activity = pilot_account.read_order_activity(reads, receipt["orderId"], after["recent_fills"],
                                                                     after["account"], time.monotonic())
                save_journal(record)
            except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError):
                record["halt_reason"] = record["halt_reason"] or "Post-order account/fill/ledger capture unavailable"
            review = review_observation(fresh["before"], after, receipt, activity, first_market, request["exchangeId"])
            if record["halt_reason"]:
                review["observations"]["isolated_execution_reconciled"] = False
                review["observations"]["execution_and_inventory_verified"] = False
            record["review"] = review
            record["halt_reason"] = record["halt_reason"] or "First leg complete or partial; STOP for manual accounting and one-sided exposure review"
            persist_review(record, review)
            return record
        except BaseException:
            # A crash/interruption after a possible POST leaves a persisted halt
            # or, if disk writes also fail, EXECUTING/SUBMITTING for recovery.
            record["halt_reason"] = record["halt_reason"] or "Probe interrupted or evidence write failed; no retry"
            persist_review(record)
            raise


def print_report(record):
    observations = (record.get("review") or {}).get("observations", {})
    if observations.get("order_state") == "UNKNOWN" and "order_observation" in record:
        observations = {**observations, "order_state": "OPEN" if record["order_observation"]["open"] else "CLOSED"}
    print(f"{MODE}: HALTED_MANUAL_REVIEW | STOP | no second-leg action")
    for label, key in (("Balance before", "balance_before"), ("Balance after", "balance_after"),
                       ("Observed cash delta (after - before)", "observed_cash_delta"),
                       ("Fill notional", "fill_notional"), ("Fee-like transactions", "fee_like_transactions"),
                       ("Position quantity change", "position_change"), ("Order state", "order_state"),
                       ("Zero-extra-fee model", "zero_extra_fee_model")):
        print(f"{label}: {json.dumps(observations.get(key, 'UNAVAILABLE'))}")
    print(f"Evidence: {JOURNAL_PATH} | Accounting model remains unverified; manual review required")
    if "default_balance_delta" in observations:
        print(f"Supplemental /account delta: {observations['default_balance_delta']} SUSQies (default scope unverified; not used for allocation)")
    print(f"Reason: {record.get('halt_reason', 'Saved evidence requires manual review')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supervised-accounting-probe", action="store_true")
    parser.add_argument("--describe", action="store_true", help="Print a design only; no API reads or executable intent")
    parser.add_argument("--preview", action="store_true", help="Fresh GET-only preview; no key or journal creation")
    parser.add_argument("--report", action="store_true", help="Read saved evidence only; never resume submission")
    parser.add_argument("--dem-market")
    parser.add_argument("--rep-market")
    parser.add_argument("--first-market", help="Explicit market for the ONE first-leg NO buy")
    args = parser.parse_args()
    try:
        require(sum((args.describe, args.preview, args.report, args.supervised_accounting_probe)) <= 1,
                "Select only one probe command")
        if args.report:
            with pilot.pilot_lock():
                print_report(json.loads(Path(JOURNAL_PATH).read_text()))
            return 0
        if args.preview or args.supervised_accounting_probe:
            require(args.dem_market is not None and args.rep_market is not None and args.first_market is not None,
                    "Both pair IDs and the first-leg market are required")
            market_ids = [scanner.numeric_id(args.dem_market), scanner.numeric_id(args.rep_market)]
            first = scanner.numeric_id(args.first_market)
            describe_pair(market_ids)  # Empty LIVE list blocks before credentials/API access.
            if args.supervised_accounting_probe:
                require_execution(True)
                require(sys.stdin.isatty(), "Probe submission requires an interactive terminal")
            with pilot.pilot_lock():
                checkpoint = pilot.load_checkpoint()
                pilot.require_execution_clear(checkpoint)
                if Path(JOURNAL_PATH).exists():
                    pilot.save_checkpoint(pilot.halted_result(checkpoint,
                        "Existing accounting-probe journal requires manual review; never resubmit")["checkpoint"])
                    raise ProbeBlocked("Existing probe journal blocks another attempt")
                load_dotenv(Path(__file__).resolve().with_name(".env"))
                key = os.getenv("SIG_API_KEY")
                require(bool(key), "Missing local API credential")
                with requests.Session() as session:
                    session.headers.update({"Authorization": f"Bearer {key}"})
                    if args.preview:
                        print_preview(fresh_preview(session, market_ids, first, checkpoint))
                    else:
                        print_report(execute_first_leg(session, market_ids, first, True))
            return 0 if args.preview else 1  # A submitted first leg ALWAYS stops HALTED.
        if args.describe:
            require(args.dem_market is not None and args.rep_market is not None, "Design requires both approved market IDs")
            design = describe_pair([scanner.numeric_id(args.dem_market), scanner.numeric_id(args.rep_market)])
            print(json.dumps(design, indent=2))
        else:
            approvals = live_settlement.load_authorizations()
            print(f"{MODE}: NOT ARMED | LIVE_PILOT disabled | {len(approvals)} explicitly LIVE-authorized pairs")
            print("No API requests, key or journal created. Use --preview before any separate supervised command.")
        return 0
    except (ProbeBlocked, pilot.PilotBlocked, live_settlement.LiveSettlementBlocked, quarantine.QuarantineError) as error:
        print(f"BLOCKED | {error}")
        return 1
    except scanner.API_ERRORS + (OSError,):
        print("BLOCKED | Probe design unavailable or invalid")
        return 1
    except KeyboardInterrupt:
        print("STOP | Interrupted; any attempted probe remains halted for manual review")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
