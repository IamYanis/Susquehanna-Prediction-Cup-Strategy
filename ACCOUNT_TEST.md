# Supervised one-share competition test

`account_test.py` prepares a single limit **BUY of one YES or NO share** in the
competition account. A submitted order can fill and leave you holding that share.
This is a directional test of order mechanics, separate from the paper scanner
and the offline execution simulator. It does not implement an arbitrage strategy,
automated trading, or automatic liquidation.

Preparation has been authorized. The market, side, and exact account order still
require the user's concrete approval before submission. Creating a journal or
printing its fingerprint does not itself approve an account order.

## Prepare and review

From the repository directory, replace `MARKET_ID` and choose `yes` or `no`:

```bash
.venv/bin/python account_test.py prepare --market MARKET_ID --side yes
```

Preparation makes GET requests for the chosen market, competition account,
holdings, open orders, and order book. It saves a local journal and prints the
fixed request, spend cap, expiry, and full approval fingerprint. **It sends no
order.** The default tournament is `midterm-elections`.

Review the saved proposal without any API calls or credential access:

```bash
.venv/bin/python account_test.py show
```

The request always specifies one share, `action: "buy"`, the selected side, an
explicit tournament UUID, and a limit price. Prices are rounded up to the next
`0.005` increment within `0.005`–`0.995`. A NO purchase uses the complement of the
best YES bid; a YES purchase uses the best YES ask. The order expires 15 minutes
after preparation. Its contract spend cap is the printed limit price; fees have
not been verified.

Preparation retains the scanner's requirement for at least 50 visible shares
on the selected book side. The risk check uses actual competition cash and
account exposure, with a conservative one-SUSQie reserve for every open order's
remaining share, including sell orders and unknown prices. Nonzero holdings'
cost basis plus that reserve provides an account-wide upper bound for the
150-SUSQie race limit. The existing 250-SUSQie trade limit also applies. Any
holding or open order on the selected exchange blocks the test.

The journal defaults to `account_test.json` beside the script and is ignored by
Git. `--state PATH` selects another journal; use that same path for every later
action. Preparation refuses to overwrite an existing file, including a finished
test. Subsequent actions update that journal atomically. The journal contains the
request and observations, not the API credential. Treat custom journal paths as
local account data and exclude them from Git too.

Running the script without an action produces a usage error. Importing it does
not prepare or submit an order.

## Submit an approved proposal

Only after reviewing and obtaining approval for the exact order, replace
`FULL_FINGERPRINT` with the complete printed fingerprint:

```bash
.venv/bin/python account_test.py submit --approve FULL_FINGERPRINT
```

The fingerprint binds the market, tournament slug, and immutable request. The
tool checks fresh account and book data, available cash, overlap, limits, and
expiry again. It refuses a current quote above the approved limit. It persists
the submission intent before making **one POST** for this invocation.

There is no automatic retry. An uncertain reply stops the operation and leaves
the saved state unresolved. A successful receipt can report a filled share, a
resting order, or a no-op in which no order was placed. Inspect the receipt and
subsequent order history rather than assuming that an HTTP success means a fill.

## Check execution

Once the journal contains a confirmed order ID:

```bash
.venv/bin/python account_test.py check
```

This uses GET requests for that specific order and every page of its fill
history. It validates the saved exchange, competition, side, limit, and expiry,
then records cumulative fills and the remaining order state. Reporting may lag;
conflicting or incomplete observations stop the check without declaring a
successful fill or cancellation.

## If a placement receipt is lost

If submission stopped in `SUBMITTING` or `UNKNOWN`, preserve the journal. Do not
prepare another test, delete the file, change the body, or generate another key.

The API documents that an identical request with the same idempotency key can
replay its stored response. Its published reference does not specify how long
that record is retained. The documented 90-second `REQUEST_IN_FLIGHT` interval
is an execution lease, not a retention guarantee.

**This tool does not implement receipt replay.** There is no `recover` action,
and a previously attempted submission cannot be submitted again. It neither
automatically nor explicitly repeats an uncertain POST. Preserve the body and
key while inspecting the account and reconciling the outcome manually before any
further account writes.

Preparation and submission use the default `account_test.json` journal. An
alternate `--state` path is accepted only for inspection, checks and cancellation
of an existing journal. Submission also refuses to run while the default paired
test directory exists. A per-journal `.operation.lock` serializes submissions;
the saved journal must still match the caller's snapshot while that lock is held.
After a process crash, preserve any remaining lock and confirm the process has
stopped before manual recovery. Never remove an unresolved journal to bypass it.

Journal saves flush both the JSON file and its parent directory metadata.
Failure of either flush stops execution. A failed receipt save may leave the old
intent or the new receipt on disk; discard in-memory state and restore the
journal before reconciliation. Neither saved attempted state permits replay.

GET order lists contain no stable client key with which to recover this receipt.
Matching a nearby order by price or time does not prove that it is the saved
request, or that a missing match never executed. Without a confirmed order ID,
the tool cannot check or cancel a guessed order.

New uncertain attempts save only a fixed failure category, the HTTP status (if
received) and an allowlisted documented API error code. They do not store raw
responses, exception messages, headers or credentials. This aids diagnosis while
retaining the same no-replay policy. Older journals still load; their discarded
error responses cannot be reconstructed. The paired tool's GET-only `diagnose`
command also checks recent scoped trade activity without changing the journals.

## Cancel only the remaining order

Cancellation also requires the full fingerprint:

```bash
.venv/bin/python account_test.py cancel --approve FULL_FINGERPRINT
```

The tool first reads and validates the specific confirmed order. If it is open,
it saves the cancellation intent and issues one DELETE for that returned order
ID. It cannot cancel all account orders. If the order is already closed, it
checks the fill history without issuing DELETE.

A `409` cancellation response means the order is already closed; fills determine
whether it filled or was cancelled. A timeout or unconfirmed `503` leaves
cancellation unknown. Preserve the journal and check that specific order. Late
fills can occur before cancellation completes. **Cancellation removes only the
unfilled remainder; it does not sell, refund, or erase an already filled share.**

## Preserve unresolved state

After a persistence failure or interrupted operation, discard the in-memory
state and restore the saved journal before taking another action. If the journal
is missing, corrupted, or invalid, the tool stops instead of silently resetting.
If a placement receipt was lost, the original body and key may be needed to
recover it safely. Do not discard an unresolved journal.

`paper_portfolio.json` and `paper_trades.csv` are not used by this workflow. The
paper scanner remains paper-only. This opt-in tool introduces account writes
only through the explicit `submit` and `cancel` actions.

## Official references

- [API reference](https://sig.thesuper.market/api/v1/docs): single-order requests,
  idempotency, order details, fill pagination, cancellation, and uncertain errors.
- [Markets & Trading](https://sig.thesuper.market/docs/markets-and-trading):
  limit orders, YES/NO prices, matching liquidity, and position netting.
- [Settlement & Payouts](https://sig.thesuper.market/docs/settlement-and-payouts):
  held-share payouts and cancellation/refund rules.
- [Changelog](https://sig.thesuper.market/docs/changelog): retryable
  `TX_CONFLICT` responses and API data caching.
