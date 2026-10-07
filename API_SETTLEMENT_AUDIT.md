# API and settlement audit

Audit date: **7 October 2026**. The scanner remains **paper-only**. The API and
settlement checks below are complete for the current strategy; they do not
establish readiness or authorize submitting competition account orders.

The Prediction Cup uses virtual **SUSQies**. Competition orders would still
change the user's account, positions, and competition results. They are separate
from the local 5,000 paper SUSQies portfolio. The official guide describes a 100,000
SUSQies competition allocation; the paper balance is our own simulation setting.
[Official competition guide](https://sig.thesuper.market/docs).

## What the official documentation establishes

- Binary YES and NO contracts normally pay 1 or 0 at settlement. Cancellation or
  an N/A outcome has separate refund rules based on refundable costs. It must not
  be treated as an ordinary YES or NO result. Some election markets require
  manual settlement, so a title alone does not define the settlement procedure.
  [Settlement and payouts](https://sig.thesuper.market/docs/settlement-and-payouts).
- Buying NO is equivalent to selling YES in the exchange's trading model. A
  compatible counterparty is still needed. A limit order can remain unmatched;
  observing a price does not prove that both legs of a pair will fill.
  [Markets and trading](https://sig.thesuper.market/docs/markets-and-trading).
- The 1 October changelog states per-account limits of 100 reads and 30 writes
  per minute for normal accounts, and 600 reads and 200 writes for DMM accounts.
  It also describes a bulk exchange-price endpoint supporting up to 100 IDs.
  The scanner uses the normal-account read allowance conservatively. Other
  applications and API keys share that account budget.
  [Official changelog](https://sig.thesuper.market/docs/changelog?page=2).
- Competition information and rules can change, and liquidity providers may
  withdraw liquidity. Neither the current rule text nor visible book quantities
  should be assumed permanent. [Competition rules, sections 3 and 6](https://predictionscup.com/rules/).

## Why matching party names is insufficient

Let `D` be the Democratic market's YES payout and `R` the Republican market's
YES payout. For ordinary binary settlement, each is either 0 or 1.

| Settlement case | D YES | R YES | Two YES contracts pay | Two NO contracts pay |
|---|---:|---:|---:|---:|
| Democratic market wins; Republican market loses | 1 | 0 | 1 | 1 |
| Republican market wins; Democratic market loses | 0 | 1 | 1 | 1 |
| Neither market wins | 0 | 0 | 0 | 2 |
| Both markets win, if their rules permit it | 1 | 1 | 2 | 0 |
| Cancellation, void, or N/A | Per refund rules | Per refund rules | Must calculate separately | Must calculate separately |

The fourth row illustrates a failure condition to rule out, rather than a claim
that a particular election permits both parties to win.

A YES pair has a payout of `D + R`. Its payout is at least 1 only when the rules
establish that **at least one** of these markets must win. Two party names do not
exclude a third-party or other result. We must verify that the pair covers every
possible ordinary outcome before treating a cheap YES pair as arbitrage.

A NO pair has a payout of `2 - D - R`. Its payout is at least 1 when the rules
establish that **both markets cannot win**. A third-party winner can be consistent
with this condition: both NO contracts would then pay 1. This does not establish
the separate cancellation/refund outcome.

If exactly one market must win, both ordinary payout floors are 1. Refund cases
and fees still need their own calculation. For example, paying 0.90 for a YES pair
does not guarantee a 0.10 profit if a valid outcome pays that pair 0.

The public Rhode Island Senate pages contain `Election Outcome` contracts.
Markets 387 and 388 share `raceId=62978`, `stageId=98108`,
`electionDate=2026-11-03`, `raceStage=General`, and `resolutionType=Party Winner`;
their `winnerName` fields name the respective parties. Scheduled settlement dates
also match. This identifies a common event and stage, not exhaustive outcomes
or refund treatment.
[Democratic market](https://sig.thesuper.market/markets/387),
[Republican market](https://sig.thesuper.market/markets/388).

The four chamber-control contracts, markets 151–154, are `Freeform`. Their public
page data describes party majority control when the new Congress is seated and
manual settlement once major news organizations call control. It does not
specify ties, independent caucusing, or Senate tie-breaking control. The scanner
excludes Freeform contracts rather than inferring complementary outcomes.
[House market](https://sig.thesuper.market/markets/151?tournament=midterm-elections),
[Senate market](https://sig.thesuper.market/markets/154?tournament=midterm-elections).

Refunds remain outside the ordinary payout calculation. Fully refunding both
legs removes the modeled profit. Refunding one leg while the other loses can
produce a loss. The guide does not promise coordinated cancellation across
separate markets. Reported profit is **conditional on ordinary settlement,
before unverified fees and refund cases**, not an unconditional guarantee.

## Findings from the initial scanner

1. **The initial list and book requests used different hosts.** Market discovery
   used `sig.thesuper.market`; order books used `www.thesuper.market` and the
   first returned exchange. Verify the documented competition endpoint and
   returned market/exchange identity. Keep the SIG credential on the intended
   API origin. External venue prices must not be presented as executable Cup
   liquidity.
2. **Pairing relied on titles and assumed a payout floor of 1.** Duplicate titles
   could silently replace earlier market IDs. Require an unambiguous identity
   and reviewed settlement evidence. Skip unresolved pairs rather than assigning
   them an unsupported minimum profit.
3. **An absent filtered pair could look like a disappeared opportunity.** A
   filtered, unverified, or partially fetched market is unknown. Only a complete
   successful observation can establish that an opportunity disappeared. Keep
   that distinction when adding status, rules, or pagination checks.
4. **Saved positions lack market and rule provenance.** Existing version-1 paper
   positions record race text and combined cost, but do not record venue, market
   IDs, leg prices, quote age, or settlement evidence. Preserve their saved cash
   and entry costs. Treat their recorded `minimum_profit` as a legacy estimate
   under the original assumptions, not a newly verified payout guarantee.
5. **Polling had no account-level request budget or quote-age check.** With `N`
   races, fetching two books per race costs `1 + 2N` requests per scan before
   pagination or other checks. Four 15-second scans per minute use the entire
   normal 100-read allowance at 12 races; a larger universe exceeds it. Slow
   sequential requests also separate the
   two leg observations in time. Add pacing, rate-limit backoff, and a maximum
   acceptable quote age or timing gap. Bulk indicative prices do not automatically
   establish executable depth.
6. **Book validation required both sides to exist.** An ask-only book can still
   support YES analysis; a bid-only book may support NO analysis under the
   documented trading model. Validate the needed side independently. Reject
   malformed market entries and book levels safely.

These describe the implementation at the start of the audit. The completed
changes and remaining limitations follow.

## Verified API model

The audit inspected the OpenAPI definition embedded in the official Scalar
reference, then checked documented endpoints with read-only GETs.

- Resolve `GET /tournaments/{slug}` to its UUID and pass `tournamentId` explicitly
  on market, node, relationship, and book requests. Organization-bound defaults
  can return multiple market contexts or default-tournament books.
- Markets use cursor pagination: default 20 records, maximum 100 per page.
  Relationship pages allow up to 200. Incomplete pagination is unknown.
- `GET /markets/{id}/nodes` supplies the resolution tree.
  `GET /exchanges/{id}/orderbook` identifies market and exchange and supplies
  YES prices: descending bids and ascending asks, between 0 and 1.
- Active `mutually_exclusive` relationships support exclusivity; `isExhaustive`
  describes coverage of every valid outcome by the member group.
- Book `asOf.at` is the engine clock when the book was read. Its `sequence` is
  the last applied engine event, not the last trade's timestamp. A null version
  indicates a book read without an engine version.

[Official API reference](https://sig.thesuper.market/api/v1/docs).

API order books may be cached for up to one second and other market data for up
to two seconds. Observed quantities do not promise execution at those prices.
[Official caching update](https://sig.thesuper.market/docs/changelog).

## Paper scanner changes completed

- All requests are GETs to the SIG API origin, with redirects refused.
  Discovery and resolution responses must describe the chosen tournament;
  books must identify the expected market and exchange.
- Complete pagination is required. Duplicate identities and ambiguous titles
  are rejected. Titles discover candidates but cannot approve trades.
- Each leg must be one simple binary YES exchange with an unsettled
  `Election Outcome`, `Party Winner`, `General` contract naming the correct
  party. Race ID, stage ID, election date, and settlement date must agree.
- Relationships are fetched once per scan. NO pairs require an active
  `mutually_exclusive` relationship containing both underlying YES exchanges.
  YES pairs additionally require an exhaustive relationship whose complete
  member group is exactly those two exchanges. Exhaustiveness of a larger
  group does not prove exhaustiveness of its Democratic/Republican subset.
- Unverified pairs are withheld before their resolution trees and books are
  requested. Missing evidence means unapproved, not proven impossible. Failed
  or incomplete observations retain previous state.
- Request starts are spaced by at least 0.75 seconds, about 80 reads per minute
  in this process. A 429 response uses `Retry-After` to establish a cooldown.
- One-sided books use the needed side independently. Malformed levels, wrong
  identities, and missing or stale engine timestamps are rejected. The local
  five-second freshness policy also bounds the delay between leg observations;
  five seconds is our policy, not an exchange execution guarantee.
- New paper positions retain market/exchange IDs, tournament ID, and a
  fingerprint of rules and relationship evidence, leg prices, and book versions.
  Recorded metadata is validated on restore; instrument IDs preserve duplicate
  protection and race limits even after a title changes. Existing saved positions,
  entry costs, and cash remain intact; their old profit fields remain estimates
  under the former assumptions.

The local 5,000-unit starting balance, 250 per-trade and 150 per-race limits,
2% minimum edge, 50-pair minimum depth, 100-pair cap, duplicate protection,
JSON persistence, and CSV history remain in place. No order endpoint is called.

## Read-only verification results

The active `midterm-elections` tournament returned UUID
`bda92870-621e-47b0-bc3c-3602c5c26f55`. Complete market discovery found 117
paired-title candidates. The scoped relationship query returned `data=[]` and
`hasMore=false`: the entire returned graph was empty, not just one race's query.
The scanner therefore approves **no pair** from that snapshot. An empty graph
does not establish that no logical opportunity exists; the chosen automatic
evidence source provides no approval. Title matching does not replace it.

Targeted checks confirmed Rhode Island markets 387/388 use exchanges 1076/1077,
have matching structured election rules, and return correctly identified scoped
books with current engine timestamps and numerical share quantities. In that
snapshot YES-pair cost was 1.025 and NO-pair cost 0.985: the latter's 1.5% ordinary
edge was below the existing 2% threshold. These are historical observations,
not current quotes; neither pair was accepted.

The original **68 offline tests passed** (41 API checks and 27 scanner/portfolio checks).
Tests use fake API responses, controlled clocks, and temporary files
to verify scope, pagination, settlement eligibility, book validation, rate-limit
recovery, and saved-state behavior. They do not require API requests or modify
the user's saved paper holdings.

The original `--audit-only` run exited successfully and byte comparisons confirmed
that `paper_portfolio.json` and `paper_trades.csv` were unchanged. No order endpoint
or account reconciliation endpoint was called during that run. `.env` remains ignored and untracked.

The engine returns timestamps with seven fractional digits, which the existing
Python 3.9 runtime does not accept directly. The parser now preserves microsecond
precision for freshness checks while retaining the original API timestamp in
saved provenance. The environment also emits an urllib3/LibreSSL compatibility
warning; read requests succeeded, but the runtime should be updated before
developing account execution.

## Read-only account and individual race follow-up

`account_reader.py` now reads the enrolled member's tournament cash, complete
holdings response, and every open-order page using explicit competition scope.
It validates metadata, numbers, identities, position totals, and pagination.
Pending enrollment, missing valuations, invalid data, and failed reads remain
unknown rather than being presented as empty holdings or no orders. It never
places or cancels orders, enrols the user, or writes account/paper files.

The official API specifies that resting orders do **not** reserve cash;
`myBalance` is current cash, while open-order quantities are executable
remainders. Account risk must consider those orders separately. Signed holdings
quantities distinguish YES from NO. The checker uses reported values rather
than guessing how NO valuation prices or average costs are normalized.
[Official API reference](https://sig.thesuper.market/api/v1/docs).

The checker completed a live GET-only account read. Byte comparisons again
confirmed the paper portfolio and CSV were unchanged. Private account values
are shown locally and are not stored in this document or repository.
The **89-test suite passed**, including 21 account-reader regressions.

The [Rhode Island Senate review](RHODE_ISLAND_SENATE_REVIEW.md) adds two relevant
findings: an independent appears on the official ballot, and the platform's
Party Winner Info template allows a winning fusion ticket to count for multiple
parties. A single election winner therefore does not establish exclusivity of
the two party contracts. The manual review does not approve either pair or
change the scanner's relationship requirement.

## Offline execution follow-up

`execution_simulator.py` now exercises invented two-leg orders against a separate,
durable fake venue. It covers partial fills, a rejected second leg, fills during
cancellation, missing replies, unavailable reconciliation, and process restart.
Stable client keys and cumulative fill accounting prevent duplicate orders and
cash charges during recovery. Unfinished or unmatched exposure blocks new pairs.
Cancelled remainder does not refund shares already bought. See the
[execution simulator guide](EXECUTION_SIMULATOR.md) for commands and limits.

This is a separate paper exercise. It does not contact the API, read credentials,
change the scanner's portfolio, approve settlement evidence, or implement an
account order adapter. The fake venue supplies complete authoritative history;
its recovery behavior cannot be assumed for eventually consistent API responses.
All 25 execution regressions passed; the complete offline suite now passes
114 tests, including the existing API, account, scanner, and portfolio checks.

## Read-only order preview follow-up

`order_preview.py` now builds a local draft using the documented two-leg body
for `/api/v1/orders/multi-leg`. It reads one chosen pair and the actual account;
it has no submission, cancellation, or enabling flag. Prices are side-relative,
rounded upward to the 0.005 tick, and rechecked against the ordinary 2% edge.
The draft uses a temporary preview key rather than a durable execution intent.
[Official API reference](https://sig.thesuper.market/api/v1/docs).

Account risk reserves one SUSQie per remaining share on every open order,
including sells. Nonzero holding cost bases plus all pending reserves form an
account-wide upper bound on candidate race exposure. This conservative bound
must leave room within the 150 race cap and actual cash; overlapping selected
instruments block the draft. This is not a claim that unrelated holdings actually
belong to the selected race. See [order preview guide](ORDER_PREVIEW.md).

All 25 new preview regressions and the full 139-test suite passed. A live GET-only
check of Rhode Island markets 387/388 completed the account read and then blocked
the preview because no active relationship verified the pair. No draft was
approved and no orders were submitted or cancelled. Private account values are
not recorded here. Fees, refunds, and actual execution remain unverified.

## What would be needed before competition account orders

Readiness requires more than positive paper results:

- The current relationship graph is empty. Investigate official evidence or
  prepare individually reviewed settlement policies with explicit sources.
  Do not weaken approval merely to generate trades. Active relationships still
  need interpretation alongside the actual market rules.
- Verified competition books, stable market/exchange IDs, reviewed settlement
  rules, and profit calculations covering fees and refund cases.
- Current competition cash, holdings, and open orders are now readable with the
  account checker. Future execution must use and reconcile this state, including
  changes between reads; the local paper files cannot substitute for it.
- Account-based risk limits that include outstanding orders and partially
  filled legs, plus checks for order size, tick size, and price movement.
- The offline exercises cover either leg failing, partial fills, cancellation,
  uncertain request outcomes, and recovery after restarting. A future account
  execution plan must verify these behaviors against actual API semantics.
  The API documents
  atomic multi-leg admission and idempotency, but this does not establish
  guaranteed matching of equal quantities on both legs. Review integer order
  quantities and the documented 0.005 limit-price tick before constructing
  orders. [Official order API reference](https://sig.thesuper.market/api/v1/docs).
- Request pacing, quote freshness checks, safe credential handling, and a way
  to stop submitting new orders immediately.
- A larger populated relationship graph can still require hundreds of reads.
  A complete fresh snapshot cannot reliably fit a 15-second cycle under the
  normal account budget. Prioritize or cache appropriate data, or use documented
  realtime/bulk feeds without confusing indicative prices with executable depth.
- Explicit user authorization for competition account orders after the concrete
  execution implementation and its validation can be reviewed.

The current work authorizes the audit and paper-only improvements. It does not
authorize implementing or submitting account orders.

## Offline validation scope

The suite exercises wrong venue/identity rejection, malformed market metadata,
independent YES and NO eligibility, exhaustive groups with additional members,
empty relationship graphs, incomplete observations retaining state, confirmed
disappearance, one-sided books, stale quote rejection, rate-limit handling,
read-only audit mode, and legacy portfolio restoration without rewriting entry
costs. Separate execution tests cover fake order cancellation and restart;
real account execution and settlement refund accounting remain future work.
The tests do not establish an unconditional profit guarantee.

These tests use fake API responses, a fake clock, and temporary portfolios.
They do not read `.env`, submit orders, or change the user's paper holdings.
