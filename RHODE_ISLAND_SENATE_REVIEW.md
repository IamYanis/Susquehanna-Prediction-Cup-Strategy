# Rhode Island Senate settlement review

Review date: **7 October 2026**. This is a public-source review of Democratic
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
