"""Real traffic: finding arrivals in ADS-B tracks, and scoring them like simulated sessions."""

import pytest

from approach.airports.sectorfile import DEFAULT_SECTOR_DIR

pytestmark = pytest.mark.skipif(not (DEFAULT_SECTOR_DIR / "Navaids").is_dir(), reason="run scripts/fetch_sector_file.sh first")


@pytest.fixture(scope="module")
def airport():
    from approach.airports import egkk

    return egkk.load()


def straight_in(airport, t0=1_700_000_000.0, ground=True, go_around=False, lateral=0.0):
    """A synthetic arrival flown straight down the 26L centreline from 45 NM, in adsb.lol point format."""
    rwy = airport.runways["26L"]
    pts, t = [], t0

    def add(along, alt, gs, lat_off=lateral):
        nonlocal t
        p = airport.grid.to_latlon(*airport.centreline_point("26L", along, lat_off))
        pts.append([t, p.lat, p.lon, alt, gs, rwy.course_true, -800, "TEST123", 0, None])
        t += 5.0

    along = 45.0
    while along > 0.4:
        gs = 250 if along > 15 else 150
        alt = 16000 if along > 42 else (rwy.glide_path_altitude_ft(along) if along < 11 else max(3000, along * 350))
        add(along, round(alt), gs)
        if go_around and 1.9 < along < 2.1:
            for k in range(30):  # climb away straight ahead to 3,000 ft, then come back round to 12 NM
                add(along - 0.2 * k, 700 + 100 * k, 160)
            along = 12.0
            go_around = False
            continue
        along -= gs * 5.0 / 3600.0
    add(0.1, rwy.elevation_ft + 40, 140)
    add(-0.3, rwy.elevation_ft + 10, 135)
    if ground:
        for k in range(5):
            add(-0.6 - 0.1 * k, "ground", 60 - 10 * k)
    return {"hex": "abc123", "type": "A320", "reg": "G-TEST", "points": pts}


def test_finds_a_landing_with_ground_reports(airport):
    from approach.data.arrivals import HANDOVER_CEILING_FT, HANDOVER_RADIUS_NM, find_arrivals

    found = find_arrivals({"aircraft": [straight_in(airport)]}, airport)
    assert len(found) == 1
    a = found[0]
    assert a["runway"] == "26L" and a["callsign"] == "TEST123" and a["go_arounds"] == 0 and a["hold_fix"] is None
    assert a["handover"]["alt"] <= HANDOVER_CEILING_FT
    x, y = airport.grid.to_xy(a["handover"]["lat"], a["handover"]["lon"])
    assert (x * x + y * y) ** 0.5 <= HANDOVER_RADIUS_NM + 0.1
    # Touchdown time is the threshold crossing, between the last point before it and the first after.
    before = [p for p in a["track"] if p[3] != "ground"][-2][0]
    assert before <= a["landed_t"] <= before + 10


def test_finds_a_landing_when_the_receivers_lose_it_over_the_runway(airport):
    from approach.data.arrivals import find_arrivals

    assert len(find_arrivals({"aircraft": [straight_in(airport, ground=False)]}, airport)) == 1


def test_counts_a_go_around_and_one_landing(airport):
    from approach.data.arrivals import find_arrivals

    found = find_arrivals({"aircraft": [straight_in(airport, go_around=True)]}, airport)
    assert len(found) == 1 and found[0]["go_arounds"] == 1


def test_final_approach_minimum_is_2_5_nm(airport):
    """Two aircraft 2.7 NM apart on final inside 10 NM are separated; the same gap at 4,000 ft off final is not."""
    from approach.scoring import score
    from approach.session import SessionLog

    def frame(t, ahead, behind, lateral=0.0, phase="established"):
        ac = []
        for cs, along in (("LEAD", ahead), ("FOLLOW", behind)):
            x, y = airport.centreline_point("26L", along, lateral)
            alt = airport.runways["26L"].glide_path_altitude_ft(along) if phase == "established" else 4000
            ac.append({"cs": cs, "type": "A320", "wake": "M", "x": x, "y": y, "alt": alt, "phase": phase})
        return {"t": t, "ac": ac}

    on_final = SessionLog(meta={}, map={}, frames=[frame(0, 5.0, 7.7)])
    assert score(on_final, airport, "26L")["separation_losses"] == 0
    off_final = SessionLog(meta={}, map={}, frames=[frame(0, 20.0, 22.7, lateral=6.0, phase="vectoring")])
    assert score(off_final, airport, "26L")["separation_losses"] == 1
