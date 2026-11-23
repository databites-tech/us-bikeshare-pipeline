"""Download one month of trips for one city, clean it and aggregate it.

Raw trips are never stored in the repository: each month is downloaded, cleaned,
summarised into the tables in data/tables and then deleted.
"""
import datetime as dt
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import duckdb
import pandas as pd

from .common import CITIES, WORK, fetch

MIN_MINUTES, MAX_MINUTES = 1, 24 * 60
TOP_ROUTES = 200


def find_file(city: str, ym: str) -> str | None:
    """Name of the trip file for a month (YYYYMM), or None if not published yet."""
    cfg = CITIES[city]
    xml = fetch(f"{cfg['bucket']}?prefix={ym}").decode()
    keys = [k for k in re.findall(r"<Key>([^<]+)</Key>", xml)
            if re.match(rf"^{ym}-", k) and re.search(cfg["pattern"], k)]
    return sorted(keys)[0] if keys else None


def clean_month(city: str, ym: str) -> tuple[Path, dict] | None:
    """Download and clean a month. Returns (parquet path, quality record)."""
    key = find_file(city, ym)
    if not key:
        return None
    WORK.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=WORK))
    try:
        local = tmp / Path(key).name
        subprocess.run(["curl", "-s", "-f", "-m", "3600", "--retry", "3", "-o", str(local),
                        CITIES[city]["bucket"] + key], check=True)
        if local.suffix == ".zip":
            subprocess.run(["unzip", "-q", "-o", str(local), "-d", str(tmp / "x")], check=True)
            local.unlink()
        csvs = [p for p in tmp.rglob("*.csv") if "__MACOSX" not in p.parts]
        files = "[" + ",".join(f"'{p}'" for p in csvs) + "]"
        con = duckdb.connect()
        con.execute(f"SET temp_directory='{tmp}/duck'")
        con.execute(f"CREATE TABLE t AS SELECT * FROM read_csv({files}, header=true, all_varchar=true, union_by_name=true)")
        n_raw = con.execute("SELECT count(*) FROM t").fetchone()[0]
        con.execute(f"""
            CREATE TABLE c AS
            SELECT DISTINCT ON (ride_id)
                '{city}' AS city, ride_id, rideable_type,
                CAST(started_at AS TIMESTAMP) AS started_at, CAST(ended_at AS TIMESTAMP) AS ended_at,
                NULLIF(trim(start_station_id), '') AS start_station_id,
                NULLIF(trim(start_station_name), '') AS start_station_name,
                NULLIF(trim(end_station_id), '') AS end_station_id,
                NULLIF(trim(end_station_name), '') AS end_station_name,
                TRY_CAST(start_lat AS DOUBLE) AS start_lat, TRY_CAST(start_lng AS DOUBLE) AS start_lng,
                TRY_CAST(end_lat AS DOUBLE) AS end_lat, TRY_CAST(end_lng AS DOUBLE) AS end_lng,
                member_casual,
                date_diff('second', CAST(started_at AS TIMESTAMP), CAST(ended_at AS TIMESTAMP)) / 60.0 AS duration_min
            FROM t""")
        n_dedup = con.execute("SELECT count(*) FROM c").fetchone()[0]
        y, m = int(ym[:4]), int(ym[4:])
        first, nxt = dt.date(y, m, 1), dt.date(y + (m == 12), m % 12 + 1, 1)
        in_month = f"CAST(started_at AS DATE) >= DATE '{first}' AND CAST(started_at AS DATE) < DATE '{nxt}'"
        s = con.execute(f"""
            SELECT count(*) FILTER (WHERE duration_min < {MIN_MINUTES}),
                   count(*) FILTER (WHERE duration_min > {MAX_MINUTES}),
                   count(*) FILTER (WHERE NOT ({in_month})),
                   count(*) FILTER (WHERE start_station_id IS NULL),
                   count(*) FILTER (WHERE end_station_id IS NULL)
            FROM c""").fetchone()
        out = WORK / f"{city}_{ym}.parquet"
        con.execute(f"""COPY (SELECT * FROM c WHERE duration_min BETWEEN {MIN_MINUTES} AND {MAX_MINUTES} AND {in_month})
                        TO '{out}' (FORMAT parquet, COMPRESSION zstd)""")
        n_kept = con.execute(f"SELECT count(*) FROM '{out}'").fetchone()[0]
        con.close()
        rec = dict(city=city, month=str(first), source_file=key, rows_raw=n_raw, duplicates=n_raw - n_dedup,
                   too_short=s[0], too_long=s[1], out_of_month=s[2], no_start_station=s[3],
                   no_end_station=s[4], rows_kept=n_kept, pct_kept=round(n_kept / n_raw, 4),
                   pct_no_start_station=round(s[3] / n_raw, 4))
        return out, rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def aggregate_month(parquet: Path) -> dict[str, pd.DataFrame]:
    """Summary tables for one city-month of clean trips."""
    con = duckdb.connect()
    con.execute(f"""CREATE VIEW trips AS SELECT *, CAST(started_at AS DATE) AS d,
                    date_trunc('month', started_at)::DATE AS m FROM read_parquet('{parquet}')""")
    q = {}
    q["daily_trips"] = """
        SELECT city, d AS date, member_casual, rideable_type, count(*) AS trips,
               count(*) FILTER (WHERE start_station_id IS NOT NULL AND end_station_id IS NOT NULL) AS docked_trips,
               round(sum(duration_min)) AS total_minutes
        FROM trips GROUP BY ALL ORDER BY date, member_casual, rideable_type"""
    q["hourly_profile"] = """
        SELECT city, m AS month, isodow(started_at) AS weekday, hour(started_at) AS hour,
               member_casual, count(*) AS trips
        FROM trips GROUP BY ALL ORDER BY weekday, hour, member_casual"""
    q["station_monthly"] = """
        WITH dep AS (
          SELECT city, m AS month, start_station_id AS station_id, count(*) AS departures,
                 count(*) FILTER (WHERE member_casual = 'member') AS member_departures,
                 count(*) FILTER (WHERE rideable_type = 'electric_bike') AS ebike_departures,
                 round(avg(duration_min), 1) AS avg_duration_min
          FROM trips WHERE start_station_id IS NOT NULL GROUP BY ALL),
        arr AS (
          SELECT city, m AS month, end_station_id AS station_id, count(*) AS arrivals
          FROM trips WHERE end_station_id IS NOT NULL GROUP BY ALL)
        SELECT coalesce(dep.city, arr.city) AS city, coalesce(dep.month, arr.month) AS month,
               coalesce(dep.city, arr.city) || ':' || coalesce(dep.station_id, arr.station_id) AS station_key,
               coalesce(departures, 0) AS departures, coalesce(arrivals, 0) AS arrivals,
               coalesce(arrivals, 0) - coalesce(departures, 0) AS net_arrivals,
               coalesce(member_departures, 0) AS member_departures,
               coalesce(ebike_departures, 0) AS ebike_departures, avg_duration_min
        FROM dep FULL JOIN arr USING (city, month, station_id)
        ORDER BY departures DESC, station_key"""
    q["routes_monthly"] = f"""
        WITH r AS (
          SELECT city, m AS month, start_station_id AS s, end_station_id AS e, count(*) AS trips,
                 round(avg(duration_min), 1) AS avg_duration_min
          FROM trips WHERE start_station_id IS NOT NULL AND end_station_id IS NOT NULL
          GROUP BY city, m, start_station_id, end_station_id),
        k AS (SELECT *, row_number() OVER (ORDER BY trips DESC, s, e) AS rank FROM r)
        SELECT city, month, city || ':' || s AS start_station_key, city || ':' || e AS end_station_key,
               trips, avg_duration_min, rank
        FROM k WHERE rank <= {TOP_ROUTES} ORDER BY rank"""
    q["station_seen"] = """
        WITH a AS (
          SELECT city, m AS month, start_station_id AS station_id, start_station_name AS name, count(*) AS n,
                 avg(start_lat) AS lat, avg(start_lng) AS lon
          FROM trips WHERE start_station_id IS NOT NULL GROUP BY ALL)
        SELECT city, month, station_id, arg_max(name, n) AS station_name,
               round(sum(lat * n) / sum(n), 6) AS lat, round(sum(lon * n) / sum(n), 6) AS lon, sum(n) AS departures
        FROM a GROUP BY ALL ORDER BY station_id"""
    out = {k: con.execute(v).df() for k, v in q.items()}
    con.close()
    for df in out.values():
        for c in ("date", "month"):
            if c in df:
                df[c] = df[c].astype(str).str[:10]
    return out
