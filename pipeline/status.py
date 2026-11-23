"""Hourly job: snapshot of every station (bikes and docks available) for the operational dashboard.

Keeps a rolling station-level history in state/ (not committed; restored from the GitHub Actions
cache) and writes two tables:
  status_latest       one row per station: current state plus % of time empty/full in the last 24 h and 7 days
  status_city_hourly  one row per city and hour: share of stations empty/full, bikes available

Usage: python -m pipeline.status
"""
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

from .common import CITIES, CONFIG, STATE, TABLES, gbfs, read_table, write_table

HIST = STATE / "station_status.parquet"
CITY = STATE / "status_city_hourly.csv"


def snapshot() -> pd.DataFrame:
    frames = []
    for city, cfg in CITIES.items():
        info = pd.DataFrame(gbfs(city, "station_information")["data"]["stations"])
        s = gbfs(city, "station_status")
        st = pd.DataFrame(s["data"]["stations"]).merge(
            info[["station_id", "short_name", "name", "lat", "lon", "capacity"]], on="station_id")
        st = st[(st.is_installed == 1) & (st.capacity > 0)].copy()
        now = dt.datetime.fromtimestamp(s["last_updated"], dt.timezone.utc)
        st["city"] = city
        st["city_name"] = cfg["name"]
        st["snapshot_utc"] = now.strftime("%Y-%m-%d %H:%M")
        st["snapshot_hour_utc"] = now.strftime("%Y-%m-%d %H:00")
        st["snapshot_local"] = now.astimezone(ZoneInfo(cfg["tz"])).strftime("%Y-%m-%d %H:%M")
        # Stations without a short_name are small "public racks" for e-bikes (mostly Chicago)
        st["station_type"] = st.short_name.isna().map({True: "public_rack", False: "dock"})
        st["station_key"] = city + ":" + st.short_name.fillna(st.station_id).astype(str)
        frames.append(st)
    d = pd.concat(frames, ignore_index=True)
    d = d.rename(columns={"name": "station_name", "num_bikes_available": "bikes_available",
                          "num_ebikes_available": "ebikes_available", "num_docks_available": "docks_available",
                          "num_bikes_disabled": "bikes_disabled", "num_docks_disabled": "docks_disabled"})
    d["fill_pct"] = (d.bikes_available / d.capacity).clip(0, 1).round(3)
    d["state"] = "ok"
    d.loc[d.docks_available <= CONFIG["low_docks"], "state"] = "nearly_full"
    d.loc[d.bikes_available <= CONFIG["low_bikes"], "state"] = "low"
    d.loc[d.docks_available == 0, "state"] = "full"
    d.loc[d.bikes_available == 0, "state"] = "empty"
    d.loc[d.is_renting == 0, "state"] = "out_of_service"
    return d[["city", "city_name", "station_key", "station_name", "station_type", "lat", "lon", "capacity",
              "bikes_available", "ebikes_available", "docks_available", "bikes_disabled", "docks_disabled",
              "fill_pct", "state", "snapshot_utc", "snapshot_hour_utc", "snapshot_local"]]


def main():
    STATE.mkdir(exist_ok=True)
    snap = snapshot()
    # Rolling station-level history (state only)
    h_new = snap[["city", "station_key", "snapshot_hour_utc", "state"]]
    if HIST.exists():
        h = pd.concat([pd.read_parquet(HIST), h_new], ignore_index=True)
    else:
        h = h_new
    h = h.drop_duplicates(["station_key", "snapshot_hour_utc"], keep="last")
    now = pd.Timestamp(snap.snapshot_hour_utc.max())
    age_h = (now - pd.to_datetime(h.snapshot_hour_utc)).dt.total_seconds() / 3600
    h = h[age_h < 24 * CONFIG["status_station_history_days"]]
    h.to_parquet(HIST, index=False)
    age_h = (now - pd.to_datetime(h.snapshot_hour_utc)).dt.total_seconds() / 3600

    def share(mask, state, name):
        g = h[mask].assign(x=lambda t: t.state == state).groupby("station_key").x.mean().round(3)
        return g.rename(name)

    latest = snap.drop(columns=["snapshot_hour_utc"]).set_index("station_key")
    latest = latest.join([share(age_h < 24, "empty", "pct_time_empty_24h"), share(age_h < 24, "full", "pct_time_full_24h"),
                          share(age_h >= 0, "empty", "pct_time_empty_7d"), share(age_h >= 0, "full", "pct_time_full_7d")])
    latest["hours_observed_7d"] = h.groupby("station_key").size()
    write_table("status_latest", latest.reset_index().sort_values(["city", "station_name"]))

    # City-level hourly history (docks only, public racks excluded)
    docks = snap[snap.station_type == "dock"]
    c_new = docks.groupby(["city", "city_name", "snapshot_hour_utc"], as_index=False).agg(
        snapshot_local=("snapshot_local", "max"), stations=("station_key", "count"),
        stations_empty=("state", lambda s: (s == "empty").sum()),
        stations_low=("state", lambda s: (s == "low").sum()),
        stations_full=("state", lambda s: (s == "full").sum()),
        stations_out_of_service=("state", lambda s: (s == "out_of_service").sum()),
        bikes_available=("bikes_available", "sum"), ebikes_available=("ebikes_available", "sum"),
        docks_available=("docks_available", "sum"), bikes_disabled=("bikes_disabled", "sum"),
        avg_fill_pct=("fill_pct", "mean"))
    c_new["pct_empty"] = (c_new.stations_empty / c_new.stations).round(4)
    c_new["pct_full"] = (c_new.stations_full / c_new.stations).round(4)
    c_new["avg_fill_pct"] = c_new.avg_fill_pct.round(3)
    old = pd.read_csv(CITY) if CITY.exists() else read_table("status_city_hourly")
    c = pd.concat([old, c_new], ignore_index=True).drop_duplicates(["city", "snapshot_hour_utc"], keep="last")
    age_d = (now - pd.to_datetime(c.snapshot_hour_utc)).dt.total_seconds() / 86400
    c = c[age_d < CONFIG["status_city_history_days"]].sort_values(["city", "snapshot_hour_utc"])
    c.to_csv(CITY, index=False)
    write_table("status_city_hourly", c)
    print(c_new[["city", "snapshot_local", "stations", "pct_empty", "pct_full", "bikes_available"]].to_string(index=False))


if __name__ == "__main__":
    main()
