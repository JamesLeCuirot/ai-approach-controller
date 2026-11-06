"""How real Gatwick arrivals fly: descent rates and speeds by aircraft type, from the real arrivals.

Descent rate is measured while an aircraft is actually descending (faster than 300 ft/min
down) between 10,000 and 3,000 ft, before it is on the glide path, so it shows how fast
aircraft come down when cleared to descend, not how often they are levelled off. Speeds
are ground speeds on final at 10 NM and 4 NM, so they include the wind.

    python -m approach.data.profiles 2024-06-01 2024-12-07
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date

from approach.airports import egkk
from approach.data.arrivals import load_day


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def profiles(days: list[date], min_flights: int = 15) -> list[dict]:
    airport = egkk.load()
    descent: dict[str, list[float]] = defaultdict(list)
    per_flight: dict[str, list[float]] = defaultdict(list)
    gs10: dict[str, list[float]] = defaultdict(list)
    gs4: dict[str, list[float]] = defaultdict(list)
    flights: dict[str, int] = defaultdict(int)
    for d in days:
        for a in load_day(d)["arrivals"]:
            t = a["type"]
            flights[t] += 1
            rates = []
            for p in a["track"]:
                alt, vr = p[3], p[6]
                if alt == "ground" or alt is None or vr is None:
                    continue
                x, y = airport.grid.to_xy(p[1], p[2])
                along, lateral = airport.runway_frame(a["runway"], x, y)
                on_final = 0 < along < 12 and abs(lateral) < 0.3
                if 3000 <= alt <= 10000 and vr < -300 and not on_final:
                    rates.append(-vr)
                if on_final and p[4]:
                    if 9.5 <= along <= 10.5:
                        gs10[t].append(p[4])
                    if 3.5 <= along <= 4.5:
                        gs4[t].append(p[4])
            descent[t] += rates
            if len(rates) >= 5:
                per_flight[t].append(sorted(rates)[len(rates) // 2])
    rows = []
    for t, n in sorted(flights.items(), key=lambda kv: -kv[1]):
        if n < min_flights or len(per_flight[t]) < 5:
            continue
        pf = per_flight[t]
        rows.append({
            "type": t, "flights": n,
            "descent_median_fpm": round(_pct(pf, 0.5), -1),
            "descent_p10_fpm": round(_pct(pf, 0.1), -1),
            "descent_p90_fpm": round(_pct(pf, 0.9), -1),
            "gs_10nm_kt": round(_pct(gs10[t], 0.5)) if gs10[t] else None,
            "gs_4nm_kt": round(_pct(gs4[t], 0.5)) if gs4[t] else None,
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("days", nargs="+")
    args = ap.parse_args()
    rows = profiles([date.fromisoformat(d) for d in args.days])
    print("| Type | Flights | Typical descent (ft/min) | Slowest 10% | Fastest 10% | Ground speed at 10 NM | at 4 NM |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['type']} | {r['flights']} | {r['descent_median_fpm']:,.0f} | {r['descent_p10_fpm']:,.0f} | "
              f"{r['descent_p90_fpm']:,.0f} | {r['gs_10nm_kt']} kt | {r['gs_4nm_kt']} kt |")


if __name__ == "__main__":
    main()
