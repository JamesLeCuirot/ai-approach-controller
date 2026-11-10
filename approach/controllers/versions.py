"""Named versions of the rules controller, for stage 1b's checks and their replays.

Each is a set of RulesController settings. scripts/compare_controllers.py flies them on the test sessions;
in a replay or milestone a version is the controller "rules_<name>", e.g. "rules_fixed_turn:2026-05-30T12".
"""

VERSIONS = {
    # Stage 1a's competitor, timed with the wind: every arrival turns base at the same point (an 11 NM final).
    "fixed_turn": {"fixed_paths": True, "speeds": "slot", "wind_aware": True},
    # The same, timed for still air: should fly like stage 1a's speed-only controller (a check on the comparison).
    "fixed_turn_still_air": {"fixed_paths": True, "speeds": "slot", "wind_aware": False},
    # Speed first, then the path: the fixed turn, with the downwind run longer only when an aircraft has more delay
    # to lose than speed can absorb. Leaves the hold as the fixed turn does ("careful") ...
    "speed_then_stretch": {"fixed_paths": True, "speeds": "slot", "wind_aware": True, "stretch": True},
    # ... or as soon as its slot can be made with the longest downwind and speed ("eager").
    "speed_then_stretch_eager": {"fixed_paths": True, "speeds": "slot", "wind_aware": True, "stretch": True,
                                 "release": "eager"},
    # The base turn planned to make each landing slot (the downwind stretched or cut short), timed with the wind.
    "stretch": {"fixed_paths": False, "speeds": "slot", "wind_aware": True},
    # ... with stage 0's final director (160 or 180 kt on final to keep the gap) instead of the standard speeds.
    "stretch_final_director": {"fixed_paths": False, "speeds": "rules", "wind_aware": True},
    # Stage 0's rules controller as it was: base turn planned to the slot, final director, timed for still air.
    "stage0_still_air": {"fixed_paths": False, "speeds": "rules", "wind_aware": False},
}

TITLES = {
    "fixed_turn": "Rules controller: fixed base turn, timed with the wind",
    "fixed_turn_still_air": "Rules controller: fixed base turn, timed for still air",
    "speed_then_stretch": "Rules controller: speed first, then a longer downwind (careful release from the hold)",
    "speed_then_stretch_eager": "Rules controller: speed first, then a longer downwind (eager release from the hold)",
    "stretch": "Rules controller: base turn planned to the slot, timed with the wind",
    "stretch_final_director": "Rules controller: base turn planned to the slot, final director, timed with the wind",
    "stage0_still_air": "Rules controller: stage 0's, timed for still air",
}
