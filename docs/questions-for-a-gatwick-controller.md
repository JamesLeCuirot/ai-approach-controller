# Questions for a Gatwick controller

Questions to put to someone who controls Gatwick arrivals, real or on VATSIM. Each one is something the simulator, the safety check or the scoring currently guesses at. Next to each is what we do now, so a quick "yes" or "no, it's like this" is enough.

Please say whether an answer is real-world practice or VATSIM practice: both are useful, but we need to know which.

## Airspace and handover

| # | Question | What we do now |
| --- | --- | --- |
| 1 | Can an arrival ever be vectored outside the RMA? If so, when, and what coordination does it need? | Never: once inside the RMA, a heading must keep it inside |
| 2 | Are the RMA outlines in VATSIM UK's sector file (`RMA.txt`, westerlies and easterlies) right for real operations? | We use them as drawn |
| 3 | Where do Gatwick's directors take arrivals over from London Terminal Control: at the holds, before them, at what levels? | The AI takes each arrival the first time it is within 40 NM and at or below FL150, so it does both jobs |
| 4 | What minimum altitudes apply where? Inside the minimum-altitude area (`SMAA.txt`), the north-east and north-west parts, and outside it but inside the RMA | 2,000 ft inside the minimum-altitude area, 3,000 ft outside it |
| 5 | Which neighbouring traffic do Gatwick arrivals have to stay clear of: Heathrow, Biggin Hill, Redhill, Shoreham, Farnborough, Gatwick's own departures? Where and at what levels? | Only the RMA keeps us clear; other traffic isn't in the simulator yet |

## Holding

| # | Question | What we do now |
| --- | --- | --- |
| 6 | Who runs the WILLO and TIMBA stacks, and how is an aircraft taken off the stack: a heading from the hold, at what level and speed? | Gatwick takes it off with a heading towards the downwind |
| 7 | What are the usual stack levels, and when is the MAY hold used? | Stacks fill from 7,000 ft (FL70) up, 1,000 ft apart; MAY isn't used |
| 8 | When do you hold rather than extend the downwind? How far out can a downwind go? | Hold when the slot can't be made with a final of about 12–14 NM, the longest the RMA allows us |

## Vectoring and spacing

| # | Question | What we do now |
| --- | --- | --- |
| 9 | How far from the centreline is a typical downwind, and how far out do aircraft usually join final? | Downwind 6 NM from the centreline, joining final 11–14 NM out |
| 10 | What intercept angles are normal? Is 45° the real limit at Gatwick? | 30° intercept; the safety check allows up to 45° |
| 11 | What spacing do you aim for on final in mixed mode, with departures in between? | 6 NM, or the wake minimum if larger. Real controllers in the data average about 5.5 NM in a busy hour, closest about 3 NM |
| 12 | Is 2.5 NM between aircraft on the same final within 10 NM approved and used at Gatwick? | The scoring allows it for every controller (UK/EASA AMC1 ATS.TR.210(c)(2)) |
| 13 | Which wake turbulence scheme does Gatwick use: RECAT-EU or the UK categories? | RECAT-EU |
| 14 | Are these speeds right: 220 kt from the hold, 180 kt on base, 160 kt to 4 DME? | Yes, as in the VATSIM UK Gatwick guide |

## Runways, weather and the unusual

| # | Question | What we do now |
| --- | --- | --- |
| 15 | How is a runway change from 26L to 08R planned and run, and how long does it take? | Not modelled yet (stage 5) |
| 16 | How do low visibility procedures change spacing and holding? | Not modelled yet |
| 17 | After a go-around, how is the aircraft brought back into the sequence? | Climbs straight ahead to 3,000 ft, turns onto 178, then is re-sequenced |
| 18 | In strong wind, what changes: spacing, speeds, how far out you join final? | Nothing special yet |

## Realism check

| # | Question | What we do now |
| --- | --- | --- |
| 19 | Watching a real busy hour next to our rules controller's version of the same traffic, what looks wrong? | The real tracks are drawn in grey behind ours |
| 20 | What would you need to see before you'd trust an AI's vectoring? | Our scores: separation, RMA, minimum altitudes, spacing, track miles, holding, go-arounds |
