"""Daily job: add any newly published trip months, refresh weather and rebuild derived tables.

Usage: python -m pipeline.run_daily [--cities boston,chicago] [--skip-trips]
"""
import argparse
import datetime as dt

import pandas as pd

from .build import build_calendar, build_kpis, build_stations
from .tableau import build_tableau_tables
from .common import CITIES, CONFIG, INTERNAL, read_table, trim_months, upsert, write_table
from .trips import aggregate_month, clean_month
from .weather import weather

SORT = {"daily_trips": ["city", "date"], "hourly_profile": ["city", "month"],
        "station_monthly": ["city", "month"], "routes_monthly": ["city", "month", "rank"]}


def window() -> list[str]:
    """Months (YYYYMM) the dashboards keep, ending with last calendar month."""
    last = pd.Period(dt.date.today(), "M") - 1
    return [(last - i).strftime("%Y%m") for i in range(CONFIG["months_to_keep"])][::-1]


def add_month(city: str, ym: str) -> bool:
    res = clean_month(city, ym)
    if res is None:
        return False
    parquet, rec = res
    try:
        tables = aggregate_month(parquet)
    finally:
        parquet.unlink(missing_ok=True)
    for name, df in tables.items():
        if name == "station_seen":
            upsert(name, df, "month", INTERNAL, ["city", "month"])
        else:
            upsert(name, df, "date" if name == "daily_trips" else "month", sort=SORT[name])
    upsert("data_quality", pd.DataFrame([rec]), "month", sort=["city", "month"])
    print(f"{city} {ym}: {rec['rows_kept']:,} trips added", flush=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default=",".join(CITIES))
    ap.add_argument("--skip-trips", action="store_true")
    a = ap.parse_args()
    months = window()
    if not a.skip_trips:
        q = read_table("data_quality")
        done = set(zip(q.city, q.month.str[:7].str.replace("-", ""))) if not q.empty else set()
        for ym in months:
            for city in a.cities.split(","):
                if (city, ym) not in done:
                    try:
                        if not add_month(city, ym):
                            print(f"{city} {ym}: not published yet", flush=True)
                    except Exception as e:  # one failing month must not stop the rest
                        print(f"{city} {ym}: ERROR {e}", flush=True)
        first = f"{months[0][:4]}-{months[0][4:]}"
        for name, col in [("daily_trips", "date"), ("hourly_profile", "month"), ("station_monthly", "month"),
                          ("routes_monthly", "month"), ("data_quality", "month")]:
            trim_months(name, col, first)
    d = read_table("daily_trips")
    w = weather(d.date.min(), min(d.date.max(), str(dt.date.today() - dt.timedelta(days=1))))
    write_table("weather_daily", w)
    build_stations()
    k = build_kpis()
    build_calendar()
    build_tableau_tables()
    print(k[k.month == k.month.max()][["city", "month", "trips", "yoy_growth", "pct_of_target"]].to_string(index=False))


if __name__ == "__main__":
    main()
