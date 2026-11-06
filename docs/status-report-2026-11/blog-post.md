# Notes for publishing (not part of the post)

Publish the post below as a blog post dated **Monday 9 November 2026**, author **James Le Cuirot**.

It uses four assets. Each one is marked in the text with a placeholder like `[ASSET: file name]`, with the alt text and the caption to use just below it.

| Placeholder | File | What it is |
| --- | --- | --- |
| `[ASSET: radar-busy-hour.png]` | `radar-busy-hour.png` | Screenshot of the radar replay |
| `[ASSET: landing-gaps.png]` | `landing-gaps.png` | Chart of the spacing between landings |
| `[ASSET: comparison.png]` | `comparison.png` | Table image comparing the real controllers and ours |
| `[ASSET: rules-controller-busy-hour.mp4]` | `rules-controller-busy-hour.mp4` (also sent as `rules_2026-05-30T12.mp4`) | 50-second video of the radar replay, no sound |

Show the video with player controls, muted, and without autoplay unless the site usually autoplays. Keep the links as they are.

Suggested summary for listings and link previews: *An open benchmark for AI approach control at London Gatwick: a simulator running on 21,160 real arrivals, scored against the real controllers who handled them.*

---

# AI Approach Controller: status report, November 2026

## The idea

Last week I came across a post from Naveen Mathew, who wrote about [building a reinforcement learning agent for air traffic control](https://snaveenmathew.github.io/tech_non_tech_blog/2026/09/12/Building-an-ATC-RL-agent.html). It left me wondering: could an AI learn an approach controller's job at a real, busy airport, starting with two aircraft and then adding the real world's complications one at a time? And how would it compare with the controllers who actually do it for real?

To start answering that question, I set about building an open benchmark for AI approach control. I wanted a real airport to build the simulator around, and I chose London Gatwick: a busy single-runway airport (for now!), with mixed traffic types, plenty of adverse weather and, more importantly, a reliable dataset to work with.

The idea was to take every real arrival on a given day, hand each one to a controller as it comes within 40 NM of Gatwick, and use the ADS-B data to score that controller on exactly the same measures as the real controllers who handled the same aircraft.

- **A simulated Gatwick approach.** A controller takes over each real arrival as it comes within 40 NM of the airport, and works it through the holds, the downwind and the base turn onto the ILS for runway 26L or 08R.
- **A fixed safety check** sits between the controller and the aircraft and blocks any instruction that would break separation, minimum altitudes or the airspace limits. It never learns.
- **One yardstick for everyone.** Real controllers, hand-written rules and the AI are all scored the same way: separation, spacing on final, time and track miles flown, holding, go-arounds, and staying inside the approach airspace.

An open benchmark matters because claims about AI in air traffic control are hard to compare without the same traffic and the same scores.

## What's built so far

The world the AI will learn in is built and running on real Gatwick traffic. There is no learned AI yet: today a hand-written rules controller flies the traffic, as the first yardstick.

- **A simulated Gatwick approach** on TU Delft's open-source BlueSky simulator, with Gatwick's airspace, holds and procedures taken from VATSIM UK's sector file and controller guides. Simulated pilots respond after a few seconds, turn at realistic rates, capture the ILS, fly the published holds and go around when an approach goes wrong.
- **Real traffic.** Every Gatwick arrival that adsb.lol's volunteer receivers saw on 60 chosen days between December 2023 and September 2026: 21,160 in all, from its open ADS-B data. The days cover storms, strong wind, easterly days on runway 08R, fog, busy summer days and ordinary days, and a quarter of them are kept back for testing only. Any real hour can be replayed into the simulator, each aircraft starting exactly where it really was.
- **Real weather.** Every session flies in that hour's real winds at every height, from NOAA's GFS weather model.
- **A safety check** that blocks unsafe instructions, including any that would take an aircraft outside Gatwick's radar manoeuvring area or below its minimum altitude.
- **A radar replay** of every session. Our controller's aircraft are in colour, and the real aircraft's tracks carry on in faint grey behind them, so you can watch what the real controllers did with the same traffic.
- **A project diary** with a replay, a video and the scores at every milestone.

[ASSET: radar-busy-hour.png]

> Alt text: The radar replay of a real busy hour at Gatwick: aircraft holding at WILLO and TIMBA, two on final for runway 26L, and the real tracks in faint grey.
>
> Caption: A real busy hour, 30 May 2026 at 12:45 UTC, flown by the rules controller. Aircraft hold at WILLO (left) and TIMBA (right) while two are on final for runway 26L; the faint grey lines are what the real aircraft actually flew.

## What the real data shows

Scoring the real controllers on all 60 days, about 1,400 hours of traffic, gives the benchmark its yardstick. On average they take an arrival from 40 NM out to touchdown in 18 minutes and about 70 track miles, and in the busiest hours more than 30 arrivals come in within the hour.

From the ADS-B data alone, it was interesting to see how clearly the real-world controllers share Gatwick's single runway with departures. With no departure in between, they close landings up to about 4.5 NM; one departure opens the gap to about 5.6 NM, and two to about 7.9 NM. That's almost exactly what the controllers' guide says: 6 NM for one departure, 8 NM for two. It may come as no surprise, but it's still satisfying to show it from ADS-B data alone.

[ASSET: landing-gaps.png]

> Alt text: Chart of the spacing between consecutive Gatwick landings by the number of departures in between: about 4.5 NM with none, 5.6 NM with one, 7.9 NM with two and 10.6 NM with three or more, beside the guide's targets of 6 and 8 NM and the 3 NM radar minimum.
>
> Caption: Real Gatwick landings, 60 days from December 2023 to September 2026. Traffic from adsb.lol (ODbL).

When we compare that performance with a hand-written rules controller (one that follows instructions written in code), given the same real arrivals in the same winds, it falls well behind the real controllers:

[ASSET: comparison.png]

> Alt text: Table comparing the real controllers with our rules controller on three real hours. Busy hour on runway 26L, 30 May 2026, 28 arrivals: the real controllers landed 28 with no separation losses or go-arounds, in 17.5 minutes each; ours landed 28 with 5 separation losses and 4 go-arounds, in 21.7 minutes each. Busy hour on runway 08R, 1 June 2024, 28 arrivals: real 28 landed, none, none, 19.8 minutes; ours 18 landed with 10 still waiting, 7 separation losses, 3 go-arounds, 40.4 minutes. Storm Darragh, 7 December 2024, 17 arrivals: real 17, none, none, 16.9 minutes; ours 17, 1 separation loss, no go-arounds, 17.7 minutes.
>
> Caption: The real controllers and ours on the same three real hours. Red marks a safety event.

[ASSET: rules-controller-busy-hour.mp4]

> Alt text: Video of the radar replay: the rules controller working the real busy hour of 30 May 2026.
>
> Caption: The rules controller working the real busy hour of 30 May 2026, the whole session sped up into 50 seconds. Aircraft queue in the WILLO and TIMBA holds; yellow lines in the transcript are instructions the safety check blocked; the faint grey lines are what the real aircraft flew.

In a quieter hour, even a storm one, the rules keep up. In a real busy hour they fall well behind, because hand-written rules can't hold the tight, even spacing the real controllers manage while keeping everyone inside the approach airspace. I'm interested to see whether AI can close that gap. That's the next objective.

Scoring real controllers also tested the scoring. Where our checks flagged them, it was nearly always our assumptions that needed fixing, such as the 2.5 NM spacing that UK rules allow on final where approved, so the checks are now calibrated against how real controllers work.

## What's next, and how you can help

Next is the first learned controller. It starts small on purpose: in its first stage the AI controls only the aircraft's speeds, then it also chooses when each aircraft turns onto base, all on real traffic in real winds. It has to beat the rules controller on the same hours before it moves on to full vectoring, holds, go-arounds, runway changes and storms.

- **Other traffic.** Gatwick's departures, Heathrow's traffic and overflights are already in the data. They'll be replayed around our arrivals so any conflict with them counts.
- **A live demo, later.** adsb.lol also has a live feed, so the same radar could show aircraft arriving live, change colour as the AI takes them over, and keep the real track in grey behind.

The biggest gap is controller knowledge. If you control Gatwick, on VATSIM or for real, I have about 20 short questions on what the simulator still guesses at, from whether arrivals are ever vectored outside the RMA to how aircraft come off the stacks. I'd also love to hear from people working on reinforcement learning or aviation data. Get in touch if you'd like to help.

**Credits.** The idea came from [Naveen Mathew's post on building a reinforcement learning agent for air traffic control](https://snaveenmathew.github.io/tech_non_tech_blog/2026/09/12/Building-an-ATC-RL-agent.html) and his [atc_app](https://github.com/SNaveenMathew/atc_app) project · [BlueSky](https://github.com/TUDelft-CNS-ATM/bluesky) (TU Delft) · [VATSIM UK](https://www.vatsim.uk)'s Gatwick sector file and controller guides · traffic from [adsb.lol](https://www.adsb.lol) (ODbL) · winds from [NOAA's GFS model](https://registry.opendata.aws/noaa-gfs-bdp-pds/) · weather reports from the [Iowa State University archive](https://mesonet.agron.iastate.edu/request/download.phtml).
