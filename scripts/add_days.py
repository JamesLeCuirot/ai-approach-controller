"""Add days to data/real/egkk/days.csv, with the same weather columns as scripts/choose_days.py.

For test days added after the first 60: extra storm days, and the most recent weeks, which
make the rolling test set that no version of the AI has trained on. Only days whose arrivals
and other traffic are already extracted are added, and days already listed are left alone.

    python scripts/add_days.py --kind recent --split recent 2026-10-26 2026-10-27
    python scripts/add_days.py --kind storm --split test --note "Storm Bert" 2024-11-24
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approach.data.arrivals import REAL_DIR  # noqa: E402

DAYS = REAL_DIR / "egkk" / "days.csv"
URL = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=EGKK&data=sknt&data=drct&data=gust"
       "&data=vsby&year1={a.year}&month1={a.month}&day1={a.day}&year2={b.year}&month2={b.month}&day2={b.day}"
       "&tz=Etc/UTC&format=onlycomma&missing=empty&report_type=3&report_type=4")


def weather(day: date) -> dict:
    """The day's mean daytime wind, strongest gust, share of easterly reports and low-visibility reports."""
    req = urllib.request.Request(URL.format(a=day, b=day + timedelta(days=1)), headers={"User-Agent": "ai-approach-controller"})
    for attempt in range(6):  # the archive asks automated clients to slow down (HTTP 429)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 5:
                raise
            time.sleep(15 * 2 ** attempt)
    rows = [x for x in csv.DictReader(io.StringIO(text))
            if x["valid"][:10] == day.isoformat() and 6 <= int(x["valid"][11:13]) <= 21 and x["sknt"]]
    u = sum(-float(r["sknt"]) * math.sin(math.radians(float(r["drct"] or 0))) for r in rows) / len(rows)
    v = sum(-float(r["sknt"]) * math.cos(math.radians(float(r["drct"] or 0))) for r in rows) / len(rows)
    tail26 = [float(r["sknt"]) * math.cos(math.radians(float(r["drct"] or 0) - 78)) for r in rows if r["drct"]]
    return {
        "mean_wind": f"{round(math.degrees(math.atan2(-u, -v)) % 360):03d}/{sum(float(r['sknt']) for r in rows) / len(rows):.0f}",
        "max_gust_kt": round(max((float(r["gust"]) for r in rows if r["gust"]), default=0.0)),
        "easterly_share": f"{sum(t > 5 for t in tail26) / max(1, len(tail26)):.2f}",
        "low_vis_reports": sum(float(r["vsby"]) < 0.4 for r in rows if r["vsby"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("days", nargs="+")
    ap.add_argument("--kind", required=True)
    ap.add_argument("--split", required=True, help="test or recent (never train: the training days are fixed)")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    if args.split == "train":
        sys.exit("The training days are fixed; add test or recent days only.")
    rows = list(csv.DictReader(DAYS.open()))
    have = {r["day"] for r in rows}
    for d in args.days:
        extracted = all((REAL_DIR / "egkk" / sub / f"{d}.json.gz").is_file() for sub in ("arrivals", "other"))
        if d in have or not extracted:
            print(f"{d}: {'already listed' if d in have else 'not extracted yet'}, skipped")
            continue
        rows.append({"day": d, "kind": args.kind, "split": args.split, **weather(date.fromisoformat(d)), "note": args.note})
        print(f"{d}: added ({args.kind}, {args.split})")
    with DAYS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["day"]))


if __name__ == "__main__":
    main()
