"""Speed control in stage 1a: the published speed bands and the steps a controller may give.

The AI and the speed-only rules controller use exactly the same steps and bands, so the only
difference between them is how they choose.
"""

from __future__ import annotations

DECISION_S = 10.0  # how often speeds are reviewed
STEPS_KT = (-20, -10, 0, 10, 20)  # the choices for each aircraft at each review; 0 = leave it alone
FINAL_FREE_NM = 7.0  # inside this the pilot flies the approach speeds itself

# Indicated airspeed bands by stage of the approach (kt).
BANDS_KT = {
    "inbound": (210, 250),  # to the hold, or holding
    "downwind": (180, 250),
    "base": (160, 180),
    "cleared": (160, 180),  # intercept and final, until FINAL_FREE_NM
}
STANDARD_KT = {"inbound": 220, "downwind": 220, "base": 180, "cleared": 170}


def band(stage: str, along_nm: float) -> tuple[int, int] | None:
    """The band for an aircraft at this stage, or None where speed is not the controller's to give."""
    if stage == "cleared" and along_nm < FINAL_FREE_NM:
        return None
    return BANDS_KT.get(stage)


def apply_step(assigned_kt: float, step_kt: int, limits: tuple[int, int]) -> int:
    """The new assigned speed after a step, kept inside the band and on a 10 kt value.

    An aircraft above or below the band is brought straight to its edge, as a controller
    would when it enters a new stage, whatever the step.
    """
    lo, hi = limits
    if assigned_kt > hi:
        return hi
    if assigned_kt < lo:
        return lo
    return int(min(hi, max(lo, round((assigned_kt + step_kt) / 10.0) * 10)))
