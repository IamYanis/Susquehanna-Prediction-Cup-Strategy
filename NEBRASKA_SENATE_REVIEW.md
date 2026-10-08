# Nebraska Senate research review

Reviewed **7 October 2026** after the first complete research report. This records
public contract/book observations and public ballot evidence. It contains no
private account data and changes no trade approval or risk limit.

## The highest-ranked observed gap

| Field | Observation |
| --- | --- |
| Position | Buy Democratic YES and Republican YES |
| Market IDs | 279 and 281 |
| Exchange IDs | 968 and 970 |
| YES asks / rounded buy limits | 0.035 and 0.730 SUSQies |
| Combined cost per pair | 0.765 SUSQies |
| Apparent gap relative to one unit | 0.235 SUSQies, not a verified profit |
| Visible YES shares | Democratic 57; Republican 6,974 |
| Available pairs at these prices | 57 |
| Democratic engine book timestamp | 2026-10-07T22:48:34.0683956+00:00 |
| Republican engine book timestamp | 2026-10-07T22:48:34.8404528+00:00 |

These were fresh observations when calculated. They are a historical snapshot
by the time the full report and this review finish, not current execution quotes.
The local ignored `research_report.csv` contains the other observed pairs.

The scoped resolution-tree reads matched `raceId=62971`, `stageId=98101`,
election date `2026-11-03`, General stage and Party Winner resolution. The two
`winnerName` values identify Democratic Party and Republican Party respectively.
Matching identities establish the same race, not coverage of all winning parties.
The scoped relationship graph provided no active payout evidence.
[Democratic market](https://sig.thesuper.market/markets/279),
[Republican market](https://sig.thesuper.market/markets/281).

## Official ballot evidence

The Secretary of State's final general-election candidate list dated
11 September 2026 lists these five Senate candidates on its first page:

| Candidate | Listed party or filing designation |
| --- | --- |
| Pete Ricketts | Republican |
| Mike Marvin | Legal Marijuana NOW |
| Robin Richards | Nebraska Working People |
| Chuck Conboy | America First |
| Dan Osborn | By Petition |

There is no Democratic-labelled Senate candidate in that list. The ballot offers
winning candidates outside the two party labels selected by this pair.
[Official candidate list, page 1](https://sos.nebraska.gov/sites/default/files/doc/elections/2026/Final_Statewide_General_Candidate_Filing_List_9.11.26.pdf),
[Secretary of State election page](https://sos.nebraska.gov/elections).

## Why the price gap does not establish arbitrage

The site's published Party Winner template resolves according to the winning
candidate's affiliation with the specified party. Its fusion-ticket treatment
can also count a winner for multiple parties. This was inspected in the public
client component retrieved on the review date; it is a shared rule template,
not a separate administrator certificate for these two markets.
[Published Info component](https://sig.thesuper.market/_next/static/immutable/chunks/26irha8qmnq1i.js).

The following is a **conditional inference** from that rule and ordinary binary
payouts. If the winner is treated as affiliated with neither selected party,
both YES shares lose. That is a possible failure of the assumed one-unit paired
payout; for example, it would occur if a by-petition winner were resolved as
neither Democratic nor Republican.

| Resolver's winning-party classification | D YES + R YES pays | Payout minus observed 0.765 cost |
| --- | ---: | ---: |
| Democratic only | 1 | +0.235 |
| Republican only | 1 | +0.235 |
| Neither Democratic nor Republican | 0 | -0.765 |
| Both, if recognized under a fusion treatment | 2 | +1.235 |

The official ballot list does not establish the platform's complete affiliation
mapping, but it prevents assuming a two-party ballot. **Exhaustiveness is not
proved; this YES pair remains unverified and is not approved.** A 23.5% price
gap relative to an assumed payout is therefore not a guaranteed return.

## Refunds and the other pair type

The official settlement guide pays one unit for a winning share and zero for a
losing share, while cancelled/N/A positions refund refundable cost. A refund is
therefore a different outcome from the ordinary binary cases above. Administrative
settlement can also override automatic settlement with a documented reason.
Fees and refund coordination were not established by this research.
[Official settlement guide](https://sig.thesuper.market/docs/settlement-and-payouts).

This review does not approve the Nebraska NO pair. It would need proof that the
two party YES outcomes cannot both occur under the accepted resolver mapping,
plus fresh prices, depth and the usual account/execution checks. No orders were
submitted or cancelled during this report or review.
