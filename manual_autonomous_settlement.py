"""Fresh evidence for an explicit manual autonomous approval; never grant one.

This reader deliberately does not decide that two outcomes are compatible.
A human must review the exact roots, policy and pair-specific rationale before
recording an approval in the separate empty-by-default configuration.
"""
import hashlib
import json
import time
from html import escape
from html.parser import HTMLParser

import live_settlement as live
import price_reader as scanner
from paper_trader import parse_api_timestamp

POLICY_URL = "https://sig.thesuper.market/docs/settlement-and-payouts"
POLICY_CONTENT_FORMAT = "settlement-article-html-v1"
REQUIRED_LIMITATIONS = [
    "Cancelled/N/A/refund outcomes can invalidate the ordinary 1-SUSQie payout floor.",
    "Administrator rulings, overrides or corrections can depart from the reviewed interpretation.",
    "Manual interpretation is not a machine relationship guarantee; independent or delayed settlement can cause losses.",
    "Legs execute separately; partial, UNKNOWN or one-sided execution requires persistent manual review.",
]


class PolicyArticle(HTMLParser):
    """Read exactly one rendered policy article, excluding page runtime code.

    The official documentation wraps its article in div.markdown-content.
    Keep text, headings, lists and semantic attributes (including link targets).
    Ignore scripts/styles/templates, comments and presentation/runtime attributes.
    A missing, duplicate or malformed article fails closed; do not hash the
    surrounding navigation or serialized framework copies of the article.
    """
    OMIT = {"script", "style", "template", "noscript"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.articles, self.stack, self.omitted, self.parts = 0, [], [], []

    def handle_starttag(self, tag, attrs):
        if self.omitted or tag in self.OMIT:
            if tag not in self.VOID:
                self.omitted.append(tag)
            return
        if tag == "div" and "markdown-content" in (dict(attrs).get("class") or "").split():
            self.articles += 1
            live.require(self.articles == 1 and not self.stack, "Policy content: multiple settlement articles")
            self.stack.append(tag)
            return
        if not self.stack:
            return
        attributes = "".join(" " + key + ("" if value is None else '="' + escape(value, quote=True) + '"')
                             for key, value in sorted(attrs)
                             if key not in {"class", "id", "style"} and not key.startswith("data-"))
        self.parts.append("<" + tag + attributes + ">")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.omitted:
            live.require(self.omitted[-1] == tag, "Policy content: malformed excluded element")
            self.omitted.pop()
        elif self.stack:
            live.require(self.stack[-1] == tag, "Policy content: malformed settlement article")
            self.stack.pop()
            if self.stack:
                self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        if self.stack and not self.omitted:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data("&" + name + ";")

    def handle_charref(self, name):
        self.handle_data("&#" + name + ";")


def policy_content(html):
    """Stable UTF-8 inner article HTML; never use a whole-page fallback."""
    article = PolicyArticle()
    article.feed(html.decode("utf-8", errors="strict"))
    article.close()
    live.require(article.articles == 1 and not article.stack and not article.omitted and article.parts,
                 "Policy content: settlement article missing, empty or incomplete")
    content = "".join(article.parts)
    live.require(content.startswith("<h1>Settlement &amp; Payouts</h1>"),
                 "Policy content: unexpected settlement article heading")
    return content.encode("utf-8")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def evidence_hash(evidence, approval):
    """Bind the reviewed approval fields and current component fingerprints."""
    payload = {k: v for k, v in evidence.items() if k != "evidence_hash"}
    payload["approval"] = {k: v for k, v in approval.items() if k != "evidence_hash"}
    return digest(payload)


def read_policy_hash(session):
    """Hash the official article, independently of scripts/page deployment."""
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
    return hashlib.sha256(policy_content(response.content)).hexdigest()


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
    live.require(tid == approval["tournament_id"], "IDs/mappings: manual autonomous tournament UUID/slug changed")
    live.check_pair_identity(approval, markets, tid)
    live.require(len(markets) == 2 and all(m["status"] == "open" for m in markets),
                 "Market/root evidence: manual autonomous markets are no longer open")
    live.require(approval["source_refs"] == live.evidence_sources(approval), "Sources: manual autonomous references changed")
    rules = []
    for mid in approval["market_ids"]:
        tree = scanner.fetch_json(session, f"{scanner.API_BASE_URL}/markets/{mid}/nodes", {"tournamentId": tid})
        live.require(scanner.numeric_id(tree["market_id"]) == mid, "IDs/mappings: manual autonomous resolution tree ID changed")
        scanner.check_context(tree["contexts"], tid)
        root = tree["root"]
        live.require(isinstance(root, dict) and root["node_type"] == "contract" and root["settled_with"] is None
                     and isinstance(root["contract_type"], str) and bool(root["contract_type"])
                     and isinstance(root["contract_details"], dict) and bool(root["contract_details"]),
                     "Market/root evidence: resolution rules are missing, ambiguous or already settled")
        parse_api_timestamp(root["settlement_date"])
        rules.append(root)
    evidence = {"evidence_version": 2, "policy_content_format": POLICY_CONTENT_FORMAT,
                "instruments": [{"market_id": scanner.numeric_id(m["id"]), "exchange_id": scanner.get_exchange_id(m),
                                 "status": m["status"], "is_composite": m["isComposite"],
                                 "is_multi_outcome": m["isMultiOutcome"]} for m in markets],
                "resolution_roots": rules, "settlement_policy_sha256": read_policy_hash(session)}
    evidence["market_root_sha256"] = digest({k: evidence[k] for k in ("instruments", "resolution_roots")})
    evidence["approval"] = {k: v for k, v in approval.items() if k != "evidence_hash"}
    evidence["evidence_hash"] = evidence_hash(evidence, approval)
    return evidence


def require_matching_evidence(approval, evidence):
    """Report the changed component before testing the combined binding."""
    live.require(approval["evidence_version"] == 2, "Policy content: legacy raw-HTML approval needs migration")
    live.require(approval["policy_content_sha256"] == evidence["settlement_policy_sha256"],
                 "Policy content changed: settlement article requires human re-review")
    live.require(approval["market_root_sha256"] == evidence["market_root_sha256"],
                 "Market/root evidence changed: instrument status or resolution rules require human re-review")
    live.require(approval["evidence_hash"] == evidence["evidence_hash"],
                 "Approved evidence binding changed: rationale, limitations or approval metadata require revalidation")
