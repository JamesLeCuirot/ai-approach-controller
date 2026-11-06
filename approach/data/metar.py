"""Gatwick's weather reports (METARs), from the Iowa State University archive.

Used for the QNH: ADS-B altitudes are pressure altitudes (set to 1013 hPa), while
controllers and minimum altitudes below the transition altitude use the local QNH.
On a 1,034 hPa day an aircraft at 3,000 ft reports about 2,430 ft, so real altitudes
are corrected before they are checked against minimum altitudes.
"""

from __future__ import annotations

import csv
import io
import urllib.request
from datetime import date, timedelta

FT_PER_HPA = 27.3
HPA_PER_INHG = 33.8639
URL = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={icao}&data=alti"
       "&year1={a.year}&month1={a.month}&day1={a.day}&year2={b.year}&month2={b.month}&day2={b.day}"
       "&tz=Etc/UTC&format=onlycomma&missing=empty")


def qnh_by_hour(day: date, icao: str = "EGKK") -> dict[int, float]:
    """The reported QNH (hPa) for each UTC hour of a day: the first report in that hour."""
    url = URL.format(icao=icao, a=day, b=day + timedelta(days=1))
    req = urllib.request.Request(url, headers={"User-Agent": "ai-approach-controller"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rows = list(csv.DictReader(io.StringIO(r.read().decode())))
    out: dict[int, float] = {}
    for row in rows:
        if not row.get("alti") or row["valid"][:10] != day.isoformat():
            continue
        hour = int(row["valid"][11:13])
        out.setdefault(hour, round(float(row["alti"]) * HPA_PER_INHG, 1))
    return out


def qnh_altitude_ft(pressure_alt_ft: float, qnh_hpa: float | None, transition_altitude_ft: int = 6000) -> float:
    """Altitude above sea level on the QNH, for altitudes below the transition altitude."""
    if qnh_hpa is None:
        return pressure_alt_ft
    alt = pressure_alt_ft + (qnh_hpa - 1013.25) * FT_PER_HPA
    return alt if alt < transition_altitude_ft else pressure_alt_ft
