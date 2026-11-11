"""Charts of a training run, for the diary: how the AI's results change with practice.

    python -m approach.ai.report runs/stage1a-01 diary/<entry>/learning.png

Three small panels sharing the horizontal axis (decisions practised): how far each gap at
landing is from its target, separation losses per 100 arrivals, and speed instructions per
arrival. Each is averaged over the last 40 training episodes; the dashed line is the speed-only
rules controller on its own episodes (the head start's teacher), for comparison.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

AI = "#2a78d6"  # categorical slot 1 (dataviz reference palette)
REFERENCE = "#52514e"  # secondary text: the comparison line
INK, GRID, SURFACE = "#0b0b0b", "#e4e3df", "#fcfcfb"
WINDOW = 40


def _rows(path: Path) -> list[dict]:
    return list(csv.DictReader(path.open())) if path.exists() else []


def _measures(rows: list[dict]) -> dict[str, np.ndarray]:
    landed = np.array([max(int(r["landed"]), 1) for r in rows], dtype=float)
    gaps = np.array([int(r["gaps"]) for r in rows], dtype=float)
    err = np.array([float(r["mean_gap_error_nm"] or 0) for r in rows]) * gaps
    return {
        "gap": (err, gaps),
        "losses": (np.array([int(r["separation_losses"]) for r in rows], dtype=float) * 100, landed),
        "instructions": (np.array([int(r["speed_instructions"]) for r in rows], dtype=float), landed),
    }


def _rolling(num: np.ndarray, den: np.ndarray, k: int) -> np.ndarray:
    cn, cd = np.cumsum(num), np.cumsum(den)
    out = np.empty(len(num))
    for i in range(len(num)):
        j = max(0, i - k + 1)
        n = cn[i] - (cn[j - 1] if j else 0)
        d = cd[i] - (cd[j - 1] if j else 0)
        out[i] = n / d if d else np.nan
    return out


def chart(run: Path, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = _rows(run / "episodes.csv")
    teacher = _rows(run / "teacher_episodes.csv")
    if not rows:
        raise SystemExit(f"no episodes yet in {run}")
    if "trained_decisions" in rows[0]:
        x = np.array([int(r["trained_decisions"]) for r in rows])
    else:  # run stage1a-01 logged each episode's own decisions: add them up for the running total
        x = np.cumsum([int(r["decisions"]) for r in rows])
    m, t = _measures(rows), _measures(teacher) if teacher else None
    import json

    stage = json.loads((run / "run.json").read_text()).get("stage", "1a") if (run / "run.json").is_file() else "1a"
    teacher_name = ("Rules controller it copied (eager release, speed then a longer downwind)" if stage == "1b"
                    else "Speed-only rules controller")
    panels = [("gap", "Distance from the target gap\nat landing (NM)"),
              ("losses", "Separation losses\nper 100 arrivals"),
              ("instructions", "Speed instructions\nper arrival")]
    plt.rcParams.update({"font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK, "xtick.color": INK,
                         "ytick.color": INK, "font.family": "DejaVu Sans"})
    fig, axes = plt.subplots(3, 1, figsize=(8, 8.5), sharex=True, facecolor=SURFACE)
    for ax, (key, label) in zip(axes, panels):
        ax.set_facecolor(SURFACE)
        y = _rolling(*m[key], WINDOW)
        ax.plot(x, y, color=AI, linewidth=2, label="AI (average of the last 40 episodes)")
        if t:
            ref = t[key][0].sum() / max(t[key][1].sum(), 1)
            ax.axhline(ref, color=REFERENCE, linewidth=1.5, linestyle=(0, (5, 4)),
                       label=f"{teacher_name}, its head-start episodes")
        ax.set_ylabel(label)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_ylim(bottom=0)
    axes[-1].set_xlabel("Decisions practised")
    axes[-1].xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1000:,.0f}k"))
    fig.suptitle(f"Training run {run.name}: the AI's results as it practises", x=0.07, ha="left", fontsize=13,
                 color=INK, fontweight="bold")
    fig.text(0.07, 0.94, "Training sessions only, each a real hour from the training days. In practice the AI picks "
             "its speeds with some\nrandomness, to try things out; the test on unseen days uses its best choice.",
             fontsize=9, color=REFERENCE, va="top")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.06, 0.905), ncol=2, frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.87))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    print(f"Wrote {out}")


if __name__ == "__main__":
    chart(Path(sys.argv[1]), Path(sys.argv[2]))
