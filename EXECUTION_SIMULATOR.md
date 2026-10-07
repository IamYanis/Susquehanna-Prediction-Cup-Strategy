# Offline execution simulator

This simulator teaches what happens when an intended two-leg paper trade does
not fill exactly as planned. It uses a local fake venue and Python's standard
library. It does not load `.env`, contact an API, submit competition orders, or
read or update `paper_portfolio.json` or `paper_trades.csv`. It is separate from
the scanner and its saved holdings.

## Run the scenarios

From the repository directory:

```bash
.venv/bin/python execution_simulator.py --scenario all
```

The default run uses disposable temporary journals. To run one example:

```bash
.venv/bin/python execution_simulator.py --scenario partial
```

Available scenarios exercise these situations:

| Scenario | What to learn |
|---|---|
| `complete` | Both legs fill their intended quantity. |
| `partial` | A 60/40 partial fill is exposed before later fills complete both legs. |
| `second-leg-failure` | The first leg can fill even when submitting the other fails. |
| `cancellation-race` | More shares can fill while cancellation is being processed. |
| `lost-submit-ack` | A lost acknowledgement does not prove an order was rejected. |
| `cancellation-timeout` | An uncertain cancellation must be checked before proceeding. |
| `restart` | A saved intent can be reconciled with venue state after restarting. |
| `venue-unavailable` | Missing authoritative state prevents safely opening another pair. |

To keep one run's journals for inspection, choose a new directory:

```bash
.venv/bin/python execution_simulator.py --scenario lost-submit-ack --state-dir execution_simulation
```

The simulator refuses an existing root directory, including an empty one,
instead of overwriting a previous run. Each scenario has its own subdirectory:
the example saves `execution_simulation/lost-submit-ack/execution_state.json`
and `execution_simulation/lost-submit-ack/simulated_venue.json`. With
`--scenario all`, the new root contains all eight scenario directories.

To load one existing scenario, pass that scenario's directory:

```bash
.venv/bin/python execution_simulator.py --resume execution_simulation/lost-submit-ack
```

Resume reconciles saved controller intent against the saved fake venue, then
continues only known intent. A completed example does not add duplicate orders.
Resume does not replay the scenario's scripted future fills: a remaining saved
submission may create an unfilled fake order. It does not discover opportunities
or open a new scanner trade. Missing or corrupt journal files stop the run
instead of creating fresh state. `--resume` and `--state-dir` cannot be combined.
Use one process per scenario directory.

A state-saving failure stops the CLI. If calling these functions directly,
discard the in-memory controller and venue objects after `SimulationError`;
load both saved files and reconcile before another operation. A failed save
does not prove that an earlier fake venue action did not happen.

## Two files with different responsibilities

`execution_state.json` records what the controller intends to do, its stable
client keys, and the cumulative fills and cash it has confirmed.
`simulated_venue.json` records the fake venue's authoritative orders, fills,
and cash. Keeping these records separate lets an example lose a response while
the venue has already accepted an order.

The controller saves intent **before** asking the fake venue to accept an order.
When it recovers, it looks up that stable client key and learns whether the order
already exists. It does not create a new key merely because the original response
was lost. The fake venue deduplicates that known submission; this is simulated
behavior, not verification of an external API's idempotency contract.

Reconciliation checks what actually happened. The fake venue supplies its entire
authoritative history; a real eventually consistent API may not prove order
absence in the same way. Unavailable venue state, unknown orders, or a mismatch
must prevent another pair from starting. A saved command or an exception alone
cannot establish that nothing filled.

## Read the states

Leg states record steps such as `planned`, `submitting`, `open`, `partial`,
`unknown`, `cancel_pending`, `filled`, `cancelled`, `rejected`, and `skipped`.
The pair summary combines them:

| Pair state | Meaning |
|---|---|
| `PREPARED` | Both leg intents have been saved. |
| `WORKING` | An order or remaining quantity is still active. |
| `UNKNOWN` | Submission or cancellation has an uncertain result. |
| `CANCELLING` | Abort intent is saved and cancellation remains unfinished. |
| `COMPLETE` | Both legs filled the target quantity. |
| `PARTIAL_PAIR` | Both legs have equal positive fills and all remainder is terminal. |
| `UNHEDGED` | Both legs are terminal but their filled quantities differ. |
| `ABORTED` | Both legs are terminal with no filled shares. |

Only a reconciled `COMPLETE` or `PARTIAL_PAIR` prints a synthetic projected
ordinary profit. `PARTIAL_PAIR` can still hold shares after cancelling remainder;
it is not a sale or realized profit. Other states keep the unfinished or
unmatched exposure visible without claiming completed-pair profit.

## Read the result as holdings and cash

Submitting an order is different from filling it. A partial fill spends cash
and creates held shares even if its remaining quantity is cancelled later.
**Cancellation removes unfilled remainder; it does not undo held shares.**

For example, 100 shares filled on leg A and 40 on leg B contain 40 equal pairs
and 60 additional leg-A shares. A positive edge calculated for 100 intended
pairs does not describe the outcome of that unequal holding. The report keeps
the two leg quantities visible so that this exposure is not mistaken for a
completed pair.

Cash must reflect each confirmed fill once, using its actual filled quantity
and price. Repeated reconciliation or a restart must not charge the same fill
twice. Open order remainder may still spend cash later, so intended and resting
orders must count when deciding whether another trade fits the risk limits.

The simulation keeps the project's 5,000-unit starting cash, 250-unit trade
limit, 150-unit total race limit, and maximum 100 shares per leg. Quantities are
integers. These are local paper settings, not a mirror of a competition account.

## Offline verification

Run the focused execution tests with:

```bash
.venv/bin/python -m unittest test_execution_simulator -v
```

All 25 execution tests and the complete 114-test suite passed on 7 October 2026.
They include a restart in a separate Python process, save failures after fake
order acceptance, invalid history, and an offline CLI run that blocks network
access and credential/paper-file reads. Persistent CLI runs were also checked:
resuming a completed pair kept exactly two orders; existing run directories and
missing state were refused. The existing paper portfolio and CSV stayed unchanged.

## Limits of the exercise

The scenario uses synthetic contracts with a stated ordinary binary pair payout
assumption. That assumption does not approve any real Democratic/Republican
race: independent winners, fusion affiliations, administrative overrides, and
refunds require their own reviewed settlement rules. Any displayed ordinary
payout estimate is conditional on those synthetic assumptions; it is not
realized cash or proof of an unconditional profit.

Fake prices, acknowledgements, fills, and failure timing are deterministic
learning inputs. Passing these examples does not establish actual API latency,
liquidity, guaranteed simultaneous fills, or a fee/refund policy. There is no
real execution adapter, scanner integration, or competition account order mode.

Before implementing account execution, verify its actual response and recovery
semantics, reconcile balances and orders, validate settlement and accounting,
and obtain explicit authorization for the proposed account order behavior.
