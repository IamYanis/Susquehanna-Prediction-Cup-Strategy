# Prediction Cup paper scanner

Run from this directory using the existing virtual environment:

```bash
.venv/bin/python price_reader.py
```

Set `SIG_API_KEY` in your local `.env` (never commit it). The scanner targets
15 seconds between scan starts, makes requests sequentially with 10-second
timeouts, and stops cleanly with Ctrl+C. Slow scans can take longer than 15 seconds.
Use `--once` for one scan. No real orders are submitted.

Paper cash and open positions are saved to `paper_portfolio.json` beside the
scripts after every accepted trade. Restarting loads that file, so existing
positions continue to block duplicates and count toward risk limits. Run one
scanner process at a time for this portfolio. Opportunity observations start
fresh on each run; the first scan may report an existing opportunity as
`APPEARED`, but its saved position will be skipped.

Only a missing portfolio file starts a fresh $5,000 balance. An invalid or
unreadable file stops the scanner and leaves the file untouched. Saves write a
temporary file, flush it to disk, and then atomically replace the portfolio file.
If saving fails, the new trade is not accepted and scanning stops. Portfolio files
and temporary files are ignored by Git; they contain no API credentials.

An opportunity disappearing does not close a position or return its capital.
The $250 per-trade and $150 per-race limits and available-cash check still apply.
To deliberately start a fresh paper portfolio, stop the scanner and move
`paper_portfolio.json` to a backup outside the repository before restarting.

Events require at least 2% edge and 50 available pairs; quantity is capped at 100.
A material change is at least one percentage point of edge or 10 executable
pairs relative to the last report. Unchanged opportunities remain quiet. Failed
requests retain prior observations until a successful scan confirms a change.
Rejected trades are reconsidered only when an event appears or changes materially.

The original payout assumptions remain: YES pairs assume one of the two parties
wins; NO pairs assume they cannot both win. These assumptions must fit each race's
settlement rules. Expected profits exclude fees and are not realized cash.

Offline tests:

```bash
.venv/bin/python -m unittest -v test_scanner.py
```

Tests use temporary portfolios and fake trades, including a restart across two
Python processes. They do not call the API or touch your saved portfolio or `.env`.
