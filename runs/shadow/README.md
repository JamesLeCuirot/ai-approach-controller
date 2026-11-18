# Shadow-mode runs

Each folder is one shadow-mode run (`python -m approach.live.shadow`): the look-ahead controller flying the real Gatwick arrivals of that hour in the simulator, in real time, while the real controllers flew the real ones. It is named after the UTC time the run started.

| File | What it holds |
| --- | --- |
| `run.json` | The run's settings, winds and scores, with the look-ahead's counts |
| `judged.json` | Every aircraft that entered the handover region (40 NM, 15,000 ft) while the run was handing over, and the spotter's chance that it was a Gatwick arrival |
| `shadow_session.json` | The AI's session: every aircraft every few seconds, and every instruction |
| `real_session.json` | The same arrivals as the real controllers flew them, scored with the same code |
| `compare.json` | Aircraft by aircraft: whether each was spotted and was really an arrival, and its minutes from handover to landing with the AI and in reality |
| `replay.html` | A radar replay of the AI's hour, with the real tracks in faint grey behind. Open it in a browser |

The recordings these were made from stay local (`data/raw/live/`, not committed).

The aircraft positions come from [adsb.lol](https://www.adsb.lol)'s live feed, under the Open Database License (ODbL 1.0), so the files in these folders that are made from it are under the ODbL too.
