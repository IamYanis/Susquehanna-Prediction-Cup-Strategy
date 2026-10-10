"""Policy rendering regressions; official snapshots replayed with fake GETs only."""
import copy
import hashlib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import live_settlement as live
import manual_autonomous_settlement as manual
import price_reader as scanner
from test_account_reader import isolate_quarantine
from test_api_audit import party_market

# Official article observed and revalidated on 10 October 2026. Keep the
# substantive HTML in this fixture; scripts/navigation are deliberately separate.
POLICY_ARTICLE = """<h1>Settlement &amp; Payouts</h1>
<p>Settlement records the final outcome and pays or refunds open positions.</p>
<h2>When the competition is final</h2>
<p>Trading closes at the published end time. The competition&#x27;s final standings are locked only after every market has settled. Chamber-control questions and any race that goes to a runoff are settled by administrators once the result is official.</p>
<h2>Automatic settlement</h2>
<p>These market types settle automatically from their configured data sources:</p>
<ul>
<li>Elections</li>
<li>Economics</li>
<li>Politics</li>
</ul>
<p>Autosettlement workers check eligible contracts every four hours. A market remains pending until its source provides a valid result. When a result is available, the market records the outcome and processes positions.</p>
<p>An administrator can force settlement of an automatically settled market when an override is required. A documented reason is required for force settlement.</p>
<h2>Manual settlement</h2>
<p>The four chamber-control questions are settled manually by an administrator once control of the relevant chamber is established. These markets remain closed to trading while awaiting their manual result; the automatic election-data checks do not settle them.</p>
<h2>Winning and losing positions</h2>
<ul>
<li>Each winning share pays 1 SUSQie. Each losing share pays 0 SUSQies.</li>
<li>For example, 25 winning shares pay 25 SUSQies. This is the gross payout, not the gain after the original purchase cost.</li>
<li>A YES position wins when the market resolves YES. A NO position wins when the market resolves NO.</li>
</ul>
<h2>Where payouts go</h2>
<ul>
<li>Positions pay into the holder&#x27;s balance in the competition where they were traded.</li>
</ul>
<h2>Refunds</h2>
<ul>
<li>A cancelled or N/A outcome refunds the remaining position instead of choosing a winner. The refund returns the refundable cost of the held shares.</li>
<li>Once processed, payouts and refunds appear in settlement transaction history.</li>
</ul>""".encode("utf-8")
POLICY_HASH = "9d98f07bea2cc4a70275f963716ee9c8a3d843a0b89c6f17ac8c4513970ca76c"


def rendering(scripts, article=POLICY_ARTICLE):
    return b'<html><head>' + scripts + b'</head><main><div class="markdown-content">' + article + b'</div></main></html>'


class PolicyContentTests(unittest.TestCase):
    def test_reordered_25_scripts_and_runtime_changes_have_identical_article_hash(self):
        scripts = [('<script src="chunk-%d.js"></script>' % i).encode() for i in range(25)]
        variants = [rendering(b"".join(scripts)), rendering(b"".join(reversed(scripts))),
                    rendering(b'<script>different framework deployment</script><style>main{color:red}</style>'),
                    rendering(b'<template><div class="markdown-content">Not a rendered article</div></template>')]
        self.assertNotEqual(hashlib.sha256(variants[0]).hexdigest(), hashlib.sha256(variants[1]).hexdigest())
        for html in variants:
            self.assertEqual(hashlib.sha256(manual.policy_content(html)).hexdigest(), POLICY_HASH)

    def test_article_runtime_elements_and_presentation_attributes_are_ignored(self):
        changed = POLICY_ARTICLE.replace(b"<h2>Refunds", b'<script>runtime()</script><style>p{color:red}</style><h2 class="updated" data-build="2">Refunds')
        self.assertEqual(manual.policy_content(rendering(b"", changed)), POLICY_ARTICLE)

    def test_substantive_text_structure_and_link_targets_are_not_ignored(self):
        for changed in (POLICY_ARTICLE.replace(b"1 SUSQie", b"0.9 SUSQie"),
                        POLICY_ARTICLE.replace(b"<h2>Refunds</h2>", b"<h3>Refunds</h3>")):
            self.assertNotEqual(hashlib.sha256(manual.policy_content(rendering(b"", changed))).hexdigest(), POLICY_HASH)
        first = rendering(b"", POLICY_ARTICLE + b'<p><a href="/rules/a">Official rules</a></p>')
        second = first.replace(b"/rules/a", b"/rules/b")
        self.assertNotEqual(manual.policy_content(first), manual.policy_content(second))

    def test_missing_duplicate_incomplete_or_serialized_only_article_fails_closed(self):
        good = rendering(b"")
        for html in (b"<html><main>Missing article</main></html>", good + good,
                     good.replace(b"</div>", b""), b'<script>escaped markdown-content only</script>',
                     good.replace(b"Settlement", b"\xffSettlement")):
            with self.subTest(html=html[:50]), self.assertRaises(ValueError):
                manual.policy_content(html)


class CurrentApprovalRenderingTests(unittest.TestCase):
    def setUp(self):
        isolate_quarantine(self)
        self.addCleanup(patch.stopall)
        patch.object(scanner, "READ_REQUEST_SPACING", 0).start()
        patch.object(scanner, "_last_request_started", None).start()
        patch.object(scanner, "_read_cooldown_until", 0).start()
        root = Path(live.__file__).parent
        self.approvals = live._load_authorizations(root / "manual_autonomous_approvals.json", live.MANUAL_AUTONOMOUS)

    def test_all_three_migrated_records_validate_against_equivalent_official_snapshots(self):
        self.assertEqual([a["market_ids"] for a in self.approvals], [["377", "378"], ["381", "382"], ["256", "257"]])
        details = {"Alaska Senate": ("62954", "98084", "AK", "U.S. Senate Alaska"),
                   "New Hampshire Senate": ("62972", "98102", "NH", "U.S. Senate New Hampshire"),
                   "Colorado Senate": ("62956", "98086", "CO", "U.S. Senate Colorado")}
        for approval in self.approvals:
            tid, name = approval["tournament_id"], approval["pair_name"]
            race, stage, state, race_name = details[name]
            markets, trees = [], {}
            for party, mid, eid in zip(("Democratic", "Republican"), approval["market_ids"], approval["exchange_ids"]):
                market = party_market(party, mid, eid)
                market["contexts"][0]["tournament"]["id"] = tid
                markets.append(market)
                node_id = str(int(mid) + 1)
                trees[mid] = {"market_id": mid, "contexts": market["contexts"], "root": {
                    "node_id": node_id, "node_type": "contract", "contract_id": node_id,
                    "contract_type": "Election Outcome", "title": "Will the " + party + " Party win the " + name + "?",
                    "settlement_date": "2026-11-04T17:00:00.000Z", "settled_with": None,
                    "contract_details": {"raceId": race, "stageId": stage, "usState": state,
                        "raceName": race_name, "raceStage": "General", "winnerName": party + " Party",
                        "officeLevel": "Federal", "contractType": "Election Outcome",
                        "electionDate": "2026-11-03", "resolutionType": "Party Winner"}}}
            scripts = [('<script src="chunk-%d.js"></script>' % i).encode() for i in range(25)]
            for html in (rendering(b"".join(scripts)), rendering(b"".join(reversed(scripts))),
                         rendering(b'<script>new framework</script>')):
                def get(url, **kwargs):
                    response = requests.Response()
                    response.status_code = 200
                    if url == manual.POLICY_URL:
                        response.headers["Content-Type"] = "text/html; charset=utf-8"
                        response._content = html
                    elif "/tournaments/" in url:
                        import json
                        response._content = json.dumps({"id": tid, "slug": approval["tournament_slug"],
                            "status": "active", "currencyName": "SUSQies"}).encode()
                    else:
                        import json
                        response._content = json.dumps(trees[url.split("/")[-2]]).encode()
                    return response
                session = Mock()
                session.get.side_effect = get
                with self.subTest(pair=name, rendering_hash=hashlib.sha256(html).hexdigest()):
                    allowed, context = live.verify_settlement(session, copy.deepcopy(markets), tid, approval)
                    self.assertEqual(allowed, {"NO-PAIR"})
                    self.assertEqual(context["settlement_fingerprint"], approval["evidence_hash"])
                    self.assertEqual(context["manual_evidence"]["settlement_policy_sha256"], POLICY_HASH)
                    for method in ("post", "delete", "put", "patch"):
                        getattr(session, method).assert_not_called()


if __name__ == "__main__":
    unittest.main()
