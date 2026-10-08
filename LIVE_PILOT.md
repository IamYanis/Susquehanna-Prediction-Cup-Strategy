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
| `READY` | All read-only candidate checks passed; submission and paper-only approval restrictions remain |
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

## Settlement and existing quarantine

Only stable IDs explicitly listed in `approved_settlements.json` are considered.
The checker reuses the scanner's expected market/exchange/tournament mapping,
machine relationship checks and narrow manual official-evidence/hash checks.
Changed/missing identities, rules, hashes or unverified relationships block the
assessment. The configuration is reread before considering a second leg.

The current approval configuration is explicitly **paper-only**. A passing
diagnostic does not convert that approval to live-pilot permission. Pilot-scoped
settlement authorization must be explicitly reviewed and recorded before future
activation. Unverified conditional settlement policies cannot enter the pilot.

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
| Leg one terminal, exactly one filled; leg two unattempted | `AWAITING_MANUAL_SECOND_LEG`; pause for a fresh supervised decision |
| Leg one partial | Preserve confirmed inventory/spend; halt for manual review; never submit leg two |
| Leg one full, leg two rejected/unfilled/partial | Preserve one-sided exposure; halt for manual review; no hedge/retry added |
| Rejected first leg or no fill | No automatic retry or new intent; manual review |
| Timeout, lost receipt, incomplete/lagging fills, open/uncertain order, mismatched cash/holdings | Latch manual review; no further live trading |
| Both legs exactly one filled and actual account matches | Persist reconciled pair/debits and return to `DISABLED` |
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
persistence and restart/manual-review halts are implemented and tested offline.
No current exchange account/candidate has been audited in this task and no real
allocation checkpoint has been initialized.

Still required before enabling:

1. Explicitly initialize the disabled baseline from the actual account after
   reviewing existing execution state. The initializer refuses any existing
   baseline; it is not a restart/recovery or reset command.
2. Record explicit pilot-scoped settlement permission. Keep the existing paper
   approval's scope unchanged until that separate approval exists.
3. Connect a separately authorized, manually supervised executor to **all**
   pilot gates before preparation/submission, and reserve both legs' possible
   cost before leg one. Use the existing durable intent-before-submit/no-retry
   discipline. The legacy tools alone do not enforce these new allocation caps.
4. Wire the existing durable read-only reconciliation and fresh human supervision
   into that future executor before any second-leg action. No executor is connected
   here, and no orders or live execution journals were created or modified.
5. Complete a fresh actual-account/settlement/quote check and confirm the maximum
   fee/debit treatment. The existing exact reconciliation fails closed on
   unexplained debits; there is no verified fee budget or automatic settlement
   credit handling in this scaffold.

No activation command is available. The existing paper scanner remains runnable.

Offline verification:

```bash
.venv/bin/python -m unittest test_live_pilot test_live_pilot_state -v
.venv/bin/python -m unittest discover -q
```

Tests cover confirmed debits/inventory across separate Python processes,
non-replenishing allocation, persistent partial/UNKNOWN/restart halts,
interruption between leg reads, corruption preservation, atomic-write failures,
stale revisions, actual-account mismatch and exclusive locks across processes.
They use temporary state and fake API responses only.
