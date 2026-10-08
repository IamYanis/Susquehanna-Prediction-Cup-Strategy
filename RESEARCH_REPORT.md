# Read-only pair research

Run from the repository directory:

```bash
.venv/bin/python research_report.py
```

For a focused refresh of a known Democratic/Republican pair, provide both IDs:

```bash
.venv/bin/python research_report.py --dem-market 387 --rep-market 388
```

The IDs must match the same discovered race, in Democratic/Republican order.
The focused report still reads the complete scoped market list, then reads books
only for that pair. Its default output is `research_pair_report.csv`, separately
ignored along with its temporary files, so it preserves the full research CSV.
Both IDs are required together; reversed, identical or unmatched IDs fail closed.

The script reads the complete competition market list, pairs Democratic and
Republican titles, reads the relationship graph, and fetches both order books
even when payout evidence is missing. All API calls are GETs to the SIG host
with explicit tournament scope, refused redirects and the scanner's request
pacing. A full scan can take several minutes. Progress prints every ten races.

It saves two rows per race, YES-PAIR and NO-PAIR, in `research_report.csv` beside
the script. This file and its temporary files are ignored by Git. `--top 20`
prints more rows; the CSV always contains the full report. `--output PATH.csv`
changes the destination; exclude custom report paths from Git yourself. Existing
paper files are protected and the script never loads or updates the portfolio.
Missing credentials, incomplete discovery, a rate limit or interruption prevents
a completed report from replacing an older CSV. Individual unavailable books
remain explicit unavailable rows; they do not become zero-cost opportunities.

## Read the numbers

| Column | Meaning |
| --- | --- |
| `dem_buy_price`, `rep_buy_price` | YES uses the best YES ask; NO uses one minus the best YES bid. |
| `raw_combined_cost` | Sum of the observed buy prices for one share of each leg. |
| `dem_limit`, `rep_limit`, `combined_limit_cost` | Buy prices rounded upward to the API's 0.005 tick, then summed. Unsupported market-order prices have no limit-cost estimate. |
| `apparent_price_gap` | One minus the combined rounded cost. This assumes a one-unit minimum paired payout that may **not** be proved. It is not expected or guaranteed profit. |
| `dem_visible_shares`, `rep_visible_shares`, `available_pairs` | Top-of-book depth for each required side and the smaller depth. |
| `research_quantity` | Whole pairs visible at those prices, capped at 100. This is not an account-risk-approved trade size. |
| `economic_classification` | Apparent gap below 0.5%: IGNORE; 0.5% to below 1%: WATCH; 1% to below 2%: PAPER TRADE; at least 2%: STRONG PAPER TRADE. This price label does not verify settlement or approve an entry. |
| `meets_price_and_depth_thresholds` | Apparent gap at least 0.01 and at least 50 visible pairs. Threshold-sized gaps rank first, then other observed gaps; unavailable rows come last. |
| `dem_book_at`, `rep_book_at`, `observed_at`, `quote_status` | Quote timestamps and whether a fresh pair observation could be calculated. An available leg is retained when its counterpart side is missing. |
| `settlement_status`, `settlement_note` | The scanner's existing relationship/rule checks, or a fixed reason that evidence is missing or unavailable. |
| `rules_review` | Rule identity inspection for the strongest observed race; other unreviewed rows say `NOT_INSPECTED`. |
| `execution_approved` | Always false. Research has no submission, cancellation or paper-trade path. |

Both books must be fresh within five seconds when their combined prices are
calculated. Each row has its own timestamps. Earlier rows age while later races
are read, so the full CSV is not one simultaneously executable snapshot.
Fresh books and account checks would be required again before any order preview.

## Read the evidence

An active mutual-exclusivity relationship and matching structured election
rules can prove a NO pair's minimum payout at ordinary binary settlement.
A YES pair additionally needs an exhaustive relationship containing exactly the
two selected markets. The report reuses those existing checks and never treats
matching titles as payout evidence.

`VERIFIED_NORMAL_SETTLEMENT` describes only the supported pair type's normal
payout evidence. It does not establish fees, refund outcomes, account cash,
capital limits, simultaneous fills, or execution readiness.

The strongest observed race receives an additional structured rule inspection.
Matching race/stage/date/party identity does not prove that Democrats and
Republicans cover all winners or cannot both resolve YES. Manual research still
needs to address:

- Third-party and independent winners, which can leave both party YES shares
  paying zero.
- Fusion-ticket treatment, which can allow multiple party labels to resolve YES.
- Cancellation/N/A refunds, which can change the paired payoff.
- Fees and execution costs, which can consume an apparent price gap.

An absent relationship remains `UNVERIFIED` even if structured identities match.
No override, order body, API write, or account authorization is created by this
report. The paper scanner and all existing risk limits retain their behavior.

The first full report on 7 October 2026 observed 234 pair-type rows across
117 races. Twenty-six met the price/depth policies; none had normal-payout
evidence. The highest-ranked Nebraska Senate YES pair had a 0.765 combined
cost and 57 visible pairs. The [Nebraska review](NEBRASKA_SENATE_REVIEW.md)
documents why the official ballot and Party Winner rule do not establish the
assumed one-unit minimum payout. These are dated observations, not current quotes.

## Sources

- [Official trading guide](https://sig.thesuper.market/docs/markets-and-trading):
  YES/NO pricing, opposite-side trades, and matching liquidity.
- [Official settlement guide](https://sig.thesuper.market/docs/settlement-and-payouts):
  binary payouts, cancelled/N/A refunds, and administrative settlement.
- [Existing Rhode Island rule review](RHODE_ISLAND_SENATE_REVIEW.md): a worked
  example of an independent winner and the published fusion-ticket rule. This
  race-specific review does not approve other races or either Rhode Island pair.
