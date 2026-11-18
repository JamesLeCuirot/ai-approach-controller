"""Shadow mode: the live feed's points, spotting arrivals as they enter the handover region, and adding them live."""

import json

import numpy as np
import pytest

from approach.airports.sectorfile import DEFAULT_SECTOR_DIR

pytestmark = pytest.mark.skipif(not (DEFAULT_SECTOR_DIR / "Navaids").is_dir(), reason="run scripts/fetch_sector_file.sh first")


@pytest.fixture(scope="module")
def airport():
    from approach.airports import egkk

    return egkk.load()


def test_feed_point_matches_the_archived_form():
    from approach.live.feed import FIELDS, to_point

    a = {"hex": "abc123", "flight": "EZY12  ", "lat": 51.0, "lon": -0.1, "alt_baro": 9000, "gs": 250.0,
         "track": 300.0, "baro_rate": -1200, "ias": 230, "seen_pos": 2.0}
    p = to_point(a, 1000.0)
    assert len(p) == len(FIELDS)
    assert p[0] == 998.0 and p[3] == 9000 and p[6] == -1200 and p[7] == "EZY12"
    assert to_point({**a, "seen_pos": 60.0}, 1000.0) is None  # an old position is skipped
    assert to_point({**a, "lat": None}, 1000.0) is None


def test_recorder_keeps_the_area_and_reads_back(tmp_path, airport):
    from approach.live.feed import Recorder, read_area

    ref = airport.reference
    rec = Recorder(ref.lat, ref.lon)
    near = {"hex": "a1", "lat": ref.lat + 0.2, "lon": ref.lon, "alt_baro": 9000, "gs": 250, "track": 0, "seen_pos": 0}
    high = {**near, "hex": "a2", "alt_baro": 30000}
    far = {**near, "hex": "a3", "lat": ref.lat + 2.0}
    assert [h for h, _ in rec.add(100.0, [near, high, far])] == ["a1"]
    assert rec.add(100.0, [near]) == []  # no new position
    path = tmp_path / "rec.jsonl"
    path.write_text(json.dumps({"now": 100.0, "new": [["a1", "A320", "G-ABCD", rec.aircraft["a1"]["points"][0]]]}) + "\n")
    area = read_area(path, ref.lat, ref.lon)
    assert area["aircraft"][0]["hex"] == "a1" and area["aircraft"][0]["type"] == "A320"


def test_entries_judge_an_aircraft_once_as_it_enters(airport):
    from approach.live.spotter import Entries

    ent = Entries(airport)
    ref = airport.reference
    pt = lambda t, dlat, alt, gs=250.0, trk=180.0: [t, ref.lat + dlat, ref.lon, alt, gs, trk, -1000, "X", 0, None]  # noqa: E731
    assert ent.add("h", pt(0, 0.8, 16000)) is None  # 48 NM out, too high
    assert ent.add("h", pt(5, 0.66, 15500)) is None  # inside 40 NM but still above 15,000 ft
    assert ent.add("h", pt(10, 0.65, 14800, gs=None)) is None  # entered, but no speed yet
    got = ent.add("h", pt(15, 0.64, 14700))
    assert got is not None and got[0] == 10 and got[1][2] == 14700  # judged at its first point with a speed
    assert ent.add("h", pt(20, 0.63, 14600)) is None  # once only


def test_spotter_votes_with_its_nearest_neighbours():
    from approach.live.spotter import Spotter

    rng = np.random.default_rng(0)
    arrivals = np.column_stack([rng.normal(20, 1, 200), rng.normal(0, 1, 200), rng.normal(10000, 300, 200),
                                np.full(200, 250.0), np.full(200, 270.0), np.full(200, -1000.0)])
    others = arrivals.copy()
    others[:, 0] -= 30  # the same, 30 NM further west
    sp = Spotter(states=np.vstack([arrivals, others]), labels=np.r_[np.ones(200), np.zeros(200)])
    p = sp.chance([[20, 0, 10000, 250, 270, -1000], [-10, 0, 10000, 250, 270, -1000]])
    assert p[0] > 0.95 and p[1] < 0.05


def test_runway_from_aircraft_on_a_final(airport):
    from approach.live.shadow import runway_from_feed

    rwy = airport.runways["26L"]
    pts = []
    for along in (4.0, 6.0, 8.0):
        p = airport.grid.to_latlon(*airport.centreline_point("26L", along, 0.0))
        pts.append([0, p.lat, p.lon, 2000, 150, rwy.course_true, -700, "X", 0, None])
    assert runway_from_feed(airport, pts) == "26L"
    assert runway_from_feed(airport, []) is None


def test_tail_waits_for_a_whole_line(tmp_path):
    from approach.live.shadow import Tail

    path = tmp_path / "f.jsonl"
    path.write_text('{"a": 1}\n{"a": ')
    tail = Tail(path)
    assert tail.lines() == [{"a": 1}]
    with open(path, "a") as f:
        f.write('2}\n')
    assert tail.lines() == [{"a": 2}]


def test_shadow_env_takes_arrivals_live(airport):
    from approach.data.gfs import WindProfile  # noqa: F401  (the session runs in calm air here)
    from approach.live.shadow import ShadowEnv

    env = ShadowEnv()
    env.start("26L", None, "calm", "2026-11-18", 1800, 1, "test")
    ref = airport.reference
    state = lambda dlat: {"lat": ref.lat + dlat, "lon": ref.lon + 0.3, "alt": 12000, "gs": 280, "track": 200}  # noqa: E731
    env.add_arrival("B", "A320", state(0.5), 60)
    env.add_arrival("A", "A320", state(0.6), 30)  # handed over earlier, added later: still released first
    assert [a.callsign for a in env.session.pending] == ["A", "B"]
    for _ in range(9):
        env.step(np.zeros(env.action_space.shape, dtype=np.int64))
    assert {v.cs for v in env.session.world.views()} == {"A", "B"}
    assert not env.session.done
