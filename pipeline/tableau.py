"""Tables shaped for the four Tableau dashboards.

They are derived from the base tables, so they cost no extra downloads:
- station_summary: one row per station with last-12-month activity, growth and map offsets in km
- daily_city: daily trips by rider and bike type, with weather and day type, readable labels
- hourly_city: hourly profile with readable labels (weekday name, day type, rider)
"""
import math

import numpy as np
import pandas as pd

from .common import CITIES, read_table, write_table

RIDER = {"member": "Members", "casual": "Casual riders"}
WEEKDAY = dict(zip(range(1, 8), ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))
MIN_DEPARTURES_12M = 200  # stations with fewer departures in 12 months are left off the maps


def city_name(s: pd.Series) -> pd.Series:
    return s.map(lambda c: CITIES[c]["name"])


def build_station_summary() -> pd.DataFrame:
    st = read_table("stations")
    sm = read_table("station_monthly")
    sm["month"] = pd.to_datetime(sm.month)
    last = sm.month.max()
    cur = sm[sm.month > last - pd.DateOffset(months=12)].groupby("station_key").agg(
        departures_12m=("departures", "sum"), net_arrivals_12m=("net_arrivals", "sum"))
    prev = (sm[(sm.month <= last - pd.DateOffset(months=12)) & (sm.month > last - pd.DateOffset(months=24))]
            .groupby("station_key").departures.sum().rename("departures_prev_12m"))
    s = (st[["station_key", "city", "city_name", "station_name", "lat", "lon", "capacity"]]
         .merge(cur.join(prev, how="left").fillna(0).reset_index(), on="station_key")
         .dropna(subset=["lat", "lon"]))
    s = s[s.departures_12m >= MIN_DEPARTURES_12M].copy()
    s["growth"] = np.where(s.departures_prev_12m == 0, "new",
                           np.where(s.departures_12m > s.departures_prev_12m, "grew", "shrank"))
    for c in ["departures_12m", "departures_prev_12m", "net_arrivals_12m"]:
        s[c] = s[c].astype(int)
    # Kilometres from the city's top-left corner, same scale in every city (for small-multiple maps)
    for _, g in s.groupby("city"):
        k = math.cos(math.radians(g.lat.median()))
        lon0, lat1 = g.lon.quantile(.005), g.lat.quantile(.995)
        s.loc[g.index, "x_km"] = ((g.lon - lon0) * 111 * k).round(3)
        s.loc[g.index, "y_km"] = ((g.lat - lat1) * 111).round(3)
    s = s.sort_values(["city", "station_key"])
    write_table("station_summary", s)
    return s


def build_daily_city() -> pd.DataFrame:
    d = read_table("daily_trips")
    w = read_table("weather_daily")[["city", "date", "temp_max_c", "precip_mm"]]
    d = d.merge(w, on=["city", "date"], how="left")
    d["city_name"] = city_name(d.city)
    d["day_type"] = np.where(pd.to_datetime(d.date).dt.weekday >= 5, "Weekends", "Weekdays")
    d["rider"] = d.member_casual.map(RIDER)
    d["bike"] = np.where(d.rideable_type == "electric_bike", "Electric", "Classic")
    d = d.groupby(["city", "city_name", "date", "day_type", "rider", "bike"], as_index=False).agg(
        trips=("trips", "sum"), temp_max_c=("temp_max_c", "first"), precip_mm=("precip_mm", "first"))
    write_table("daily_city", d)
    return d


def build_hourly_city() -> pd.DataFrame:
    h = read_table("hourly_profile")
    h["city_name"] = city_name(h.city)
    h["rider"] = h.member_casual.map(RIDER)
    h["weekday_name"] = h.weekday.map(WEEKDAY)
    h["day_type"] = np.where(h.weekday >= 6, "Weekends", "Weekdays")
    h = h[["city", "city_name", "month", "weekday", "weekday_name", "day_type", "hour", "rider", "trips"]]
    write_table("hourly_city", h)
    return h


def build_tableau_tables() -> None:
    s, d, h = build_station_summary(), build_daily_city(), build_hourly_city()
    print(f"tableau tables: station_summary {len(s):,}, daily_city {len(d):,}, hourly_city {len(h):,} rows")


if __name__ == "__main__":
    build_tableau_tables()
