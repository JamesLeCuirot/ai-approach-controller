"""Wake turbulence categories and the spacing they need on final.

Two schemes, with the same six letters:

* "UK" (the default): the UK CAA's arrival categories and final-approach minima, from
  CAP 493 (MATS Part 1) Section 1 Chapter 3 and Appendix B. Types are categorised from
  the CAA's "UK Wake Turbulence Categorisation" list (V7, August 2025), which covers every
  type in the real Gatwick arrivals. J Super, H Heavy, U Upper Medium, M Lower Medium,
  S Small, L Light.
* "RECAT-EU": EUROCONTROL's six categories (A to F), as in VATSIM UK's vMATS, here as
  J H U M S L for A to F. NATS has used them at Gatwick with time-based separation since
  March 2025 (Advanced Mixed Mode, announced 26 March 2025).

A real day is scored with the scheme in force at Gatwick that day (`scheme_for`), for every
controller on that day's traffic. The real arrivals agree: under the UK scheme the real
controllers show 0.2 wake infringements per 1,000 arrivals before the switch and 1.8 after.
Made-up scenarios use SCHEME. Spacing is checked on final only for now: a question for a
Gatwick controller.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

CATEGORIES = ["J", "H", "U", "M", "S", "L"]
RECAT_EU_FROM = {"EGKK": date(2025, 3, 26)}  # the day NATS announced it; the exact go-live is to confirm
RADAR_MINIMUM_NM = 3.0
SCHEME = "UK"

NAMES = {
    "UK": {"J": "Super", "H": "Heavy", "U": "Upper Medium", "M": "Lower Medium", "S": "Small", "L": "Light"},
    "RECAT-EU": {"J": "A Super Heavy", "H": "B Upper Heavy", "U": "C Lower Heavy", "M": "D Upper Medium",
                 "S": "E Lower Medium", "L": "F Light"},
}

# Minimum distance (NM) behind a leader on final, keyed (leader, follower). Pairs not
# listed need only the 3 NM radar minimum.
SEPARATION_NM = {
    "UK": {  # CAP 493 Section 1 Chapter 3, final approach (as reproduced in RA 3277 Issue 3, Table 3)
        ("J", "H"): 5, ("J", "U"): 7, ("J", "M"): 7, ("J", "S"): 7, ("J", "L"): 8,
        ("H", "H"): 4, ("H", "U"): 5, ("H", "M"): 5, ("H", "S"): 6, ("H", "L"): 7,
        ("U", "M"): 4, ("U", "S"): 4, ("U", "L"): 6,
        ("M", "L"): 5,
        ("S", "L"): 4,
    },
    "RECAT-EU": {  # stage 0 spec
        ("J", "H"): 4, ("J", "U"): 5, ("J", "M"): 5, ("J", "S"): 6, ("J", "L"): 8,
        ("H", "U"): 4, ("H", "M"): 4, ("H", "S"): 5, ("H", "L"): 7,
        ("U", "S"): 4, ("U", "L"): 6,
        ("M", "L"): 5,
        ("S", "L"): 4,
    },
}


def _types(groups: dict[str, str]) -> dict[str, str]:
    return {t: cat for cat, names in groups.items() for t in names.split()}


TYPE_CATEGORY = {
    # UK arrival categories from the CAA list, for every type in data/real plus the scenarios' types.
    "UK": _types({
        "J": "A388",
        "H": "A310 A332 A333 A338 A339 A343 A359 A35K B744 B748 B763 B764 B772 B773 B77L B77W B788 B789 B78X",
        "U": "B752 B753",
        "M": "A20N A21N A319 A320 A321 B38M B39M B733 B734 B736 B737 B738 B739 BCS1 BCS3 E190 E195 E290 E295 "
             "GL5T GLEX GLF5 GLF6",
        "S": "AT75 AT76 CL35 CRJ9 DH8D E145 E170 F900",
        "L": "C172 C208 C42 C56X C650 C68A DA62 E55P EFOX H25C LJ45 LJ60 PC12",
    }),
    # Provisional, from the stage 0 spec.
    "RECAT-EU": _types({
        "J": "A388",
        "H": "B77W B772 B773 B744 B748 B788 B789 B78X A332 A333 A359 A35K",
        "U": "B763 B764 A310",
        "M": "A319 A320 A20N A321 A21N B737 B738 B38M B39M B752",
        "S": "E190 E195 CRJ9 AT76 DH8D",
        "L": "C172",
    }),
}
APPROACH_SPEED_KT = {"J": 150, "H": 150, "U": 145, "M": 140, "S": 125, "L": 90}


def use_scheme(name: str) -> None:
    """Switch every controller and the scoring to another scheme ("UK" or "RECAT-EU")."""
    global SCHEME
    if name not in SEPARATION_NM:
        raise ValueError(f"unknown wake scheme {name!r}: use one of {sorted(SEPARATION_NM)}")
    SCHEME = name


def scheme_for(day: date | str | None, icao: str = "EGKK") -> str:
    """The scheme in force at the airport on a real day: UK before its switch to RECAT-EU, RECAT-EU after."""
    if day is None:
        return SCHEME
    d = date.fromisoformat(day) if isinstance(day, str) else day
    switch = RECAT_EU_FROM.get(icao.upper())
    return "RECAT-EU" if switch and d >= switch else "UK"


@contextmanager
def scheme(name: str):
    """Use a scheme for the duration of a with-block, then switch back."""
    old = SCHEME
    use_scheme(name)
    try:
        yield name
    finally:
        use_scheme(old)


def category(actype: str, scheme: str | None = None) -> str:
    return TYPE_CATEGORY[scheme or SCHEME].get(actype.upper(), "M")


def separation_nm(leader_type: str, follower_type: str, scheme: str | None = None) -> float:
    s = scheme or SCHEME
    pair = (category(leader_type, s), category(follower_type, s))
    return float(SEPARATION_NM[s].get(pair, RADAR_MINIMUM_NM))


def approach_speed(actype: str) -> int:
    return APPROACH_SPEED_KT[category(actype)]
