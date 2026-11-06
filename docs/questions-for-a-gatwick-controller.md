# Questions for a Gatwick controller

Questions to put to someone who controls Gatwick arrivals, real or on VATSIM. Each one is something the simulator, the safety check or the scoring currently guesses at. Next to each is what we do now, so a quick "yes" or "no, it's like this" is enough. Where another guide or our own ADS-B data already suggests an answer, that is noted too, but we'd still like it confirmed.

Please say whether an answer is real-world practice or VATSIM practice: both are useful, but we need to know which.

## Airspace and handover

| # | Question | What we do now |
| --- | --- | --- |
| 1 | Can an arrival ever be vectored outside the RMA? If so, when, and what coordination does it need? | Never: once inside the RMA, a heading must keep it inside |
| 2 | Are the RMA outlines in VATSIM UK's sector file (`RMA.txt`, westerlies and easterlies) right for real operations? | We use them as drawn. They match older published Gatwick RMA maps (2004) closely, apart from the east edge: an arc in ours, straight lines in the older maps |
| 3 | Where do Gatwick's directors take arrivals over from London Terminal Control: at the holds, before them, at what levels? How much does it vary with what the previous sector has arranged? | The AI takes each arrival the first time it is within 40 NM and at or below FL150, which is our assumption. Another guide says Terminal Control offers each arrival at the stack, at the level above the highest aircraft holding (or minimum stack level plus 1,000 ft if nobody is holding), and keeps the upper levels itself in heavy holding. Is that how it works now? |
| 4 | What minimum altitudes apply where? Inside the minimum-altitude area (`SMAA.txt`), the north-east and north-west parts, and outside it but inside the RMA | 2,000 ft inside the minimum-altitude area, 3,000 ft outside it |
| 5 | Which neighbouring traffic do Gatwick arrivals have to stay clear of: Heathrow, Biggin Hill, Redhill, Shoreham, Farnborough, Gatwick's own departures? Where and at what levels? | Only the RMA keeps us clear; other traffic isn't in the simulator yet |
| 6 | Are arrivals ever vectored north of the extended centreline? | No: vectored arrivals are kept south of it, clear of the departure routes. In our ADS-B data, over 99% of real 26L arrivals and about 97% of 08R arrivals never went more than 1 NM north of it |

## Holding

| # | Question | What we do now |
| --- | --- | --- |
| 7 | Who runs the WILLO and TIMBA stacks, and how is an aircraft taken off the stack: a heading from the hold, at what level and speed? | Gatwick takes it off with a heading towards the downwind. Another guide says every arrival is told to hold, whether or not anyone is holding, and approach cancels the hold. Is that right? |
| 8 | What are the usual stack levels, and when is the MAY hold used? | Stacks fill from 7,000 ft (FL70) up, 1,000 ft apart; MAY isn't used |
| 9 | When do you hold rather than extend the downwind? How far out can a downwind go? | Hold when the slot can't be made with a final of about 12–14 NM, the longest the RMA allows us |

## Vectoring and spacing

| # | Question | What we do now |
| --- | --- | --- |
| 10 | How is the work split between the Intermediate and Final Directors today: where is the merge, and where, at what level and at what speed does traffic pass from one to the other? | One AI does both jobs. Another guide says the streams merge around Mayfield for westerlies and just north of WILLO for easterlies, and pass to the Final Director descending to 4,000 ft at 210 kt or less |
| 11 | How far from the centreline is a typical downwind, and how far out do aircraft usually join final? | Downwind 6 NM from the centreline, joining final 11–14 NM out. Another guide says 8–12 NM, which we will check against the ADS-B data |
| 12 | What intercept angles are normal? Is 45° the real limit at Gatwick? | 30° intercept; the safety check allows up to 45°. Another guide says 30–45°, joining at 3,000 ft and 180 kt |
| 13 | Is "no lower than 6,000 ft at 20 NM from touchdown, established on the ILS at 3,000 ft" a fair rule of thumb for being on profile? | We use it as a rough guide, in the AI's inputs, to whether an aircraft is on the right path. It is not a target or a gate |
| 14 | What spacing do you aim for on final, and how do you decide when to leave a gap for departures (6 or 8 NM) and when to close up to the minimum? | Departures aren't modelled yet, so arrivals are spaced to the minimum: 3 NM, or the wake minimum if larger, plus a 1 NM margin. Real controllers in the data average about 5.5 NM in a busy hour, closest about 3 NM |
| 15 | Is 2.5 NM between aircraft on the same final within 10 NM approved and used at Gatwick? | The scoring allows it for every controller (UK/EASA AMC1 ATS.TR.210(c)(2)) |
| 16 | Which wake turbulence scheme did Gatwick use, and when? We understand it moved from the UK categories to RECAT-EU, with time-based separation, in March 2025: on what date? | Each real day is scored with the scheme in force then: the UK categories (CAP 493) before 26 March 2025, RECAT-EU from then on. Made-up scenarios use the UK scheme. Our data agrees: measured against the UK scheme, real spacing falls short ten times as often after March 2025 as before |
| 17 | Do wake minima matter only on final, or also on intermediate approach, directly behind or crossing behind at the same level or up to 1,000 ft below? | Checked on final only |
| 18 | Are these speeds right: 220 kt from the hold, 180 kt on base, 160 kt to 4 DME? | 220 kt, as in the VATSIM UK Gatwick guide. Another guide says 210 kt on passing to the Final Director, about 180 kt for final sequencing and 160 kt once established |

## Runways, weather and the unusual

| # | Question | What we do now |
| --- | --- | --- |
| 19 | How is a runway change from 26L to 08R planned and run, and how long does it take? | Not modelled yet (stage 5) |
| 20 | How do low visibility procedures change spacing and holding? | Not modelled yet (stage 5) |
| 21 | After a go-around, how is the aircraft brought back into the sequence? Is it vectored straight back round, or ever sent to a hold? | Climbs straight ahead to 3,000 ft and turns onto 178, then goes to the front of the sequence; if it still has to hold, it climbs to its level in the stack |
| 22 | In strong wind, what changes: spacing, speeds, how far out you join final? | Nothing special yet |

## Realism check

| # | Question | What we do now |
| --- | --- | --- |
| 23 | Watching a real busy hour next to our rules controller's version of the same traffic, what looks wrong? | The real tracks are drawn in grey behind ours |
| 24 | What would you need to see before you'd trust an AI's vectoring? | Our scores: separation, RMA, minimum altitudes, spacing, track miles, holding, go-arounds |

## Controller tools

| # | Question | What we do now |
| --- | --- | --- |
| 25 | What are the STCA settings in Gatwick's approach airspace: look-ahead time, lateral and vertical thresholds, and anything that suppresses alerts, such as on final? | Not built yet. Plan: about 2 minutes ahead, 3 NM and 1,000 ft, on a prediction that knows each aircraft's cleared level, including traffic the sector doesn't control |
| 26 | Which support tools do Gatwick's approach controllers use (traffic probe, vertical assistance window, sector conflict tool, vector lines, halos), and how much do they lean on each? | Not built yet. Plan: the same tools for people and the AI, with the option of running the benchmark without them |
