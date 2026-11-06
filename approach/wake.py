"""RECAT-EU wake turbulence categories and arrival separation (stage 0 spec)."""

from __future__ import annotations

CATEGORIES = ["J", "H", "U", "M", "S", "L"]  # Super, Heavy, Upper, Medium, Small, Light (vMATS naming)
NAMES = {"J": "Super", "H": "Heavy", "U": "Upper", "M": "Medium", "S": "Small", "L": "Light"}

# Minimum distance (NM) behind a leader when it crosses the threshold, keyed (leader, follower).
# Pairs not listed only need the 3 NM radar minimum.
SEPARATION_NM = {
    ("J", "H"): 4, ("J", "U"): 5, ("J", "M"): 5, ("J", "S"): 6, ("J", "L"): 8,
    ("H", "U"): 4, ("H", "M"): 4, ("H", "S"): 5, ("H", "L"): 7,
    ("U", "S"): 4, ("U", "L"): 6,
    ("M", "L"): 5,
    ("S", "L"): 4,
}
RADAR_MINIMUM_NM = 3.0

# Provisional type mapping for the scenarios. Replace with the vMATS appendix and
# EUROCONTROL's RECAT-EU type list (open task in the stage 0 spec).
TYPE_CATEGORY = {
    "A388": "J",
    "B77W": "H", "B772": "H", "B773": "H", "B744": "H", "B748": "H", "B788": "H", "B789": "H", "B78X": "H",
    "A332": "H", "A333": "H", "A359": "H", "A35K": "H",
    "B763": "U", "B764": "U", "A310": "U",
    "A319": "M", "A320": "M", "A20N": "M", "A321": "M", "A21N": "M", "B737": "M", "B738": "M", "B38M": "M",
    "B39M": "M", "B752": "M",
    "E190": "S", "E195": "S", "CRJ9": "S", "AT76": "S", "DH8D": "S",
    "C172": "L", "PA28": "L",
}
APPROACH_SPEED_KT = {"J": 150, "H": 150, "U": 145, "M": 140, "S": 125, "L": 90}


def category(actype: str) -> str:
    return TYPE_CATEGORY.get(actype.upper(), "M")


def separation_nm(leader_type: str, follower_type: str) -> float:
    return float(SEPARATION_NM.get((category(leader_type), category(follower_type)), RADAR_MINIMUM_NM))


def approach_speed(actype: str) -> int:
    return APPROACH_SPEED_KT[category(actype)]
