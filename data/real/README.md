# Real arrivals

Every arrival at each benchmark airport on the chosen days, from real ADS-B tracks.

**Source and licence.** Derived from [adsb.lol](https://www.adsb.lol)'s open historical data. This database is made available under the [Open Database License (ODbL 1.0)](https://opendatacommons.org/licenses/odbl/1-0/): you can use, share and adapt it, as long as you credit adsb.lol and keep anything built from it under the same licence. This folder's licence is separate from the licence of the project's code.

## What's here

| Path | What it is |
| --- | --- |
| `egkk/days.csv` | The 60 Gatwick days, chosen from Gatwick's weather reports by `scripts/choose_days.py`: storms, strong wind, easterly days (runway 08R), low visibility, busy summer days and ordinary days. One day in four of each kind is marked `test` and is never used for training |
| `egkk/arrivals/<day>.json.gz` | Every arrival that day (UTC) |
| `egkk/other/<day>.json.gz` | Everything else near Gatwick that day: Gatwick's departures (with take-off time and runway, a point every 8 s) and other traffic within 10 NM of either RMA, below FL150 (a point every 15 s) |
| `egkk/real_controller_hours.csv` | The real controllers' scores for every hour of every day, measured exactly as simulated sessions are (`python -m approach.data.real_baseline`). Read [the note on its safety columns](#the-real-controllers-safety-columns) before using them |
| `egkk/landing_gaps.csv` | Every pair of consecutive landings: how many departures went between them, and how far apart the two arrivals were (`python -m approach.data.other_traffic --gaps`) |

Each arrival has:

- `callsign`, `hex` (the airframe's ICAO address), `type` and `wake` (the wake category when the day was extracted, in RECAT-EU letters; the code works out each day's category from `type` with `approach/wake.py`, so this field is for reference only)
- `handover`: where it was the first time it was within 40 NM of Gatwick and at or below FL150. This is where the AI takes over in the simulator, and where real controllers are scored from. It holds the time, position, level, ground speed, track and vertical rate
- `runway` and `landed_t`: the runway it landed on and when it crossed the threshold
- `hold_fix`, `hold_minutes`, `hold_laps`, `hold_t`: holding, if any
- `go_arounds`, `go_around_t`
- `track`: its positions from 50 NM out (below FL200) to touchdown, at most one every 4 seconds: time, latitude, longitude, altitude (ft, or `"ground"`), ground speed (kt), track (degrees true), vertical rate (ft/min)

Times are seconds since 1970 (UTC). Altitudes are barometric, as the aircraft reported them.

## The real controllers' safety columns

`real_controller_hours.csv` scores the real controllers with exactly the code that scores our controllers, including its safety columns: `separation_losses`, `wake_violations`, `left_rma`, `minutes_outside_rma` and `below_minimum_altitude`. Across the 1,377 hours they show 6 separation losses (in 5 hours), 2 wake infringements, 157 exits from the radar manoeuvring area (RMA) and 118 times below a minimum altitude. Wake spacing is measured with the scheme in force at Gatwick that day: the UK categories before 26 March 2025 and RECAT-EU from then (see `approach/wake.py`).

**These are not evidence that the real controllers broke any rule.** They show where the benchmark's picture of Gatwick is still wrong or incomplete:

- **The RMA outlines** are VATSIM UK's drawings for its controllers' screens, not the official areas. Most exits are only just beyond the scoring's 1 NM tolerance, east of Gatwick on runway 26L and in the TIMBA hold, which points to the outline rather than the controllers.
- **The minimum altitudes** (2,000 ft inside the minimum-altitude area, 3,000 ft elsewhere in the RMA) are our assumption, not published values.
- **The data is ADS-B from volunteer receivers.** Positions are a few seconds apart and sometimes missing near the ground, and altitudes are barometric, corrected for the day's pressure below the transition altitude. The real controllers' instructions, and any coordination with neighbouring sectors, aren't in it.
- **The scoring doesn't yet model everything the real rules allow,** so a pair it counts as too close may have been separated in a way it can't see.

Each of these is in [Questions for a Gatwick controller](../../docs/questions-for-a-gatwick-controller.md). The flags should fall as the answers come in, as they already did when the scoring was first calibrated against the real controllers: separation flags fell from 56 to 6 and wake flags from 117 to none. Until then, treat the real controllers' safety columns as a test of the benchmark, not of the controllers.

## How it was made

```bash
python -m approach.data.adsblol 2025-06-27
```

```bash
python -m approach.data.arrivals 2025-06-27
```

The first command streams the day's worldwide file from adsb.lol (about 3.5 GB) and keeps the traffic within 50 NM of the airport below FL200, in `data/raw/adsblol/` (not committed). The second finds the arrivals in it.

## Known limits

- Coverage depends on who was running receivers that day. On some days the receivers see little near the ground, so landings are recognised from tracks that end lined up on final below 1,500 ft.
- Departures and other traffic (Heathrow, overflights) are saved in `egkk/other/` but are not yet replayed in the simulator, which only has the arrivals.
- The real controllers' instructions are not in the data, so instruction counts can't be measured for them.
