# Disabled live-pilot layer

The paper scanner is unchanged. `config.py` names `PAPER`,
`LIVE_PILOT_DISABLED`, and `LIVE_PILOT`, with `PAPER` as the default and
`LIVE_PILOT_SUBMISSION_ENABLED = False`. `live_pilot.py` is a separate diagnostic
and accounting module. It has **no order-submission adapter**, no request-body
builder, no order queue, and no journal/checkpoint writer. Selecting `LIVE_PILOT`
fails before credential or API access. Changing the enable flag alone still
cannot submit an order.

Run a local readiness/policy report without credentials, API reads or state writes:

```bash
.venv/bin/python live_pilot.py
```

Once an allocation checkpoint has been explicitly initialized in a future task,
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

At future explicit allocation initialization, fix the untouchable cash floor to
`max(initial account cash - 5000, 0)`. Also record allocated cash remaining,
last reconciled account cash, per-pair confirmed debits and any manual-review
halt in `live_pilot_allocation.json`. That accounting file is ignored by Git.
It contains no executable order requests or API credentials. The in-memory
factory is for scaffolding/tests; the CLI never calls it or creates the file.
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
checker. Existing holding costs and the full quarantine reserve count toward
exposure and available allocation. Actual cash above the fixed allocation cannot
increase capacity. Selected-exchange holdings or orders block a new pair.
Until authoritative race attribution exists, the existing account-wide exposure
bound also counts against the 100 per-race cap. This can reject unrelated account
holdings conservatively; it never guesses ownership or removes them from risk.

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
paired reconciler. It returns a proposed accounting checkpoint; it does not save
or alter the original journals. Pair fingerprints prevent duplicate debits.

| Observation | Required handling |
|---|---|
| Leg one terminal, exactly one filled; leg two unattempted | `AWAITING_MANUAL_SECOND_LEG`; pause for a fresh supervised decision |
| Leg one partial | Preserve confirmed inventory/spend; halt for manual review; never submit leg two |
| Leg one full, leg two rejected/unfilled/partial | Preserve one-sided exposure; halt for manual review; no hedge/retry added |
| Rejected first leg or no fill | No automatic retry or new intent; manual review |
| Timeout, lost receipt, incomplete/lagging fills, open/uncertain order, mismatched cash/holdings | Latch manual review; no further live trading |
| Both legs exactly one filled and actual account matches | Return reconciled pair and tracked confirmed debits |
| Restart | Reobserve/review; never continue or submit an unattempted second leg |

`consider_second_leg` is another read-only diagnostic. It requires the same pair
fingerprint, a freshly reconciled full terminal first fill, an unattempted second
leg, and an explicit fresh supervised decision. Its default rejects restart
continuation. It revalidates configuration/settlement, refreshes both books and
account-age checks, refuses a second price above the original reviewed limit,
and checks actual first-fill cost plus second price against the unchanged 2%
edge. It reruns pilot risk for the second exchange while counting the first
holding. Every outcome still has `submission_enabled=False`.

## Readiness before enabling a one-pair pilot

**NO-GO for activation.** The disabled validator/reconciler is implemented and
tested offline; no current exchange account/candidate has been audited in this
task and no allocation checkpoint has been initialized.

Still required before enabling:

1. Explicitly initialize and atomically persist the allocation baseline under
   one live-operation lock. Wire durable debits and manual-review halts before
   any next action; restart must fail closed. No reset or halt-clear command is
   provided here.
2. Record explicit pilot-scoped settlement permission. Keep the existing paper
   approval's scope unchanged until that separate approval exists.
3. Connect a separately authorized, manually supervised executor to **all**
   pilot gates before preparation/submission, and reserve both legs' possible
   cost before leg one. Use the existing durable intent-before-submit/no-retry
   discipline. The legacy tools alone do not enforce these new allocation caps.
4. After leg one, persist/verify actual account reconciliation and request fresh
   human supervision before any second-leg action. Persist a global halt for
   uncertainty/partial execution; no restart continuation.
5. Complete a fresh actual-account/settlement/quote check and confirm the maximum
   fee/debit treatment. The existing exact reconciliation fails closed on
   unexplained debits; there is no verified fee budget or automatic settlement
   credit handling in this scaffold.

No activation command is available. The existing paper scanner remains runnable.
