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

Both modes submit GET requests only. No competition account orders are implemented.
`--tournament` selects another competition slug, but it must resolve to an active
SUSQies tournament and pass the same checks.

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
are not modeled; paper fills still assume the observed quantities are available.

The 7 October 2026 audit found 117 title pairs but an empty scoped relationship
graph, so none passed automatic approval. This does not prove arbitrage is
impossible; it means the scanner lacks the required payout evidence. Read the
[API and settlement audit](API_SETTLEMENT_AUDIT.md) for sources, findings, and
remaining work before competition account execution.

Offline tests:

```bash
.venv/bin/python -m unittest discover -v
```

Tests use fake API responses and clocks, temporary portfolios, and a restart
across two Python processes. They cover pagination, venue identity, settlement
eligibility, snapshot freshness, network/rate failures, event transitions, risk
limits, provenance, portfolio/CSV recovery, and read-only audit mode. They do not
call the API or touch your saved portfolio or `.env`.
