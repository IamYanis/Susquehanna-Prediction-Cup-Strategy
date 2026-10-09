"""Fresh evidence for an explicit manual autonomous approval; never grant one.

This reader deliberately does not decide that two outcomes are compatible.
A human must review the exact roots, policy and pair-specific rationale before
recording an approval in the separate empty-by-default configuration.
"""
import hashlib
import json
import time

import live_settlement as live
import price_reader as scanner
from paper_trader import parse_api_timestamp

POLICY_URL = "https://sig.thesuper.market/docs/settlement-and-payouts"
REQUIRED_LIMITATIONS = [
    "Cancelled/N/A/refund outcomes can invalidate the ordinary 1-SUSQie payout floor.",
    "Administrator rulings, overrides or corrections can depart from the reviewed interpretation.",
    "Manual interpretation is not a machine relationship guarantee; independent or delayed settlement can cause losses.",
    "Legs execute separately; partial, UNKNOWN or one-sided execution requires persistent manual review.",
]


def read_policy_hash(session):
    """Pin actual official HTML bytes, not merely an unchanged URL.

    Cosmetic/document deployments can require reapproval too. This conservative
    hash never ignores a potentially meaningful policy change. Read failures,
    redirects and rate limits cannot supply usable evidence.
    """
    now = time.monotonic()
    if now < scanner._read_cooldown_until:
        raise scanner.RateLimitError("API read cooldown is still active")
    if scanner._last_request_started is not None:
        delay = scanner.READ_REQUEST_SPACING - (now - scanner._last_request_started)
        if delay > 0:
            time.sleep(delay)
    scanner._last_request_started = time.monotonic()
    response = session.get(POLICY_URL, timeout=scanner.REQUEST_TIMEOUT, allow_redirects=False)
    live.require(response.status_code == 200, "Official settlement policy is unavailable or redirected")
    live.require(response.headers.get("Content-Type", "").lower().startswith("text/html")
                 and isinstance(response.content, bytes) and response.content.strip(),
                 "Official settlement policy content is missing or invalid")
    return hashlib.sha256(response.content).hexdigest()


def read_evidence(session, markets, approval):
    """Return evidence only. No approval timestamp, intent/key or state is made.

    The hash covers every approval field except itself, stable open instrument
    identities, full official resolution roots and current policy content.
    Price/balance changes cannot change a settlement hash. Root changes can.
    """
    live.require(approval["verification_route"] == live.MANUAL_AUTONOMOUS
                 and approval["execution_mode"] == live.AUTONOMOUS_MODE,
                 "This evidence reader is limited to explicit manual autonomous review")
    tid = scanner.get_tournament(session, approval["tournament_slug"])
    live.require(tid == approval["tournament_id"], "Manual autonomous tournament UUID/slug changed")
    scanner.check_approved_pair(approval, markets, tid)
    live.require(len(markets) == 2 and all(m["status"] == "open" for m in markets),
                 "Manual autonomous markets are no longer open")
    live.require(approval["source_refs"] == live.evidence_sources(approval), "Manual autonomous sources changed")
    rules = []
    for mid in approval["market_ids"]:
        tree = scanner.fetch_json(session, f"{scanner.API_BASE_URL}/markets/{mid}/nodes", {"tournamentId": tid})
        live.require(scanner.numeric_id(tree["market_id"]) == mid, "Manual autonomous resolution tree ID changed")
        scanner.check_context(tree["contexts"], tid)
        root = tree["root"]
        live.require(isinstance(root, dict) and root["node_type"] == "contract" and root["settled_with"] is None
                     and isinstance(root["contract_type"], str) and bool(root["contract_type"])
                     and isinstance(root["contract_details"], dict) and bool(root["contract_details"]),
                     "Manual autonomous resolution rules are missing, ambiguous or already settled")
        parse_api_timestamp(root["settlement_date"])
        rules.append(root)
    evidence = {"evidence_version": 1, "approval": {k: v for k, v in approval.items() if k != "evidence_hash"},
                "instruments": [{"market_id": scanner.numeric_id(m["id"]), "exchange_id": scanner.get_exchange_id(m),
                                 "status": m["status"], "is_composite": m["isComposite"],
                                 "is_multi_outcome": m["isMultiOutcome"]} for m in markets],
                "resolution_roots": rules, "settlement_policy_sha256": read_policy_hash(session)}
    evidence["evidence_hash"] = hashlib.sha256(json.dumps(evidence, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return evidence
