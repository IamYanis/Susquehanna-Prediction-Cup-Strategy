# Rhode Island Senate settlement review

Initial review: **7 October 2026**; focused refresh: **8 October 2026** (London).
This is a public-source review of Democratic
market **387** and Republican market **388**. It contains no private account
information and does not authorize orders. **Neither pair is automatically
approved by the current scanner.**

## What identifies the two markets

The public pages' embedded contract data identifies `Election Outcome` contracts
for `U.S. Senate Rhode Island`, with the following shared fields:

| Field | Value |
|---|---|
| `raceId` | `62978` |
| `stageId` | `98108` |
| `electionDate` | `2026-11-03` |
| `raceStage` | `General` |
| `resolutionType` | `Party Winner` |
| Scheduled settlement | `2026-11-04T17:00:00.000Z` |

`winnerName` is `Democratic Party` for market 387 and `Republican Party` for
market 388. Their exchange IDs are 1076 and 1077. This identifies the same
race and stage; it does not prove either payout floor.
[Democratic market](https://sig.thesuper.market/markets/387),
[Republican market](https://sig.thesuper.market/markets/388).

## Published Party Winner rule

The market Info component is loaded separately from the initial page. Its public
script provides the applicable `Party Winner` template: YES follows the winning
candidate's affiliation with the specified party; otherwise it resolves NO.
It explicitly says:

> A candidate running on a fusion ticket counts for every party on that ticket

The template explains that multiple party markets can consequently resolve YES
for one race. Therefore **one winning candidate does not by itself prove that
two party YES outcomes are mutually exclusive**.

The same template bases election resolution on official results from relevant
election authorities and final certification. Recounts or legal challenges may
delay resolution. It does not identify a particular provider or feed URL.
[Published Info component](https://sig.thesuper.market/_next/static/immutable/chunks/26irha8qmnq1i.js).

This text was inspected in the site's public client script, not observed through
a signed-in Info tab. It is a shared template selected by the contract's
`Election Outcome` and `Party Winner` fields; it is not a separate administrator
certificate of exclusivity for these two markets.

## Official ballot evidence

The Rhode Island Department of State's candidate list shows these Senate
candidates marked as on the election ballot:

| Candidate | Official listed party |
|---|---|
| John Francis Reed | Democrat |
| Raymond T McKay | Republican |
| Michael Anthony Bahry | Independent |

The page was current to 6–7 October 2026 in the retrieved responses.
[Official Senate candidate list](https://vote.sos.ri.gov/Candidates/CandidateSearchSummary?Election=18144&OfficeType=620).

The Department's 2026 declaration form gives the general election date as
3 November 2026 and separately permits unaffiliated candidacy. Its declaration
guidance says a candidate cannot file for the same office under different party
labels. These are useful evidence of distinct ballot affiliations; they do not
specify how the platform's resolver handles every possible affiliation change,
write-in, substitute candidate, tie, or administrative override.
[2026 declaration form](https://vote.sos.ri.gov/Forms/elections/Forms/DecOfCand26.pdf),
[Official declaration guidance](https://vote.sos.ri.gov/Candidates/DeclarationOfCadidacy).

## Payoff reasoning and its limits

The table assumes ordinary binary resolution and the affiliations shown in
each row. The fusion row illustrates the published template's behavior; it
does not assert that the current Rhode Island ballot contains such a ticket.
These conditional payoffs are not promises about exceptional settlements.

| Winning candidate's applicable party | D YES / R YES | YES pair pays | NO pair pays |
|---|---|---:|---:|
| Democrat only | YES / NO | 1 | 1 |
| Republican only | NO / YES | 1 | 1 |
| Independent, neither D nor R | NO / NO | 0 | 2 |
| Both D and R, if a fusion affiliation were recognized | YES / YES | 2 | 0 |

**YES pair:** the actual independent candidate defeats an assumption that the
ballot contains only the two reviewed parties. Under the published affiliation
rule, an independent winner with neither affiliation would pay this pair zero.
Exhaustiveness is not established and this pair should remain withheld.

**NO pair:** the current distinct candidate labels support a conditional payout
of at least 1 if the final winner cannot count for both D and R. The empty
engine graph does not disprove that logic. However, the published fusion rule
makes the platform's exact affiliation mapping material. This review does not
establish exclusivity for every accepted result, so it does not replace the
scanner's requirement for an active relationship or approve account orders.

## Exceptional outcomes and unresolved accounting

The official guide states that winning shares pay 1 SUSQie, losing shares pay 0,
and cancelled/N/A held positions instead refund refundable cost. An automatic
market can be force-settled by an administrator with a documented reason;
runoff races await an official result and administrative settlement. No source
reviewed promises that these two markets must refund together or explains a
Rhode Island tie-specific platform result.
[Settlement guide](https://sig.thesuper.market/docs/settlement-and-payouts).

Both legs fully refunding would remove the ordinary modeled profit. One leg
refunding while the other loses could lose the other leg's purchase cost.
The scheduled settlement date is therefore not proof of a timely binary payout.

The competition uses non-monetary SUSQies, and its rules require no real-world
fee or financial obligation. That statement alone does not establish all
possible virtual-unit fee accounting. No verified race-specific fee schedule
was found in this review.
[Official competition rules, section 3](https://predictionscup.com/rules/).

## Decision and evidence needed next

The prior read-only API audit returned an empty tournament-scoped relationship
graph. The current scanner withholds both positions; this review changes no
eligibility gate or historical paper holding.

Before approving a NO pair through a reviewed-rule path, obtain official
confirmation of how the resolver maps the complete certified winner and party
affiliations for `raceId=62978` / `stageId=98108`, including fusion, write-ins,
substitutions, and any tie treatment. Confirm that D and R cannot both resolve
YES for an accepted ordinary result. Retain a dated source and require review
again if the rules or identifiers change.

Separately establish refund coordination, relevant fee accounting, and the
behavior of administrative overrides. Even verified ordinary exclusivity would
support only **profit conditional on ordinary settlement** until these cases
are accounted for. Rules and resolution can change under the competition's
published terms. Execution, account reconciliation, and order authorization
remain separate requirements.

## Focused refresh: 8 October 2026

The scoped books were refreshed at approximately **00:10 BST** on 8 October
(23:10 UTC on 7 October). These are dated observations, not current executable
quotes. The research script validated both snapshots as fresh when calculating
the prices.

| NO-pair field | Refreshed observation |
| --- | --- |
| Democratic NO buy / rounded limit | 0.125 SUSQies |
| Republican NO buy / rounded limit | 0.845 SUSQies |
| Combined limit cost | 0.970 SUSQies |
| Apparent gap relative to one-unit payout | 0.030 SUSQies per pair |
| Democratic-side visible shares | 11,674 |
| Republican-side visible shares | 3,874 |
| Pair depth | 3,874 |
| Research quantity cap | 100 pairs; not account-approved |
| Democratic engine timestamp | 2026-10-07T23:10:21.7749527+00:00 |
| Republican engine timestamp | 2026-10-07T23:10:22.5299131+00:00 |

This passes the research price/depth policies. Both structured rule identities
still match race 62978 / stage 98108 / General / Party Winner / 3 November 2026.
There is still no active scoped relationship verifying the normal payout.
The local ignored `research_pair_report.csv` records both pair types; the YES
pair cost 1.040 and did not meet the price policy.

### What the public rules support

The official candidate list, retrieved again, still shows Reed as Democrat,
McKay as Republican and Bahry as Independent. Its displayed data timestamp was
6 October; retrieving it on 8 October does not make the underlying list newer.
The Department's declaration guidance prohibits filings for the same office
under different party labels. The party-affiliation statute additionally
restricts membership in a different party before the declaration; the separate
party/independent nomination statute prohibits filing both forms of candidacy.
[Candidate list](https://vote.sos.ri.gov/Candidates/CandidateSearchSummary?Election=18144&OfficeType=620),
[Declaration guidance](https://vote.sos.ri.gov/Candidates/DeclarationOfCadidacy),
[R.I. Gen. Laws 17-14-1.1](https://webserver.rilegislature.gov/Statutes/TITLE17/17-14/17-14-1.1.htm),
[R.I. Gen. Laws 17-14-2.1](https://webserver.rilegislature.gov/Statutes/TITLE17/17-14/17-14-2.1.htm).

The published Party Winner component was downloaded again without credentials.
Its affiliation and fusion wording was unchanged from the previous retrieval.
The listed ballot does not show a Democratic/Republican fusion candidate;
the generic fusion clause is not evidence that this particular ballot contains
one. It remains important to distinguish that ballot observation from a complete
statement of the platform's resolver mapping.

**Conditional inference:** if the platform uses these official party labels and
settles both contracts as ordinary binary outcomes, each listed winner produces
the following NO-pair result. An independent winner benefits this NO pair.

| Listed winner / assumed resolver classification | NO-pair payout | Payout minus 0.970 cost |
| --- | ---: | ---: |
| Reed / Democratic only | 1 | +0.030 |
| McKay / Republican only | 1 | +0.030 |
| Bahry / neither selected party | 2 | +1.030 |

This supports a conditional ordinary-settlement price gap. It does not establish
the resolver's behavior for every accepted exceptional result or change the
scanner's engine-evidence gate. No independently confirmed dual-party result was
found for the current listed candidates.

### Refund cases at the observed cost

Assuming each refunded leg returns its full acquisition cost, these are simple
payoff scenarios derived from the official refund description. They are not
predictions that the platform will refund a particular leg or refund them
independently.

| Settlement scenario | Pair payout/refund | Result after acquisition cost |
| --- | ---: | ---: |
| Both legs refund | 0.970 | 0 |
| Democratic NO refunds; Republican NO loses | 0.125 | -0.845 |
| Republican NO refunds; Democratic NO loses | 0.845 | -0.125 |
| Both party markets resolve YES | 0 | -0.970 |

The final row illustrates a failure of ordinary exclusivity, not an observed
ballot outcome. The published guide does not promise coordinated refund or
override behavior for these two separate contracts.
[Settlement guide](https://sig.thesuper.market/docs/settlement-and-payouts).

**Decision:** price and depth checks pass, and public ballot evidence supports
the stated ordinary-payoff inference. Automatic settlement approval remains
unverified. No approved order body was produced, no account risk or cash was
substituted with paper values, and no orders were submitted or cancelled. A
two-leg account preview still requires settlement evidence accepted by the
existing checker, followed by fresh account/book and risk-limit checks.

## One-pair conditional proposal, 8 October 2026

The new read-only `order_preview.py --conditional-proposal` option completed
scoped market, rule, account and book reads. At approximately 00:01:22 UTC
(01:01:22 London time), one Democratic NO share had a 0.125 limit and depth
2,857; one Republican NO share had a 0.845 limit and depth 150. The combined
contract-cost cap was 0.970 SUSQies. Actual account cash, overlap and conservative
exposure checks passed; private account values are deliberately omitted here.

Under the ordinary settlement assumptions above, the conditional gain remains
0.030 when exactly one selected party wins. If only the Democratic NO share
fills and loses, its loss is 0.125; if only the Republican NO fills and loses,
its loss is 0.845. The two-share contract-cost loss can reach 0.970 if both NO
shares lose. These figures omit unverified fees and do not guarantee fills.

The relationship evidence was still missing. The tool produced no order body,
execution key, approval fingerprint or journal, and placed or cancelled no
orders. The strict paired handler remains blocked. This dated numerical proposal
does not authorize execution or relax the existing settlement policy.
