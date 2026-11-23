"""Derived tables: stations, monthly KPIs and calendar."""
import holidays
import pandas as pd

from .common import CITIES, CONFIG, INTERNAL, gbfs, read_table, write_table


def build_stations() -> pd.DataFrame:
    seen = read_table("station_seen", INTERNAL)
    seen["w_lat"], seen["w_lon"] = seen.lat * seen.events, seen.lon * seen.events
    last = seen.month.max()
    cut = (pd.Period(last[:7]) - 11).strftime("%Y-%m")
    seen["dep12"] = seen.departures.where(seen.month.str[:7] >= cut, 0)
    names = (seen.groupby(["city", "station_id", "station_name"], as_index=False).events.sum()
             .sort_values("events").drop_duplicates(["city", "station_id"], keep="last")
             [["city", "station_id", "station_name"]])
    st = seen.groupby(["city", "station_id"], as_index=False).agg(
        w_lat=("w_lat", "sum"), w_lon=("w_lon", "sum"), events=("events", "sum"), trips_total=("departures", "sum"),
        trips_last_12m=("dep12", "sum"), first_month=("month", "min"), last_month=("month", "max"))
    st["lat"], st["lon"] = (st.w_lat / st.events).round(6), (st.w_lon / st.events).round(6)
    st = st.drop(columns=["w_lat", "w_lon", "events"]).merge(names, on=["city", "station_id"])
    feeds = []
    for city in CITIES:
        try:
            regions = {r["region_id"]: r["name"] for r in gbfs(city, "system_regions")["data"]["regions"]}
        except Exception:
            regions = {}
        for x in gbfs(city, "station_information")["data"]["stations"]:
            if x.get("short_name"):
                feeds.append({"city": city, "station_id": str(x["short_name"]), "capacity": x.get("capacity"),
                              "region": regions.get(x.get("region_id")), "gbfs_station_id": x.get("station_id"),
                              "g_lat": x.get("lat"), "g_lon": x.get("lon")})
    fd = pd.DataFrame(feeds).drop_duplicates(["city", "station_id"])
    st = st.merge(fd, on=["city", "station_id"], how="left")
    st["active_now"] = st.capacity.notna()
    st["lat"], st["lon"] = st.g_lat.fillna(st.lat), st.g_lon.fillna(st.lon)
    # Drop demo or test stations that are nowhere near the city (for example "LA Metro Demo" in New York)
    c_lat, c_lon = st.city.map(lambda c: CITIES[c]["lat"]), st.city.map(lambda c: CITIES[c]["lon"])
    st = st[((st.lat - c_lat).abs() < 1.0) & ((st.lon - c_lon).abs() < 1.5)].copy()
    st["station_key"] = st.city + ":" + st.station_id
    st["city_name"] = st.city.map(lambda c: CITIES[c]["name"])
    st["system"] = st.city.map(lambda c: CITIES[c]["system"])
    cols = ["station_key", "city", "city_name", "system", "station_id", "station_name", "lat", "lon", "capacity",
            "region", "active_now", "first_month", "last_month", "trips_total", "trips_last_12m", "gbfs_station_id"]
    st = st[cols].sort_values(["city", "station_id"])
    write_table("stations", st)
    return st


def build_kpis() -> pd.DataFrame:
    d = read_table("daily_trips")
    d["month"] = d.date.str[:7] + "-01"
    d["member"] = d.trips.where(d.member_casual == "member", 0)
    d["ebike"] = d.trips.where(d.rideable_type == "electric_bike", 0)
    k = d.groupby(["city", "month"], as_index=False).agg(
        trips=("trips", "sum"), docked_trips=("docked_trips", "sum"), member=("member", "sum"),
        ebike=("ebike", "sum"), minutes=("total_minutes", "sum"), days=("date", "nunique"))
    sm = read_table("station_monthly")
    used = sm[sm.departures > 0].groupby(["city", "month"], as_index=False).station_key.nunique()
    k = k.merge(used.rename(columns={"station_key": "stations_used"}), on=["city", "month"], how="left")
    pop = read_table("population")[["city", "service_area_population"]]
    k = k.merge(pop, on="city", how="left")
    k["member_share"] = (k.member / k.trips).round(4)
    k["ebike_share"] = (k.ebike / k.trips).round(4)
    k["dockless_share"] = (1 - k.docked_trips / k.trips).round(4)
    k["avg_duration_min"] = (k.minutes / k.trips).round(1)
    k["trips_per_station_day"] = (k.trips / k.stations_used / k.days).round(2)
    k["trips_per_1000_residents"] = (k.trips / (k.service_area_population / 1000)).round(1)
    ly = k[["city", "month", "trips", "docked_trips"]].copy()
    ly["month"] = (pd.to_datetime(ly.month) + pd.DateOffset(years=1)).dt.strftime("%Y-%m-%d")
    k = k.merge(ly.rename(columns={"trips": "trips_last_year", "docked_trips": "docked_trips_last_year"}),
                on=["city", "month"], how="left")
    g = 1 + CONFIG["target_growth"]
    k["yoy_growth"] = (k.trips / k.trips_last_year - 1).round(4)
    k["docked_yoy_growth"] = (k.docked_trips / k.docked_trips_last_year - 1).round(4)
    k["target_trips"] = (k.trips_last_year * g).round()
    k["pct_of_target"] = (k.trips / k.target_trips).round(4)
    k["city_name"] = k.city.map(lambda c: CITIES[c]["name"])
    k = k.drop(columns=["member", "ebike", "minutes", "service_area_population"]).sort_values(["city", "month"])
    write_table("kpi_monthly", k)
    return k


def build_calendar() -> pd.DataFrame:
    d = read_table("daily_trips")
    days = pd.date_range(d.date.min(), d.date.max(), freq="D")
    us = holidays.US(years=range(days[0].year, days[-1].year + 1))
    cal = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "year": days.year,
                        "month": days.strftime("%Y-%m-01"), "month_name": days.strftime("%B"),
                        "weekday": days.isocalendar().day.values, "weekday_name": days.day_name(),
                        "is_weekend": days.dayofweek >= 5,
                        "holiday": [us.get(x) for x in days],
                        "season": pd.Series((days.month % 12) // 3).map(
                            {0: "Winter", 1: "Spring", 2: "Summer", 3: "Autumn"}).values})
    write_table("calendar", cal)
    return cal
