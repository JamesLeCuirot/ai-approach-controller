"""Test the model of how aircraft respond (approach.response) in the simulator and on real tracks.

    python scripts/response_check.py sim --worlds 60      # simulator: random instructions, measured pilots
    python scripts/response_check.py real                 # real tracks from the test days

Simulator: in each world, eight aircraft far apart at random positions, levels, speeds and headings, in the real
winds of a random test-day hour, with pilots who fly like real arrivals. Each gets one random instruction of the
kinds stage 2's AI can give (or none), and the next two minutes are recorded.

Real tracks: the ADS-B data has no instructions, so the check takes stretches of real flight where an aircraft
makes one turn, one descent or one speed change and ends steady, and asks each model, from the moment the
change starts, to predict the next two minutes given the heading, level and speed the aircraft ended up with.
That tests how aircraft respond, not when controllers speak, so the pilot's response delay is left out.

Each prediction is scored at 30, 60 and 120 s against constant velocity and the safety check's old look-ahead:
the error of its best guess, the CRPS (for the draws: how far they are from the truth, less half how far they
are from each other; for a single guess, its error), and how often the truth is inside the middle 80% of the
draws. Results go to runs/response/.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from approach.airports import egkk  # noqa: E402
from approach.response import (ResponseModel, State, constant_velocity, crps, draw_pilots,  # noqa: E402
                               median_pilot, state_from_views)

HORIZONS = {30: 5, 60: 11, 120: 23}  # seconds ahead: index of the 5 s step
DRAWS = 32
OUT = ROOT / "runs" / "response"
TYPES = ["A320"] * 5 + ["A319"] * 3 + ["A20N", "A21N", "A21N", "B738", "B738", "B38M", "A321", "B772", "B789",
                                       "BCS3", "E190", "A359"]


def _models(ap, runway, s: State, new, gs_e, gs_n, vs, old_lookahead, rng, wind_sd_kt: float = 0.0,
            speed_sd_kt: float = 0.0):
    """Every model's prediction for one aircraft: name -> (draws, steps, 3)."""
    model = ResponseModel(ap, runway)
    return {
        "constant velocity": constant_velocity(s, gs_e, gs_n, vs)[:, 0],
        "old look-ahead": old_lookahead[None],
        "model, typical pilot": model.predict(s, median_pilot(ap.icao, delay_s=new[3]), new[:3])[:, 0],
        "model, 32 draws": model.predict(s, draw_pilots(DRAWS, ap.icao, rng, delay_s=new[4], wind_sd_kt=wind_sd_kt,
                                                        speed_sd_kt=speed_sd_kt), new[:3])[:, 0],
    }


def _score(pred: np.ndarray, truth: np.ndarray) -> dict:
    """Scores at each horizon for one prediction (draws, steps, 3) against the truth (steps, 3)."""
    out = {}
    for sec, k in HORIZONS.items():
        p, tr = pred[:, k], truth[k]
        best = p.mean(axis=0)
        h_err = float(math.hypot(best[0] - tr[0], best[1] - tr[1]))
        v_err = float(abs(best[2] - tr[2]))
        c = crps(p, tr)
        row = {"h_err": h_err, "v_err": v_err, "h_crps": float((c[0] + c[1]) / 2), "v_crps": float(c[2])}
        if len(p) > 1:
            radii = np.hypot(p[:, 0] - best[0], p[:, 1] - best[1])
            row["h_in80"] = bool(h_err <= np.quantile(radii, 0.8) + 0.05)
            lo, hi = np.quantile(p[:, 2], [0.1, 0.9])
            row["v_in80"] = bool(lo - 50 <= tr[2] <= hi + 50)
        out[sec] = row
    return out


def _summary(results: list[dict]) -> dict:
    """Medians and 90th percentiles of the errors, mean CRPS and coverage, by model, kind and horizon."""
    groups = defaultdict(list)
    for r in results:
        for model, by_h in r["scores"].items():
            for sec, row in by_h.items():
                groups[(r["kind"], model, sec)].append((r["dims"], row))
                groups[("all", model, sec)].append((r["dims"], row))
    out = defaultdict(lambda: defaultdict(dict))
    for (kind, model, sec), rows in sorted(groups.items()):
        hr = [x for dims, x in rows if "h" in dims]
        vr = [x for dims, x in rows if "v" in dims]
        cell = {"cases": len(rows)}
        if hr:
            h = np.array([x["h_err"] for x in hr])
            cell.update({"h_cases": len(hr), "h_err_p50_nm": round(float(np.median(h)), 3),
                         "h_err_p90_nm": round(float(np.quantile(h, 0.9)), 3),
                         "h_crps_nm": round(float(np.mean([x["h_crps"] for x in hr])), 3)})
            if "h_in80" in hr[0]:
                cell["h_in_middle_80pct"] = round(float(np.mean([x["h_in80"] for x in hr])), 3)
        if vr:
            v = np.array([x["v_err"] for x in vr])
            cell.update({"v_cases": len(vr), "v_err_p50_ft": round(float(np.median(v))),
                         "v_err_p90_ft": round(float(np.quantile(v, 0.9))),
                         "v_crps_ft": round(float(np.mean([x["v_crps"] for x in vr])))})
            if "v_in80" in vr[0]:
                cell["v_in_middle_80pct"] = round(float(np.mean([x["v_in80"] for x in vr])), 3)
        out[kind][model][f"{sec}s"] = cell
    return out


# ---- simulator ---------------------------------------------------------------------------------------------------

def _random_instruction(rng: random.Random, v) -> tuple[str, float] | None:
    kind = rng.choices(["none", "heading", "altitude", "speed"], [0.15, 0.35, 0.25, 0.25])[0]
    if kind == "heading":
        return kind, (v.target_hdg + rng.choice([5, 10, 20, 30, 45, 90]) * rng.choice([-1, 1])) % 360.0
    if kind == "altitude":
        return kind, float(min(12000, max(3000, v.target_alt + rng.choice([-3000, -2000, -1000, 1000]))))
    if kind == "speed":
        return kind, float(min(250, max(160, v.target_spd + rng.choice([-30, -20, -10, 10, 20]))))
    return None


def sim(worlds: int, seed: int) -> list[dict]:
    from approach.ai.evaluate import test_hours
    from approach.controllers.safety import SafetyCheck
    from approach.data.gfs import fetch_for_airport, parse_time
    from approach.instructions import Instruction
    from approach.sim.world import World

    ap = egkk.load()
    rng = random.Random(seed)
    hours = test_hours()
    results = []
    for w in range(worlds):
        d, h = rng.choice(hours)
        wind = fetch_for_airport(parse_time(f"{d}T{h:02d}"), ap)
        world = World(ap, "26L", seed=rng.randrange(1 << 30), wind=wind, pilots="measured")
        spots = []
        for k in range(8):
            for _ in range(50):
                x, y = rng.uniform(-30, 30), rng.uniform(-28, 28)
                if all(math.hypot(x - a, y - b) > 14 for a, b in spots):
                    break
            spots.append((x, y))
            ll = ap.grid.to_latlon(x, y)
            world.spawn(f"T{k}", rng.choice(TYPES), ll.lat, ll.lon, rng.uniform(0, 360),
                        round(rng.uniform(4000, 11000), -2), round(rng.uniform(180, 250)))
        world.step(20)
        views = world.views()
        before = state_from_views(views)
        safety = SafetyCheck(ap, "26L")
        tracks = safety._tracks(views)
        news = {}
        for i, v in enumerate(views):
            ins = _random_instruction(rng, v)
            if ins is not None:
                news[i] = ins
                world.instruct(Instruction(v.cs, *ins))
                t = tracks[v.cs]
                if ins[0] == "heading":
                    t.target_hdg = ins[1]
                elif ins[0] == "altitude":
                    t.target_alt = ins[1]
        old = safety._predict(tracks)
        truth = np.zeros((len(views), 24, 3))
        for j in range(24):
            world.step(5)
            now = {v.cs: v for v in world.views()}
            for i, v in enumerate(views):
                u = now.get(v.cs)
                if u is not None:
                    truth[i, j] = (u.x, u.y, u.alt_ft)
        for i, v in enumerate(views):
            s1 = State(**{f: getattr(before, f)[i:i + 1] for f in before.__dataclass_fields__})
            kind, value = news.get(i, ("none", 0.0))
            delay = world.pilot_delay_s
            new = (0, kind, value, sum(delay) / 2, delay) if kind != "none" else None
            ge = np.array([v.gs_kt * math.sin(math.radians(v.trk))])
            gn = np.array([v.gs_kt * math.cos(math.radians(v.trk))])
            vs = np.array([v.vs_fpm])
            old_i = np.array([snap[v.cs] for snap in old])
            preds = _models(ap, "26L", s1, new if new else (0, "speed", float(v.target_spd), 4.5, delay), ge, gn, vs,
                            old_i, rng)
            results.append({"world": w, "cs": v.cs, "actype": world.pilots[v.cs].actype if v.cs in world.pilots else "",
                            "kind": kind, "value": value, "dims": "hv",
                            "scores": {m: _score(p, truth[i]) for m, p in preds.items()}})
        print(f"world {w + 1}/{worlds}", flush=True)
    return results


# ---- real tracks -------------------------------------------------------------------------------------------------

def _steady(series: np.ndarray, k0: int, k1: int, limit: float) -> bool:
    return bool(np.all(np.abs(np.gradient(series[k0:k1], 5.0)) < limit)) if k1 - k0 >= 2 else False


def real(max_cases: int, seed: int, split: str = "test", wind_sd_kt: float = 0.0,
         speed_sd_kt: float = 0.0) -> list[dict]:
    from approach.controllers.safety import SafetyCheck, Track
    from approach.data.arrivals import load_day
    from approach.data.response import Winds, _segments, split_days

    ap = egkk.load()
    rng = random.Random(seed)
    winds = Winds(ap.icao)
    safety = SafetyCheck(ap, "26L")
    results = []
    days = split_days(split)
    rng.shuffle(days)
    for d in days:
        day = load_day(date.fromisoformat(d))
        for arr in day["arrivals"]:
            rwy = arr["runway"] if arr["runway"] in ap.runways else "26L"
            safety.runway = ap.runways[rwy]
            hold = arr.get("hold_t")
            for seg in _segments(arr["track"], winds, ap):
                n = len(seg["t"])
                rate = np.gradient(seg["hdg"], 5.0)
                vr = np.nan_to_num(seg["vr"])
                cas = np.convolve(seg["cas"], np.ones(3) / 3, mode="same")
                k = 2
                while k + 26 < n - 1:
                    window = slice(k, k + 25)
                    kind = None
                    if abs(rate[k + 1]) >= 0.6 and abs(rate[k - 1]) < 0.3 and abs(rate[k - 2]) < 0.3:
                        kind = "heading"
                    elif vr[k + 1] < -400 and abs(vr[k - 1]) < 150 and abs(vr[k - 2]) < 150:
                        kind = "altitude"
                    elif abs(cas[k + 2] - cas[k]) >= 3 and _steady(cas, k - 2, k + 1, 0.15):
                        kind = "speed"
                    t0 = seg["t"][k]
                    if (kind is None or seg["alt"][k] < 3000 or (hold and hold[0] - 60 <= t0 <= hold[1] + 60)):
                        k += 1
                        continue
                    end = slice(k + 22, k + 25)
                    # One change, ending steady: the heading, level and speed it ended with are its clearance.
                    turn = rate[window]
                    one_turn = np.all(turn >= -0.3) or np.all(turn <= 0.3)
                    ok = one_turn and _steady(seg["hdg"], k + 21, k + 25, 0.3) and np.all(vr[window] < 300)
                    if not ok:
                        k += 1
                        continue
                    level_end = bool(np.all(np.abs(vr[end]) < 250))
                    talt = float(seg["alt"][k + 24]) if level_end else float(seg["alt"][k + 24] - 3000)
                    spd_end = _steady(cas, k + 21, k + 25, 0.15)
                    tspd = float(cas[k + 24]) if spd_end else float(cas[k + 24] + 30 * np.sign(cas[k + 24] - cas[k]))
                    thdg = float(seg["hdg"][k + 24] % 360.0)
                    wind_e = (seg["gs"][k] * math.sin(math.radians(seg["trk"][k]))
                              - seg["tas"][k] * math.sin(math.radians(seg["hdg"][k])))
                    wind_n = (seg["gs"][k] * math.cos(math.radians(seg["trk"][k]))
                              - seg["tas"][k] * math.cos(math.radians(seg["hdg"][k])))
                    s1 = State(x=np.array([seg["x"][k]]), y=np.array([seg["y"][k]]), alt=np.array([seg["alt"][k]]),
                               cas=np.array([cas[k]]), tas=np.array([seg["tas"][k]]),
                               hdg=np.array([seg["hdg"][k] % 360.0]), wind_e=np.array([wind_e]),
                               wind_n=np.array([wind_n]), target_hdg=np.array([thdg]), target_alt=np.array([talt]),
                               target_spd=np.array([tspd]), approach_kt=np.array([135.0]), cleared=np.array([False]),
                               loc=np.array([False]), gs=np.array([False]), holding=np.array([False]))
                    truth = np.stack([seg["x"][k + 1:k + 25], seg["y"][k + 1:k + 25], seg["alt"][k + 1:k + 25]],
                                     axis=-1)
                    ge = np.array([seg["gs"][k] * math.sin(math.radians(seg["trk"][k]))])
                    gn = np.array([seg["gs"][k] * math.cos(math.radians(seg["trk"][k]))])
                    track = Track("R", seg["x"][k], seg["y"][k], seg["hdg"][k] % 360.0, seg["gs"][k], seg["alt"][k],
                                  thdg, talt, False, False, seg["tas"][k], wind_e, wind_n, False)
                    old = np.array([snap["R"] for snap in safety._predict({"R": track})])
                    # The clearance is in force from the start: no response delay (it isn't in the data).
                    preds = _models(ap, rwy, s1, (0, "speed", tspd, 0.0, (0.0, 0.0)), ge, gn, np.array([vr[k]]), old,
                                    rng, wind_sd_kt, speed_sd_kt)
                    # Score each case on its own change: when the other parts of the clearance were given isn't
                    # known, so the height in a turn (or the track in a descent) can't be a fair test.
                    results.append({"day": d, "cs": arr["callsign"], "actype": arr["type"], "kind": kind,
                                    "dims": "v" if kind == "altitude" else "h",
                                    "t": float(t0), "scores": {m: _score(p, truth) for m, p in preds.items()}})
                    k += 25
                    if len(results) >= max_cases:
                        return results
        print(f"{d}: {len(results)} cases so far", flush=True)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("where", choices=("sim", "real"))
    ap.add_argument("--worlds", type=int, default=60)
    ap.add_argument("--cases", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--split", default="test", help="real: the days to take tracks from (train to set the wind error)")
    ap.add_argument("--wind-sd", type=float, default=0.0, help="real: wind error in each draw, kt per component")
    ap.add_argument("--speed-sd", type=float, default=0.0, help="real: how far off its assigned speed each draw's "
                    "pilot settles, kt")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    results = (sim(args.worlds, args.seed) if args.where == "sim"
               else real(args.cases, args.seed, args.split, args.wind_sd, args.speed_sd))
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {"where": args.where, "seed": args.seed, "cases": len(results),
               **({"split": args.split, "wind_sd_kt": args.wind_sd, "speed_sd_kt": args.speed_sd}
                  if args.where == "real" else {}),
               "made": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "by_kind": _summary(results)}
    (OUT / (args.out or f"{args.where}.json")).write_text(json.dumps(summary, indent=1) + "\n")
    for kind, by_model in summary["by_kind"].items():
        for model, by_h in by_model.items():
            c = by_h["120s"]
            print(f"{kind:9} {model:22} 120s: h {c.get('h_err_p50_nm', '-')} NM (crps {c.get('h_crps_nm', '-')}, "
                  f"in80 {c.get('h_in_middle_80pct', '-')}) | v {c.get('v_err_p50_ft', '-')} ft "
                  f"(crps {c.get('v_crps_ft', '-')}, in80 {c.get('v_in_middle_80pct', '-')})")


if __name__ == "__main__":
    main()
