"""Shared configuration and helpers."""
import json
import subprocess
import time
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
TABLES = ROOT / "data" / "tables"
INTERNAL = ROOT / "data" / "internal"
STATE = ROOT / "state"
WORK = ROOT / "work"
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())

CITIES = {
    "boston": {
        "name": "Boston", "system": "Bluebikes", "gbfs": "bos",
        "bucket": "https://s3.amazonaws.com/hubway-data/", "pattern": r"bluebikes",
        "lat": 42.3601, "lon": -71.0589, "tz": "America/New_York",
        "states": ["25"], "core_city": ("25", "Boston city")},
    "chicago": {
        "name": "Chicago", "system": "Divvy", "gbfs": "chi",
        "bucket": "https://divvy-tripdata.s3.amazonaws.com/", "pattern": r"divvy",
        "lat": 41.8781, "lon": -87.6298, "tz": "America/Chicago",
        "states": ["17"], "core_city": ("17", "Chicago city")},
    "washington": {
        "name": "Washington DC", "system": "Capital Bikeshare", "gbfs": "dca-cabi",
        "bucket": "https://s3.amazonaws.com/capitalbikeshare-data/", "pattern": r"capitalbikeshare",
        "lat": 38.9072, "lon": -77.0369, "tz": "America/New_York",
        "states": ["11", "51", "24"], "core_city": ("11", "Washington city")},
    "san_francisco": {
        "name": "San Francisco", "system": "Bay Wheels", "gbfs": "bay",
        "bucket": "https://s3.amazonaws.com/baywheels-data/", "pattern": r"baywheels|baywheeels|lyftbikes",
        "lat": 37.7749, "lon": -122.4194, "tz": "America/Los_Angeles",
        "states": ["06"], "core_city": ("06", "San Francisco city")},
    "new_york": {
        "name": "New York", "system": "Citi Bike", "gbfs": "bkn",
        "bucket": "https://s3.amazonaws.com/tripdata/", "pattern": r"citibike",
        "lat": 40.7128, "lon": -74.0060, "tz": "America/New_York",
        "states": ["36", "34"], "core_city": ("36", "New York city")},
}

ID_COLS = ["station_id", "start_station_id", "end_station_id", "station_key",
           "start_station_key", "end_station_key", "gbfs_station_id"]


def fetch(url: str, tries: int = 5, timeout: int = 120) -> bytes:
    """Download a URL with curl, retrying on transient network errors."""
    for i in range(tries):
        p = subprocess.run(["curl", "-s", "-f", "-L", "-m", str(timeout), url], capture_output=True)
        if p.returncode == 0:
            return p.stdout
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"Could not fetch {url}")


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url))


def gbfs(city: str, feed: str) -> dict:
    return fetch_json(f"https://gbfs.lyft.com/gbfs/2.3/{CITIES[city]['gbfs']}/en/{feed}.json")


def read_table(name: str, folder: Path = TABLES) -> pd.DataFrame:
    path = folder / f"{name}.csv"
    if not path.exists():
        return pd.DataFrame()
    cols = pd.read_csv(path, nrows=0).columns
    return pd.read_csv(path, dtype={c: str for c in cols if c in ID_COLS}, low_memory=False)


def write_table(name: str, df: pd.DataFrame, folder: Path = TABLES) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    df.to_csv(folder / f"{name}.csv", index=False)


def upsert(name: str, new: pd.DataFrame, month_col: str, folder: Path = TABLES, sort: list | None = None) -> None:
    """Replace the rows of the (city, month) pairs present in `new` and keep the rest."""
    if new.empty:
        return
    old = read_table(name, folder)
    if not old.empty:
        key_new = set(zip(new.city, new[month_col].astype(str).str[:7]))
        keep = [k not in key_new for k in zip(old.city, old[month_col].astype(str).str[:7])]
        new = pd.concat([old[keep], new.astype({c: str for c in new.columns if c in ID_COLS})], ignore_index=True)
    if sort:
        new = new.sort_values(sort, kind="stable")
    write_table(name, new, folder)


def trim_months(name: str, month_col: str, first_month: str, folder: Path = TABLES) -> None:
    df = read_table(name, folder)
    if not df.empty:
        write_table(name, df[df[month_col].astype(str).str[:7] >= first_month[:7]], folder)
