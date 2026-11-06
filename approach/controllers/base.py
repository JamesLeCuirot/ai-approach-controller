"""The interface every controller implements: rules, random, scripted, and later the AI."""

from __future__ import annotations

import random

from approach.instructions import Instruction


class Controller:
    name = "base"

    def decide(self, t: float, views: list) -> list[Instruction]:
        """Called every few seconds with what the controller can see; returns instructions."""
        return []

    def on_event(self, event: dict) -> None:
        """Told about landings, go-arounds and blocked instructions."""


class NoController(Controller):
    name = "none"


class ScriptController(Controller):
    """Issues a fixed list of (time, instruction) pairs. Used for tests and demos."""

    name = "script"

    def __init__(self, script: list[tuple[float, Instruction]]):
        self.script = sorted(script, key=lambda s: s[0])
        self.next = 0

    def decide(self, t, views):
        out = []
        while self.next < len(self.script) and self.script[self.next][0] <= t:
            out.append(self.script[self.next][1])
            self.next += 1
        return out


class RandomController(Controller):
    """A sanity floor: every 15 s, one random instruction to one random aircraft."""

    name = "random"

    def __init__(self, seed: int = 1, interval_s: float = 15.0):
        self.rng = random.Random(seed)
        self.interval_s = interval_s
        self.last = -1e9

    def decide(self, t, views):
        if not views or t - self.last < self.interval_s:
            return []
        self.last = t
        v = self.rng.choice(views)
        kind = self.rng.choice(["heading", "altitude", "speed"])
        if kind == "heading":
            value = float(self.rng.randrange(0, 360, 5))
        elif kind == "altitude":
            value = self.rng.randrange(1000, 9001, 1000)
        else:
            value = self.rng.randrange(140, 261, 10)
        return [Instruction(v.cs, kind, value)]
