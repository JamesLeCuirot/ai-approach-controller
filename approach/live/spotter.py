"""Which aircraft entering the handover region are Gatwick arrivals, decided as they enter.

The live feed doesn't say where an aircraft is going. Arrivals are handed over the first time they are within 40 NM
of the airport and at or below 15,000 ft (approach.data.arrivals), and so are plenty of others: Heathrow arrivals
and departures, Gatwick departures, aircraft passing through. So when an aircraft enters that region, its state
(position, height, ground speed, track, vertical rate) is compared with every aircraft that entered it on the
training days: of the K most like it, the share that went on to land at Gatwick is the chance it is an arrival.

Judged on the 14 most recent days (none of them in its table), it found 98.7% of the arrivals as they entered the
region, and about 2% of the aircraft it called arrivals were not; it was at least 90% sure either way of 99.3% of
the aircraft. Waiting up to 5 minutes before deciding barely helps (99.5% found).

The table (data/real/egkk/entry_states.npz) is built from adsb.lol's open data, so it is under the same licence
(ODbL 1.0) and credits adsb.lol.

    python -m approach.live.spotter build        # the table, from the training and recent days
    python -m approach.live.spotter check        # build from the training days only and judge on the recent days
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path

import numpy as np

from approach.data.arrivals import HANDOVER_CEILING_FT, HANDOVER_RADIUS_NM, REAL_DIR

K = 25
DECIDE_WITHIN_S = 60.0  # an aircraft that enters without a speed or track is decided at its first point with them
VISIT_GAP_S = 30 * 60
ARRIVAL_P = 0.5
TABLE = REAL_DIR / "egkk" / "entry_states.npz"
RAW = Path(__file__).resolve().parents[2] / "data" / "raw" / "adsblol" / "egkk"


def scaled(s: np.ndarray) -> np.ndarray:
    """States (x NM east, y NM north, altitude ft, ground speed kt, track deg, vertical rate ft/min) scaled so that
    one unit is about 4 NM, 1,500 ft, 25 degrees of track, 60 kt or 1,000 ft/min."""
    s = np.atleast_2d(np.asarray(s, dtype=float))
    trk = np.radians(s[:, 4])
    return np.column_stack([s[:, 0] / 4, s[:, 1] / 4, s[:, 2] / 1500, np.sin(trk) / 0.44, np.cos(trk) / 0.44,
                            s[:, 3] / 60, np.clip(s[:, 5], -3000, 3000) / 1000])


class Entries:
    """Follows aircraft point by point and says when one has entered the handover region and can be judged."""

    def __init__(self, airport):
        self.ref = airport.reference
        self.coslat = math.cos(math.radians(self.ref.lat))
        self.state: dict[str, dict] = {}

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.ref.lon) * 60.0 * self.coslat, (lat - self.ref.lat) * 60.0

    def add(self, hexid: str, p: list) -> tuple[float, list[float]] | None:
        """A point (t, lat, lon, alt, gs, track, vrate, ...). Returns (entry time, state) when the aircraft can be
        judged: at its entry, or at its first point with a speed and track within DECIDE_WITHIN_S of it."""
        st = self.state.setdefault(hexid, {"inside": False, "last_t": -1e18, "entered": None})
        if p[0] - st["last_t"] > VISIT_GAP_S:
            st["inside"], st["entered"] = False, None
        st["last_t"] = p[0]
        alt = p[3]
        if alt is None:
            return None
        x, y = self.xy(p[1], p[2])
        inside = alt != "ground" and alt <= HANDOVER_CEILING_FT and math.hypot(x, y) <= HANDOVER_RADIUS_NM
        if inside and not st["inside"]:
            st["entered"] = p[0]
        st["inside"] = inside
        if st["entered"] is None:
            return None
        if p[0] - st["entered"] > DECIDE_WITHIN_S or alt == "ground":
            st["entered"] = None
            return None
        if p[4] is None or p[5] is None:
            return None
        entered, st["entered"] = st["entered"], None
        return entered, [x, y, float(alt), float(p[4]), float(p[5]), float(p[6] or 0.0)]


class Spotter:
    def __init__(self, path: Path = TABLE, states: np.ndarray | None = None, labels: np.ndarray | None = None):
        if states is None:
            d = np.load(path)
            states, labels = d["states"], d["labels"]
        self.table = scaled(states)
        self.labels = np.asarray(labels, dtype=float)
        self.t2 = (self.table * self.table).sum(1)

    def chance(self, states) -> np.ndarray:
        """For each state, the share of its K nearest training states that were Gatwick arrivals."""
        b = scaled(states)
        out = np.empty(len(b))
        for i in range(0, len(b), 2000):
            c = b[i:i + 2000]
            d = self.t2[None, :] - 2 * c @ self.table.T + (c * c).sum(1)[:, None]
            idx = np.argpartition(d, K, axis=1)[:, :K]
            out[i:i + 2000] = self.labels[idx].mean(1)
        return out


def day_states(day: str, airport) -> tuple[list[list[float]], list[int]]:
    """Every entry into the handover region on one archived day, judged as Entries would judge it live, and
    whether it was a Gatwick arrival handed over there."""
    with gzip.open(RAW / f"{day}.json.gz", "rt") as f:
        area = json.load(f)
    with gzip.open(REAL_DIR / "egkk" / "arrivals" / f"{day}.json.gz", "rt") as f:
        handovers: dict[str, list[float]] = {}
        for a in json.load(f)["arrivals"]:
            handovers.setdefault(a["hex"], []).append(a["handover"]["t"])
    states, labels = [], []
    for ac in area["aircraft"]:
        ent = Entries(airport)
        for p in sorted((p for p in ac["points"] if p[1] is not None), key=lambda p: p[0]):
            got = ent.add(ac["hex"], p)
            if got:
                states.append(got[1])
                labels.append(int(any(abs(h - got[0]) < 1.0 for h in handovers.get(ac["hex"], []))))
    return states, labels


def build(splits: tuple[str, ...], airport, log=print) -> tuple[np.ndarray, np.ndarray]:
    days = [ln.split(",")[0] for ln in (REAL_DIR / "egkk" / "days.csv").read_text().splitlines()[1:]
            if ln.split(",")[2] in splits]
    states, labels = [], []
    for d in days:
        if not (RAW / f"{d}.json.gz").exists():
            log(f"{d}: not downloaded, skipped")
            continue
        s, lab = day_states(d, airport)
        states += s
        labels += lab
        log(f"{d}: {len(s)} entries, {sum(lab)} arrivals")
    return np.array(states, dtype=np.float32), np.array(labels, dtype=np.int8)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("build", "check"))
    args = ap.parse_args()
    from approach.airports import egkk

    airport = egkk.load()
    if args.what == "build":
        states, labels = build(("train", "recent"), airport)
        np.savez_compressed(TABLE, states=states, labels=labels,
                            source=np.array("adsb.lol (https://www.adsb.lol), ODbL 1.0"))
        print(f"{len(states)} entries, {int(labels.sum())} arrivals -> {TABLE}")
        return
    s_tr, y_tr = build(("train",), airport, log=lambda *_: None)
    s_te, y_te = build(("recent",), airport, log=lambda *_: None)
    p = Spotter(states=s_tr, labels=y_tr).chance(s_te)
    called, arr = p >= ARRIVAL_P, y_te == 1
    sure = (p >= 0.9) | (p <= 0.1)
    print(f"{len(s_te)} entries on the recent days, {arr.sum()} of them arrivals")
    print(f"arrivals found {(called & arr).sum()} ({(called & arr).sum() / arr.sum():.1%}); called arrivals that "
          f"were not {(called & ~arr).sum()} ({(called & ~arr).sum() / max(called.sum(), 1):.1%} of those called)")
    print(f"at least 90% sure either way: {sure.mean():.1%}, right {((p[sure] >= 0.5) == arr[sure]).mean():.2%} of those")


if __name__ == "__main__":
    main()
