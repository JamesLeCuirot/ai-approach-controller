"""Stage 2: the AI's network learns to make the look-ahead's moves.

The look-ahead controller (approach.ai.lookahead, moves="wide") finds each move by trying it in copies of the
simulator, about 10 seconds a look. Practice alone never taught the AI's network a change worth making (runs
stage2-01 to stage2-03 kept "as planned" everywhere), so here it copies the look-ahead first: its head start is
the look-ahead's decisions, gathered on training hours, and practice can build on that.

What it sees (observe_copy), one row per aircraft:

* each aircraft in the landing sequence (up to N_MAX), as stage 2's AI sees it (approach.ai.vector), plus its
  height and cleared level, how far its downwind can still be extended, and how long since each of its
  adjustments was made;
* each aircraft in or on its way to a stack (up to N_STACK), which the look-ahead can stop at a level: which
  stack, whether it is holding, its height, cleared level and the planner's stack level for it, its place in the
  queue, how far it is from the fix, whether a level of the look-ahead's is in force and since when, and the
  traffic around it (the nearest aircraft above and below within VACATE_NM, whether its cleared level is taken
  there, and whether another aircraft is between it and that level).

What it chooses: one move per look, every LOOK_EVERY_S, as the look-ahead does (SEQ_MOVES, STACK_MOVES): nothing;
for an aircraft in the sequence, a heading, level or speed adjustment, one of those back to as planned, or 1 or
2 NM more downwind; for an aircraft at a stack, back to the planner's levels, stop where it is, or 1,000 ft above
or below that. Moves that can't be made now are left out (valid_moves), as the look-ahead leaves them out,
including taking back a move made less than MIN_KEEP_S ago. The look-ahead only moves aircraft that run into
trouble in its copies; the copy has no copies, so it judges that from what it sees.

    PYTHONPATH=. python -m approach.ai.imitate collect --sessions 96 --workers 4 --out runs/imitate/data/round0
    PYTHONPATH=. python -m approach.ai.imitate fit --data runs/imitate/data/round0 --out runs/imitate/copy-01
    PYTHONPATH=. python -m approach.ai.lookahead --copy runs/imitate/copy-01/model.zip --train --sessions 12 ...

With --driver, collect flies a copy and records what the look-ahead would have done at each look instead (DAgger:
a copy's mistakes take it where the look-ahead never went, and the look-ahead's answers there teach it what to do).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import time
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from stable_baselines3.common.policies import ActorCriticPolicy
from torch import nn

from approach.ai.lookahead import (EXTEND_NM, LOOK_EVERY_S, MIN_KEEP_S, REVERTS, LookAhead, LookAheadEnv, apply_move,
                                   stack_hold_level)
from approach.ai.vector import FEATURES_2, N_MAX, OFFSETS, PLANNED
from approach.controllers.rules import STACK_BASE_FT, STACKS, VACATE_NM
from approach.geo import distance_xy

N_STACK = 12  # aircraft in or on their way to a stack the copy can see and act on at once
N_ROWS = N_MAX + N_STACK
EXTRA = ("alt_k", "cleared_k", "vs_k", "room_to_extend_nm", "since_heading_change", "since_level_change",
         "since_speed_change")
STACK_FEATURES = (
    "stack_row", "at_willo", "at_timba", "holding", "below_stack", "stack_level_k", "to_cleared_k", "queue_place",
    "next_out", "in_stack", "to_fix_nm", "held", "held_vs_alt_k", "since_hold", "above_nearest_k", "below_nearest_k",
    "cleared_level_taken", "crossing",
)
COPY_FEATURES = FEATURES_2 + EXTRA + STACK_FEATURES
COL = {f: k for k, f in enumerate(COPY_FEATURES)}
CAN = [FEATURES_2.index(f) for f in ("can_heading", "can_level", "can_speed")]
SINCE = ("since_heading_change", "since_level_change", "since_speed_change")

# The moves for one aircraft in the sequence and for one at a stack, as (kind, *arguments) of approach.ai.lookahead's
# moves without the callsign. Choice 0 of the whole action is "nothing".
SEQ_MOVES = tuple(m for b in range(3) for m in
                  [("set", b, PLANNED + k) for k in range(1, len(OFFSETS[b]))] + [("set_planned", b, PLANNED)]
                  ) + tuple(("extend", nm) for nm in EXTEND_NM)
STACK_MOVES = (("release",), ("hold_level", 0), ("hold_level", 1), ("hold_level", -1))
STACK_START = 1 + N_MAX * len(SEQ_MOVES)
N_ACTIONS = STACK_START + N_STACK * len(STACK_MOVES)


# ---- what the copy sees and can do ------------------------------------------------------------------------------
def stack_rows(env) -> list[str]:
    """The aircraft in or on their way to a stack, by stack and then height (the first N_STACK)."""
    rules = env.vector.rules
    views = {v.cs: v for v in env.session.world.views()}
    inbound = [p for p in rules.plans.values() if p.stage == "inbound" and p.cs in views]
    inbound.sort(key=lambda p: (STACKS.index(p.hold_fix) if p.hold_fix in STACKS else len(STACKS),
                                views[p.cs].alt_ft))
    return [p.cs for p in inbound]


def observe_copy(env, obs: np.ndarray, made: dict, t: float) -> tuple[np.ndarray, tuple[list[str], list[str]]]:
    """The copy's picture (N_ROWS x COPY_FEATURES) and its rows' aircraft: (the sequence, the stacks)."""
    ctl = env.vector
    rules = ctl.rules
    views = {v.cs: v for v in env.session.world.views()}
    x = np.zeros((N_ROWS, len(COPY_FEATURES)), dtype=np.float32)
    x[:N_MAX, :len(FEATURES_2)] = obs
    seq = list(env.slots[:N_MAX])
    for r, cs in enumerate(seq):
        v, p = views[cs], rules.plans.get(cs)
        x[r, COL["alt_k"]] = v.alt_ft / 10000.0
        x[r, COL["cleared_k"]] = v.target_alt / 10000.0
        x[r, COL["vs_k"]] = v.vs_fpm / 2000.0
        if p is not None and p.stage == "downwind":
            x[r, COL["room_to_extend_nm"]] = max(rules.max_join(p.side) - p.d_join, 0.0) / 2.0
        for b, f in enumerate(SINCE):
            x[r, COL[f]] = min(t - made.get((cs, b), -1e9), MIN_KEEP_S) / MIN_KEEP_S
    stack = stack_rows(env)[:N_STACK]
    queues = {fix: [q.cs for q in rules._stack(fix, views)] for fix in STACKS}
    for j, cs in enumerate(stack):
        v, p, r = views[cs], rules.plans[cs], N_MAX + j
        fx, fy = rules.airport.xy(rules.airport.holds[p.hold_fix].position)
        queue = queues.get(p.hold_fix, [])
        place = queue.index(cs) if cs in queue else len(queue)
        near = [w for w in views.values() if w.cs != cs and distance_xy(v.x, v.y, w.x, w.y) < VACATE_NM]
        above = [w.alt_ft - v.alt_ft for w in near if w.alt_ft >= v.alt_ft]
        below = [v.alt_ft - w.alt_ft for w in near if w.alt_ft < v.alt_ft]
        lo, hi = sorted((v.alt_ft, v.target_alt))
        held = ctl.held_level.get(cs)
        row = {
            "present": 1.0, "gs_kt": v.gs_kt / 250.0, "along_nm": v.along / 30.0, "lateral_nm": v.lateral / 10.0,
            "alt_k": v.alt_ft / 10000.0, "cleared_k": v.target_alt / 10000.0, "vs_k": v.vs_fpm / 2000.0,
            "stack_row": 1.0, "at_willo": float(p.hold_fix == "WILLO"), "at_timba": float(p.hold_fix == "TIMBA"),
            "holding": float(p.holding), "below_stack": float(p.below_stack), "stack_level_k": p.stack_level / 10000.0,
            "to_cleared_k": (v.target_alt - v.alt_ft) / 3000.0, "queue_place": place / 8.0,
            "next_out": float(place == 0), "in_stack": len(queue) / 8.0, "to_fix_nm": distance_xy(v.x, v.y, fx, fy) / 20.0,
            "held": float(held is not None), "held_vs_alt_k": (held - v.alt_ft) / 1000.0 if held is not None else 0.0,
            "since_hold": min(t - made.get((cs, "stack"), -1e9), MIN_KEEP_S) / MIN_KEEP_S,
            "above_nearest_k": min(above, default=3000.0) / 3000.0, "below_nearest_k": min(below, default=3000.0) / 3000.0,
            "cleared_level_taken": float(any(abs(w.alt_ft - v.target_alt) < 1000 for w in near)),
            "crossing": float(hi - lo > 300 and any(lo + 300 < w.alt_ft < hi - 300 for w in near)),
        }
        for f, val in row.items():
            x[r, COL[f]] = val
    return np.clip(x, -5.0, 5.0), (seq, stack)


def move_of(index: int, rows: tuple[list[str], list[str]]) -> tuple | None:
    """The look-ahead move (approach.ai.lookahead.apply_move) an action index stands for, or None for nothing."""
    seq, stack = rows
    if index <= 0:
        return None
    if index < STACK_START:
        r, m = divmod(index - 1, len(SEQ_MOVES))
        return (SEQ_MOVES[m][0], seq[r], *SEQ_MOVES[m][1:]) if r < len(seq) else None
    r, m = divmod(index - STACK_START, len(STACK_MOVES))
    return (STACK_MOVES[m][0], stack[r], *STACK_MOVES[m][1:]) if r < len(stack) else None


def index_of(move: tuple | None, rows: tuple[list[str], list[str]]) -> int | None:
    """The action index of a look-ahead move, or None if its aircraft has no row."""
    if move is None:
        return 0
    seq, stack = rows
    kind, cs, *rest = move
    if kind in ("hold_level", "release"):
        key = (kind, *(rest or [0])) if kind == "hold_level" else (kind,)
        return STACK_START + stack.index(cs) * len(STACK_MOVES) + STACK_MOVES.index(key) if cs in stack else None
    return 1 + seq.index(cs) * len(SEQ_MOVES) + SEQ_MOVES.index((kind, *rest)) if cs in seq else None


def valid_moves(env, obs: np.ndarray, rows: tuple[list[str], list[str]], made: dict, t: float) -> np.ndarray:
    """Which actions can be made now: the look-ahead's own rules (LookAhead.candidates, moves="wide"), for every
    aircraft rather than only those in trouble in its copies."""
    ctl = env.vector
    rules = ctl.rules
    seq, stack = rows
    mask = np.zeros(N_ACTIONS, dtype=bool)
    mask[0] = True
    for r, cs in enumerate(seq):
        now, p = ctl.offsets.get(cs, (0, 0, 0)), rules.plans.get(cs)
        for m, (kind, *rest) in enumerate(SEQ_MOVES):
            if kind == "extend":
                ok = p is not None and p.stage == "downwind" and p.d_join + rest[0] <= rules.max_join(p.side) + 1e-6
            elif obs[r, CAN[rest[0]]] < 0.5:
                ok = False
            elif kind == "set":
                ok = rest[1] - PLANNED != now[rest[0]]
            else:
                ok = now[rest[0]] != 0 and t - made.get((cs, rest[0]), -1e9) >= MIN_KEEP_S
            mask[1 + r * len(SEQ_MOVES) + m] = ok
    views = {v.cs: v for v in env.session.world.views()}
    for r, cs in enumerate(stack):
        held = cs in ctl.held_level
        for m, (kind, *rest) in enumerate(STACK_MOVES):
            if kind == "release":
                ok = held and t - made.get((cs, "stack"), -1e9) >= MIN_KEEP_S
            else:
                step = rest[0]
                ok = ((step != 0 or not held) and (step >= 0 or views[cs].target_alt - 1000 >= STACK_BASE_FT)
                      and stack_hold_level(views[cs], step) is not None)
            mask[STACK_START + r * len(STACK_MOVES) + m] = ok
    return mask


def note_made(made: dict, move: tuple | None, t: float) -> None:
    """When a move was made, so it isn't taken back for MIN_KEEP_S (as LookAhead._look records it)."""
    if move is None:
        return
    if move[0] in ("set", "set_planned"):
        made[(move[1], move[2])] = t
    elif move[0] == "hold_level":
        made[(move[1], "stack")] = t


# ---- the network --------------------------------------------------------------------------------------------------
def _constant_lr(_progress: float) -> float:
    return 1e-3


COPY_POLICY_KWARGS = dict(features_extractor_kwargs=dict(dim=64, heads=4, layers=2), net_arch=dict(pi=[], vf=[128]))


class RowHead(nn.Module):
    """Each row's moves from that row's place in the attention's output (the same weights for every row of a
    kind), and "nothing" from the average over the aircraft present."""

    def __init__(self, dim: int, hidden: int = 64):
        super().__init__()
        self.dim = dim
        self.seq = nn.Sequential(nn.Linear(dim, hidden), nn.ReLU(), nn.Linear(hidden, len(SEQ_MOVES)))
        self.stack = nn.Sequential(nn.Linear(dim, hidden), nn.ReLU(), nn.Linear(hidden, len(STACK_MOVES)))
        self.nothing = nn.Sequential(nn.Linear(dim, hidden), nn.ReLU(), nn.Linear(hidden, 1))

    def forward(self, latent: torch.Tensor) -> torch.Tensor:
        h = latent.view(latent.shape[0], N_ROWS, self.dim)
        present = (h.abs().sum(dim=-1, keepdim=True) > 0).float()  # empty rows come out of the attention as 0
        pooled = (h * present).sum(dim=1) / present.sum(dim=1).clamp(min=1.0)
        return torch.cat([self.nothing(pooled), self.seq(h[:, :N_MAX]).flatten(1),
                          self.stack(h[:, N_MAX:]).flatten(1)], dim=1)


class CopyPolicy(ActorCriticPolicy):
    """Stable-Baselines3's actor and critic over the copy's rows, with one choice among N_ACTIONS."""

    def _build(self, lr_schedule) -> None:
        super()._build(lr_schedule)
        self.action_net = RowHead(self.features_extractor_kwargs["dim"])
        self.optimizer = self.optimizer_class(self.parameters(), lr=lr_schedule(1), **self.optimizer_kwargs)


def make_policy():
    from gymnasium import spaces

    from approach.ai.policy import AircraftAttention

    obs_space = spaces.Box(-5.0, 5.0, shape=(N_ROWS, len(COPY_FEATURES)), dtype=np.float32)
    return CopyPolicy(obs_space, spaces.Discrete(N_ACTIONS), _constant_lr, features_extractor_class=AircraftAttention,
                      **COPY_POLICY_KWARGS)


def load_copy(path: str):
    torch.set_num_threads(1)
    policy = CopyPolicy.load(path, device="cpu")
    policy.set_training_mode(False)
    return policy


def masked_logits(policy, x, mask):
    """Log-probabilities over the actions, with those that can't be made now left out."""
    logits = policy.get_distribution(x).distribution.logits
    return torch.log_softmax(logits.masked_fill(~mask, -1e9), dim=-1)


class CopyDriver:
    """Flies the copy: at each look, the move it rates best among those that can be made now (or nothing)."""

    def __init__(self, policy):
        self.policy = policy
        self.stats = Counter()
        self.changes: list[dict] = []
        self.reset()

    def reset(self) -> None:
        self.last_look = -1e9
        self.made: dict[tuple, float] = {}

    def pick(self, x: np.ndarray, mask: np.ndarray, rows) -> tuple | None:
        with torch.no_grad():
            lp = masked_logits(self.policy, torch.as_tensor(x[None]), torch.as_tensor(mask[None]))[0]
        return move_of(int(lp.argmax()), rows)

    def choose(self, env, obs: np.ndarray) -> np.ndarray:
        t = env.session.t
        if t - self.last_look < LOOK_EVERY_S:
            return apply_move(env, None)
        self.last_look = t
        x, rows = observe_copy(env, obs, self.made, t)
        mask = valid_moves(env, obs, rows, self.made, t)
        if not mask[1:].any():
            self.stats["nothing_to_adjust"] += 1
            return apply_move(env, None)
        t0 = time.time()
        move = self.pick(x, mask, rows)
        self.stats["look_seconds"] += time.time() - t0
        self.stats["looks"] += 1
        if move is not None:
            self.stats[move[0]] += 1
            self.stats["back_to_plan" if move[0] in REVERTS else "changes"] += 1
            self.changes.append({"t": round(t), "move": move[0], "aircraft": move[1], "args": list(move[2:])})
            note_made(self.made, move, t)
        return apply_move(env, move)


# ---- gathering the look-ahead's decisions -------------------------------------------------------------------------
def collect_session(env, options: dict, la: LookAhead, driver: CopyDriver | None = None, beta: float = 0.0,
                    rng: random.Random | None = None) -> tuple[dict, dict, Counter]:
    """Fly one session, recording at each look the copy's picture, the moves that could be made and the
    look-ahead's move. The look-ahead's moves are made, or with a driver the copy's (the look-ahead's with
    probability beta)."""
    from approach.ai.train import episode_row

    obs, _ = env.reset(options=options)
    la.reset()
    keep = np.zeros(3 * N_MAX, dtype=np.int64)
    xs, masks, labels, problems, ts = [], [], [], [], []
    counts = Counter()
    last_look, done, total = -1e9, False, 0.0
    while not done:
        act, t = keep, env.session.t
        if t - last_look >= LOOK_EVERY_S:
            last_look = t
            x, rows = observe_copy(env, obs, la.made, t)
            mask = valid_moves(env, obs, rows, la.made, t)
            if mask[1:].any():
                counts["looks"] += 1
                counts["stack_overflow"] += len(stack_rows(env)) > N_STACK
                made_before, problems_before = dict(la.made), la.stats["looks_with_a_problem"]
                t0 = time.time()
                teacher = la._look(env, obs, t)
                la.stats["look_seconds"] += time.time() - t0
                label = index_of(teacher, rows)
                if label is None or not mask[label]:
                    counts["label_without_row" if label is None else "label_not_valid"] += 1
                else:
                    xs.append(x.astype(np.float16))
                    masks.append(mask)
                    labels.append(label)
                    problems.append(la.stats["looks_with_a_problem"] > problems_before)
                    ts.append(t)
                    counts["teacher_moves"] += label != 0
                move = teacher
                if driver is not None and (rng is None or rng.random() >= beta):
                    la.made = made_before
                    move = driver.pick(x, mask, rows)
                    note_made(la.made, move, t)
                    counts["same_as_teacher"] += move == teacher
                counts["moves_made"] += move is not None
                act = apply_move(env, move)
        obs, r, done, _, info = env.step(act)
        total += float(r)
    data = {"x": np.array(xs, dtype=np.float16).reshape(-1, N_ROWS, len(COPY_FEATURES)),
            "mask": np.packbits(np.array(masks, dtype=bool).reshape(-1, N_ACTIONS), axis=1),
            "label": np.array(labels, dtype=np.int16), "problem": np.array(problems, dtype=bool),
            "t": np.array(ts, dtype=np.float32)}
    return episode_row(info, total), data, counts


def _collect(args: tuple) -> list[dict]:
    sessions, out, driver_path, beta, window_s = args
    env = LookAheadEnv(hours=[s["hour"] for s in sessions], window_s=window_s)
    policy = load_copy(driver_path) if driver_path else None
    rows = []
    for s in sessions:
        path = Path(out) / f"session_{s['id']:03d}.npz"
        if path.is_file():
            continue
        options = {"hour": s["hour"], "offset_s": s["offset_s"], "pilot_seed": s["pilot_seed"]}
        la = LookAhead(workers=1, seed=s["pilot_seed"], moves="wide")
        driver = CopyDriver(policy) if policy is not None else None
        t0 = time.time()
        try:
            row, data, counts = collect_session(env, options, la, driver, beta, random.Random(s["pilot_seed"]))
        except Exception as e:  # noqa: BLE001
            print(f"session {s['id']} {s['hour']} skipped: {e!r}", flush=True)
            continue
        np.savez_compressed(path, **data)
        meta = {"session": s["id"], "hour": f"{s['hour'][0]}T{s['hour'][1]:02d}", "offset_min": round(s["offset_s"] / 60),
                **row, "minutes": round((time.time() - t0) / 60, 1), **counts}
        path.with_suffix(".json").write_text(json.dumps(meta))
        rows.append(meta)
        print(f"session {s['id']}: {counts['looks']} looks, {counts['teacher_moves']} look-ahead moves, "
              f"{counts['moves_made']} made; {row['separation_losses']} losses of separation, {row['go_arounds']} "
              f"go-arounds, {row['gaps_inside_minimum']} gaps inside the minimum; {meta['minutes']} min", flush=True)
    return rows


def collect_main(args) -> None:
    from multiprocessing import Pool

    from approach.ai.env import training_hours

    rng = random.Random(args.seed)
    hours = training_hours()
    sessions = [{"id": k, "hour": rng.choice(hours), "offset_s": rng.uniform(0, 3600), "pilot_seed": rng.randrange(1 << 30)}
                for k in range(args.first + args.sessions)][args.first:]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "collect.json").write_text(json.dumps({**vars(args), "hours": "train"}, indent=2))
    chunks = [(sessions[k::args.workers], str(out), args.driver, args.beta, args.window) for k in range(args.workers)]
    with Pool(args.workers) as pool:
        pool.map(_collect, chunks)
    metas = [json.loads(p.read_text()) for p in sorted(out.glob("session_*.json"))]
    if metas:
        with (out / "sessions.csv").open("w", newline="") as f:
            fields = list(dict.fromkeys(k for m in metas for k in m))
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(metas)
    print(f"{len(metas)} sessions in {out}", flush=True)


# ---- the head start: copying the look-ahead -----------------------------------------------------------------------
def load_data(dirs: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Every recorded look in these folders: pictures, valid moves, the look-ahead's moves, whether its copies
    showed a problem, and the session of each."""
    xs, ms, ys, ps, ss = [], [], [], [], []
    for k, d in enumerate(dirs):
        for path in sorted(Path(d).glob("session_*.npz")):
            z = np.load(path)
            n = len(z["label"])
            if n == 0:
                continue
            xs.append(z["x"])
            ms.append(np.unpackbits(z["mask"], axis=1, count=N_ACTIONS).astype(bool))
            ys.append(z["label"].astype(np.int64))
            ps.append(z["problem"])
            ss.append(np.full(n, k * 10000 + int(path.stem.split("_")[1])))
    return (np.concatenate(xs).astype(np.float32), np.concatenate(ms), np.concatenate(ys), np.concatenate(ps),
            np.concatenate(ss))


def agreement(pred: np.ndarray, label: np.ndarray) -> dict:
    """How the copy's best moves compare with the look-ahead's."""
    moved, acted = label != 0, pred != 0
    return {"looks": int(len(label)), "look_ahead_moves": int(moved.sum()), "copy_moves": int(acted.sum()),
            "same_move_when_look_ahead_moved": round(float((pred[moved] == label[moved]).mean()), 3) if moved.any() else None,
            "some_move_when_look_ahead_moved": round(float(acted[moved].mean()), 3) if moved.any() else None,
            "look_ahead_moved_when_copy_moved": round(float(moved[acted].mean()), 3) if acted.any() else None,
            "same_move_when_copy_moved": round(float((pred[acted] == label[acted]).mean()), 3) if acted.any() else None,
            "nothing_when_look_ahead_did_nothing": round(float((~acted[~moved]).mean()), 3) if (~moved).any() else None}


def move_score(a: dict) -> float:
    """How well the copy's moves match the look-ahead's: the share of the look-ahead's moves it makes exactly, and
    the share of its moves that are exactly the look-ahead's, combined (their harmonic mean)."""
    r, p = a["same_move_when_look_ahead_moved"] or 0.0, a["same_move_when_copy_moved"] or 0.0
    return 2 * r * p / (r + p) if r + p > 0 else 0.0


def fit(dirs: list[str], out: str, epochs: int = 100, lr: float = 1e-3, batch: int = 128, move_weight: float = 4.0,
        weight_decay: float = 0.01, val_every: int = 7, seed: int = 0, threads: int = 4) -> dict:
    """Train the copy on the recorded looks (sessions whose number is a multiple of val_every are held out to
    measure it), keeping the network whose moves best match the look-ahead's on the held-out looks (move_score).

    With 14 sessions, a network trained long enough made every recorded move exactly on its training looks but only
    13-23% of them on held-out sessions, so it is held back from memorising them (weight decay), and the held-out
    loss is not the measure to keep it by: it was lowest after 14 passes, when the copy still made a fifth of the
    look-ahead's moves even on its training looks."""
    import copy as copy_module

    from torch.nn import functional as F

    torch.manual_seed(seed)
    torch.set_num_threads(threads)
    x, m, y, prob, sess = load_data(dirs)
    val = (sess % 10000) % val_every == 0
    xt, mt, yt = (torch.as_tensor(a[~val]) for a in (x, m, y))
    xv, mv, yv = (torch.as_tensor(a[val]) for a in (x, m, y))
    policy = make_policy()
    opt = torch.optim.AdamW(policy.parameters(), lr=lr, weight_decay=weight_decay)
    weight_t = torch.where(yt == 0, 1.0, move_weight)

    def evaluate(xe, me, ye):
        policy.set_training_mode(False)
        with torch.no_grad():
            lp = torch.cat([masked_logits(policy, xe[k:k + 1024], me[k:k + 1024]) for k in range(0, len(xe), 1024)])
            w = torch.where(ye == 0, 1.0, move_weight)
            loss = float((F.nll_loss(lp, ye, reduction="none") * w).sum() / w.sum())
        return loss, lp.argmax(dim=1).numpy()

    best, best_score, history, t0 = copy_module.deepcopy(policy.state_dict()), -1.0, [], time.time()
    for epoch in range(epochs):
        policy.set_training_mode(True)
        perm = torch.randperm(len(xt))
        for k in range(0, len(xt), batch):
            idx = perm[k:k + batch]
            lp = masked_logits(policy, xt[idx], mt[idx])
            w = weight_t[idx]
            loss = (F.nll_loss(lp, yt[idx], reduction="none") * w).sum() / w.sum()
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), 1.0)
            opt.step()
        vloss, vpred = evaluate(xv, mv, yv)
        a = agreement(vpred, y[val])
        history.append({"epoch": epoch + 1, "held_out_loss": round(vloss, 4), "move_score": round(move_score(a), 3), **a})
        print(f"epoch {epoch + 1}: held-out loss {vloss:.4f}, move score {move_score(a):.3f}; same move "
              f"{a['same_move_when_look_ahead_moved']}, look-ahead moved when copy moved "
              f"{a['look_ahead_moved_when_copy_moved']}", flush=True)
        if move_score(a) > best_score:
            best, best_score = copy_module.deepcopy(policy.state_dict()), move_score(a)
    policy.load_state_dict(best)
    _, tpred = evaluate(xt, mt, yt)
    vloss, vpred = evaluate(xv, mv, yv)
    path = Path(out)
    path.mkdir(parents=True, exist_ok=True)
    policy.save(str(path / "model.zip"))
    kinds = {}
    for name, sel in (("held_out", val), ("training", ~val)):
        lab, pred = y[sel], (vpred if name == "held_out" else tpred)
        moved = lab != 0
        by = Counter()
        for li, pi in zip(lab[moved], pred[moved]):
            kind = (SEQ_MOVES[(li - 1) % len(SEQ_MOVES)][0] if li < STACK_START
                    else STACK_MOVES[(li - STACK_START) % len(STACK_MOVES)][0])
            by[f"{kind}_moves"] += 1
            by[f"{kind}_same"] += int(pi == li)
        kinds[name] = dict(by)
    record = {"data": dirs, "looks": int(len(y)), "held_out_looks": int(val.sum()),
              "sessions": int(len(set(sess.tolist()))), "held_out_sessions": int(len(set(sess[val].tolist()))),
              "look_ahead_moves": int((y != 0).sum()), "looks_with_a_problem": int(prob.sum()),
              "epochs": epochs, "lr": lr, "batch": batch, "move_weight": move_weight, "weight_decay": weight_decay,
              "seed": seed, "kept_by": "held-out move_score",
              "policy": {"rows": N_ROWS, "features": len(COPY_FEATURES), "actions": N_ACTIONS, **COPY_POLICY_KWARGS},
              "parameters": int(sum(p.numel() for p in policy.parameters())),
              "best_epoch": int(np.argmax([h["move_score"] for h in history]) + 1), "held_out_loss": round(vloss, 4),
              "held_out_move_score": round(best_score, 3),
              "held_out": agreement(vpred, y[val]), "training": agreement(tpred, y[~val]), "by_kind": kinds,
              "minutes": round((time.time() - t0) / 60, 1), "history": history}
    (path / "fit.json").write_text(json.dumps(record, indent=2))
    return record


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="fly the look-ahead (or a copy) on training hours and record its decisions")
    c.add_argument("--sessions", type=int, default=96)
    c.add_argument("--first", type=int, default=0, help="skip this many of the seed's sessions")
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--seed", type=int, default=31)
    c.add_argument("--window", type=int, default=3600, help="seconds of traffic per session")
    c.add_argument("--driver", default="", help="a saved copy: fly it, and record the look-ahead's moves (DAgger)")
    c.add_argument("--beta", type=float, default=0.0, help="with --driver: make the look-ahead's move this often")
    c.add_argument("--out", required=True)
    f = sub.add_parser("fit", help="train the copy on recorded decisions")
    f.add_argument("--data", action="append", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--epochs", type=int, default=100)
    f.add_argument("--lr", type=float, default=1e-3)
    f.add_argument("--move-weight", type=float, default=4.0)
    f.add_argument("--weight-decay", type=float, default=0.01)
    f.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    if args.cmd == "collect":
        collect_main(args)
    else:
        record = fit(args.data, args.out, epochs=args.epochs, lr=args.lr, move_weight=args.move_weight,
                     weight_decay=args.weight_decay, seed=args.seed)
        print(json.dumps({k: v for k, v in record.items() if k != "history"}, indent=2))


if __name__ == "__main__":
    main()
