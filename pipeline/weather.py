"""Daily weather per city from the Open-Meteo archive API (free, no key)."""
import pandas as pd

from .common import CITIES, fetch_json

VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,wind_speed_10m_max"


def weather(start: str, end: str) -> pd.DataFrame:
    rows = []
    for city, c in CITIES.items():
        d = fetch_json(f"https://archive-api.open-meteo.com/v1/archive?latitude={c['lat']}&longitude={c['lon']}"
                       f"&start_date={start}&end_date={end}&daily={VARS}&timezone={c['tz']}")["daily"]
        rows.append(pd.DataFrame({"city": city, "date": d["time"],
                                  "temp_max_c": d["temperature_2m_max"], "temp_min_c": d["temperature_2m_min"],
                                  "precip_mm": d["precipitation_sum"], "snow_cm": d["snowfall_sum"],
                                  "wind_max_kmh": d["wind_speed_10m_max"]}))
    w = pd.concat(rows, ignore_index=True)
    return w.dropna(subset=["temp_max_c"])  # the most recent days are not in the archive yet
