"""Publish tables from data/tables to Google Sheets (one tab per table).

Needs the environment variable GOOGLE_SERVICE_ACCOUNT_JSON (the full JSON key of a service
account that has edit access to the sheets) and the sheet IDs in config.yaml.
Skips quietly if either is missing, so the pipeline also works without Google.

Usage: python -m pipeline.publish core|status
"""
import json
import os
import sys
import time

import pandas as pd

from .common import CONFIG, read_table

CHUNK_ROWS = 20_000


def to_values(df: pd.DataFrame) -> list[list]:
    """Rows as plain Python values; missing values become empty cells."""
    obj = df.astype(object).where(df.notna(), "")
    return [list(df.columns)] + obj.values.tolist()


def write_worksheet(sh, name: str, df: pd.DataFrame) -> None:
    import gspread
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=2, cols=max(len(df.columns), 1))
    values = to_values(df)
    ws.resize(rows=len(values), cols=len(df.columns))
    for start in range(0, len(values), CHUNK_ROWS):
        chunk = values[start:start + CHUNK_ROWS]
        for attempt in range(5):
            try:
                # RAW keeps station ids such as "6257.03" or "31292" as text
                ws.update(range_name=f"A{start + 1}", values=chunk, value_input_option="RAW")
                break
            except gspread.exceptions.APIError:
                if attempt == 4:
                    raise
                time.sleep(20 * (attempt + 1))


def main():
    group = sys.argv[1]
    sheet_id = os.environ.get(f"SHEET_ID_{group.upper()}") or CONFIG["sheets"].get(group)
    key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not key:
        print(f"publish {group}: skipped (no sheet id or no service account key)")
        return
    import gspread
    gc = gspread.service_account_from_dict(json.loads(key))
    sh = gc.open_by_key(sheet_id)
    names = CONFIG["sheet_tables"][group]
    for name in names:
        df = read_table(name)
        if df.empty:
            continue
        write_worksheet(sh, name, df)
        print(f"publish {group}: {name} {len(df):,} rows", flush=True)
    # Remove tabs that are no longer published, so the Sheet stays small enough for Tableau
    for ws in sh.worksheets():
        if ws.title not in names and len(sh.worksheets()) > 1:
            sh.del_worksheet(ws)
            print(f"publish {group}: removed old tab {ws.title}", flush=True)


if __name__ == "__main__":
    main()
