"""The real controllers' scores for every hour of every chosen day: the benchmark's real-controller baseline.

Each hour is scored exactly as a simulated session is (approach.data.real_sessions):
the arrivals handed over in that hour, from handover to touchdown.

    python -m approach.data.real_baseline            (all days in data/real/egkk/days.csv)
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
from multiprocessing import Pool
from pathlib import Path

from approach.data.arrivals import REAL_DIR, load_day

FIELDS = ["day", "hour", "kind", "split", "runway", "arrivals", "landed", "separation_losses", "wake_violations",
          "min_spacing_nm", "mean_spacing_nm", "track_miles_per_arrival", "minutes_per_arrival", "go_arounds",
          "holding_minutes_per_arrival", "left_rma", "minutes_outside_rma", "below_minimum_altitude"]


def score_day(args: tuple[str, str, str]) -> list[dict]:
    from approach.airports import egkk
    from approach.data.real_sessions import real_session, window_arrivals

    day_str, kind, split = args
    airport = egkk.load()
    day = load_day(date.fromisoformat(day_str), airport.icao)
    midnight = datetime.fromisoformat(day_str).replace(tzinfo=timezone.utc).timestamp()
    rows = []
    for hour in range(24):
        start = midnight + hour * 3600
        if not window_arrivals(day, start, start + 3600):
            continue
        log = real_session(day, airport, start, start + 3600)
        s = log.scores
        rows.append({"day": day_str, "hour": hour, "kind": kind, "split": split, "runway": log.meta["runway"],
                     **{k: s.get(k, "") for k in FIELDS[5:]}})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--airport", default="egkk")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    folder = REAL_DIR / args.airport
    days = [(r["day"], r["kind"], r["split"]) for r in csv.DictReader((folder / "days.csv").open())]
    out: list[dict] = []
    with Pool(args.workers) as pool:
        for rows in pool.imap_unordered(score_day, days):
            out += rows
            if rows:
                print(f"{rows[0]['day']}: {len(rows)} hours, {sum(r['arrivals'] for r in rows)} arrivals", flush=True)
    out.sort(key=lambda r: (r["day"], r["hour"]))
    path = folder / "real_controller_hours.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    print(f"Wrote {path} ({len(out)} hours)")


if __name__ == "__main__":
    main()
