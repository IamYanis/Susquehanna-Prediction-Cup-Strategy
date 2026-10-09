# Disabled live-pilot layer

The paper scanner is unchanged. `config.py` names `PAPER`,
`LIVE_PILOT_DISABLED`, and `LIVE_PILOT`, with `PAPER` as the default and
`LIVE_PILOT_SUBMISSION_ENABLED = False`. `live_pilot.py` is a separate diagnostic
and accounting module. It has **no order-submission adapter**, no request-body
builder and no order queue. It writes only separate pilot accounting, never live
execution journals. Selecting `LIVE_PILOT`
fails before credential or API access. Changing the enable flag alone still
cannot submit an order.

`supervised_accounting_probe.py` now has a **separate, interactive first-leg-only
command** described below. It is not connected to the paper scanner or
`LIVE_PILOT`; no default/background mode can call it. Its implementation was
tested with mocks only. No real probe has been run or pair authorized.

Run a local readiness/state report without credentials or API reads:

```bash
.venv/bin/python live_pilot.py
```

This command does not initialize a missing baseline. Under the process lock, it
loads existing state and persistently halts unfinished execution found at startup.
Missing/corrupt state reports `HALTED_MANUAL_REVIEW` and exits nonzero; existing
corrupt bytes are kept intact for review.

When deliberately initializing the allocation for the first time, run:

```bash
.venv/bin/python live_pilot.py --initialize-state
```

This explicit command reads the enrolled Midterm Elections account with GETs and
saves a new **DISABLED** baseline. It refuses an existing state file, checks the
existing execution/quarantine gates, and never prepares or submits an order.
No reset, migration, or halt-clear command is provided. If state is lost or an old
version is present, review/recover the original records; do not initialize a new
budget over previously used allocation. This implementation task did not run
initialization against the real account.

Once an allocation checkpoint has been explicitly initialized,
the optional `--dem-market ID --rep-market ID` arguments can assess a listed pair
using GETs. They return diagnostics only: `live_eligible=False` and
`submission_enabled=False`. They never prepare, stage or queue an order.

## Limits and protected allocation

| Policy | SUSQies / quantity |
|---|---:|
| LIVE_ALLOCATION | 5,000 |
| MAX_LIVE_CAPITAL_PER_TRADE | 50 |
| MAX_LIVE_CAPITAL_PER_RACE | 100 |
| MAX_TOTAL_LIVE_EXPOSURE | 500 |
| MAX_LIVE_QUANTITY_PER_LEG | 1 |

Existing live checks remain unchanged: at least 2% tick-rounded edge, at least
50 shares of executable depth, five-second authoritative quote freshness, and
account observations no older than 15 seconds. Quantity is exactly one per leg.
The new configuration does not change the paper or legacy supervised limits.

Explicit initialization fixes the untouchable cash floor to
`max(initial account cash - 5000, 0)`. The version-2
`live_pilot_allocation.json` records:

- configured allocation (always 5,000), initial cash and immutable reserve floor;
- cumulative confirmed debits by pair fingerprint, gross allocated cash remaining,
  last reconciled account cash and calculated remaining allocation after reserves;
- stable market/exchange IDs, direction, confirmed quantities/costs, maximum
  possible additional quantities, reviewed price limits and reconciliation status;
- quarantine reserve, total saved exposure, manual-review status/reason;
- state, schema version, increasing revision and UTC creation/update timestamps.

The accounting file, its lock and temporary files are ignored by Git. It contains
no executable order requests, idempotency keys or API credentials.
Missing/corrupt checkpoints block candidate assessment rather than resetting
the budget. Decimal strings preserve exact monetary debits in JSON.

For example, with 20,000 initial account cash, the floor is 15,000 and bot
capacity is at most 5,000. A confirmed 0.800 pair debits tracked bot cash to
4,999.200. Increasing account cash later cannot replenish that allocation:
unexpected cash changes require reconciliation/manual review. Repeated
reconciliation of the same fills does not debit twice. Confirmed debits are
currently cumulative; settlement credits/refunds are not automatically credited
back into the bot allocation.

Pending orders reserve one SUSQie per remaining share as in the existing risk
checker. Saved possible pilot fills also reserve their maximum reviewed cost.
They count in addition to external open orders; overlapping known orders may be
reserved twice conservatively. Existing holding costs and the full quarantine reserve count toward
exposure and available allocation. Actual cash above the fixed allocation cannot
increase capacity. Selected-exchange holdings or orders block a new pair.
Until authoritative race attribution exists, the existing account-wide exposure
bound also counts against the 100 per-race cap. This can reject unrelated account
holdings conservatively; it never guesses ownership or removes them from risk.

## Durable lifecycle and exclusive locking

Mode and accounting state are distinct: every state below still has submission
disabled. `READY` records a successful diagnostic, not reusable quotes or live
permission; every future assessment must rerun the existing freshness/risk gates.

| State | Meaning / transition |
|---|---|
| `DISABLED` | Newly initialized baseline, or fully reconciled pair; no unfinished execution |
| `READY` | All read-only candidate checks, including separate live settlement authorization, passed; submission remains disabled |
| `EXECUTING` | Existing journal reconciliation in progress, or first fill awaiting a fresh supervised decision |
| `HALTED_MANUAL_REVIEW` | Partial/unknown/failed reconciliation, invalid readiness/account observations, or unfinished prior operation; no automatic exit |

The CLI holds `.live_pilot_allocation.json.lock` for its entire run **before**
loading state. This is a separate stable `fcntl.flock` file, using the paper
scanner's same exclusive, nonblocking lock pattern without changing paper mode.
A competing pilot exits clearly. Closing the descriptor releases the OS lock on
normal exit, Ctrl+C, exceptions and process death; the empty lock file is retained.
Local helper operations acquire the same lock. A multi-step supervised diagnostic
must hold `with pilot_lock():` throughout; ending that operation with unfinished
state prevents a subsequent operation from continuing it.

At the first lock acquisition, saved `EXECUTING` becomes
`HALTED_MANUAL_REVIEW` atomically before any account/order reads. A saved halt
retains its reason and all exposure/reserves on every restart. There is no retry,
automatic second leg, or automatic failed/filled classification during recovery.
Missing, duplicate-field, unsupported-version, malformed or inconsistent state
blocks all progress and requires manual review. Nothing is reconstructed from
`paper_portfolio.json`.

Every write validates monetary identities and exposure, checks the prior revision,
and refuses changed baselines, decreased confirmed debits/fills, forgotten exposure
or automatic halt clearing. It writes a temporary file beside the destination,
flushes and `fsync`s it, atomically replaces the destination, then `fsync`s the
directory. A write failure stops the caller. An already saved `EXECUTING` marker
keeps an interrupted reconciliation blocked on the next load. No completed write
credits cash back into the allocation. Actual holdings must match saved confirmed
positions before another pair can pass risk checks.

For example, a first 0.400 fill leaves gross pilot cash at 4,999.600; an unattempted
second leg capped at 0.400 reserves another 0.400, so calculated remaining is
4,999.200 before any quarantine reserve. A process crash at that point restarts
HALTED with the same confirmed debit, inventory and second-leg reserve.

## Separate PAPER and LIVE settlement permission

`approved_settlements.json` remains **paper-only** and is unchanged. The paper
scanner continues to use it independently of live authorization or pilot halts.
The pilot now uses only [`live_approved_settlements.json`](live_approved_settlements.json)
through `live_settlement.py`. Paper approval is never copied, inferred, or used
as fallback permission for LIVE. A pair may have permission in either file,
both files, or neither.

The live file starts with an **empty `pairs` list**. No real pair, including
153/154, has been live-authorized by this implementation. Adding a valid record
requires a separate explicit human approval of the exact reviewed evidence.
The checker never writes, generates, or renews such approval.

The root schema is `version: 1`, `allowed_mode: "LIVE_PILOT"`, and `pairs: [...]`.
The mode tag scopes permission; it does not enable submission. Each entry must
contain all of these fields:

| Field | Binding / requirement |
|---|---|
| `approval_version`, `approved_at` | Positive record version and the explicit human approval's timezone-aware timestamp |
| `pair_name` | Human-readable label; never used to match or infer settlement |
| `tournament_id`, `tournament_slug` | Canonical UUID and exact enrolled competition slug |
| `market_ids`, `exchange_ids` | Two distinct positive IDs as strings, ordered Democratic then Republican |
| `relationship_type` | `mutually_exclusive`; no inferred relationship or generic override |
| `position_types`, `max_quantity`, `execution_mode` | `["NO-PAIR"]`, `1`, `"supervised-one-contract"` |
| `verification_route` | `"machine-verified"` or the narrow `"manual-supervised"` route |
| `settlement_rationale` | Explicit reviewed ordinary-settlement proposition/interpretation |
| `evidence_hash` | Exact SHA-256 fingerprint of the approved official rules/relationship evidence |
| `source_refs` | The exact scoped official evidence URLs described below |
| `limitations` | Explicit exceptions/limitations, including cancellation/N/A/refunds and administrator override risks |

For machine verification, `evidence_hash` must match the existing scanner's
fresh `get_pair_rules` fingerprint. `source_refs` contains, in order, the two
`/markets/{id}/nodes?tournamentId={uuid}` URLs, the scoped
`/relationships?tournamentId={uuid}&exchangeId={first_exchange_id}&limit=200`
URL, and `https://sig.thesuper.market/docs/settlement-and-payouts`.
Active machine evidence and matching official structured rules are required on
every check. A missing graph never switches a machine authorization to manual.

For manual verification, only the existing reviewed **153/154, exchanges
842/843** evidence reader is supported. It rereads scoped market identities and
the exact official settlement wording. The rationale must equal the reviewed
proposition; sources, limitations and evidence hash must match the reader's
record exactly. Its sources are the two scoped node URLs and the official policy
URL. The live approval timestamp is reused; no approval timestamp is generated.
This route is restricted to a manually supervised one-contract pair and retains
the disclosed reference-time/tie/caucus, cancellation/N/A/refund, administrator
override and one-sided execution limitations. It never becomes permission for
unattended trading. Referencing the settlement policy does not remove those
exceptions or guarantee an ordinary payout in exceptional settlement.

Readiness flows as follows:

1. Load the live file and find the exact approved pair/scope. An unlisted or
   paper-approved-only pair returns `LIVE_SETTLEMENT_NOT_AUTHORIZED` before API
   account/quote reads.
2. Read the actual scoped account and market identities; confirm both markets
   still exist, are open and map to the expected exchanges. Apply quarantine.
3. Revalidate the explicitly selected official settlement route, relationship
   type and evidence fingerprint. Missing, ambiguous, changed or unavailable
   evidence returns `LIVE_SETTLEMENT_NEEDS_REVALIDATION`.
4. Reread authorization during the check to detect edits/revocation, then apply
   the unchanged freshness, depth, live edge, account and pilot risk gates.
5. A pass reports `LIVE_SETTLEMENT_VERIFIED`, the route, approved evidence hash,
   approval version and timestamp. `live_eligible=False` and
   `submission_enabled=False` still apply because there is no enabled executor.

`LIVE_SETTLEMENT_NEEDS_REVALIDATION` is saved in the existing durable pilot halt
reason under the exclusive pilot lock. It survives restart. Restoring old
quotes/evidence or editing either approval file does not clear it automatically.
A new human review is required; no halt-clear command is provided. Corrupt,
duplicate, wrong-mode or incomplete live configurations also fail closed and
require review. The second-leg diagnostic rereads this separate permission and
fresh official evidence; removal/change after leg one persists the same halt.
Reconciliation of existing fills still records exposure regardless of permission
revocation: losing permission never erases already incurred risk.

No live order, intent, queue or journal is created by any of these checks.
Unverified conditional settlement policies cannot enter the pilot.

## Existing quarantine

The existing frozen market-387 / exchange-1076 quarantine remains blocked and
fully reserved. Its source journals are verified and never modified. The already
quarantined attempt can remain isolated as before; any new active UNKNOWN,
submitting or uncertain-cancel journal halts all pilot activity. An unrelated
legacy execution journal also requires review because it has no pilot budget
binding. Expiry/empty history never releases an UNKNOWN or authorizes a retry.

## Endpoint support already implemented

All paths below are relative to `https://sig.thesuper.market/api/v1`.

| Method / endpoint | Repository support |
|---|---|
| GET `/tournaments/{slug}` | `account_reader.read_account`: enrolled scope and reported `myBalance` |
| GET `/tournaments/{slug}/portfolio/positions` | Validated signed holdings, cost basis and totals |
| GET `/orders?tournamentId=...&status=open` | Complete scoped pagination and validated remainders |
| GET `/orders/{id}` | `account_test.read_order`: identity, scope, limit, expiry and lifecycle |
| GET `/orders/{id}/fills` | `account_test.observe_test`: all pages, coverage, signed fills, cumulative costs, monotonic history |
| GET `/tournaments/{slug}/portfolio/transactions?type=trade` | Paired diagnostic trade-history inspection; does not recover a lost receipt |
| GET `/tournaments/{slug}/portfolio/fills` | `pilot_account`: recent scoped fills with actual `orderId` receipts |
| GET `/tournaments/{slug}/portfolio/transactions` (no type filter) | `pilot_account`: trades plus fees, settlement, deposits and collateral events |
| GET `/markets/{id}` and `/markets/{id}/nodes` | Scoped instrument identity and settlement evidence |
| GET `/relationships` | Complete engine graph evidence |
| GET `/exchanges/{id}/orderbook` | Scoped identity, authoritative timestamp, prices and depth |
| POST `/orders` | Existing separate single-share supervised tool; never called by this layer |
| DELETE `/orders/{id}` | Existing specific-order supervised cancellation; never called by this layer |
| POST `/orders/multi-leg` | Preview schema/path only; no implemented atomic paired submission |

`observe_test` is the read-only portion of the existing `check_test`; normal
supervised checks still persist the same observations as before. Paired account
reconciliation now returns its already-validated snapshot, so the pilot need not
fetch an inconsistent second view. There is no implemented receipt recovery by
client key after an uncertain placement without an order ID. No such replay is
added here.

## Fresh actual-account readiness and accounting authority

Run the new independent, GET-only account audit:

```bash
.venv/bin/python pilot_account.py
```

It holds the existing exclusive pilot lock and reads an optional durable
checkpoint. It never initializes a baseline, writes execution journals, prepares
orders, or calls POST/DELETE. A missing checkpoint is displayed; it is never
reconstructed from paper state. The command exits nonzero while the accounting
model is unverified, even when the account data itself is complete and fresh.

`read_snapshot` reuses the existing tournament/positions/open-order readers and
validators. It reads the last 24 hours of portfolio fills and **unfiltered**
transactions, extending that window back to checkpoint creation when present.
Pagination must cover the whole window; incomplete coverage, repeated cursors,
invalid scope/signs, missing receipts and exhausted read budgets fail closed.
Every relevant fill's actual `orderId`, plus every open order ID, is inspected
using GET `/orders/{id}` and its complete fill history. Portfolio fills must
match those receipts and their exact lifecycle totals. Trade IDs are never used
as order IDs, and no UNKNOWN receipt is reconstructed or retried.

The snapshot includes cash, positions, all open orders, relevant recent fills,
transactions, order/fill observations, history coverage, UTC observation time
and local start/completion times. It rereads the basic account and history heads
before returning to detect changes during collection. Age is measured from
**before the first GET**, with the existing 15-second limit checked again at use.
The platform documents up to two seconds of cached tournament data in its
[changelog](https://sig.thesuper.market/docs/changelog). These checked sequential
reads are not an atomic exchange snapshot. Quotes are checked again after account
reads so history collection cannot silently make the five-second books stale.

Candidate and supervised second-leg diagnostics require this account gate.
Unavailable/invalid essential reads and inconsistent observations return these
explicit codes; candidate failures persist the existing manual-review halt:

| Code | Meaning |
|---|---|
| `ACCOUNT_BALANCE_UNAVAILABLE` | Enrolled tournament metadata or balance missing/invalid |
| `POSITIONS_UNAVAILABLE` | Holdings or reported totals missing/invalid |
| `OPEN_ORDERS_UNAVAILABLE` | Complete scoped open-order inventory unavailable |
| `FILL_TRANSACTION_RECONCILIATION_UNAVAILABLE` | History, actual receipts, scope, pagination or fill totals unavailable/ambiguous |
| `STALE_ACCOUNT_DATA` | Read/use exceeds the existing freshness limit |
| `ACCOUNT_STATE_INCONSISTENT` | Account changes during collection, or actual scope/cash/holdings disagree with durable state |
| `INSUFFICIENT_ACCOUNT_BALANCE` | Pair cannot be funded without touching the reserve |
| `LIVE_ACCOUNT_RISK_FAILED` | Existing overlap, allocation, exposure, quantity or halt check fails |
| `ACCOUNTING_MODEL_UNVERIFIED` | Exact cash debit/fees cannot be proved |

Allocation remains fixed at 5,000. Available contractual capital is bounded by
the **minimum** of saved unspent pilot allocation, actual cash above the immutable
reserve floor, and 5,000 minus conservative holdings cost; open-order remainders,
saved possible fills and the quarantine reserve further reduce it. All account
positions count toward exposure, including unexpected external positions. Open
orders are conservatively reserved at 1 per remaining share; selected-exchange
holdings/orders block duplicates/netting. Cash changes require reconciliation,
not replenishment. Saved confirmed inventory must match actual signed holdings.
These contractual bounds are not a verified fee allowance. With accounting
unverified, usable live capital is zero regardless of the actual balance.

### What the official API establishes about fees and debits

The current [official API reference](https://sig.thesuper.market/api/v1/docs)
establishes these field meanings:

| Field | Authority and limitation |
|---|---|
| Tournament `myBalance` | Actual tournament cash, **rounded to two decimals**; insufficient to prove fractional per-fill cash deltas |
| Position `quantity`, `costBasis`, `lots` | Signed current inventory, cost basis and FIFO lots; not an all-in debit/fee ledger |
| Fill `price`, `quantity` | Side-relative price (NO price for NO), outcome-signed quantity; no fee/debit field is documented |
| Placement `totalCost` | Sum of fill **notionals**, price × quantity; not guaranteed to equal cash debit |
| Ordinary non-trade transaction `amount` | Signed balance change, including separate `event_type=fee` records |
| Trade transaction `amount` | **Null**; trade events have no documented exact cash-debit or order-ID field |
| ALL transaction `amount` | Collateral delta, not a generic cash delta; inspect `transactionType` |
| Placement `all.effectiveEntryCost` | Authoritative cash debited for an ALL execution after collateral savings, when supplied; not exposed on the documented GET order/fill schemas and not an established all-in fee authority |

Opposite-side buys can net holdings and credit 1 per netted share. Relationship
collateral advances/repayments can also change cash/buying power relative to fill
notional. Neither `totalCost`, cost basis, transaction price × quantity nor a
rounded balance difference is adopted as an authoritative **all-in pilot debit**.
No such field is currently verified. There is no configuration boolean/override
that can declare this model verified, and observing zero fees does not prove a
zero-fee policy.

On 2026-10-08, actual read-only responses reported **20,572.37 SUSQies, zero
positions and zero open orders**. A full historical inspection found 388 fills
and 50 transactions (49 trades and one opening deposit), with null trade amounts
and no fee rows. The repository audit subsequently completed in about 7.2 seconds
with no fills/transactions in its last-24-hour window. These observations confirm
the schema, not the absence of trading fees. Quarantined market 387/exchange 1076
still reserves 0.125 SUSQie; empty visible holdings/orders never release it.
No real pilot baseline was initialized or live settlement permission added.

Until official evidence supplies an exact, receipt-linked all-in cash-debit rule
and a bounded fee policy for this tournament, readiness returns
`ACCOUNTING_MODEL_UNVERIFIED`. Existing pilot reconciliation also refuses to
report completion or advance the reconciled cash baseline under that model.
Observed fills and their nominal allocation charges remain durably recorded for
review; those charges are **not proof of exact fee-inclusive cash debits**.
The persistent halt prevents further trading and any automatic retry/replenishment.

## Accounting investigation and disabled supervised probe

The 2026-10-08 follow-up inspected the current official OpenAPI reference,
product guide, and repository readers, submitters, reconcilers and tests.
**Only public documentation was fetched in this follow-up. No actual-account
API request, order preparation, POST, DELETE, approval or state initialization
was performed. `ACCOUNTING_MODEL_UNVERIFIED` remains mandatory.**

### Completed-trade lifecycle: known versus unknown

| Stage | Documented facts | What is not established |
|---|---|---|
| Placement receipt | `orderId`, `quantityTraded`, `totalCost`, last `fillPrice`, nullable `all`; engine scope also reports canonical side/action/quantity/limit and remaining quantity. No-op can have null ID. | An all-in fee field, an all-in maximum-debit request field, and a later GET equivalent of the full `all` receipt |
| GET order | Scope, side/action, limit, open remainder, terminal reason, closed filled quantity | Placement economics, fees, client key and a cash-debit total are absent from the documented schema |
| GET fills | Exact lifecycle quantity/weighted average; side-relative prices, outcome-signed quantity. Portfolio fills link to actual `orderId` when available. | Fee allocation or actual cash debit per fill. A trade ID is shared by counterparties and is not an order ID. |
| Participant transactions | Trades plus separate fee/deposit/settlement/collateral events; ordinary non-trade `amount` is a balance change | Trade `amount` is null; no order/client-key field or documented fill-ID-to-event-ID join. Transaction price/quantity cannot replace an exact cash ledger. |
| Tournament cash | Explicit UUID scope, cash moves on fills, resting orders do not escrow it; `myBalance` has two decimals | Exact rounding mode, full-precision per-command debit and the time at which all fee/collateral records are final |
| Positions | Signed inventory, cost basis, FIFO lots/average entry. Netting can close/cross a position. | Cost basis is not documented as a fee-inclusive cash ledger; engine `moneyEarned=0` does not mean no proceeds |
| Fees and collateral | Fee events are a supported history type. `all.effectiveEntryCost` describes actual ALL entry debit after an advance; repayment and buying-power fields have separate meanings. | Whether entry debit includes every charge, how future resting fills expose equivalent economics, and a proven maximum SUSQie charge |

`account_test.submit_test` already generates the documented `idempotencyKey`
and allows only one attempt. It currently retains nominal cost/quantity, not
the full placement receipt/`all` economics. `observe_test` computes nominal fill
cost, and the legacy paired reconciler compares that with inventory and rounded
cash. Its rounding tests are consistency checks, not proof of zero fees.
`pilot_account` now prevents those observations from completing pilot debit
accounting under the unverified model. No legacy submission tool was changed or
connected to the probe.

### Idempotency is documented; receipt recovery is not

`POST /orders` uses `SingleOrderInput`, which **requires** a client-supplied
`idempotencyKey` of 1–255 characters. The docs specify that an identical resolved
payload with the same key returns the stored response, including before mutable
market/settlement preflights; a different resolved payload returns HTTP 409.
This is supported API behavior, not an invented request parameter.

The docs do **not** specify retention duration or a GET receipt lookup/filter by
key. GET order details and order lists do not expose the key. An empty order
list therefore does not prove that an ambiguous placement never ran. There is
still no automatic POST retry or receipt replay. A future probe would durably
save the exact explicit tournament/request/key before its single attempt; timeout,
lost response, malformed reply or process crash would halt, preserve evidence,
and never generate a replacement key/order or automatically submit leg two.

### Other authoritative evidence routes

The official schema also documents:

- GET `/dmm/tournaments/{slug}/transactions`: **administrator-only** virtual
  ledger, with `assetType`, `transactionType`, `amount`, `balanceAfter`, profile
  and market identity. For `MONEY`, `balanceAfter` is cash; for `COLLATERAL` it is
  an outstanding advance. Nominal trades and collateral adjustments are separate.
  The row schema still lacks an order/client-key link, and the amount's direction
  convention needs confirmation. A platform-supplied export plus receipt mapping
  could resolve an existing trade without placing a new one. No admin endpoint
  or access escalation was attempted.
- GET `/account`: organization-default-tournament or public-global cash, not an
  explicitly selected tournament UUID. Precision is unspecified; matching a
  displayed balance is not proof of scope or full precision.
- Scoped P&L/history: valuations and stored/interpolated history, not a documented
  per-order cash ledger. Do not subtract valuations to manufacture exact debits.
- GET `/portfolio/collateral?tournamentId=...`: component-level advances and
  guaranteed backing, useful for a future before/after capture but not a
  fee-inclusive order debit.

The admin ledger documentation excludes **real-money** fees, and the contest
disclosure excludes participant financial costs. Neither is adopted as a precise
guarantee about SUSQie execution charges. No missing fee record is interpreted as
a zero-fee rule. These findings are exposed by `pilot_account.accounting_model`;
its verified status remains false.

### Historical accounting audit without a new order

On 2026-10-08, a subsequent GET-only audit reread the official OpenAPI reference
(unchanged from the preceding capture), searched the official product guide and
[competition rules](https://predictionscup.com/rules/), and inspected the existing
account history. The rules' absence of participant financial costs does not
specify a SUSQie trading-fee formula. No published statement was found that makes
`totalCost` fee-inclusive or guarantees zero SUSQie execution charges.

The audit reused `account_reader.read_account`, `pilot_account.read_history`,
`pilot_account.read_order_activity`, and the scanner's paced, redirect-refusing
GET helper. It read these official endpoints:

- `/tournaments/midterm-elections` and its `/portfolio/positions`;
- `/orders?status=open&tournamentId=...`, plus the 49 known `/orders/{id}` records;
- scoped `/portfolio/fills` and **unfiltered** `/portfolio/transactions`, through
  their final pages with complete coverage;
- `/orders/{id}/fills` for examples 5640289, 12663871 and 20267005;
- scoped `/portfolio/history?interval=1h&from=2026-09-30T00:00:00Z`;
- `/account` and `/portfolio/collateral?tournamentId=...`.

This was a historical investigation, **not a fresh live-readiness snapshot**.
Account and history-head rereads were unchanged. The account contained 388 fills,
49 closed orders (34 buys and 15 sales), and 50 transaction entries: 49 trades
plus an opening balance of 100,000. Every trade had `amount: null`; there were no
fee or collateral ledger events. Current positions, open orders and collateral
components were empty. The quarantined UNKNOWN order is separate: its original
journal has no receipt ID and cannot be treated as one of these known orders.
Its journal and 0.125 reserve remain unchanged.

Portfolio fills link to known order IDs. Each order's fills had one candidate
aggregate transaction with matching exchange, timestamp, action and quantity.
That is **observed correspondence**, not a documented order-to-ledger join;
the transaction schema still has no order ID. For example, order 20267005 has
14 fills but one candidate trade event, `engine-85475539`. Its first fill ID is
85475535: constructing ledger IDs from fill IDs would misidentify the event.

| Historical order | Observed execution | Notional and inferred cash effect |
|---|---|---|
| 5640289, market 380 / exchange 1069 | BUY 1,104 NO at 0.905; one fill; existing receipt/fill validation passes | 999.120 notional; inferred ordinary debit 999.120 |
| 5655097, market 385 / exchange 1074 | BUY 109 NO at 0.910 against inferred YES inventory | 99.190 notional; documented 109 netting credit implies a **9.810 cash increase** |
| 12663871, market 385 / exchange 1074 | SELL 7,406 YES at 0.120; one fill; existing receipt/fill validation passes | 888.720 notional proceeds |
| 20267005, market 338 / exchange 1027 | SELL 45,662 NO across 14 fills | Approximately 3,436.960 nominal proceeds; exact portfolio/order fill comparison rejects float-representation differences |

For order 20267005, one portfolio fill reports price `0.06999999999999995`
where the order-fill endpoint reports `0.07`. Scope, fill IDs, quantities, side
and timestamps agree. The existing validator's rejection was retained; the raw
response was used only for diagnosis, never a readiness pass. No comparator or
other execution check was weakened in this audit.

Starting with the documented opening balance, the historical **hypothesis** of
no additional fees or cash adjustments gives:

```text
ending cash = opening balance - gross BUY notional
              + backed SELL proceeds + opposite-side BUY netting credits
            = 100,000 - 1,629,842.410 + 43,999.775 + 1,506,415
            = 20,572.365 SUSQies
```

Using the API's actual floating-point values, the reconstruction differs from
`GET /account`'s observed **20,572.365** by about **3.16e-12 SUSQie**, and predicts
zero remaining inventory, matching the account. Explicit tournament `myBalance`
reports **20,572.37**. The default-scoped `/account` value exposes more precision
in this response, but its precision contract and an explicit tournament identity
are not documented; it does not replace the scoped readiness balance authority.

This is strong evidence that the **observed aggregate history** follows nominal
fills plus netting, without an additional net deduction. It is not proof that
every future fill has zero fees, or that no offsetting/unexposed adjustment could
exist. Inferred intermediate balances depend on that hypothesis; they are not
independent balance observations and must not be used to prove it.

The history contains 168 points, of which 64 are not interpolated. No stored
before/after cash interval isolates one historical order: the intervals with
activity contain 12, 30, 2 and 5 orders respectively. For the last five sales,
actual stored cash goes from 3,921.96 at 2026-10-07 15:00 UTC to 20,572.37 at
16:00 UTC. That validates their combined displayed proceeds, not a precise debit
or fee for any individual receipt. Original placement responses, including
`totalCost` and nullable `all`, were not available for these historical orders;
GET order/fill reads do not recover them.

**Result: `ACCOUNTING_MODEL_UNVERIFIED` remains in force.** To resolve it without
a new order, obtain official confirmation of the SUSQie execution-fee rule
(explicitly zero, or its complete formula/bound), the fee inclusion of each cash
field, and the receipt/fill-to-ledger mapping and finality semantics. For an
existing trade, a platform-provided order-linked cash ledger/export with exact
before/after balances and all fee/netting/collateral adjustments would supply the
missing independent debit evidence. A trade-specific export alone would not
establish a fee rule for future trades.

An isolated supervised one-contract probe could add the original placement
receipt and directly bracket one fill with account reads. That would reduce the
missing-receipt and multiple-orders-per-interval uncertainty, but would not prove
a general fee policy or undocumented precision/finality. The existing 49-order
history already provides substantial behavioral evidence, so another trade is
not necessary as the next investigative step. The probe stays disabled; official
accounting confirmation or an export can be requested without trading.

Only this documentation was changed by this follow-up. No readiness gate,
paper behavior, live authorization, allocation, execution journal or `.env` was
changed; no order was prepared, submitted, cancelled or retried. Focused tests
passed **154/154**, and the full suite passed **424/424**. Existing regressions
cover absent fee rows, retained fee/collateral records, rounded-balance ambiguity
and refusal to finalize pilot debit accounting under an unverified model.

### SUPERVISED_ACCOUNTING_PROBE — separate controlled manual command

The 2026-10-09 implementation adds a durable first-leg-only coordinator. Default
mode is **NOT ARMED**. `LIVE_PILOT_SUBMISSION_ENABLED` remains false, and normal
pilot readiness still fails `ACCOUNTING_MODEL_UNVERIFIED`. There is no autonomous
probe, generic accounting override, retry, second-leg request, cancellation,
hedge, resume or halt-clear command.

**Currently eligible pairs: none.** `live_approved_settlements.json` is empty;
paper approval of 153/154 is not LIVE permission. The real durable pilot baseline
was initialized in the later read-only prerequisites review recorded below.
No real probe journal, request/key or order was created by this implementation.
No approval or `.env` was changed.

These commands cannot submit orders:

```bash
.venv/bin/python supervised_accounting_probe.py
.venv/bin/python supervised_accounting_probe.py --describe --dem-market DEM_ID --rep-market REP_ID
.venv/bin/python supervised_accounting_probe.py --preview --dem-market DEM_ID --rep-market REP_ID --first-market FIRST_ID
.venv/bin/python supervised_accounting_probe.py --report
```

The placeholders must be replaced with explicitly LIVE-authorized IDs; they are
not approvals. `--describe` reads configuration only. `--preview` performs fresh
GET-only checks under the pilot lock, prints the eligible pair, IDs, prices,
maximum first-leg notional, all gates and the exact price-bound confirmation.
It creates no request, key or probe journal. `--report` reads saved diagnostic
evidence without API access or continuation. Existing `EXECUTING` state still
latches HALTED when the lock is acquired, as before.

The **only future submission command**, not executed in this task, is:

```bash
.venv/bin/python supervised_accounting_probe.py --supervised-accounting-probe --dem-market DEM_ID --rep-market REP_ID --first-market FIRST_ID
```

It requires a real interactive terminal and this exact dynamically populated
phrase (the capitals/placeholders below show its format):

```text
PROBE BUY 1 NO MARKET MARKET_ID EXCHANGE EXCHANGE_ID TOURNAMENT TOURNAMENT_UUID MAX NOTIONAL PRICE SUSQies FEES UNVERIFIED NO LEG 2 REVIEW BINDING
```

`PRICE` has three decimals. `BINDING` is the first 16 hex characters of SHA-256
over the full LIVE authorization, chosen first market, both reviewed limits and
durable pilot revision. Only the **literal phrase printed by the fresh preview**
is accepted; `yes`, piped input and stale/changed prices are rejected. The generic
configuration-only design phrase cannot submit an order.

The gates are:

1. Original valid durable baseline, no saved halt/unfinished execution or prior
   probe journal; hold the existing exclusive pilot lock for the whole operation.
2. Separate pair-specific LIVE permission, unchanged official rules/evidence,
   expected stable market/exchange IDs and quarantine exclusions/reserve.
3. Full fresh actual-account snapshot: scoped cash, positions, all open orders,
   unfiltered recent transactions, relevant fills and receipt reconciliation.
   No selected holdings/netting; no open orders anywhere that could spoil the
   isolation. All existing account/durable-state consistency checks still apply.
4. Existing allocation and caps: fixed 5,000, trade 50, race 100, total exposure
   500, exactly one per leg. The probe checks one SUSQie of available capacity
   before confirmation; account cash above the allocation adds no capacity.
5. Both authoritative books meet the existing five-second freshness, executable
   depth of 50 and the separate tick-rounded **0.5% probe edge**, even though
   only leg one can be submitted. Pair maximum notional remains at most one
   SUSQie. The normal LIVE_PILOT threshold remains **2%**; paper behavior is
   unchanged.
6. Separate command plus exact TTY confirmation. After human input, repeat every
   fresh account/settlement/quote/risk check. Reject any changed limit or binding;
   never silently widen a price or accept old quotes.

Only the pre-existing `ACCOUNTING_MODEL_UNVERIFIED` result is accepted as an
expected **probe purpose** in this separate coordinator. Every other readiness
failure blocks. This exception is not passed to the ordinary pilot, and a probe
never marks its accounting model verified automatically.

### Separate fixed probe edge policy

`PROBE_MIN_EDGE = Decimal("0.005")` applies only to the explicitly supervised
accounting probe. The shared `executable_limits` helper retains the existing
account/quote freshness, top-of-book depth, quantity, tick rounding and quote
version checks. It prices executable buy limits with Decimal and computes:

```text
ordinary edge = Decimal("1.000") - total tick-rounded pair notional
```

`observed_probe_limits` always requests exactly one NO contract per leg and
requires pair notional <= 1.000 and ordinary edge >= 0.005. Zero and negative
edges fail. It never uses the paper scanner's classification as permission.
The same probe checker is called during preview, again after interactive
confirmation, and immediately before submission after the durable pre-order
writes. No runtime threshold flag or normal LIVE threshold override was added.

The normal `observed_limits` checker continues to require >= 0.020, including
all LIVE_PILOT candidate and second-leg checks. Existing scanner eligibility
checks remain in that normal checker. Settlement authorization, risk/allocation,
quarantine, manual confirmation, no retries, no automatic second leg and the
persistent post-probe manual-review halt remain unchanged.

The 0.005 diagnostic minimum is one price tick, the smallest positive ordinary
edge for a one-contract pair. With this tick grid, a 0.25% minimum admits the same
positive limits; a 0% minimum would also admit break-even pairs. This margin is
not a verified fee allowance or guaranteed profit on the actual first leg.
Fees remain unverified, and the acquired single NO contract can lose its cost.

| Tick-rounded pair notional | Ordinary edge | Probe quote gate | Normal LIVE quote gate |
| --- | --- | --- | --- |
| 1.005 | -0.005 | FAIL | FAIL |
| 1.000 | 0.000 | FAIL | FAIL |
| 0.995 | 0.005 | PASS | FAIL |
| 0.990 | 0.010 | PASS | FAIL |
| 0.980 | 0.020 | PASS | PASS |

These are quote-policy examples, not trade eligibility. The live authorization
list is still empty and LIVE_PILOT remains disabled. No real probe was run.

### Read-only Senate readiness watcher

Run from the repository with:

```bash
.venv/bin/python senate_readiness_watcher.py
# One observation, without starting the continuous loop:
.venv/bin/python senate_readiness_watcher.py --once
```

The watcher checks only markets 153/154, exchanges 842/843, for one NO contract
per leg in Midterm Elections. It uses the existing 15-second start-to-start
cadence, paced official GET requests and Retry-After cooldown. A slow cycle can
take longer; old or unavailable data never preserves READY status.

**READY means every non-authorization gate for the supervised accounting probe
passes at the displayed timestamp.** It does not grant LIVE authorization,
enable LIVE_PILOT, prepare an order or run a probe. Explicit LIVE authorization,
a separate manual probe command and exact interactive confirmation remain
required. The normal LIVE_PILOT 2% edge/accounting gates are unchanged. The
existing probe-purpose `ACCOUNTING_MODEL_UNVERIFIED` exception remains explicit;
the watcher does not verify fees or an all-in debit cap.

Each observation compares fresh official market/node evidence with the recorded
Senate paper review, using stable IDs, exchange mappings, exact rules and the
evidence hash. Paper permission remains an evidence comparison anchor only.
If a real LIVE entry exists, the existing LIVE verifier also revalidates it;
no temporary or synthetic authorization is created. The existing evidence
fingerprint binds the settlement policy URL, not its live HTML contents.

The watcher reuses the full actual-account/history reader, durable allocation,
quarantine reserve, duplicate holdings/open orders and existing risk checks.
Open orders anywhere block an isolated probe. Both authoritative books must
meet the existing freshness, version, tick and depth-of-50 checks. Exactly-one
NO limits are rounded up to the executable tick; Decimal pair notional must be
at most 1.000 and ordinary edge at least 0.005. Local approval/state files and
execution evidence are checked again before READY is reported.

It opens the **existing** stable pilot lock read-only and holds its exclusive
flock for one complete observation, releasing it before the next interval.
It uses the strict read-only state loader, avoiding the mutating restart loader.
Missing/corrupt state, a busy lock, EXECUTING/HALTED state or prior probe evidence
produce NOT READY without initializing, repairing or writing anything. Existing
halts and frozen UNKNOWN journals are preserved. This watcher creates no log,
journal, idempotency key, request body or authorization record.

The initial verdict is printed once. Further output occurs only on READY/NOT
READY transitions, a changed blocking reason, or an evidence/authorization
status change. Identical verdicts are suppressed, including READY cycles with
changing prices/timestamps. A READY report includes current NO buy limits,
pair notional, ordinary edge, return on capital, executable depth and timestamp.
Quotes must still be refreshed by the actual manual probe before execution.
Ctrl+C stops the watcher and releases any observation lock.

### Durable first-leg lifecycle and accounting

After confirmation and fresh rechecks, the coordinator atomically saves private
`accounting_probe.json` (mode 0600), under the same exclusive pilot lock:

- full scoped before snapshot, durable state/revision, balances, positions/lots,
  open orders, transactions/fills/receipt history and freshness/coverage;
- supplemental GET `/account` balance for precision comparison, explicitly marked
  as default scope unverified and excluded from allocation/debit authority;
- exact authorization/evidence, market and exchange IDs, both orderbooks and
  timestamps, limits and expected maximum first-leg notional;
- **one** exact first-leg request with its newly generated required
  `idempotencyKey`, one-contract quantity and a 30-second expiry.

It then saves pilot `EXECUTING` and the first leg's possible notional exposure,
with zero confirmed debit and no possible/submitted second leg. A durable
`SUBMITTING` journal write/fsync precedes the sole `POST /orders` call. There is
no automatic HTTP retry, redirect, key replacement or second POST. Expiry is not
proof of cancellation: any open/uncertain remainder stays reserved for review.

The raw placement body/status is flushed before parsing, and the complete JSON
receipt, including `all` and unfamiliar fields, is retained. GET order state and
full lifecycle fills are reconciled with the existing one-share reader; then
fresh transactions, portfolio fills, positions and scoped balance are captured.
The supplemental default-account balance is also captured after execution.
Every GET body/error kind is saved before further use, even if validation fails.
Headers/credentials are never retained; credential reflection is redacted and
rejected. API bodies are not printed. Timeout/ambiguous receipt latches HALTED
**before** the remaining diagnostic GETs; no receipt ID is inferred from history.

The report prints before/after balances, observed cash delta (after minus before),
fill notional, fee-like transactions, position quantity change, order state and:

- `MATCH_AT_REPORTED_PRECISION`: isolated records agree with notional at the
  scoped balance's displayed precision;
- `ROUNDING_AMBIGUOUS`: notional is consistent with the conservative rounding
  interval but does not exactly equal the displayed debit;
- `DIFFERS_OR_UNEXPLAINED`, or `UNKNOWN`: additional/ambiguous accounting or
  incomplete/partial/unknown execution.

**Every outcome stops in `HALTED_MANUAL_REVIEW`, including a matching full fill.**
Leg one is a real one-sided position; no paired payout can be assumed until a
separately supervised second leg is eventually reconciled. This implementation
provides no second-leg command. Restart never continues the probe. Any existing
probe journal, including PREPARED/corrupt/rejected/completed, blocks a new attempt
or a new key. There is no automatic journal deletion or halt clearing.

When full receipt/fills/inventory and isolated ledger/cash evidence reconcile,
the **observed scoped balance debit** is charged once to the original allocation;
extra account cash cannot replenish it. Exposure `confirmed_costs` keeps fill
notional separately from optional `observed_cash_debit`. Existing version-2
checkpoints remain valid; the new field is allowed only on a persistently HALTED
`MANUAL_REVIEW` exposure. `None` means cash attribution remains unconfirmed and
cannot be charged as confirmed. Saved debits cannot shrink or disappear.

For example, a 0.335 fill with a displayed 0.340 debit retains position cost
0.335, records observed debit 0.340, and leaves allocation 4,999.660. This is
**reported-precision bookkeeping**, not proof of a fee or exact fractional cash.
Unexplained cash/fees do not fabricate a confirmed debit or advance the cash
baseline. Known inventory is retained; partial/UNKNOWN fills keep the possible
first-leg reserve and halt. All raw evidence remains available for manual review.

Both file writers use temporary-file fsync, atomic replace and directory fsync.
Any failed save stops progress. If a hard crash interrupts cleanup, persisted
`EXECUTING` becomes HALTED at restart and `SUBMITTING` is never replayed. A crash
between journal creation and state reservation still leaves a non-reusable
journal, preventing submission on restart.

The notional limit is the expected maximum debit **under the zero-extra-fee
hypothesis**. The API does not expose an all-in maximum-debit parameter, and this
task did not invent one or verify a fee ceiling. The exact confirmation discloses
that uncertainty. Two-decimal scoped balances can also hide fractional changes;
the comparison's ±0.02 rounding interval tests consistency only. A match does
not automatically clear `ACCOUNTING_MODEL_UNVERIFIED` or permit autonomous use.

The historical audit found a concrete price-representation mismatch:
`0.06999999999999995` versus `0.07` for the same fill. The account reader now uses
its existing 1e-9 numeric tolerance for that **price comparison only**. Identity,
side, quantity and timestamps remain exact, and changed prices beyond the
tolerance still fail. The accounting gate remains unchanged.

### Is a new real probe necessary?

**Not necessarily.** First obtain official tournament-specific clarification or
a mapped export of an already completed trade. Sufficient evidence must specify
the exact order/request-to-cash-ledger mapping and debit sign, applicable SUSQie
charges and their maximum, ALL advance/repayment treatment, balance precision,
and ledger finality/availability after immediate and later fills. An exact
participant debit field or explicit validated ordinary-buy accounting guarantee
could supply the missing authority without any new order.

A separately authorized, isolated probe can add a placement receipt and directly
bracket one fill. Its evidence must be reviewed before changing accounting
verification. One observation does not establish a general fee rule, retention,
future collateral behavior or exact hidden fractional cash. **No actual pair is
currently eligible to run the probe**, and normal pilot readiness remains blocked.

## Two-leg decision and reconciliation scaffold

`reconcile_pilot` copies existing journals, observes specific orders/fills using
GETs, and checks actual cash and signed selected holdings through the existing
paired reconciler. Before GETs, it durably saves `EXECUTING` and possible exposure;
each fresh confirmed leg is saved before observing the next. Final accounting or
halt is saved before return. It never alters the original journals. Pair
fingerprints prevent duplicate debits, and cached journal observations cannot
erase already saved confirmed fills.

| Observation | Required handling |
|---|---|
| Leg one terminal, exactly one filled; leg two unattempted | `AWAITING_MANUAL_SECOND_LEG` only after verified debit accounting; pause for a fresh supervised decision |
| Leg one partial | Preserve confirmed inventory/spend; halt for manual review; never submit leg two |
| Leg one full, leg two rejected/unfilled/partial | Preserve one-sided exposure; halt for manual review; no hedge/retry added |
| Rejected first leg or no fill | No automatic retry or new intent; manual review |
| Timeout, lost receipt, incomplete/lagging fills, open/uncertain order, mismatched cash/holdings | Latch manual review; no further live trading |
| Both legs exactly one filled and actual account matches | Persist completion only with verified debit accounting; otherwise halt without advancing reconciled cash |
| Restart with `EXECUTING` | Persist `HALTED_MANUAL_REVIEW`; no continuation or submission |

`consider_second_leg` is another read-only diagnostic. It requires the same pair
fingerprint, a freshly reconciled full terminal first fill, an unattempted second
leg, and an explicit fresh supervised decision. Its default rejects restart
continuation. It revalidates configuration/settlement, refreshes both books and
account-age checks, refuses a second price above the original reviewed limit,
and checks actual first-fill cost plus second price against the unchanged 2%
edge. It reruns pilot risk for the second exchange while counting the first
holding. It checks the durable checkpoint under the same still-held process lock;
failed checks persist a halt. Every outcome still has `submission_enabled=False`.

## Readiness before enabling a one-pair pilot

**NO-GO for activation.** Durable accounting, exclusive locking, confirmed-debit
persistence, restart/manual-review halts and the separate live settlement gate
are implemented and tested offline. The live authorization file approves no pairs.
Fresh actual account data was audited read-only as described above. Fee/debit
accounting remains unverified and no real allocation checkpoint was initialized.

Still required before enabling:

1. Explicitly initialize the disabled baseline from the actual account after
   reviewing existing execution state. The initializer refuses any existing
   baseline; it is not a restart/recovery or reset command.
2. Review and explicitly record a pair-specific authorization in
   `live_approved_settlements.json`. Existing paper approvals stay unchanged and
   grant no live permission. The gate is implemented; no actual pair is authorized.
3. Connect a separately authorized, manually supervised executor to **all**
   pilot gates before preparation/submission, and reserve both legs' possible
   cost before leg one. Use the existing durable intent-before-submit/no-retry
   discipline. The legacy tools alone do not enforce these new allocation caps.
4. Wire the existing durable read-only reconciliation and fresh human supervision
   into that future paired executor before any second-leg action. The separate
   accounting probe can submit only its manually confirmed first leg; it is not
   connected to the normal pilot or to a second-leg executor. No real execution
   journal was created or modified during implementation.
5. Obtain official exact, receipt-linked debit semantics and a maximum trading-fee
   policy, implement that proven accounting rule, then repeat the fresh account,
   settlement and quote checks. The account gate exists and fails closed now;
   the available official fields cannot prove fee-inclusive pilot debits.

No activation command is available. The existing paper scanner remains runnable.

Offline verification:

```bash
.venv/bin/python -m unittest test_pilot_account test_live_settlement test_live_pilot test_live_pilot_state -v
.venv/bin/python -m unittest test_supervised_accounting_probe test_pilot_account test_account_test -v
.venv/bin/python -m unittest discover -q
```

Tests cover confirmed debits/inventory across separate Python processes,
non-replenishing allocation, persistent partial/UNKNOWN/restart halts,
interruption between leg reads, corruption preservation, atomic-write failures,
stale revisions, actual-account mismatch and exclusive locks across processes.
Settlement tests cover paper-only rejection, explicit machine/manual live
permission, changed IDs/mappings/rules/relationships, unlisted pairs, configuration
revocation/corruption, persistent revalidation after restart, and unchanged paper
trading even when live authorization is invalid or halted.
They use temporary state and fake API responses only.
Account tests exercise full GET snapshots/receipts, unavailable endpoints, 429
without retries, history coverage/sign/totals, freshness, moving accounts, excess
cash, unexpected positions/orders, durable mismatches, persistent accounting
halts and the fee/debit gate on candidates, reconciliation and second legs.
Existing settlement/durability component tests isolate the new accounting gate;
dedicated account tests exercise it without an override.
Probe tests verify command/TTY/exact confirmation, separate LIVE permission,
quarantine, fresh rechecks after human input, pre-POST journal/key/reserve writes,
exactly one mocked POST and no second leg, raw receipt preservation, no retry on
timeout/rejection/malformed response, partial fills, unavailable or inconsistent
account evidence, rounding/fee ambiguity, independent debit/notional persistence,
restart blocking, credential redaction, interrupts and write failures. All use
temporary files and fake API responses, not real-account probes.

Latest coordinator validation: **177 focused tests passed; 447 full-suite tests
passed**. No real probe, authorization, baseline initialization or order was
performed, and protected paper state, `.env` and execution journals were unchanged.

## Read-only prerequisites review — 9 October 2026 (BST)

The explicitly requested baseline was initialized with GET-only account data.
LIVE_PILOT remains disabled; the live authorization list remains empty. The
supervised probe execution source was unchanged. No order, request key, probe
journal, cancellation or second-leg action was created or performed.

### Baseline initialization

`live_pilot.py --initialize-state` now uses the existing full
`pilot_account.read_snapshot` reader: tournament balance, positions, complete
open orders, recent fills/receipts and unfiltered transactions, bracketed by
account/history-head rereads. The 15-second account freshness limit still
applies at persistence. The initializer refuses existing baseline/probe evidence,
orphan state-write files, unquarantined legacy execution journals or insufficient
cash to fund exactly 5,000. It never resets an existing allocation.

The baseline snapshot and SHA-256 fingerprint are optional, immutable additions
to version-2 state. They are saved together with the allocation by the existing
exclusive lock and fsync/atomic-replace mechanism, with file permissions 0600.
Archived timestamps describe the original capture; later trading still requires
fresh account reads. Existing positions/open orders remain external account
exposure and are counted by the unchanged risk checks, not converted into
confirmed pilot debits. No paper state is used.

Saved baseline: `live_pilot_allocation.json` (ignored by Git).

| Field | Saved value |
| --- | --- |
| State / version / revision | DISABLED / 2 / 1 |
| Created at | 2026-10-08T23:50:36.698126+00:00 |
| Tournament | Midterm Elections, `midterm-elections` |
| Tournament ID | bda92870-621e-47b0-bc3c-3602c5c26f55 |
| Account cash, contextual | 20,572.37 SUSQies |
| Configured pilot allocation | 5,000 SUSQies |
| Untouchable cash reserve | 15,572.37 SUSQies |
| Confirmed cumulative pilot debits | 0 |
| Allocated cash remaining, before reserves | 5,000 |
| Market 387 UNKNOWN quarantine reserve | 0.125 |
| Remaining pilot allocation after reserves | 4,999.875 |
| Account positions / open orders | 0 / 0 |
| Current holdings cost / order reserve | 0 / 0 |
| Saved pilot positions / possible pilot fills | 0 / 0 |
| Total saved exposure including quarantine | 0.125 |
| Manual-review halt | false |
| Recent fills / transactions / receipts in captured 24-hour window | 0 / 0 / 0 |
| Account capture duration | 7.470347625 seconds |
| Fill coverage | complete; projected through sequence 103066267 |
| Transaction coverage | complete |
| Snapshot hash | b2788ec0bfcf5b828d50ff54388ca9072e0254f2241049f2861b2b16fda1ced5 |

No prior pilot execution or probe journal was found. The frozen legacy UNKNOWN
in market 387/exchange 1076 remains unresolved, unchanged and quarantined; its
source journal hashes were verified and its possible cost remains reserved.
Historical personal trades do not establish pilot debits. The supplemental
GET /account balance was 20,572.365, but its unverified default scope and different
precision make it contextual only; the scoped tournament balance is the baseline.

### Senate LIVE settlement review: draft only

Fresh scoped responses still identify markets 153/154 as the Democratic and
Republican Senate-control contracts, mapped to exchanges 842/843 in the same
active, enrolled Midterm Elections tournament. Both markets and tournament
contexts were open, with no recorded settlement. Neither instrument is quarantined.
Scoped relationship queries for 842 and 843 both returned empty lists.

Both official Freeform contract nodes still contain the exact previously reviewed
wording: majority control in the November 3, 2026 elections, determined when the
new Congress is seated, with manual settlement after major news organizations
call chamber control. The current manual evidence fingerprint is unchanged:
`8d9a988d1ab1504f387edeed6c48cc54ef0e36b64842310d198a47187484b551`.
It matches every evidence/rationale/limitation field in the existing paper approval.

The official [settlement policy](https://sig.thesuper.market/docs/settlement-and-payouts)
was fetched again successfully at 2026-10-08T23:51:38.930377+00:00. It describes
administrative settlement for chamber control, ordinary winning/losing payouts,
and cancellation/N/A refunds. Its fetched HTML SHA-256 was
`08c86fe0211d4130c3b0adbffb173c4e8027c7df3e38685b8d3cd1cee880b36c`.
This is a retrieval fingerprint, not a stable semantic policy version: the
existing manual evidence hash binds the node data, proposition, limitations and
policy URL, **not the policy page contents**. Today's policy was reviewed
separately; the existing LIVE gate does not detect policy-text changes by hashing
that page. Manual verification also does not eliminate reference-time, tie,
caucus-classification, administrator-override, refund or one-sided-execution risk.

The pair would satisfy the existing manual-supervised LIVE settlement evidence
checks if the operator explicitly approves this exact interpretation for LIVE,
records the actual approval timestamp and adds the reviewed entry. This is
conditional evidence suitability, not present LIVE authorization or full trade
readiness. Paper approval does not supply that permission.

The exact proposed entry is below. `approved_at: null` intentionally makes this
a **non-authorizing draft**: replace it with the actual ISO-8601 timestamp only
after explicit LIVE approval. No entry was added to `live_approved_settlements.json`.

```json
{
  "approval_version": 1,
  "pair_name": "U.S. Senate",
  "tournament_id": "bda92870-621e-47b0-bc3c-3602c5c26f55",
  "tournament_slug": "midterm-elections",
  "market_ids": ["153", "154"],
  "exchange_ids": ["842", "843"],
  "relationship_type": "mutually_exclusive",
  "position_types": ["NO-PAIR"],
  "max_quantity": 1,
  "execution_mode": "supervised-one-contract",
  "verification_route": "manual-supervised",
  "settlement_rationale": "Under ordinary binary settlement, Democratic and Republican majority control of the same U.S. Senate at the new-Congress reference time cannot both hold. One NO share in each market therefore pays at least 1 SUSQie.",
  "evidence_hash": "8d9a988d1ab1504f387edeed6c48cc54ef0e36b64842310d198a47187484b551",
  "source_refs": [
    "https://sig.thesuper.market/api/v1/markets/153/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55",
    "https://sig.thesuper.market/api/v1/markets/154/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55",
    "https://sig.thesuper.market/docs/settlement-and-payouts"
  ],
  "limitations": [
    "Both contracts must use the same reference time and interpretation of majority control.",
    "Congress-seating and news-call timing, ties and caucus classification require human interpretation.",
    "Cancelled/N/A outcomes refund refundable cost and do not preserve the ordinary payout floor.",
    "Administrator rulings, overrides or corrections can depart from the reviewed interpretation.",
    "Partial fills, UNKNOWN submissions and one-sided execution remain possible; no retries are authorized."
  ],
  "approved_at": null
}
```

### Read-only quote audit and real probe preview

The real command was run without altering authorization or any execution journal:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python supervised_accounting_probe.py --preview --dem-market 153 --rep-market 154 --first-market 153
```

Result: `LIVE_SETTLEMENT_NOT_AUTHORIZED | Pair is not explicitly live-approved`.
The probe correctly exited before any executable preview or confirmation phrase.
Its gates were not bypassed. The following independent GET-only quote/account
audit uses the existing readers/risk/quote validators to show the other current
conditions; it is not an authorized execution preview.

Order books observed at approximately 2026-10-08T23:51:48 UTC
(9 October, 00:51:48 BST):

| Instrument | YES bid | Executable NO buy limit | Top bid depth | Book sequence |
| --- | --- | --- | --- | --- |
| Market 153 / exchange 842 | 0.640 | 0.360 | 4,846 | 103077313 |
| Market 154 / exchange 843 | 0.365 | 0.635 | 4,155 | 103077434 |

For one share per leg: pair notional 0.995; ordinary-settlement floor 1.000;
minimum ordinary profit 0.005; edge 0.5%; return on capital approximately 0.5025%.
Current paper classification is WATCH. The unchanged LIVE requirement is 2%.
The possible first leg in market 153 would have notional at most 0.360; market
154 would have notional at most 0.635. Neither figure is a verified fee-inclusive
maximum cash debit. Fees/debit authority remain unverified.

| Readiness gate | Result |
| --- | --- |
| Durable disabled baseline | PASS |
| No unresolved prior pilot execution / prior probe intent | PASS; legacy 387 remains quarantined |
| Fresh active/enrolled tournament identity | PASS |
| Exact market IDs, exchange mappings, current open statuses | PASS |
| Pair excluded from quarantine | PASS |
| Fresh official settlement nodes match paper evidence | PASS for manual-review evidence; graph remains empty |
| Explicit independent LIVE authorization | FAIL: no entry exists |
| Account balance, positions, orders and complete recent history | PASS; 20,572.37, zero holdings/orders/recent activity |
| Actual cash matches durable baseline | PASS |
| Fixed allocation and reserve | PASS; 4,999.875 available |
| Trade/race/total exposure caps | PASS at conservative 1.000 new notional; exposure after 1.125 |
| Exactly one share per leg | PASS for this read-only sizing |
| Fresh authoritative scoped quotes | PASS; source ages 0.776/0.039 seconds at check |
| Fresh account at quote assessment | PASS; 9.318 seconds, below 15-second limit |
| Sufficient executable depth | PASS; both exceed existing 50-share requirement |
| Tick-rounded executable edge | FAIL: 0.005 < 0.020 |
| Probe maximum pair-notional target | PASS: 0.995 <= 1.000 |
| Fee/debit model | ACCOUNTING_MODEL_UNVERIFIED retained; blocks ordinary LIVE, disclosed exception only in the separate probe |

**Probe readiness: BLOCKED.** Even after explicit LIVE settlement authorization,
this quote would still fail the existing edge gate. Part 3's condition for a
fully qualifying probe preview therefore is not met.

That result records the historical audit under the then-shared 2% checker. The
later separate probe policy above admits a fresh tick-rounded 0.005 ordinary
edge for the diagnostic quote gate only. This archived quote is not fresh
execution data; explicit LIVE authorization and every other gate remain required.

No exact executable confirmation phrase exists for this failed preview. A future
passing preview generates the price-, authorization- and pilot-revision-bound
phrase in the unchanged coordinator, with this structure:

```text
PROBE BUY 1 NO MARKET 153 EXCHANGE 842 TOURNAMENT bda92870-621e-47b0-bc3c-3602c5c26f55 MAX NOTIONAL <fresh limit to 3 decimals> SUSQies FEES UNVERIFIED NO LEG 2 REVIEW <16-character current review hash>
```

The review hash cannot be supplied truthfully until there is an actual LIVE
authorization timestamp and a passing fresh quote. No phrase was accepted and no
interactive execution command was invoked.

### Changed files and verification for this task

- `live_pilot.py`: full GET-only baseline initialization, immutable archived
  snapshot/hash and refusal to forget prior probe/orphan-write evidence.
- `test_live_pilot_state.py`: update the initialization CLI test for full reads.
- `test_pilot_baseline.py`: seven focused initialization/persistence tests.
- `LIVE_PILOT.md`: this review and non-authorizing JSON draft.
- `live_pilot_allocation.json`: newly initialized local, Git-ignored runtime state.

Focused suite: **184 passed**. Full suite: **454 passed**. Tests cover archived
holdings/orders and conservative exposure, zero pilot debit/fixed allocation,
restart persistence, immutable/corrupt snapshots, missing/stale GET data, prior
probe/orphan evidence, low cash and failed atomic writes, in addition to the
existing settlement, quarantine, risk and probe tests. Tests use fake APIs and
temporary state. Hash checks confirmed `.env`, both approval configurations,
paper sources/state, frozen execution journals and the supervised probe source
and tests remained unchanged. No commit was made.

## Separate probe edge implementation verification

Only `order_preview.py`, `supervised_accounting_probe.py`,
`test_order_preview.py`, `test_supervised_accounting_probe.py` and this document
were changed for the edge-policy task. The probe's minimum is 0.005 and normal
LIVE_PILOT remains at 0.020. No LIVE authorization, `.env`, durable baseline,
execution journal, paper state, allocation or risk configuration was changed.
No real API write, probe execution or commit was performed.

New tests cover the exact 0.005 boundary, smaller/zero/negative edges, a raw
qualifying edge lost to tick rounding, retained freshness/depth/version checks,
a read-only small-edge preview, all three gates accepting the diagnostic edge,
an edge lost after human input, and final pre-submission rejection with a durable
halt. The coordinator tests use fake HTTP and temporary state only. The normal
live checker is tested across its price-tick grid to ensure every edge below
0.020 is rejected; existing LIVE_PILOT tests also retain the 2% requirement.

Validation: **132 focused tests passed; 465 full-suite tests passed**.
Protected-file fingerprints confirmed that only the five requested files changed.
The real live authorization list remains empty, the durable baseline is unchanged,
and no accounting-probe journal was created.
