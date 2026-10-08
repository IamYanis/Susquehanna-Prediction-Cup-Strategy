"""Explicit LIVE_PILOT settlement permission and fresh read-only verification.

Paper approvals are never read here. This module cannot approve a new pair,
change either approval file, create an order intent, or submit an order.
"""
import json
import re
from pathlib import Path
from uuid import UUID

import config
import execution_quarantine as quarantine
import paired_account_test as paired
import price_reader as scanner
from paper_trader import parse_api_timestamp

APPROVAL_PATH = Path(__file__).resolve().with_name("live_approved_settlements.json")
VERIFIED = "LIVE_SETTLEMENT_VERIFIED"
NOT_AUTHORIZED = "LIVE_SETTLEMENT_NOT_AUTHORIZED"
NEEDS_REVALIDATION = "LIVE_SETTLEMENT_NEEDS_REVALIDATION"
MACHINE = "machine-verified"
MANUAL = "manual-supervised"
SUPERVISED_MODE = "supervised-one-contract"


class LiveSettlementBlocked(ValueError):
    """A safe local reason/code, never an HTTP error containing credentials."""
    def __init__(self, reason, code=NEEDS_REVALIDATION):
        self.code = code
        super().__init__(f"{code} | {reason}")


def require(condition, reason):
    if not condition:
        raise LiveSettlementBlocked(reason)


def evidence_sources(approval):
    """Bind source references to the exact scoped official reads we revalidate."""
    tournament_id = approval["tournament_id"]
    sources = [f"{scanner.API_BASE_URL}/markets/{market_id}/nodes?tournamentId={tournament_id}"
               for market_id in approval["market_ids"]]
    if approval["verification_route"] == MACHINE:
        sources.append(f"{scanner.API_BASE_URL}/relationships?tournamentId={tournament_id}"
                       f"&exchangeId={approval['exchange_ids'][0]}&limit=200")
    return sources + ["https://sig.thesuper.market/docs/settlement-and-payouts"]


def validate_authorization(approval):
    """Reject incomplete/broad permissions; IDs are ordered Democratic/Republican."""
    require(isinstance(approval, dict) and set(approval) == {
        "approval_version", "pair_name", "tournament_id", "tournament_slug", "market_ids", "exchange_ids",
        "relationship_type", "position_types", "max_quantity", "execution_mode", "verification_route",
        "settlement_rationale", "evidence_hash", "source_refs", "limitations", "approved_at"},
        "Live settlement authorization is incomplete or invalid")
    require(type(approval["approval_version"]) is int and approval["approval_version"] >= 1,
            "Live approval must have a positive version")
    require(all(isinstance(approval[key], str) and approval[key].strip()
                for key in ("pair_name", "tournament_slug", "settlement_rationale")), "Missing live approval rationale/scope")
    require(isinstance(approval["tournament_id"], str)
            and str(UUID(approval["tournament_id"])) == approval["tournament_id"]
            and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", approval["tournament_slug"]), "Invalid live tournament scope")
    for key in ("market_ids", "exchange_ids"):
        ids = approval[key]
        require(isinstance(ids, list) and len(ids) == 2
                and all(isinstance(value, str) and scanner.numeric_id(value) == value for value in ids)
                and ids[0] != ids[1], "Invalid live market/exchange IDs")
    require(approval["relationship_type"] == "mutually_exclusive" and approval["position_types"] == ["NO-PAIR"],
            "Live pilot authorization requires the reviewed mutually-exclusive NO pair")
    require(type(approval["max_quantity"]) is int and approval["max_quantity"] == 1
            and approval["execution_mode"] == SUPERVISED_MODE,
            "Live settlement permission is limited to one supervised contract per leg")
    require(approval["verification_route"] in {MACHINE, MANUAL}, "Invalid live verification route")
    require(isinstance(approval["evidence_hash"], str) and re.fullmatch(r"[0-9a-f]{64}", approval["evidence_hash"]),
            "Missing exact approved live evidence hash")
    for key in ("source_refs", "limitations"):
        require(isinstance(approval[key], list) and approval[key]
                and all(isinstance(value, str) and value.strip() for value in approval[key]),
                "Live approval must record official sources and limitations")
    require(approval["source_refs"] == evidence_sources(approval), "Live evidence sources do not match the authorized IDs/route")
    parse_api_timestamp(approval["approved_at"])


def load_authorizations():
    """Reread explicit permissions every time; an empty file approves nothing."""
    def unique_fields(fields):
        result = {}
        for key, value in fields:
            require(key not in result, "Duplicate live authorization field")
            result[key] = value
        return result

    try:
        data = json.loads(APPROVAL_PATH.read_text(), object_pairs_hook=unique_fields)
        require(isinstance(data, dict) and set(data) == {"version", "allowed_mode", "pairs"}
                and type(data["version"]) is int and data["version"] == 1
                and data["allowed_mode"] == config.LIVE_PILOT and isinstance(data["pairs"], list),
                "Live authorization configuration has invalid version/mode")
        seen = set()
        for approval in data["pairs"]:
            validate_authorization(approval)
            identity = (approval["tournament_id"], tuple(sorted(approval["market_ids"])))
            require(identity not in seen, "Duplicate live settlement authorization")
            seen.add(identity)
        return data["pairs"]
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        raise LiveSettlementBlocked("Live authorization configuration is missing or invalid; manual review required") from error


def configured_authorization(market_ids, tournament_id=None, slug="midterm-elections"):
    authorizations = load_authorizations()
    matches = [entry for entry in authorizations if set(entry["market_ids"]) == set(market_ids)
               and (tournament_id is None or entry["tournament_id"] == tournament_id)]
    if not matches:
        # A listed identity in a different scope is a mismatch, not permission
        # to guess the tournament from market titles or the paper config.
        if any(set(entry["market_ids"]) == set(market_ids) for entry in authorizations):
            raise LiveSettlementBlocked("Authorized pair tournament scope changed")
        raise LiveSettlementBlocked("Pair is not explicitly live-approved", NOT_AUTHORIZED)
    require(len(matches) == 1 and matches[0]["market_ids"] == market_ids and matches[0]["tournament_slug"] == slug,
            "Authorized pair ID order or tournament slug changed")
    return matches[0]


def revalidate_authorization(approval):
    """Revocation or edits during a check need review, even if now unlisted."""
    try:
        current = configured_authorization(approval["market_ids"], approval["tournament_id"], approval["tournament_slug"])
        require(current == approval, "Live authorization changed during readiness checks")
    except LiveSettlementBlocked as error:
        raise LiveSettlementBlocked("Previously selected live authorization was removed, changed or is invalid") from error


def verify_settlement(session, markets, tournament_id, approval):
    """Revalidate the explicitly chosen route; never fall back to paper approval."""
    try:
        validate_authorization(approval)
        revalidate_authorization(approval)
        scanner.check_approved_pair(approval, markets, tournament_id)
        quarantine.require_unblocked_markets(approval["market_ids"])
        quarantine.require_unblocked_exchanges(approval["exchange_ids"])
        if approval["verification_route"] == MACHINE:
            allowed, context = scanner.get_pair_rules(session, *markets, tournament_id)
        else:
            # The existing reader supports only the reviewed official 153/154
            # wording. Reusing it neither infers compatibility nor prepares orders.
            _, record = paired.read_manual_evidence(session, tournament_id, *approval["market_ids"])
            require(record["market_ids"] == approval["market_ids"] and record["exchange_ids"] == approval["exchange_ids"]
                    and record["proposition"] == approval["settlement_rationale"]
                    and record["source_refs"] == approval["source_refs"]
                    and record["limitations"] == approval["limitations"], "Reviewed manual settlement evidence changed")
            record["approved_at"] = approval["approved_at"]  # Reuse a recorded human time; never generate one.
            context = {"mode": paired.MANUAL_POLICY, "tournament_id": tournament_id,
                       "market_ids": record["market_ids"], "exchange_ids": record["exchange_ids"],
                       "settlement_fingerprint": record["evidence_hash"], "manual_approval": record}
            paired.validate_manual_context(context)
            allowed = {"NO-PAIR"}
        require("NO-PAIR" in allowed and context["settlement_fingerprint"] == approval["evidence_hash"],
                "Authorized relationship/rules evidence changed or is unverified")
        return {"NO-PAIR"}, context
    except LiveSettlementBlocked:
        raise
    except scanner.API_ERRORS as error:
        raise LiveSettlementBlocked("Official live settlement evidence is unavailable, invalid or changed") from error
