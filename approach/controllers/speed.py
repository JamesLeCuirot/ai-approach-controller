"""Speed control in stage 1a: the published speed bands and the speeds a controller may give.

Every 10 seconds a controller may give each aircraft in the sequence a speed, as a controller
says it ("speed 180"), from 160 to 250 kt in 10 kt values and inside the band for the
aircraft's stage. Giving its current speed means leaving it alone. The AI and the speed-only
rules controller choose from exactly the same speeds and bands, so the only difference
between them is how they choose.
"""

from __future__ import annotations

DECISION_S = 10.0  # how often speeds are reviewed
SPEEDS_KT = tuple(range(160, 260, 10))  # the choices: 160, 170, ... 250 kt
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


def in_band(speed_kt: float, limits: tuple[int, int]) -> int:
    """A speed brought inside the band, on a 10 kt value."""
    lo, hi = limits
    return int(min(hi, max(lo, round(speed_kt / 10.0) * 10)))


def nearest_choice(speed_kt: float) -> int:
    """The index in SPEEDS_KT of the speed nearest to this one."""
    return min(range(len(SPEEDS_KT)), key=lambda i: abs(SPEEDS_KT[i] - speed_kt))
