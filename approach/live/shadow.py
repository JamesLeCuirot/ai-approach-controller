"""Shadow mode: the AI controls today's real Gatwick arrivals in the simulator, in real time, while the real
controllers control the real ones.

Three parts run side by side:

* the recorder (approach.live.feed) polls adsb.lol's live feed every 5 seconds and appends what it sees to a file;
* this runner follows that file. When an aircraft enters the handover region (40 NM, 15,000 ft), the spotter
  (approach.live.spotter) judges whether it is a Gatwick arrival; if so it is handed to the simulator where it
  really is, at its real height, speed and track, as in the benchmark's sessions. The simulator keeps pace with
  the clock, in the hour's GFS forecast winds, and the look-ahead controller (approach.ai.lookahead) flies it, with
  the settings of the latest 120-session test;
* afterwards, ``compare`` finds the real arrivals in the recording (approach.data.arrivals, as for an archived day),
  scores the real controllers on them exactly as the benchmark does, and sets the two side by side, aircraft by
  aircraft, with a radar replay of the AI's hour and the real tracks behind it.

What the AI doesn't get live, and does in the benchmark: the look-ahead's copies there see arrivals due in the next
few minutes (as flight strips would show them); live they see only aircraft already handed over. The runway is the
one the real arrivals are using when it starts, and stays so.

    python -m approach.live.feed --out data/raw/live/egkk/<name>.jsonl --minutes 100 &
    python -m approach.live.shadow run --feed data/raw/live/egkk/<name>.jsonl --minutes 60 --out runs/shadow/<name>
    python -m approach.live.shadow compare --feed data/raw/live/egkk/<name>.jsonl --out runs/shadow/<name>
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from approach import wake
from approach.ai import env as env_module
from approach.ai.lookahead import LookAhead, LookAheadEnv
from approach.airports import egkk
from approach.scenarios import Arrival, Scenario
from approach.session import SessionLog, map_for_replay
from approach.sim.live import LiveSession
from approach.live.spotter import ARRIVAL_P, Entries, Spotter

STEP_S = 10  # the look-ahead decides every step, as in the benchmark
SETTLE_MIN = 40.0  # after the last handover, how long to keep going while the aircraft land
# The planner with the four changes of 18 and 19 November (expedite descent, free_speed="fast", straight in,
# landing in release order; the setting of the second live hour) and slow_room: with the look-ahead, 20.13 minutes per
# arrival on the 48 new training sessions with 2 go-arounds, against 19.76 with 13 without slow_room and the go-around
# fixes of 19 November, and 21.85 with 4 with the settings of the 120-session test on the updated simulator.
PLANNER = {"height_by": "time", "free_speed": "fast", "expedite": True, "straight_in": True, "released_order": True,
           "slow_room": True}
LOOKAHEAD = {"moves": "wide", "time_weight": 0.1, "releases": True, "stack_moves": True}


class ShadowSession(LiveSession):
    """A session whose arrivals are added as they come; it ends only when its time is up."""

    @property
    def done(self) -> bool:
        return self.t > self.scenario.duration_s


class ShadowEnv(LookAheadEnv):
    """The look-ahead's world with no recorded hour behind it: arrivals are added live."""

    def __init__(self, planner: dict | None = None):
        super().__init__(hours=[("2024-01-09", 12)], planner=planner)  # the hours are never drawn from

    def start(self, runway: str, wind, wind_label: str, day: str, duration_s: float, pilot_seed: int, title: str):
        self.scheme = wake.scheme_for(day)
        self.teacher = False
        self.wind_hour = wind_label
        scenario = Scenario(name=f"shadow_{day}", title=title, runway=runway, arrivals=[], duration_s=duration_s)
        self.controller, in_session = self._make_controller(runway)
        log = SessionLog(meta={"title": title, "scenario": scenario.name, "runway": runway, "wind": wind_label,
                               "controller": in_session.name, "wake_scheme": self.scheme},
                         map=map_for_replay(self.airport, runway, egkk.DISPLAY_FIXES))
        with wake.scheme(self.scheme):
            self.session = ShadowSession(self.airport, scenario, in_session, log, seed=pilot_seed, wind=wind,
                                         frames=True, pilots=self.pilots, lookahead=self.lookahead)
        self.in_conflict, self.wake_flagged, self.gaps = set(), set(), []
        self.counts = {"landed": 0, "go_arounds": 0, "separation_losses": 0, "losses_at_stack": 0,
                       "losses_out_of_reach": 0, "wake_infringements": 0, "instructions": 0, "blocked": 0,
                       "decisions": 0, "base_turns": 0, "turns_held": 0}
        with wake.scheme(self.scheme):
            return self._observe()

    def add_arrival(self, callsign: str, actype: str, state: dict, at_s: float) -> Arrival:
        """Hand an arrival to the simulator at its real state (lat, lon, alt, gs, track) at session time at_s."""
        x, y = self.airport.grid.to_xy(state["lat"], state["lon"])
        fix = min(self.airport.holds, key=lambda k: math.hypot(*(u - v for u, v in zip(
            self.airport.xy(self.airport.holds[k].position), (x, y)))))
        s = self.session
        a = Arrival(callsign=callsign, actype=actype, fix=fix, release_s=max(float(at_s), float(s.t)),
                    level_ft=int(round(state["alt"] / 100.0) * 100), speed_kt=0, lat=state["lat"], lon=state["lon"],
                    track=state["track"], gs_kt=state["gs"])
        later = s.pending[s.released:]  # keep the ones still to come in time order
        k = bisect.bisect_right([p.release_s for p in later], a.release_s)
        s.pending.insert(s.released + k, a)
        return a


def runway_from_feed(airport, points: list[list]) -> str | None:
    """The runway most of the low aircraft lined up on a final are using."""
    votes: dict[str, int] = {}
    for p in points:
        if p[3] in (None, "ground") or p[3] > 3000 or p[5] is None:
            continue
        x, y = airport.grid.to_xy(p[1], p[2])
        for name, rwy in airport.runways.items():
            along, lateral = airport.runway_frame(name, x, y)
            if 0 < along < 12 and abs(lateral) < 0.6 and abs((p[5] - rwy.course_true + 180) % 360 - 180) < 25:
                votes[name] = votes.get(name, 0) + 1
    return max(votes, key=votes.get) if votes else None


class Tail:
    """Follows the recorder's file as it grows."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.pos = 0
        self.part = ""

    def lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            f.seek(self.pos)
            chunk = f.read()
            self.pos = f.tell()
        text = self.part + chunk
        rows = text.split("\n")
        self.part = rows.pop()  # an unfinished last line waits for the rest
        return [json.loads(r) for r in rows if r.strip()]


def run(feed: Path, out: Path, minutes: float, copy_workers: int = 2, seed: int = 1, runway: str | None = None,
        settle_min: float = SETTLE_MIN, log=print, planner: dict | None = None) -> dict:
    """``planner``: the planner's settings, PLANNER (those of the latest 120-session test) if not given."""
    from approach.data.gfs import fetch_for_airport

    planner = PLANNER if planner is None else planner

    env_module.MAX_SKIP = 0  # keep to the clock: never skip ahead through a quiet spell
    airport = egkk.load()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    tail, entries, spotter = Tail(feed), Entries(airport), Spotter()

    # The runway: wait up to 3 minutes for aircraft on a final.
    t_wait, seen = time.time(), []
    while runway is None:
        for row in tail.lines():
            seen += [p for _, _, _, p in row["new"]]
        runway = runway_from_feed(airport, seen)
        if runway is None and time.time() - t_wait > 180:
            runway = "26L"
            log("no aircraft seen on a final in 3 minutes: runway 26L")
        elif runway is None:
            time.sleep(5)
    tail = Tail(feed)  # from the start again: entries while waiting count too

    t0 = float(math.floor(time.time()))
    now_utc = datetime.fromtimestamp(t0, tz=timezone.utc)
    wind = fetch_for_airport(now_utc, airport, earlier_runs=3)
    day = now_utc.date().isoformat()
    window_end = t0 + minutes * 60
    title = f"Shadow mode: the AI on live Gatwick arrivals, {now_utc:%Y-%m-%d %H:%M} UTC"
    env = ShadowEnv(planner=planner)
    obs = env.start(runway, wind, wind.source, day, (minutes + settle_min) * 60, seed, title)
    la = LookAhead(workers=copy_workers, seed=seed, **LOOKAHEAD)
    la.reset()
    log(f"{title}; runway {runway}; winds {wind.source}; planner {json.dumps(planner)}")

    judged: list[dict] = []
    names: dict[str, int] = {}
    total, behind_max, finished = 0.0, 0.0, False
    while True:
        now = time.time()
        for row in tail.lines():
            for hexid, actype, reg, p in row["new"]:
                got = entries.add(hexid, p)
                if not got or not (t0 <= got[0] < window_end):
                    continue
                chance = float(spotter.chance([got[1]])[0])
                cs = p[7] or hexid.upper()
                rec = {"hex": hexid, "callsign": cs, "type": actype, "entered_t": got[0], "t": p[0],
                       "lat": p[1], "lon": p[2], "alt": p[3], "gs": p[4], "track": p[5], "vrate": p[6],
                       "chance": round(chance, 3), "handed_over": chance >= ARRIVAL_P}
                if rec["handed_over"]:
                    names[cs] = names.get(cs, 0) + 1
                    rec["sim_callsign"] = cs if names[cs] == 1 else f"{cs}_{names[cs]}"
                    env.add_arrival(rec["sim_callsign"], actype or "A320", rec, p[0] - t0)
                    log(f"{datetime.fromtimestamp(p[0], tz=timezone.utc):%H:%M:%S} {cs} ({actype}) handed over, "
                        f"{p[3]} ft, {chance:.0%} sure it is a Gatwick arrival")
                judged.append(rec)
        s = env.session
        if s.t + STEP_S <= now - t0:  # the simulator is behind the clock: catch up
            behind_max = max(behind_max, now - t0 - s.t)
            act = la.choose(env, obs)
            obs, r, done, _, info = env.step(act)
            total += float(r)
            if done:
                finished = True
                break
            continue
        if now > window_end and s.released == len(s.pending) and not s.world.pilots:
            break  # every aircraft handed over has landed
        time.sleep(0.5)

    sim_log = env.session.log if finished else env.session.finish()
    sim_log.meta.update({"t0_epoch": t0, "window_s": minutes * 60, "source": "adsb.lol (https://www.adsb.lol), ODbL 1.0",
                         "winds": wind.source, "most_behind_clock_s": round(behind_max)})
    sim_log.save(out / "shadow_session.json")
    summary = {"t0_epoch": t0, "start_utc": f"{now_utc:%Y-%m-%d %H:%M:%S}", "minutes": minutes, "runway": runway,
               "winds": wind.source, "handed_over": sum(r["handed_over"] for r in judged), "judged": len(judged),
               "counts": env.counts, "scores": sim_log.scores, "lookahead": {k: v for k, v in la.stats.items()},
               "most_behind_clock_s": round(behind_max), "planner": planner, "lookahead_settings": LOOKAHEAD}
    (out / "judged.json").write_text(json.dumps(judged, indent=1))
    (out / "run.json").write_text(json.dumps(summary, indent=1, default=float))
    log(f"done: {summary['handed_over']} handed over, {env.counts['landed']} landed, "
        f"{env.counts['separation_losses']} losses of separation, {env.counts['go_arounds']} go-arounds; "
        f"at most {behind_max:.0f} s behind the clock")
    return summary


def compare(feed: Path, out: Path, log=print) -> dict:
    """The real controllers on the same arrivals, from the recording, and the AI's hour beside them."""
    from approach import scoring
    from approach.data import real_sessions
    from approach.data.arrivals import HANDOVER_CEILING_FT, HANDOVER_RADIUS_NM, find_arrivals
    from approach.live.feed import read_area
    from approach.radar import replay

    airport = egkk.load()
    out = Path(out)
    run_info = json.loads((out / "run.json").read_text())
    judged = json.loads((out / "judged.json").read_text())
    t0, window = run_info["t0_epoch"], run_info["minutes"] * 60
    area = read_area(feed, airport.reference.lat, airport.reference.lon)
    arrivals = find_arrivals(area, airport)
    try:
        from approach.data.metar import qnh_by_hour

        qnh = qnh_by_hour(datetime.fromtimestamp(t0, tz=timezone.utc).date(), airport.icao)
    except Exception:  # noqa: BLE001
        qnh = {}
    day = {"day": area["day"], "airport": airport.icao, "source": area["source"], "licence": "ODbL 1.0",
           "instance": "live", "handover": {"radius_nm": HANDOVER_RADIUS_NM, "ceiling_ft": HANDOVER_CEILING_FT},
           "qnh_hpa_by_hour": {str(h): q for h, q in sorted(qnh.items())}, "arrivals": arrivals}
    real_log = real_sessions.real_session(day, airport, t0, t0 + window,
                                          title=f"Real controllers, live: Gatwick {run_info['start_utc']} UTC")
    real_log.save(out / "real_session.json")

    sim_log = SessionLog.load(out / "shadow_session.json")
    sim_log.ghosts = real_sessions.ghosts(day, airport, t0, t0 + window)
    replay.write(sim_log, out / "replay.html")

    landed_sim = {e["cs"]: e["t"] for e in sim_log.events if e["kind"] == "landed"}
    real_by_hex: dict[str, list[dict]] = {}
    for a in real_sessions.window_arrivals(day, t0, t0 + window):
        real_by_hex.setdefault(a["hex"], []).append(a)
    rows, matched = [], set()
    for r in judged:
        real = next((a for a in real_by_hex.get(r["hex"], []) if abs(a["handover"]["t"] - r["entered_t"]) < 120), None)
        if real:
            matched.add((real["hex"], real["handover"]["t"]))
        sim_landed = landed_sim.get(r.get("sim_callsign", ""))
        rows.append({
            "callsign": r["callsign"], "type": r["type"], "chance": r["chance"], "handed_over": r["handed_over"],
            "real_arrival": bool(real),
            "real_minutes": round((real["landed_t"] - r["t"]) / 60.0, 1) if real else None,
            "ai_minutes": round((sim_landed + t0 - r["t"]) / 60.0, 1) if sim_landed is not None else None,
            "real_held_min": real["hold_minutes"] if real else None,
        })
    missed = [a for hs in real_by_hex.values() for a in hs if (a["hex"], a["handover"]["t"]) not in matched]
    both = [x for x in rows if x["handed_over"] and x["real_arrival"] and x["ai_minutes"] is not None]
    result = {
        "start_utc": run_info["start_utc"], "minutes": run_info["minutes"], "runway": run_info["runway"],
        "real_arrivals": sum(len(v) for v in real_by_hex.values()),
        "handed_over": sum(x["handed_over"] for x in rows),
        "handed_over_real_arrivals": sum(x["handed_over"] and x["real_arrival"] for x in rows),
        "handed_over_not_arrivals": [x["callsign"] for x in rows if x["handed_over"] and not x["real_arrival"]],
        "real_arrivals_missed": [a["callsign"] for a in missed],
        "both_landed": len(both),
        "ai_minutes_per_arrival": round(float(np.mean([x["ai_minutes"] for x in both])), 2) if both else None,
        "real_minutes_per_arrival": round(float(np.mean([x["real_minutes"] for x in both])), 2) if both else None,
        "ai": {k: sim_log.scores.get(k) for k in ("arrivals", "landed", "separation_losses", "go_arounds",
                                                    "wake_violations", "minutes_per_arrival_all")},
        "real": {k: real_log.scores.get(k) for k in ("arrivals", "landed", "separation_losses", "go_arounds",
                                                      "wake_violations", "minutes_per_arrival_all")},
        "ai_counts": run_info["counts"], "aircraft": rows,
        "labels": scoring.LABELS,
    }
    (out / "compare.json").write_text(json.dumps(result, indent=1, default=float))
    log(json.dumps({k: v for k, v in result.items() if k not in ("aircraft", "labels")}, indent=1, default=float))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("run", "compare"))
    ap.add_argument("--feed", required=True, help="the recorder's file (approach.live.feed)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--minutes", type=float, default=60.0, help="hand over arrivals for this long")
    ap.add_argument("--copy-workers", type=int, default=2)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--runway", default=None)
    ap.add_argument("--settle", type=float, default=SETTLE_MIN, help="minutes to keep going after the last handover")
    ap.add_argument("--planner", default=None, help="the planner's settings as JSON (default: PLANNER)")
    args = ap.parse_args()
    if args.what == "run":
        run(Path(args.feed), Path(args.out), args.minutes, args.copy_workers, args.seed, args.runway, args.settle,
            planner=json.loads(args.planner) if args.planner else None)
    else:
        compare(Path(args.feed), Path(args.out))


if __name__ == "__main__":
    main()
