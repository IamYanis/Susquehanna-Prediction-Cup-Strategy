# Supervised one-pair execution test

`paired_account_test.py` handles one NO share in each of two party markets.
Preparation, inspection and checks do not place orders. Only an
explicit submission can create competition-account positions. The paper scanner
never imports or calls this tool.

The default policy requires the existing settlement checker to verify the pair
using active engine relationship evidence and matching structured rules.
The separate explicit `--conditional` preparation policy accepts the documented
settlement risks for the capped Rhode Island test described below. A price gap
or an ordinary preparation command does not select this policy. The scanner and
paper trader still require their existing engine evidence.

## Prepare an exact proposal

From the repository directory:

```bash
.venv/bin/python paired_account_test.py prepare --dem-market 387 --rep-market 388
```

These IDs select the Rhode Island Senate contracts; they do not imply that the
pair is approved. Missing settlement evidence blocks preparation and creates no
pair directory. All requests during preparation are scoped GETs.

The proposal requires fresh books, at least 50 visible pairs, a rounded-limit
edge of at least 0.02, actual cash, no selected-instrument holdings or orders, and
the existing capital limits. Existing holdings and all pending-order remainders
count conservatively. Quantity is fixed at **one share per leg**.

Successful preparation saves one controller and two independent leg journals in
the ignored `paired_account_test/` directory. Each leg has a unique idempotency
key, an explicit NO buy limit and tournament UUID, and a 15-minute expiry.
The full pair fingerprint binds both bodies and their settlement evidence to the
execution and cancellation policy.

Inspect the saved proposal without network calls or credential reads:

```bash
.venv/bin/python paired_account_test.py show
```

Review the exact limits, maximum contract spend, expiry and policy before any
submission. Fees and exceptional refund treatment are not established by a
normal-payout relationship. A pair filling is not settlement or realized profit.

## Conditional Rhode Island preparation

After accepting the conditional settlement and unmatched-leg risks:

```bash
.venv/bin/python paired_account_test.py prepare --dem-market 387 --rep-market 388 --conditional
```

This opt-in is limited to the midterm competition, markets 387/388, exchanges
1076/1077 and their reviewed structured Rhode Island Senate race/stage. Quantity
is fixed at one NO share per leg, and the maximum combined contract cost is
**0.970 SUSQies**. Larger prices, other pairs and other competitions are rejected.
It retains the existing liquidity, freshness, account overlap, cash and exposure
checks. Failed reads and malformed evidence still block preparation.

The controller records an explicitly conditional policy, the actual rule
fingerprint, whether relationship evidence was observed, both book versions and
the accepted assumptions. Its approval hash covers that material and both exact
orders. Missing evidence is never replaced with a fabricated relationship or
passed to the paper strategy as a verified payout. Preparation remains GET-only.

The normal payoff calculation assumes exclusive party winner affiliations and
ordinary binary settlement. If both shares fill at limits totaling 0.970 and
exactly one selected party wins, the conditional gain is 0.030 before unverified
fees. Both NO shares losing can lose 0.970. Independent refunds or one filled
leg can also cause losses. Cancellation cannot undo a filled share. See the
[Rhode Island review](RHODE_ISLAND_SENATE_REVIEW.md) for the underlying evidence.

Submission has no `--conditional` switch. It uses the saved policy and exact
approval, refreshes the conditional observations and risk checks, and refuses
changed rules, evidence, higher individual quotes or expired orders. Both policies
use the same sequencing, durable journals, remainder cancellation and unknown-
outcome handling described below. No scanner automatically submits either policy.

## Submit only after exact approval

After the exact proposal has been reviewed and approved:

```bash
.venv/bin/python paired_account_test.py submit --approve FULL_PAIR_FINGERPRINT
```

The tool refreshes account data, both books and settlement evidence, checks the
fingerprint and approved limits again, and saves the controller's execution intent
before any POST. It uses the already tested single-order adapter for each leg.
That adapter also rechecks the target instrument, cash, overlap, risk and expiry
immediately before its one POST.

Execution is **sequential and not atomic**:

1. Submit the first saved order and reconcile its specific order and fill history.
2. If it is open, cancel its unfilled remainder and reconcile any late fills.
3. Submit the second order only if the first is confirmed closed with one full
   acquired share. A late full fill during cancellation can satisfy this condition.
4. Reconcile the second order and cancel an unfilled remainder if necessary.
5. Report `COMPLETE` only when both acquired quantities are confirmed as one.

A first partial fill stops before the second order. A second-leg failure or
partial fill can leave unmatched exposure. **Cancellation does not sell, refund
or erase an acquired share.** The tool records that exposure and stops; it has
no automatic liquidation, replacement leg, key regeneration, or order retry.
The approval covers the two exact one-share orders and cancellation of only their
known unfilled remainders. It does not authorize other trades.

## Reconcile and cancel

```bash
.venv/bin/python paired_account_test.py check
.venv/bin/python paired_account_test.py diagnose
.venv/bin/python paired_account_test.py cancel --approve FULL_PAIR_FINGERPRINT
```

`check` uses only GETs for known order IDs and all their fills. It never completes
a missing second leg or resubmits an uncertain first leg. A lost receipt without
an order ID remains unknown; nearby orders or empty lists cannot prove its outcome.
`cancel` validates each known order before cancelling its remainder. Already
filled shares remain held, even if the cancellation command returns successfully.

`diagnose` reads the current account and its recent scoped trade audit trail,
following cursor pages until the preparation-time boundary or the end. It checks
coverage, ordering and duplicate events before reporting the selected exchanges'
recent activity. It makes only GETs and leaves all execution journals unchanged.
It never identifies an order by a guessed client key, releases an uncertain
attempt, or interprets an empty audit as proof that the POST did not execute.

New uncertain placements retain a safe diagnostic category, HTTP status and a
recognized documented API error code. Raw server messages, exception strings,
headers and credentials are never saved or printed. Earlier journals remain
readable, but diagnostics cannot reconstruct their discarded HTTP responses.

Controller states include `UNKNOWN`, `UNMATCHED`, `CLOSED_NO_FILL` and `COMPLETE`.
All attempted states block another submission, including after a restart.
`COMPLETE` means two confirmed acquired shares, not a guaranteed payoff or profit.

## Preserve state

Use one process and make no competing account writes during this test. There is
one fixed production state directory; no CLI option creates an alternate attempt
under another path. Existing directories, incomplete journals and single-order
test journals block new preparation. The separate single-order CLI also refuses
new preparation while the default paired directory exists.

An exclusive `.operation.lock` prevents concurrent submit/check/cancel actions
on these journals. If a killed process leaves it behind, confirm the process is
stopped and inspect all journals and account state before manual lock recovery.
Never remove the directory or generate replacement keys to clear an unknown
order. Custom single-order journals and external trading processes are not a
global account lock; the operator must keep those inactive.

After any persistence error, discard in-memory state and restore all three
journals. An order may have been accepted even if saving its receipt failed.
The controller and leg intent records prevent an automatic replacement attempt.
There is no receipt replay because the API's idempotency retention duration is
undocumented. Preserve the original bodies and keys for manual reconciliation.

The module's tests use a fake HTTP venue and temporary journals. Development and
offline tests do not submit competition orders or touch your `.env`, paper
portfolio or trade log.

All 251 repository tests passed on 8 October 2026 after the conditional policy
was added. Tests cover its GET-only preparation, fixed scope and cost cap,
changed-rule/quote/cash rejection, bound assumptions, strict default behavior
and preservation of unmatched shares in the fake venue.

After the user accepted preparation under the conditional policy, a live GET-only
preparation completed at about 00:09 UTC on 8 October 2026. The two saved one-share
NO limits were 0.125 and 0.845, with a combined contract-cost cap of 0.970. Fresh
account, rules, books and risk checks passed; the relationship evidence remained
missing. Only ignored local controller/leg journals were saved. No orders were
submitted or cancelled, and submission still requires review and exact approval.

## Authorized submission attempt, 8 October 2026

The user subsequently authorized the exact one-share NO orders in markets
387/388, with combined contract cost capped at 0.970 SUSQies and cancellation of
known unfilled remainders. Submission passed the pair preflight and attempted
the first order. A valid placement receipt could not be confirmed, so the first
leg and controller were saved as `UNKNOWN`, with no confirmed order ID. The
second leg remains `PREPARED` and was not submitted. No placement was replayed
and no guessed order ID was cancelled.

Subsequent GET-only account reads showed no nonzero holdings or open orders.
All-status history scoped to the first exchange contained no records created
since preparation. These observations do not prove that the uncertain POST
never executed. The original journals, request bodies and keys remain preserved;
no successful trade or completed pair is confirmed. Further submissions remain
blocked pending reconciliation of the original attempt.

A later live GET-only `diagnose` run again reported zero holdings, zero open
orders and zero selected-exchange trade events since preparation, with complete
recent audit coverage. The original controller and leg journals stayed unchanged.
All 259 repository tests passed after safe placement diagnostics and the read-only
audit command were added. The original failure cause remains unavailable because
its HTTP status and response were not retained by the earlier handler.
