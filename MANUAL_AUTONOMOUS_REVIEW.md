# Manual autonomous candidate review

Official GET observations: 2026-10-09T13:21:04+00:00 to 2026-10-09T13:22:26+00:00.

This is an evidence review, not an authorization. No pair is approved. Submission remains disabled. The quotes were fresh when each row was evaluated; observations are sequential and must be refreshed before any future trade.

All twelve pairs are open, non-quarantined individual-race Election Outcome contracts. Within each pair, official raceId, stageId, General raceStage, electionDate, Party Winner resolutionType and settlement date match. The two winnerName fields are Democratic Party and Republican Party. These are official structured identifiers, not evidence inferred from titles. They make the pairs candidates for human review; they do not establish a machine relationship.

| Pair | Market IDs | Exchange IDs | Potential ordinary edge | NO executable depth D / R | Race / stage IDs | Review status |
|---|---|---|---:|---:|---|---|
| Oklahoma Senate | 383/286 | 1072/975 | 2.0% | 12368 / 88 | 62976 / 98106 | Candidate for explicit human review |
| New Hampshire Senate | 381/382 | 1070/1071 | 3.0% | 4393 / 2598 | 62972 / 98102 | Candidate for explicit human review |
| Alaska Senate | 377/378 | 1066/1067 | 2.0% | 5591 / 19610 | 62954 / 98084 | Candidate for explicit human review |
| NH-01 House race | 375/376 | 1064/1065 | 2.0% | 6792 / 15765 | 64482 / 99791 | Candidate for explicit human review |
| New Hampshire Governor | 372/373 | 1061/1062 | 3.0% | 239505 / 1450 | 76772 / 118975 | Candidate for explicit human review |
| Wyoming Senate | 365/366 | 1054/1055 | 2.0% | 16372 / 1534 | 62985 / 98115 | Candidate for explicit human review |
| Kansas Senate | 356/357 | 1045/1046 | 2.0% | 30288 / 7036 | 62962 / 98092 | Candidate for explicit human review |
| Delaware Senate | 353/386 | 1042/1075 | 3.0% | 3033 / 3130 | 62957 / 98087 | Candidate for explicit human review |
| Vermont Governor | 312/313 | 1001/1002 | 1.5% | 7251 / 367 | 76866 / 119069 | Review possible; PRICE NOT READY (<2%) |
| Virginia Senate | 295/364 | 984/1053 | 2.0% | 9277 / 1864 | 62983 / 98113 | Candidate for explicit human review |
| Colorado Senate | 256/257 | 945/946 | 2.5% | 97435 / 79028 | 62956 / 98086 | Candidate for explicit human review |
| FL-09 House race | 211/322 | 900/1011 | 2.0% | 7653 / 5248 | 64336 / 99645 | Candidate for explicit human review |

## Evidence and assumptions requiring human approval

For EACH specific pair, the human must review both complete official roots and the current settlement policy, then explicitly approve this proposition for its exact race/stage: under the platform's ordinary binary Party Winner interpretation, the two configured party-winner outcomes cannot both be YES, so one NO contract in each market pays at least 1 SUSQie. This is a human interpretation; it is not proven by a relationship graph.

The returned fields do not define treatment of a winner with multiple party affiliations/endorsements, a tied or contested result, runoff handling, or changes/corrections to party classification. Those ambiguities make unconditional automatic approval unsuitable. A reviewer must resolve them or explicitly justify the narrow ordinary-settlement interpretation; a matching raceId alone is insufficient.

The official policy allows administrator overrides and different timing for unresolved/runoff results. Cancelled/N/A outcomes refund refundable held cost instead of guaranteeing the ordinary payout floor. Both markets can be processed independently. These exceptions and one-sided execution must be accepted explicitly. The manual tier cannot eliminate them.

[Official settlement policy](https://sig.thesuper.market/docs/settlement-and-payouts). Observed policy HTML SHA-256: `ae4d6d3c25fefb3a232de629883071a60be818624be05d81467b888838e285f7`. The new tier pins the full HTML bytes; cosmetic/deployment changes also require revalidation.

An evidence fingerprint for a FUTURE approval must also bind that human's exact rationale, limitations, approval timestamp/version, stable IDs and ordered sources. The root fingerprints below are diagnostic evidence fingerprints only; they are not approval hashes or permissions.

[Scoped tournament identity](https://sig.thesuper.market/api/v1/tournaments/midterm-elections): `bda92870-621e-47b0-bc3c-3602c5c26f55`.

## Oklahoma Senate

Markets 383/286; exchanges 1072/975. Review scope: race `62976`, stage `98106`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. Senate Oklahoma` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.895 / 0.085; pair notional 0.980; potential ordinary edge 0.020; depth 12368 / 88; observed `2026-10-09T13:21:20+00:00`.

Complete-root SHA-256: `7aaef9e4640d55d177bc37e4c73fa3b0172f6a85865d1e8e45ab38586ce6b350`.

- [Market 383](https://sig.thesuper.market/api/v1/markets/383?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/383/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 286](https://sig.thesuper.market/api/v1/markets/286?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/286/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## New Hampshire Senate

Markets 381/382; exchanges 1070/1071. Review scope: race `62972`, stage `98102`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. Senate New Hampshire` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.135 / 0.835; pair notional 0.970; potential ordinary edge 0.030; depth 4393 / 2598; observed `2026-10-09T13:21:26+00:00`.

Complete-root SHA-256: `338a53964725cbbdc6a5f844a96d46723342e50b381991e6868830618c27b660`.

- [Market 381](https://sig.thesuper.market/api/v1/markets/381?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/381/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 382](https://sig.thesuper.market/api/v1/markets/382?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/382/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## Alaska Senate

Markets 377/378; exchanges 1066/1067. Review scope: race `62954`, stage `98084`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. Senate Alaska` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.295 / 0.685; pair notional 0.980; potential ordinary edge 0.020; depth 5591 / 19610; observed `2026-10-09T13:21:32+00:00`.

Complete-root SHA-256: `a5fc102cece05b92eca681eea0ca4a992dbc142a76da44a687fa0e388d8476d6`.

- [Market 377](https://sig.thesuper.market/api/v1/markets/377?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/377/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 378](https://sig.thesuper.market/api/v1/markets/378?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/378/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## NH-01 House race

Markets 375/376; exchanges 1064/1065. Review scope: race `64482`, stage `99791`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. House New Hampshire District 1` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.075 / 0.905; pair notional 0.980; potential ordinary edge 0.020; depth 6792 / 15765; observed `2026-10-09T13:21:38+00:00`.

Complete-root SHA-256: `5089637f81bcea29b663ded9bdfb72de7e9ae2344ab9b62a68d261b6c90e3aff`.

- [Market 375](https://sig.thesuper.market/api/v1/markets/375?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/375/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 376](https://sig.thesuper.market/api/v1/markets/376?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/376/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## New Hampshire Governor

Markets 372/373; exchanges 1061/1062. Review scope: race `76772`, stage `118975`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `Governor of New Hampshire` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.865 / 0.105; pair notional 0.970; potential ordinary edge 0.030; depth 239505 / 1450; observed `2026-10-09T13:21:44+00:00`.

Complete-root SHA-256: `0eba82494b659309e97302d75dd56bffed485039b9a3aa5829e77e621ec46316`.

- [Market 372](https://sig.thesuper.market/api/v1/markets/372?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/372/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 373](https://sig.thesuper.market/api/v1/markets/373?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/373/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## Wyoming Senate

Markets 365/366; exchanges 1054/1055. Review scope: race `62985`, stage `98115`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. Senate Wyoming` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.935 / 0.045; pair notional 0.980; potential ordinary edge 0.020; depth 16372 / 1534; observed `2026-10-09T13:21:50+00:00`.

Complete-root SHA-256: `aa27e86fdb50578e64f7b99821db4619183e98beacc525fff23da36ef2bb86ac`.

- [Market 365](https://sig.thesuper.market/api/v1/markets/365?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/365/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 366](https://sig.thesuper.market/api/v1/markets/366?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/366/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## Kansas Senate

Markets 356/357; exchanges 1045/1046. Review scope: race `62962`, stage `98092`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. Senate Kansas` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.575 / 0.405; pair notional 0.980; potential ordinary edge 0.020; depth 30288 / 7036; observed `2026-10-09T13:21:56+00:00`.

Complete-root SHA-256: `f49a2f0213deafa266a451f4559d3d5531318d99a5158569f4a537896418d30c`.

- [Market 356](https://sig.thesuper.market/api/v1/markets/356?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/356/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 357](https://sig.thesuper.market/api/v1/markets/357?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/357/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## Delaware Senate

Markets 353/386; exchanges 1042/1075. Review scope: race `62957`, stage `98087`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. Senate Delaware` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.075 / 0.895; pair notional 0.970; potential ordinary edge 0.030; depth 3033 / 3130; observed `2026-10-09T13:22:02+00:00`.

Complete-root SHA-256: `32c67182e0122044049a963cb79b3580af68b712336e8709b76b0735ceab2ffb`.

- [Market 353](https://sig.thesuper.market/api/v1/markets/353?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/353/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 386](https://sig.thesuper.market/api/v1/markets/386?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/386/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## Vermont Governor

Markets 312/313; exchanges 1001/1002. Review scope: race `76866`, stage `119069`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `Governor of Vermont` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.705 / 0.280; pair notional 0.985; potential ordinary edge 0.015; depth 7251 / 367; observed `2026-10-09T13:22:08+00:00`.

Complete-root SHA-256: `93fe1671122b979d45bbc24a6addea1b2cd24e5530138d12ed97b02701f010ec`.

- [Market 312](https://sig.thesuper.market/api/v1/markets/312?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/312/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 313](https://sig.thesuper.market/api/v1/markets/313?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/313/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## Virginia Senate

Markets 295/364; exchanges 984/1053. Review scope: race `62983`, stage `98113`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. Senate Virginia` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.060 / 0.920; pair notional 0.98; potential ordinary edge 0.020; depth 9277 / 1864; observed `2026-10-09T13:22:14+00:00`.

Complete-root SHA-256: `c518b3d6ab9284bb1c8a57f89bcfd192864944a0a7ce7099620c9d4b0beab208`.

- [Market 295](https://sig.thesuper.market/api/v1/markets/295?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/295/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 364](https://sig.thesuper.market/api/v1/markets/364?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/364/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## Colorado Senate

Markets 256/257; exchanges 945/946. Review scope: race `62956`, stage `98086`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. Senate Colorado` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.085 / 0.890; pair notional 0.975; potential ordinary edge 0.025; depth 97435 / 79028; observed `2026-10-09T13:22:20+00:00`.

Complete-root SHA-256: `fd7685e54adac09a3e2a864393765086cdf14bf3fa00f793bfcdf8982500c287`.

- [Market 256](https://sig.thesuper.market/api/v1/markets/256?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/256/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 257](https://sig.thesuper.market/api/v1/markets/257?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/257/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.

## FL-09 House race

Markets 211/322; exchanges 900/1011. Review scope: race `64336`, stage `99645`, `General`, election `2026-11-03`, resolution `Party Winner`. Both configured settlement dates: `2026-11-04T17:00:00.000Z`.

Pair-specific rationale to review: the Democratic Party and Republican Party propositions for this exact `U.S. House Florida District 9` race/stage must be mutually exclusive under the same ordinary winner-party classification. Dual affiliation, ties/runoffs and independent administrator rulings remain unresolved in the returned wording; this review does not grant permission.

Observed NO limits 0.690 / 0.290; pair notional 0.98; potential ordinary edge 0.020; depth 7653 / 5248; observed `2026-10-09T13:22:26+00:00`.

Complete-root SHA-256: `c758a9c1fae8902f145d4abc4724634eb7380f04c20e9c61970c41414be5d4b0`.

- [Market 211](https://sig.thesuper.market/api/v1/markets/211?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/211/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).
- [Market 322](https://sig.thesuper.market/api/v1/markets/322?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55); [complete official resolution root](https://sig.thesuper.market/api/v1/markets/322/nodes?tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55).

Human approval would additionally cover the scoped tournament source and the exact current policy content above.
