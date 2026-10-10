"""Explicit LIVE_PILOT settlement permission and fresh read-only verification.

Paper approvals are never read here. This module cannot approve a new pair,
change any approval file, create an order intent, or submit an order.
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
MANUAL_AUTONOMOUS_PATH = Path(__file__).resolve().with_name("manual_autonomous_approvals.json")
VERIFIED = "LIVE_SETTLEMENT_VERIFIED"
NOT_AUTHORIZED = "LIVE_SETTLEMENT_NOT_AUTHORIZED"
NEEDS_REVALIDATION = "LIVE_SETTLEMENT_NEEDS_REVALIDATION"
MACHINE = "machine-verified"
MANUAL = "manual-supervised"
SUPERVISED_MODE = "supervised-one-contract"
AUTONOMOUS_MODE = "autonomous-one-contract"
MACHINE_AUTONOMOUS = "MACHINE_VERIFIED_AUTONOMOUS"
MANUAL_AUTONOMOUS = "MANUAL_AUTONOMOUS_APPROVAL"


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
    if approval["verification_route"] == MANUAL_AUTONOMOUS:
        # The new tier pins market mapping/status and actual policy content,
        # as well as the full resolution roots. No title implies permission.
        return ([f"{scanner.API_BASE_URL}/tournaments/{approval['tournament_slug']}"]
                + [f"{scanner.API_BASE_URL}/markets/{mid}?tournamentId={tournament_id}"
                   for mid in approval["market_ids"]]
                + [f"{scanner.API_BASE_URL}/markets/{mid}/nodes?tournamentId={tournament_id}"
                   for mid in approval["market_ids"]]
                + ["https://sig.thesuper.market/docs/settlement-and-payouts"])
    sources = [f"{scanner.API_BASE_URL}/markets/{market_id}/nodes?tournamentId={tournament_id}"
               for market_id in approval["market_ids"]]
    if approval["verification_route"] == MACHINE:
        sources.append(f"{scanner.API_BASE_URL}/relationships?tournamentId={tournament_id}"
                       f"&exchangeId={approval['exchange_ids'][0]}&limit=200")
    return sources + ["https://sig.thesuper.market/docs/settlement-and-payouts"]


def validate_authorization(approval):
    """Reject incomplete/broad permissions; IDs are ordered Democratic/Republican."""
    manual_autonomous = isinstance(approval, dict) and approval.get("verification_route") == MANUAL_AUTONOMOUS
    fields = {
        "approval_version", "pair_name", "tournament_id", "tournament_slug", "market_ids", "exchange_ids",
        "relationship_type", "position_types", "max_quantity", "execution_mode", "verification_route",
        "settlement_rationale", "evidence_hash", "source_refs", "limitations", "approved_at"}
    if manual_autonomous:
        fields.add("evidence_version")
        if approval.get("evidence_version") == 2:
            fields.update({"policy_content_format", "policy_content_sha256", "market_root_sha256"})
    require(isinstance(approval, dict) and set(approval) == fields,
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
            and approval["execution_mode"] in {SUPERVISED_MODE, AUTONOMOUS_MODE},
            "Live settlement permission must name an explicit one-contract execution mode")
    require(approval["verification_route"] in {MACHINE, MANUAL, MANUAL_AUTONOMOUS}, "Invalid live verification route")
    require(approval["execution_mode"] != AUTONOMOUS_MODE or approval["verification_route"] in {MACHINE, MANUAL_AUTONOMOUS},
            "Autonomous execution requires machine-readable verification or a distinct manual autonomous approval")
    if manual_autonomous:
        from manual_autonomous_settlement import REQUIRED_LIMITATIONS, POLICY_CONTENT_FORMAT
        # Historical v1 approvals remain readable in immutable execution audit.
        # Fresh manual eligibility below requires the migrated v2 binding.
        require(approval["execution_mode"] == AUTONOMOUS_MODE and type(approval["evidence_version"]) is int
                and approval["evidence_version"] in {1, 2}, "Manual autonomous permission has invalid evidence version/mode")
        if approval["evidence_version"] == 2:
            require(approval["policy_content_format"] == POLICY_CONTENT_FORMAT
                    and all(isinstance(approval[k], str) and re.fullmatch(r"[0-9a-f]{64}", approval[k])
                            for k in ("policy_content_sha256", "market_root_sha256")),
                    "Policy content/market-root fingerprints are missing or invalid")
        require(isinstance(approval["limitations"], list)
                and all(value in approval["limitations"] for value in REQUIRED_LIMITATIONS),
                "Manual autonomous approval must explicitly acknowledge all settlement/execution limitations")
    require(isinstance(approval["evidence_hash"], str) and re.fullmatch(r"[0-9a-f]{64}", approval["evidence_hash"]),
            "Missing exact approved live evidence hash")
    for key in ("source_refs", "limitations"):
        require(isinstance(approval[key], list) and approval[key]
                and all(isinstance(value, str) and value.strip() for value in approval[key]),
                "Live approval must record official sources and limitations")
    require(approval["source_refs"] == evidence_sources(approval), "Sources: live references do not match authorized IDs/route")
    require(parse_api_timestamp(approval["approved_at"]).tzinfo is not None, "Approval time must include a timezone")


def _load_authorizations(path, tier=None):
    """Strict shared JSON reader; the separate tier cannot enter the old file."""
    def unique_fields(fields):
        result = {}
        for key, value in fields:
            require(key not in result, "Duplicate live authorization field")
            result[key] = value
        return result

    try:
        data = json.loads(path.read_text(), object_pairs_hook=unique_fields)
        fields = {"version", "allowed_mode", "pairs"} | ({"authorization_tier"} if tier else set())
        require(isinstance(data, dict) and set(data) == fields
                and type(data["version"]) is int and data["version"] == 1
                and data["allowed_mode"] == config.LIVE_PILOT and isinstance(data["pairs"], list),
                "Live authorization configuration has invalid version/mode")
        require(tier is None or data["authorization_tier"] == tier, "Wrong settlement authorization tier")
        seen = set()
        for approval in data["pairs"]:
            validate_authorization(approval)
            require((approval["verification_route"] == MANUAL_AUTONOMOUS) == (tier == MANUAL_AUTONOMOUS),
                    "Manual autonomous approval must be in its own explicit configuration")
            identity = (approval["tournament_id"], tuple(sorted(approval["market_ids"])))
            require(identity not in seen, "Duplicate live settlement authorization")
            seen.add(identity)
        return data["pairs"]
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        raise LiveSettlementBlocked("Live authorization configuration is missing or invalid; manual review required") from error


def load_authorizations():
    """Existing supervised/machine permissions; no paper or new-tier permission."""
    return _load_authorizations(APPROVAL_PATH)


def load_manual_autonomous_authorizations():
    return _load_authorizations(MANUAL_AUTONOMOUS_PATH, MANUAL_AUTONOMOUS)


def autonomous_authorizations():
    """Prefer an explicitly configured machine tier; never downgrade its failure.

    The two files are independently strict. A malformed approval blocks use,
    rather than disappearing into a fallback. No permission is synthesized.
    """
    machine = [entry for entry in load_authorizations() if entry["execution_mode"] == AUTONOMOUS_MODE]
    identities = {(entry["tournament_id"], tuple(sorted(entry["market_ids"]))) for entry in machine}
    manual = [entry for entry in load_manual_autonomous_authorizations()
              if (entry["tournament_id"], tuple(sorted(entry["market_ids"]))) not in identities]
    return machine + manual


def authorization_tier(approval):
    if approval["execution_mode"] == AUTONOMOUS_MODE:
        return MACHINE_AUTONOMOUS if approval["verification_route"] == MACHINE else MANUAL_AUTONOMOUS
    return approval["verification_route"]


def configured_authorization(market_ids, tournament_id=None, slug="midterm-elections", execution_mode=SUPERVISED_MODE):
    authorizations = autonomous_authorizations() if execution_mode == AUTONOMOUS_MODE else load_authorizations()
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
    require(matches[0]["execution_mode"] == execution_mode, "Authorized execution mode differs from the requested mode")
    return matches[0]


def revalidate_authorization(approval):
    """Revocation or edits during a check need review, even if now unlisted."""
    try:
        current = configured_authorization(approval["market_ids"], approval["tournament_id"], approval["tournament_slug"],
                                           execution_mode=approval["execution_mode"])
        require(current == approval, "Live authorization changed during readiness checks")
    except LiveSettlementBlocked as error:
        raise LiveSettlementBlocked("Previously selected live authorization was removed, changed or is invalid") from error


def check_pair_identity(approval, markets, tournament_id):
    try:
        scanner.check_approved_pair(approval, markets, tournament_id)
    except scanner.API_ERRORS as error:
        raise LiveSettlementBlocked("IDs/mappings: market IDs, exchanges or tournament scope changed") from error


def verify_settlement(session, markets, tournament_id, approval):
    """Revalidate the explicitly chosen route; never fall back to paper approval."""
    try:
        validate_authorization(approval)
        revalidate_authorization(approval)
        check_pair_identity(approval, markets, tournament_id)
        quarantine.require_unblocked_markets(approval["market_ids"])
        quarantine.require_unblocked_exchanges(approval["exchange_ids"])
        if approval["verification_route"] == MACHINE:
            allowed, context = scanner.get_pair_rules(session, *markets, tournament_id)
        elif approval["verification_route"] == MANUAL_AUTONOMOUS:
            from manual_autonomous_settlement import read_evidence, require_matching_evidence
            evidence = read_evidence(session, markets, approval)
            require_matching_evidence(approval, evidence)
            context = {"mode": MANUAL_AUTONOMOUS, "tournament_id": tournament_id,
                       "market_ids": approval["market_ids"], "exchange_ids": approval["exchange_ids"],
                       "settlement_fingerprint": evidence["evidence_hash"], "manual_evidence": evidence,
                       "authorization_tier": MANUAL_AUTONOMOUS, "machine_verified": False}
            allowed = {"NO-PAIR"}
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
