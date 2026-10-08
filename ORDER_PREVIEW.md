# Read-only order previews

`order_preview.py` checks a proposed two-leg purchase against the competition's
market data and your current account. If every check passes, it prints a JSON
draft in the documented multi-leg order format. **It cannot submit orders.**
There is no live-trading flag, and the tool makes only HTTP GET requests.

It does not use or change `paper_portfolio.json` or `paper_trades.csv`. The paper
scanner and the offline execution simulator remain separate tools.

## Run a preview

From the repository directory:

```bash
.venv/bin/python order_preview.py --dem-market 387 --rep-market 388 --position NO-PAIR
```

These example IDs identify the Democratic and Republican Rhode Island Senate
markets. Their presence does not guarantee that a preview will pass the current
eligibility or account checks.

To request a smaller quantity explicitly:

```bash
.venv/bin/python order_preview.py --dem-market 387 --rep-market 388 --position NO-PAIR --quantity 10
```

`--quantity` accepts whole numbers from 1 to 100. Without it, the tool chooses up
to 100 pairs, using the whole shares available at both relevant best prices.
Both books must still show at least 50 available shares, even when you request
fewer pairs. An oversized or unaffordable proposal is blocked rather than
bypassing the checks.

`--position` can be `YES-PAIR` or `NO-PAIR`. The default tournament is
`midterm-elections`; `--tournament` selects a different competition slug, subject
to the same scope and eligibility checks. There is no override for an unverified
pair.

## Analyze a conditional one-pair proposal

To inspect a numerical proposal while settlement evidence remains unresolved:

```bash
.venv/bin/python order_preview.py --dem-market 387 --rep-market 388 --conditional-proposal
```

This option fixes the quantity at **one NO share per leg**. An explicit quantity
other than one, or a YES pair, is rejected before reading credentials or the
account. It still checks actual account cash, holdings, open orders, capital
limits, scoped matching election rules, fresh books, depth of at least 50 and a
tick-rounded price gap of at least 0.02.

A missing relationship permits numerical analysis only. Malformed evidence,
failed reads or mismatched rules still stop the tool. The output labels any
missing relationship and shows conditional ordinary payouts, both-party-YES
losses, refund assumptions and the loss if only one leg fills. Fees are not
modeled. It does not produce an order body, execution key, approval fingerprint
or journal, even if relationship evidence is present. **Execution is never
approved by this option.** The default preview and default paired preparation
retain their existing settlement gate. A separately accepted conditional paired
preparation is described in [the execution guide](PAIRED_ACCOUNT_TEST.md); this
analysis cannot prepare or submit it.

## How the draft is checked

The tool resolves the tournament slug to its UUID and uses that explicit scope
on market, order-book, relationship, and account reads. It verifies the supplied
market IDs and their single binary exchanges, then checks matching party titles,
structured election rules, and an active canonical relationship. Titles help
identify candidates; the stored race and stage identifiers and relationship
evidence determine whether a pair is eligible.

A NO pair requires mutual exclusivity. A YES pair additionally requires an
exhaustive relationship containing exactly those two outcomes. Freeform,
composite, mismatched, or unsupported contracts are blocked. If the competition's
relationship graph is empty, neither pair type receives approval and no order
draft is produced.

For YES purchases, the proposed price comes from the best YES ask. For NO
purchases, it is the complement of the best YES bid. Each purchase limit is
rounded **up** to the next permitted `0.005` price increment; rounding up can
increase the proposed cost. The tool recalculates the pair's edge after rounding
and requires at least `0.02`, or 2% of the assumed one-unit ordinary payout.

The proposal retains the configured limits of 250 SUSQies per trade and 150
SUSQies of exposure per race. It uses the competition account's current cash,
holdings, and fully read open-order list, rather than the local paper balance.

The current exposure check deliberately uses an account-wide upper bound:

```text
pending reserve = sum of every open order's remaining shares × 1 SUSQie
cash available to the proposal = reported cash − pending reserve
exposure upper bound = nonzero holdings' cost basis + pending reserve
```

The reserve includes sell orders and orders with an unknown limit price. This is
the preview's conservative calculation; the platform itself does not reserve
cash for resting orders. The proposal must fit the available cash and keep the
exposure upper bound plus its cost within 150 SUSQies. Consequently, holdings or
orders in other races can block a preview. The tool does not present this bound
as a measured exposure for the selected race.

Any existing holding or open order on either selected exchange also blocks the
proposal, regardless of side. This avoids drafting an order that could duplicate
an existing position, net against an opposite position, or interact with your
resting orders.

Account and market responses are separate observations, not one atomic snapshot.
The entire read must remain within a 15-second age limit, and book snapshots must
be within five seconds. Failed reads, incomplete pagination, unexpected scopes,
or stale data stop the preview safely.

## What the JSON means

A successful draft contains one `idempotencyKey` and two `legs`. Each leg includes
the exchange ID, `action: "buy"`, lowercase `side: "yes"` or `"no"`, an integer
quantity, a side-relative limit price, and the explicit tournament UUID.

The generated `preview-…` key is temporary. It is not stored as a durable
execution intent and does not implement restart recovery or safe live retries.
No credential or authorization header belongs in the printed payload.

The optional API field `relationshipConstraint` is omitted intentionally. It
checks supplied prices in YES terms, rather than merely checking settlement
membership. For example, NO limits of `0.40` and `0.45` correspond to YES prices
of `0.60` and `0.55`; their sum exceeds the documented non-exhaustive exclusivity
bound of one. Automatically adding the relationship ID could therefore reject
the very price discrepancy the scanner found.

The documented API has no participant endpoint for validating hypothetical
orders without placing them. This draft is checked locally for schema and risk
compatibility; it is **not server-approved**, an execution guarantee, or proof of
risk-free profit. Fees and cancellation/refund outcomes are not modeled.
Atomic multi-leg placement also does not guarantee equal or complete fills.
Supervised submission and recovery are implemented separately in the
[paired account test](PAIRED_ACCOUNT_TEST.md); this preview cannot invoke them.

## Verification

Run the focused tests with:

```bash
.venv/bin/python -m unittest test_order_preview test_conditional_proposal -v
```

The tests use fake API responses and temporary files; they cover price rounding,
NO complements, real-account risk calculations, fresh observations, scope,
settlement gates, protected files, and GET-only behavior.
Conditional proposal tests also cover explicit unapproved output, absence of
executable material, single-leg/refund losses and rejection of larger quantities.

All 242 repository tests passed on 8 October 2026, including the strict preview,
conditional analysis and paired-handler checks. A live GET-only conditional
proposal also completed: matching structured rules, account checks and books
passed, with one-share NO limits of 0.125 and 0.845. The ordinary-payout
relationship remained missing. It produced analysis only and no execution
material. Quotes were observed at about 00:01:22 UTC; they require refreshing
before any later decision. Private account values were not saved in this guide.

A separate live read-only check of the example Rhode Island pair completed the
account read and blocked the preview because no active relationship verified
the pair. It produced no approved order draft and submitted or cancelled no
orders. Private account values are displayed locally rather than saved here.

## Official references

- [API reference](https://sig.thesuper.market/api/v1/docs): request schemas,
  tournament scopes, prices, idempotency, account reads, and multi-leg placement.
- [Markets & Trading](https://sig.thesuper.market/docs/markets-and-trading):
  YES/NO complements, matching liquidity, and position netting.
- [Settlement & Payouts](https://sig.thesuper.market/docs/settlement-and-payouts):
  ordinary payouts and cancellation/refund treatment.
- [Official competition rules](https://predictionscup.com/rules/): market-specific
  resolution rules and the competition framework.
