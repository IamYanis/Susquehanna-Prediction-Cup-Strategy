# Prediction Cup paper scanner

Run from this directory using the existing virtual environment:

```bash
.venv/bin/python price_reader.py
```

Set `SIG_API_KEY` in your local `.env` (never commit it). The scanner targets
15 seconds between scan starts and stops cleanly with Ctrl+C. Requests use
10-second timeouts, one SIG API origin, explicit Midterm Elections tournament
IDs, and complete cursor pagination. Reads are spaced at least 0.75 seconds
apart (about 80/minute), below the documented ordinary-account allowance of
100/minute. Other processes and keys share that account allowance. A 429 response
starts a cooldown using `Retry-After`, defaulting to 60 seconds when invalid.
Redirects are refused so the key stays on the SIG origin.

Use `--once` for one paper scan. For a read-only API diagnostic that never loads,
updates, or logs paper positions:

```bash
.venv/bin/python price_reader.py --audit-only
```

Both scanner modes submit GET requests only. Competition account orders require
the separate supervised account test tools described below.
`--tournament` selects another competition slug, but it must resolve to an active
SUSQies tournament and pass the same checks.

To investigate prices even when settlement evidence cannot approve a pair:

```bash
.venv/bin/python research_report.py
```

This separate GET-only tool ranks apparent price gaps and saves all YES/NO-pair
rows to the ignored local `research_report.csv`. It shows each leg's buy price,
rounded limit cost, visible depth, quote timestamps, and available/missing
settlement evidence. It also inspects the strongest observed pair's structured
rule identity. Every row has `execution_approved=False`; neither this ranking nor
matching rule identity authorizes trading. See [research report guide](RESEARCH_REPORT.md)
for the columns, snapshot limitations, and remaining manual checks.

To refresh only the Rhode Island Senate pair, keeping the full report:

```bash
.venv/bin/python research_report.py --dem-market 387 --rep-market 388
```

This writes the separately ignored `research_pair_report.csv`. The focused
report uses the same freshness and settlement checks and cannot approve orders.

To inspect your competition account's reported cash, holdings, and open orders:

```bash
.venv/bin/python account_reader.py
```

Add `--details` to print each holding and order, or `--tournament` to select a
competition. This separate checker makes GET requests only and never loads or
updates the paper portfolio, writes an account snapshot, or submits/cancels
orders. Your competition account and local paper simulation remain independent.
Account values are displayed locally rather than recorded in repository files.

The checker follows all open-order pages with explicit tournament scope and
rejects incomplete reads. A network error, unavailable valuation, pending
enrollment, or invalid response reports an unknown account state instead of
zero holdings or zero orders. Signed position quantities are displayed as YES
or NO shares; open-order quantities are the remaining shares.

Resting orders do not reserve cash on the platform. A displayed cash balance
therefore does not account for all possible future fills. These sequential
reads do not provide an atomic account snapshot or establish execution readiness.

A separate read-only order preview checks one selected pair against actual
account cash, holdings, open orders, current settlement evidence, and books:

```bash
.venv/bin/python order_preview.py --dem-market 387 --rep-market 388 --position NO-PAIR --quantity 10
```

It can display a local two-leg request body, but has no submission or cancellation
path. Buy limits round upward to the API's 0.005 tick, then the edge and capital
checks run again. All resting order remainders receive a conservative one-unit
reserve per share, and all holding costs count toward an upper bound on race
exposure. Either selected exchange having a holding or order blocks the draft.
The example market IDs do not imply an approved trade; missing relationship
evidence blocks the preview. See [order preview guide](ORDER_PREVIEW.md) for the
account checks, schema details, and limits of this read-only tool.

For a numerical one-pair NO proposal while settlement evidence remains unresolved:

```bash
.venv/bin/python order_preview.py --dem-market 387 --rep-market 388 --conditional-proposal
```

This GET-only option checks the current account and books and shows conditional
gains, refund risks and losses if only one leg fills. It creates no order body or
execution intent and does not approve a trade. The strict settlement gate on
default order preparation and submission remains in place.

For a supervised one-share test on a competition account, the separate
`account_test.py` tool can first prepare and save a limit order using GETs:

```bash
.venv/bin/python account_test.py prepare --market 387 --side yes
```

The example chooses a directional YES purchase; it is not an approved arbitrage
or a recommendation to buy that market. Preparation saves an ignored local
`account_test.json` with its exact body and approval fingerprint. It does not
submit the order. Submission and specific-order cancellation each require a
separate command with that fingerprint; the scanner never calls this tool.
A filled share can remain in the account after cancellation of any remainder.
See [supervised account test guide](ACCOUNT_TEST.md) before any account write.

The separate `paired_account_test.py` adds a supervised one-pair NO workflow:

```bash
.venv/bin/python paired_account_test.py prepare --dem-market 387 --rep-market 388
```

Preparation is GET-only and requires the existing engine settlement evidence;
that is the default policy. For the separately accepted Rhode Island test,
`prepare --dem-market 387 --rep-market 388 --conditional` selects an explicit
conditional policy, limited to those contracts and a combined cost cap of 0.970
SUSQies. It records the missing evidence and accepted settlement/partial-fill
risks rather than claiming verified arbitrage. Both policies retain account and
liquidity checks. Successful preparation saves two one-share
limit orders and an exact pair fingerprint in the ignored `paired_account_test/`
directory. Submission is a separate opt-in command. The handler confirms a full
first fill before attempting the second, stops on uncertain replies, and records
unmatched shares after a failed leg. It can cancel known unfilled remainders but
cannot undo fills. See [paired execution guide](PAIRED_ACCOUNT_TEST.md).

The authorized test on 8 October 2026 left its first order `UNKNOWN`; the second
was not submitted. No successful pair is confirmed. Preserve the ignored local
journals and resolve the original attempt before any further submission. The
read-only diagnostic checks the current account and recent scoped trade audit:

```bash
.venv/bin/python paired_account_test.py diagnose
```

It leaves execution journals unchanged. Empty reads do not release an uncertain
attempt for replay. See the execution guide for the recorded checks and limits.

The scanner fetches relationships once per scan and skips rule/book requests
when evidence cannot approve a pair. Large eligible universes can still make a
full scan take minutes under the request budget; 15 seconds is a target, not a
guarantee. Scans never overlap.

Paper cash and open positions are saved to `paper_portfolio.json` beside the
scripts after every accepted trade. Restarting loads that file, so existing
positions continue to block duplicates and count toward risk limits. Run one
scanner process at a time for this portfolio. Opportunity observations start
fresh on each run; the first scan may report an existing opportunity as
`APPEARED`, but its saved position will be skipped.

Only a missing portfolio file starts a fresh 5,000 paper SUSQies balance. This
local simulation balance is independent of the competition account. An invalid or
unreadable file stops the scanner and leaves the file untouched. Saves write a
temporary file, flush it to disk, and then atomically replace the portfolio file.
If saving fails, the new trade is not accepted and scanning stops. Portfolio files
and temporary files are ignored by Git; they contain no API credentials.

An opportunity disappearing does not close a position or return its capital.
The 250 per-trade and 150 per-race limits and available-cash check still apply,
in paper SUSQies. Recorded market/exchange IDs also prevent duplicate positions
and preserve race exposure when a market title changes.
To deliberately start a fresh paper portfolio, stop the scanner and move
`paper_portfolio.json` to a backup outside the repository before restarting.

Accepted paper trades are logged in `paper_trades.csv` beside the scripts. You
can open it in a spreadsheet to review the trade ID, UTC timestamp, race,
YES/NO-pair type, quantity, cost per pair, capital used, minimum expected profit,
and paper cash remaining after that trade. The historical
`minimum_expected_profit` column name remains for compatibility; its value is
a projection under normal settlement, excluding fees. Rejected trades and
duplicate attempts do not add rows. Projected profit is not realized cash.

Each new position saves its original timestamp, trade ID, and remaining cash in
the portfolio. If the CSV write fails, the accepted position stays saved and the
scanner stops. Startup retries any missing rows without duplicating existing IDs.
CSV updates also use a temporary file and atomic replacement. An invalid header,
incomplete row, or mismatch with a saved trade stops scanning and leaves the log
untouched. The log is ignored by Git and contains no API credentials.

Older portfolios load normally. Positions created before logging was added have
no recorded timestamp and are left out of the CSV. The CSV retains history if you
start a fresh paper portfolio; move it to a backup too if you want a fresh log.

Events require at least 2% edge and 50 available pairs; quantity is capped at 100.
A material change is at least one percentage point of edge or 10 executable
pairs relative to the last report. Unchanged opportunities remain quiet. Failed
requests retain prior observations until a successful scan confirms a change.
Rejected trades are reconsidered only when an event appears or changes materially.

Titles only discover candidates. Approval requires one binary exchange per
market, matching structured General Election Party Winner rules (race, stage,
date, party and settlement date), and an active engine relationship linking
the exact market/exchange IDs. Mutual exclusivity permits NO-pair analysis;
YES-pair analysis additionally requires an exhaustive relationship containing
exactly those two members. Freeform chamber markets and unresolved rules are
withheld. Missing evidence retains prior observations rather than announcing
that an opportunity disappeared.

Books are requested from the SIG competition API with explicit tournament and
exchange IDs. Bid-only books can support NO analysis; ask-only books can support
YES analysis. Nonempty or empty books must have an authoritative engine snapshot
timestamp within five seconds of the local clock. Both legs are checked again
when evaluating the pair. Indicative prices are never used as executable depth.
Five seconds is our conservative policy, so slow reads or clock differences can
cause observations to be withheld.

New paper positions record tournament, market and exchange IDs, race identity,
settlement evidence and fingerprint, leg prices, and book versions in the JSON
portfolio. Saved context is validated on restore. Older positions keep their
original costs and cash and are identified as historical simulations without
recorded API provenance. No position is automatically closed or settled.

All projected profits depend on ordinary binary settlement. Cancellation/N/A
refunds can erase the gain or produce a different result. Fees and partial fills
are not modeled by the scanner's immediate paper fills; it still assumes the
observed quantities are available.

A separate offline simulator now exercises two-leg order handling:

```bash
.venv/bin/python execution_simulator.py --scenario all
```

It uses invented prices and a fake venue to show partial fills, failed legs,
fills during cancellation, missing replies, and restart recovery. Default runs
use disposable temporary files. An unfinished or unmatched pair blocks new
pairs, and cancellation never returns the cost of shares already bought.
It reuses the configured capital limits and charges each observed fill once.
The simulator has no network or credential access and does not use the scanner's
paper portfolio or CSV. It is not connected to competition order endpoints.
See [execution exercises](EXECUTION_SIMULATOR.md) for persistent-state and resume
commands, accounting rules, and the limits of this model.

The 7 October 2026 audit found 117 title pairs but an empty scoped relationship
graph, so none passed automatic approval. This does not prove arbitrage is
impossible; it means the scanner lacks the required payout evidence. Read the
[API and settlement audit](API_SETTLEMENT_AUDIT.md) for sources, findings, and
remaining work before competition account execution.
The [Rhode Island Senate review](RHODE_ISLAND_SENATE_REVIEW.md) documents the
independent candidate and the platform's fusion-ticket rule. Neither party-pair
type is enabled by that manual review.

Offline tests:

```bash
.venv/bin/python -m unittest discover -v
```

Tests use fake API responses and clocks, temporary portfolios, and a restart
across two Python processes. They cover pagination, venue identity, settlement
eligibility, snapshot freshness, network/rate failures, event transitions, risk
limits, provenance, portfolio/CSV recovery, simulated execution/recovery, local
order drafting, supervised account-test control with fake HTTP writes, and
read-only research/account/audit modes. They do not call the API or touch your saved
portfolio or `.env`.
