"""How well each look-ahead predicts a loss of separation: the safety check's old one, the model of how aircraft
respond (approach.response), and Project Bluebird's geometric safety filter.

    python scripts/lookahead_check.py --encounters 500

Each encounter is two aircraft in the simulator, in the real winds of a random test-day hour, with pilots who fly
like real arrivals (World(pilots="measured")). B is set on a course that passes within 0 to 7 NM of where A will
be in 40 to 150 s, within 2,000 ft of A's level, sometimes descending; then A is given one instruction of the kinds
stage 2's AI can give (or none). Each look-ahead says whether the pair will come within 3 NM and 1,000 ft in the
next 2 minutes, and the simulator then shows whether it did (checked every 5 s, as the safety check does).

Bluebird's filter (https://github.com/project-bluebird/geometric_safety, MIT licence, by George De Ath, University
of Exeter) answers a different question: it certifies a pair safe when no motion in its declared class can bring
them within the minimum, and otherwise leaves the pair unresolved. It is given each aircraft's track and the
track it will turn to, a turn rate (its pilot's limit at its true airspeed), ground speed bounds 10 kt either side
of the speeds it can reach, and a minimum climb or descent rate of 400 ft/min (below every measured pilot's). Its
motion class has no response delay, and the wind makes turns over the ground slightly uneven, so a "certified
safe" pair that loses separation shows where those assumptions don't hold. It needs ``pip install`` of that
repository; without it the check runs the other look-aheads only.

Results go to runs/response/lookahead.json.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from approach.airports import egkk  # noqa: E402
from approach.response import ResponseModel, draw_pilots, median_pilot, state_from_views  # noqa: E402

H_SEP_NM, V_SEP_FT, STEPS = 3.0, 1000.0, 24
KT_TO_MPS = 0.514444
OUT = ROOT / "runs" / "response"
TYPES = ["A320", "A320", "A319", "A20N", "A21N", "B738", "B38M", "A321", "B772", "E190"]


def conflict(a: np.ndarray, b: np.ndarray) -> bool:
    """Whether two paths (steps, 3) come within the minimum at any step."""
    h = np.hypot(a[:, 0] - b[:, 0], a[:, 1] - b[:, 1])
    return bool(np.any((h < H_SEP_NM) & (np.abs(a[:, 2] - b[:, 2]) < V_SEP_FT)))


def _track_and_gs(tas: float, hdg: float, we: float, wn: float) -> tuple[float, float]:
    vx, vy = tas * math.sin(math.radians(hdg)) + we, tas * math.cos(math.radians(hdg)) + wn
    return math.degrees(math.atan2(vx, vy)) % 360.0, math.hypot(vx, vy)


def bluebird(v, target_hdg: float, target_alt: float, target_spd: float, bank_deg: float):
    """Bluebird's motion description for one aircraft: (HeadingMotion, current FL, selected FL)."""
    from geometric_safety import HeadingMotion

    from approach.data.response import tas_from_cas

    trk0, gs0 = _track_and_gs(v.tas_kt, v.hdg, v.wind_e_kt, v.wind_n_kt)
    tas_end = float(tas_from_cas(np.array([target_spd]), np.array([v.alt_ft]))[0]) * v.tas_kt / max(
        float(tas_from_cas(np.array([v.cas_kt]), np.array([v.alt_ft]))[0]), 1.0)
    trk1, gs1 = _track_and_gs(v.tas_kt, target_hdg, v.wind_e_kt, v.wind_n_kt)
    _, gs2 = _track_and_gs(tas_end, target_hdg, v.wind_e_kt, v.wind_n_kt)
    _, gs3 = _track_and_gs(tas_end, v.hdg, v.wind_e_kt, v.wind_n_kt)
    speeds = [gs0, gs1, gs2, gs3]
    turn = abs((trk1 - trk0 + 180.0) % 360.0 - 180.0)
    rate = 1092.6 * math.tan(math.radians(bank_deg)) / max(v.tas_kt, 100.0) if turn > 0.5 else 0.0
    motion = HeadingMotion(heading0_deg=trk0, target_heading_deg=trk1 if turn > 0.5 else trk0, turn_rate_deg_s=rate,
                           speed_min_mps=(min(speeds) - 10.0) * KT_TO_MPS, speed_max_mps=(max(speeds) + 10.0) * KT_TO_MPS)
    return motion, v.alt_ft / 100.0, target_alt / 100.0


def encounter(ap, rng: random.Random, hours, have_bluebird: bool) -> dict:
    from approach.controllers.safety import SafetyCheck
    from approach.data.gfs import fetch_for_airport, parse_time
    from approach.instructions import Instruction
    from approach.sim.world import World

    d, h = rng.choice(hours)
    wind = fetch_for_airport(parse_time(f"{d}T{h:02d}"), ap)
    world = World(ap, "26L", seed=rng.randrange(1 << 30), wind=wind, pilots="measured")
    ax, ay = rng.uniform(-15, 15), rng.uniform(-15, 15)
    a_hdg, a_spd, a_alt = rng.uniform(0, 360), round(rng.uniform(180, 250)), round(rng.uniform(4000, 10000), -2)
    # Where A will be in tc seconds (roughly), and B aimed to pass within `miss` of it from 8-20 NM away.
    tc, miss = rng.uniform(40, 150), rng.uniform(0, 7)
    gs = a_spd * 1.1
    px = ax + gs * tc / 3600 * math.sin(math.radians(a_hdg))
    py = ay + gs * tc / 3600 * math.cos(math.radians(a_hdg))
    b_spd = round(rng.uniform(180, 250))
    b_dist = b_spd * 1.1 * tc / 3600
    b_brg = rng.uniform(0, 360)
    side = rng.choice([-1, 1])
    qx = px + miss * side * math.cos(math.radians(b_brg))
    qy = py - miss * side * math.sin(math.radians(b_brg))
    bx, by = qx + b_dist * math.sin(math.radians(b_brg)), qy + b_dist * math.cos(math.radians(b_brg))
    b_hdg = (b_brg + 180.0) % 360.0
    b_alt = a_alt + rng.choice([-2000, -1500, -1000, -500, 0, 0, 500, 1000, 1500, 2000])
    b_alt = max(3000, b_alt)
    for cs, (x, y, hd, alt, spd) in {"A": (ax, ay, a_hdg, a_alt, a_spd), "B": (bx, by, b_hdg, b_alt, b_spd)}.items():
        ll = ap.grid.to_latlon(x, y)
        world.spawn(cs, rng.choice(TYPES), ll.lat, ll.lon, hd, alt, spd)
    if rng.random() < 0.3:  # B already descending through
        world.instruct(Instruction("B", "altitude", max(3000, b_alt - rng.choice([1000, 2000, 3000]))))
    world.step(8)
    views = world.views()
    if len(views) < 2:
        return {}
    vs = {v.cs: v for v in views}
    a = vs["A"]
    kind = rng.choices(["none", "heading", "altitude", "speed"], [0.2, 0.4, 0.2, 0.2])[0]
    value = {"heading": (a.target_hdg + rng.choice([10, 20, 30, 45, 60, 90]) * rng.choice([-1, 1])) % 360.0,
             "altitude": float(max(3000, min(12000, a.target_alt + rng.choice([-3000, -2000, -1000, 1000])))),
             "speed": float(max(160, min(250, a.target_spd + rng.choice([-30, -20, -10, 10, 20])))),
             "none": 0.0}[kind]
    order = [v.cs for v in views]
    ia = order.index("A")
    state = state_from_views(views)
    # The safety check's old look-ahead, with A's new clearance in force at once.
    safety = SafetyCheck(ap, "26L")
    tracks = safety._tracks(views)
    if kind == "heading":
        tracks["A"].target_hdg = value
    elif kind == "altitude":
        tracks["A"].target_alt = value
    old = safety._predict(tracks)
    old_a, old_b = (np.array([snap[c] for snap in old]) for c in ("A", "B"))
    new = (ia, kind, value) if kind != "none" else None
    model = ResponseModel(ap, "26L")
    typ = model.predict(state, median_pilot(ap.icao), new)[0]
    ens = model.predict(state, draw_pilots(32, ap.icao, rng), new)
    p_conflict = float(np.mean([conflict(e[order.index("A")], e[order.index("B")]) for e in ens]))
    row = {"kind": kind, "old": conflict(old_a, old_b), "typical": conflict(typ[ia], typ[order.index("B")]),
           "p_conflict": p_conflict}
    if have_bluebird:
        from geometric_safety import certify_heading_separation_with_vertical

        pa, pb = world.pilots["A"], world.pilots["B"]
        b = vs["B"]
        ma, fa0, fa1 = bluebird(a, value if kind == "heading" else a.target_hdg,
                                value if kind == "altitude" else a.target_alt,
                                value if kind == "speed" else a.target_spd, 23.0)
        mb, fb0, fb1 = bluebird(b, b.target_hdg, b.target_alt, b.target_spd, 23.0)
        res = certify_heading_separation_with_vertical(
            relative_position_m=((a.x - b.x) * 1852.0, (a.y - b.y) * 1852.0), a=ma, b=mb,
            a_current_fl=fa0, a_selected_fl=fa1, b_current_fl=fb0, b_selected_fl=fb1,
            minimum_vertical_rate_fpm=400.0, separation_threshold_m=H_SEP_NM * 1852.0, horizon_s=120.0,
            required_vertical_gap_fl=V_SEP_FT / 100.0, vertical_rounding_fl=1)
        row["bluebird"] = res.status
        del pa, pb
    if kind != "none":
        world.instruct(Instruction("A", kind, value))
    truth = {"A": np.zeros((STEPS, 3)), "B": np.zeros((STEPS, 3))}
    for j in range(STEPS):
        world.step(5)
        for u in world.views():
            truth[u.cs][j] = (u.x, u.y, u.alt_ft)
    row["truth"] = conflict(truth["A"], truth["B"])
    return row


def summarise(rows: list[dict]) -> dict:
    truth = np.array([r["truth"] for r in rows])
    out = {"encounters": len(rows), "losses_of_separation": int(truth.sum())}

    def score(pred: np.ndarray) -> dict:
        return {"caught": int((pred & truth).sum()), "missed": int((~pred & truth).sum()),
                "false_alarms": int((pred & ~truth).sum()), "correct_all_clear": int((~pred & ~truth).sum())}

    out["old look-ahead"] = score(np.array([r["old"] for r in rows]))
    out["model, typical pilot"] = score(np.array([r["typical"] for r in rows]))
    p = np.array([r["p_conflict"] for r in rows])
    for cut in (0.1, 0.25, 0.5):
        out[f"model, 32 draws, P >= {cut}"] = score(p >= cut)
    bins = [(0.0, 0.0), (0.0, 0.25), (0.25, 0.75), (0.75, 1.0), (1.0, 1.0)]
    cal = {}
    for lo, hi in bins:
        sel = (p == lo) if lo == hi else ((p > lo) & (p <= hi) if hi < 1.0 else (p > lo) & (p < 1.0))
        if sel.any():
            cal[f"{lo}-{hi}"] = {"encounters": int(sel.sum()), "lost_separation": round(float(truth[sel].mean()), 3)}
    out["model draws: how often separation was lost, by predicted chance"] = cal
    if "bluebird" in rows[0]:
        st = Counter((r["bluebird"], r["truth"]) for r in rows)
        out["bluebird"] = {
            "certified_safe": sum(v for (s, _), v in st.items() if s == "certified_safe"),
            "certified_safe_but_lost_separation": st[("certified_safe", True)],
            "unresolved_and_lost_separation": st[("unresolved", True)],
            "unresolved_but_kept_separation": st[("unresolved", False)],
            "unsupported": sum(v for (s, _), v in st.items() if s == "unsupported"),
            "old_look_ahead_alarm_on_certified_safe": sum(1 for r in rows if r["bluebird"] == "certified_safe" and r["old"]),
            "model_alarm_on_certified_safe": sum(1 for r in rows if r["bluebird"] == "certified_safe" and r["typical"]),
        }
    return out


def main() -> None:
    ap_ = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap_.add_argument("--encounters", type=int, default=500)
    ap_.add_argument("--seed", type=int, default=2026)
    args = ap_.parse_args()
    from approach.ai.env import winds_cached
    from approach.ai.evaluate import test_hours

    try:
        import geometric_safety  # noqa: F401

        have_bluebird = True
    except ImportError:
        have_bluebird = False
        print("geometric_safety is not installed: Bluebird's filter is left out", flush=True)
    ap = egkk.load()
    rng = random.Random(args.seed)
    hours = [h for h in test_hours() if winds_cached(*h)]  # winds already downloaded: no waiting on NOAA
    rows = []
    while len(rows) < args.encounters:
        r = encounter(ap, rng, hours, have_bluebird)
        if r:
            rows.append(r)
        if len(rows) % 50 == 0 and r:
            print(f"{len(rows)} encounters", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {"made": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "seed": args.seed,
               **summarise(rows)}
    (OUT / "lookahead.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
