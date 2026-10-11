# v0.3.1 external fair-value and shadow-return research

Run one GET-only scan with:

```bash
.venv/bin/python external_value_scanner.py
```

Use `--json` for all IDs, both outcome books, depths, timestamps, flags and
mapping fingerprints. This is independent of `autonomous_pilot.py`: it never
loads or writes pilot state, journals, authorizations, positions or paper state.
It has no order adapter. Nothing in the existing live strategy or its thresholds
changes. The separate HTTP sessions permit only these GET routes:

- SIG: tournament identity, selected markets, resolution nodes, exchange books.
- Public Polymarket Gamma: `/markets/{id}`.
- Public Polymarket CLOB: `/book?token_id=...`.

Polymarket needs no credentials. The SIG key is read from the existing local
`.env` and sent only to the fixed SIG host. Public sessions reject authentication
headers/cookies and `.netrc` login, and neither session follows redirects.
No new environment variables or dependencies are needed.

## Exact mappings and settlement limitations

[`external_market_mappings.json`](external_market_mappings.json) is the research
allowlist, separate from all live and paper settlement approvals. Each entry
records tournament UUID/slug, exact market/exchange/condition/YES/NO-token IDs,
semantic outcome, election/stage, scheduled resolution dates, source URLs,
review note and fingerprints of the complete SIG resolution root and substantive
Polymarket rules. IDs, mappings, questions, slugs, rules, resolution sources and
end dates are rechecked from official GET data on every scan. Prices and mutable
market metadata cannot change the rules hash. Whitespace-only description changes
do not invalidate it. Rule changes block comparison and require another review.

| Race | SIG market/exchange | SIG YES proposition | Polymarket market |
| --- | --- | --- | --- |
| Colorado | 256 / 945 | Democratic Party wins | 630666 |
| Colorado | 257 / 946 | Republican Party wins | 630667 |
| Alaska | 377 / 1066 | Democratic Party wins | Unmatched |
| Alaska | 378 / 1067 | Republican Party wins | Unmatched |
| New Hampshire | 381 / 1070 | Democratic Party wins | 630844 |
| New Hampshire | 382 / 1071 | Republican Party wins | 630845 |

Colorado and New Hampshire are party-nominee contracts on Polymarket even when
the display names candidates. Their descriptions explicitly follow replacement
nominees and exclude independents from party options. SIG exposes the same general
election's Party Winner proposition. These are research comparisons under ordinary
settlement, not unconditional identical claims or live settlement approvals.
Polymarket includes run-offs and a named AP/Fox/NBC/certification hierarchy; the SIG
roots do not promise identical runoff/replacement/source handling. Scheduled end
times differ. Cancellation/N/A/refund, administrator and oracle dispute/override
rules can also differ. All of these limitations are retained in each record/report.

Alaska's official event has named-candidate contracts, multiple possible same-party
winners, and a July 31, 2027 deadline resolving to Other if unconfirmed. Neither
the reviewed event/public search nor the exact party-winner market slugs exposed
an equivalent party contract. It is explicitly `NO_EQUIVALENT_EXTERNAL_MARKET`:
SIG prices are reported, but no candidate token is substituted and no external
edge/signal is produced. Matching titles or adding candidate probabilities cannot
approve an external mapping.

## Prices, normalization and research signal

All arithmetic uses Decimal. SIG BUY YES is the ask rounded up to the existing
0.005 tick; BUY NO is `1 - YES bid`, also rounded up. SELL YES uses the bid rounded
down; SELL NO is `1 - YES ask`, rounded down. Each party's contract is evaluated
independently; Republican YES is never inferred from Democratic NO.

Polymarket bids/asks are obtained from the exact outcome token's full CLOB book.
The highest bid and lowest ask are selected regardless of array order; equal
top-price levels are summed for depth. A headline probability, last trade or
Gamma `bestBid`/`bestAsk` field is never called executable. Both YES and NO books
and Gamma headline references are reported separately.

The corresponding semantic outcome's two-sided midpoint is the primary reference.
Normally Polymarket YES represents SIG YES. The explicit `SIG_NO` orientation is
also supported by selecting Polymarket NO as the corresponding SIG YES outcome.
`fair_no = 1 - fair_yes`. This complement applies within one binary contract;
it does not imply a relationship between different party markets.

```text
buy_yes_edge = external_fair_yes - tick_rounded_sig_buy_yes
buy_no_edge  = external_fair_no  - tick_rounded_sig_buy_no
```

The table reports edges in probability units: `0.0300` means 3 percentage points,
not 3% return on capital. Absolute probability difference uses the SIG midpoint,
while the signal uses the executable SIG buy limit. Rows sort by positive external
edge; large negative gaps do not outrank positive opportunities.

`BUY_SIG_YES` / `BUY_SIG_NO` are research labels only. They require:

- An exact `APPROVED_RESEARCH_ONLY` mapping with unchanged bound rules and no mismatch flag.
- Both instruments open and Polymarket accepting orders.
- SIG's existing authoritative book freshness (5 seconds).
- Polymarket book timestamp no older than 60 seconds, and response received within
  5 seconds of that row's comparison. Up to 5 seconds of future-clock skew is
  tolerated. This separate research policy does not relax any live freshness gate.
- At least one share at the SIG executable top price.
- At least 50 shares on **both** top sides of the corresponding external reference
  book; aggregate liquidity/volume alone cannot prove an executable reference.
- Decimal edge at least `MIN_EXTERNAL_EDGE = 0.03`.

Missing bid/ask uses a labelled one-sided quote or Gamma headline as a low-confidence
reference. It cannot produce a signal, since two-sided reference depth is missing.
Missing/stale timestamps, crossed/malformed books, API failures and mapping/rule
mismatches block the affected comparison. `HIGH` describes complete fresh book
data, not proof of fair probability. Sequential rows retain their own observation
and venue timestamps; they are not a simultaneously executable cross-venue portfolio.

The public client paces reads and makes at most three GET attempts for timeout,
connection failure, 429 or transient 5xx responses. It respects short Retry-After
waits and stops the affected read for waits above five seconds. SIG keeps its
existing pacing/rate-limit handling. Errors print fixed reasons, never credentials.

## Optional history

No existing DuckDB collector or dependency is present. Rather than add one,
the scanner offers standard-library SQLite research history:

```bash
.venv/bin/python external_value_scanner.py --snapshots
.venv/bin/python external_value_scanner.py --json --snapshots
```

This explicitly opts in to atomic, deduplicated inserts in
`external_research/comparisons.sqlite3` (gitignored). Default runs persist nothing.
The `comparisons` table includes timestamps, instrument IDs, side, mapping version,
SIG buy/sell, external bid/ask/midpoint, edge, depths, signal and the full JSON row
with condition/token IDs, both books, rule hashes and quality flags. Decimals are
stored as exact strings, avoiding binary-float rounding in research history.
No account balances, holdings, authorization keys or execution state are captured.

```sql
SELECT observed_at, race, side, sig_buy, external_midpoint, external_edge, signal
FROM comparisons
ORDER BY observed_at, id;
```

Collect repeated observations before measuring lag, convergence or forward returns.
Blocked/unmatched rows remain in history so missing data is not silently removed.

## Collector, shadow tracker and report

```bash
# Continuous GET-only research collector; Ctrl+C stops cleanly.
.venv/bin/python external_value_scanner.py --snapshots --loop

# One collection cycle (also creates/evaluates shadows).
.venv/bin/python external_value_scanner.py --snapshots

# Offline evaluator: no API calls, credentials or account reads.
.venv/bin/python external_shadow_tracker.py

# Offline report: opens the research database strictly read-only.
.venv/bin/python external_shadow_report.py
.venv/bin/python external_shadow_report.py --json
```

The loop targets one scan every 60 seconds, with no overlapping cycles. A slow
cycle finishes before the next starts. API/storage failures skip a cycle and
never create a pilot halt. The collector uses no pilot lock or submission adapter.
It writes only the gitignored research database. No autonomous process is started.

The existing `comparisons` audit history is retained. An idempotent migration
adds `observations`, `shadow_trades` and `shadow_horizons`. Observation IDs bind
the UTC timestamp, semantic side and exact mapping/evidence fingerprint. Repeating
the identical snapshot inserts nothing; conflicting data for that identity fails
the research transaction rather than overwriting history. Legacy paired YES/NO
rows supply one another's historical sell depth; missing depth remains unknown.
New rows explicitly retain bid, ask and sell depth, venue market status, both buy
edges, signal strength, threshold flags and the complete original quote metadata.
Prices/P&L/returns are Decimal calculations and exact decimal strings in SQLite.

A fresh, liquid, valid `BUY_SIG_YES`/`BUY_SIG_NO` signal with edge **>= 0.03**
creates a one-share `OPEN_SHADOW`. The entry price is the tick-rounded SIG BUY
quote, not the midpoint. `threshold_met` is inclusive; a separate
`edge_exceeded_threshold` field records strict `>` for analysis. The midpoint is
the entry convergence reference; a missing midpoint uses a labelled entry-BUY
fallback, never an invented midpoint.

Repeated signals for the same exact mapping/side create no new shadow unless
the executable entry price or external fair value changes by at least **0.005**
from the last created shadow. Timestamp, depth-only and metadata changes do not
create duplicates. This research deduplication rule also applies after expiry;
it does not change any paper/live entry or sizing threshold. Independent shadows
may overlap when materially new signals occur; they consume no actual capital.

## Forward returns and independent exit rules

Each shadow has 5/15/30/60-minute counterfactual exits. Choose the **first valid
observation at/after** the target, no later than target + 120 seconds. Do not
choose the most profitable quote in that window or interpolate a missing price.
A valid executable exit needs an unchanged mapping, open SIG market, fresh SIG
book at that historical observation and at least one share at the SELL price.
`PENDING` means the window has not ended; `MISSING_HORIZON_DATA` means it ended
without an eligible observation. Neither contributes a zero return to averages.
Later historical backfills can supply actual missing observations.

```text
executable_pnl = observed SIG SELL for the held side - executable entry BUY
executable_return = executable_pnl / entry BUY
mark_pnl = SIG side midpoint - entry BUY
initial_external_gap = entry external fair - entry SIG reference
frozen_fair_convergence = current SIG reference - entry SIG reference
contemporaneous_convergence = initial gap - (current external fair - current SIG reference)
gap_closed_fraction = convergence / initial gap
```

The same formulas apply to YES and NO using their own executable prices.
Remaining executable external edge uses current external fair minus current
SIG BUY. Frozen-entry and contemporaneous fair gaps/convergence/widening flags
are stored separately. Percentages may exceed 100% or be negative. If the
external reference has become stale/unavailable, SIG-only executable returns
can still be evaluated; current external metrics stay null. Missing SIG midpoint
leaves mark/convergence measurements null. Freshness is assessed at observation
time, not against the wall clock when offline evaluation runs.

The independent early-exit simulation closes at the first executable P&L
**>= 0.01 SUSQie per share** (`CLOSED_SHADOW_TARGET`), without touching the
fixed-horizon counterfactuals. First positive P&L is strictly `> 0`. Store both
first-profit and first-target times. MFE is the largest nonnegative executable
P&L; MAE is the most negative, including the entry spread when executable.
The overall trajectory covers up to 60 minutes + 120 seconds; each horizon's
trajectory uses observations only up to that horizon's matched quote, avoiding
future-data leakage. Without a target hit, the shadow expires after that window
as `EXPIRED_SHADOW_NO_TARGET`; no sale price or realized P&L is fabricated.

## Report and lead/lag interpretation

Group fixed-horizon results by edge buckets 3%–<5%, 5%–<8%, >=8%, and separately
by race, semantic side and exact mapping fingerprint. Each group shows total,
evaluated and profitable counts; win rate among evaluated samples; mean/median
executable ROI; mean mark ROI; median frozen-fair gap convergence; mean observed
first-profit time; worst adverse excursion; and separate missing/pending counts.
ROI is a fraction of entry capital, while edge/convergence/P&L are price points.
Missing mark/profit-time data are excluded, not assigned zero. First-profit
averages are conditional on actually observing a profitable quote.

Lead/lag uses each mapping's YES rows once (NO would duplicate the complement).
An external move is a nonzero fair-value change between adjacent valid quotes
at most 120 seconds apart. At 1/5/15/30-minute lags, measure SIG midpoint change
from the external event to the first fresh SIG quote at/after the lag, within
another 120 seconds. Report sample/missing counts, Pearson correlation, direction
hit rate and mean subsequent SIG move. Flat SIG changes are not directional
hits. Correlation is unavailable with fewer than two observations or zero
variance. Sparse multi-hour gaps are not treated as short-lag events.

These are descriptive, possibly overlapping research samples, with no claim of
statistical significance. Timestamp span alone does not establish horizon
coverage: the report counts actual matching observations. Keep collecting before
drawing conclusions about prediction or convergence.

## Initial GET-only audit: October 11, 2026, 11:58 UTC

The implemented scanner captured 12 rows in the separate research database. All
four configured external contracts passed ID/root/rule revalidation. No pilot
state, live authorization, threshold, `.env` or execution journal changed.

| SIG proposition | BUY YES / NO | External fair YES / NO | Edge YES / NO | Research result |
| --- | --- | --- | --- | --- |
| Colorado Democratic, 256/945 | 0.930 / 0.080 | 0.976 / 0.024 | +0.046 / -0.056 | BUY_SIG_YES |
| Colorado Republican, 257/946 | 0.115 / 0.895 | 0.015 / 0.985 | -0.100 / +0.090 | Blocked: external top depth 30 < 50 |
| Alaska Democratic, 377/1066 | 0.720 / 0.285 | Unmatched | Unavailable | NO_EQUIVALENT_EXTERNAL_MARKET |
| Alaska Republican, 378/1067 | 0.295 / 0.710 | Unmatched | Unavailable | NO_EQUIVALENT_EXTERNAL_MARKET |
| New Hampshire Democratic, 381/1070 | 0.875 / 0.130 | 0.895 / 0.105 | +0.020 / -0.025 | Below 0.030 |
| New Hampshire Republican, 382/1071 | 0.140 / 0.865 | 0.105 / 0.895 | -0.035 / +0.030 | BUY_SIG_NO |

These are dated observations, not standing trade instructions. For example, the
Colorado Democratic headline was 0.980, but its executable external YES book was
0.960 bid / 0.992 ask, yielding the 0.976 midpoint actually used. The default
command fetches new quotes; `--snapshots` appends another set of observations.

## Official API references

- [Discover markets (public data)](https://docs.polymarket.com/market-data/discover-markets)
- [Market-by-ID schema](https://docs.polymarket.com/api-reference/markets/get-market-by-id)
- [Prices and order books](https://docs.polymarket.com/market-data/prices-order-books)
- [Colorado official event](https://polymarket.com/event/colorado-senate-election-winner)
- [Alaska official event](https://polymarket.com/event/alaska-senate-election-winner)
- [New Hampshire official event](https://polymarket.com/event/new-hampshire-senate-election-winner)

Focused tests:

```bash
.venv/bin/python -m unittest test_polymarket_client test_external_value_scanner test_external_shadow_tracker test_external_shadow_report -q
```
