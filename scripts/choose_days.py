"""Choose the real Gatwick days for the benchmark, from Gatwick's weather reports.

Reads a METAR export from the Iowa State University archive (see --help) and picks a
spread of days that adsb.lol has data for: storms, other strong wind, easterly days
(runway 08R), low visibility, busy summer days and ordinary days in every season.
One day in four of each kind is kept for testing only, so the AI is never trained on
the days it is tested on.

    curl -o metar.csv "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=EGKK&data=sknt&data=drct&data=gust&data=vsby&data=wxcodes&year1=2023&month1=12&day1=28&year2=2026&month2=11&day2=6&tz=Etc/UTC&format=onlycomma&missing=empty&report_type=3&report_type=4"
    git ls-remote --tags https://github.com/adsblol/globe_history_2025 > tags.txt   (and 2024, 2026)
    python scripts/choose_days.py metar.csv tags*.txt > data/real/egkk/days.csv
"""

from __future__ import annotations

import csv
import math
import random
import sys
from collections import defaultdict

STORMS = {"2024-01-21": "Storm Isha", "2024-12-07": "Storm Darragh"}
TARGETS = {"storm": 2, "strong_wind": 8, "easterly": 12, "low_visibility": 6, "summer": 16, "ordinary": 16}


def main() -> None:
    metar, tag_files = sys.argv[1], sys.argv[2:]
    have = set()
    for f in tag_files:
        for line in open(f):
            tag = line.strip().split("refs/tags/")[-1].replace("^{}", "")
            if tag.startswith("v") and ("-prod-0" in tag or "-staging-0" in tag) and "tmp" not in tag:
                have.add(tag[1:11].replace(".", "-"))

    days: dict[str, list[dict]] = defaultdict(list)
    for r in csv.DictReader(open(metar)):
        hour = int(r["valid"][11:13])
        if 6 <= hour <= 21 and r["sknt"]:
            days[r["valid"][:10]].append(r)

    info = {}
    for d, rows in days.items():
        if d not in have or len(rows) < 20:
            continue
        u = sum(-float(r["sknt"]) * math.sin(math.radians(float(r["drct"] or 0))) for r in rows) / len(rows)
        v = sum(-float(r["sknt"]) * math.cos(math.radians(float(r["drct"] or 0))) for r in rows) / len(rows)
        mean_dir = math.degrees(math.atan2(-u, -v)) % 360
        mean_kt = sum(float(r["sknt"]) for r in rows) / len(rows)
        # Tailwind on 26L (course about 258 true) above 5 kt usually means runway 08R.
        tail26 = [float(r["sknt"]) * math.cos(math.radians(float(r["drct"] or 0) - 78)) for r in rows if r["drct"]]
        easterly = sum(t > 5 for t in tail26) / max(1, len(tail26))
        vis = [float(r["vsby"]) for r in rows if r["vsby"]]
        low_vis = sum(x < 0.4 for x in vis)  # under about 600 m
        gust = max((float(r["gust"]) for r in rows if r["gust"]), default=0.0)
        info[d] = {"dir": round(mean_dir), "kt": round(mean_kt, 1), "gust": round(gust), "easterly": easterly,
                   "low_vis_reports": low_vis, "month": int(d[5:7])}

    rng = random.Random(2026)
    chosen: dict[str, str] = {}

    def pick(kind: str, pool: list[str], n: int) -> None:
        pool = [d for d in pool if d not in chosen]
        rng.shuffle(pool)
        # spread over the period: take every k-th day of the sorted pool
        pool = sorted(pool[: max(n * 3, n)])
        step = max(1, len(pool) // n)
        for d in pool[::step][:n]:
            chosen[d] = kind

    for d in STORMS:
        if d in info:
            chosen[d] = "storm"
    pick("strong_wind", sorted(info, key=lambda d: -info[d]["kt"])[:40], TARGETS["strong_wind"])
    pick("easterly", [d for d in info if info[d]["easterly"] > 0.7], TARGETS["easterly"])
    pick("low_visibility", [d for d in info if info[d]["low_vis_reports"] >= 4], TARGETS["low_visibility"])
    pick("summer", [d for d in info if info[d]["month"] in (6, 7, 8, 9) and info[d]["easterly"] < 0.3
                    and info[d]["kt"] < 15], TARGETS["summer"])
    pick("ordinary", [d for d in info if info[d]["month"] not in (6, 7, 8, 9) and info[d]["easterly"] < 0.3
                      and 4 <= info[d]["kt"] < 15 and info[d]["low_vis_reports"] == 0], TARGETS["ordinary"])

    w = csv.writer(sys.stdout)
    w.writerow(["day", "kind", "split", "mean_wind", "max_gust_kt", "easterly_share", "low_vis_reports", "note"])
    by_kind: dict[str, list[str]] = defaultdict(list)
    for d in sorted(chosen):
        by_kind[chosen[d]].append(d)
    for kind, ds in by_kind.items():
        for i, d in enumerate(ds):
            x = info[d]
            split = "test" if i % 4 == 3 else "train"
            w.writerow([d, kind, split, f"{x['dir']:03d}/{x['kt']:.0f}", x["gust"], f"{x['easterly']:.2f}",
                        x["low_vis_reports"], STORMS.get(d, "")])


if __name__ == "__main__":
    main()
