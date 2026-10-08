# Disabled live-pilot layer

The paper scanner is unchanged. `config.py` names `PAPER`,
`LIVE_PILOT_DISABLED`, and `LIVE_PILOT`, with `PAPER` as the default and
`LIVE_PILOT_SUBMISSION_ENABLED = False`. `live_pilot.py` is a separate diagnostic
and accounting module. It has **no order-submission adapter**, no request-body
builder and no order queue. It writes only separate pilot accounting, never live
execution journals. Selecting `LIVE_PILOT`
fails before credential or API access. Changing the enable flag alone still
cannot submit an order.

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
   into that future executor before any second-leg action. No executor is connected
   here, and no orders or live execution journals were created or modified.
5. Obtain official exact, receipt-linked debit semantics and a maximum trading-fee
   policy, implement that proven accounting rule, then repeat the fresh account,
   settlement and quote checks. The account gate exists and fails closed now;
   the available official fields cannot prove fee-inclusive pilot debits.

No activation command is available. The existing paper scanner remains runnable.

Offline verification:

```bash
.venv/bin/python -m unittest test_pilot_account test_live_settlement test_live_pilot test_live_pilot_state -v
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
