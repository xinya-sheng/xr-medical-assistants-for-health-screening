#!/usr/bin/env python3
"""Export screening data to CSV (Excel-friendly).

  python export_csv.py               -> ./export_<timestamp>/
  python export_csv.py my_folder

Files: participants_wide.csv (1 row/participant, columns <station>.<field>),
       results_long.csv, scans.csv, raw_messages.csv
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = os.getenv("DB_PATH", "screening.db")
TZ = os.getenv("DISPLAY_TZ", "Asia/Singapore")


def connect_ro(path=DB_PATH):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def local_time(df, cols):
    for c in cols:
        if c in df:
            df[c] = pd.to_datetime(df[c], utc=True).dt.tz_convert(TZ).dt.strftime("%Y-%m-%d %H:%M:%S")
    return df


def results_long(con):
    df = pd.read_sql_query("SELECT uid, station, measured_at, attempts, data, received_at, session_id "
                           "FROM results ORDER BY uid, station", con)
    return local_time(df, ["measured_at", "received_at"])


def participants_wide(con):
    df = pd.read_sql_query("SELECT uid, station, measured_at, data FROM results", con)
    rows = {}
    for r in df.itertuples():
        row = rows.setdefault(r.uid, {"uid": r.uid})
        row[f"{r.station}.time"] = r.measured_at
        for k, v in json.loads(r.data).items():
            row[f"{r.station}.{k}"] = json.dumps(v) if isinstance(v, (dict, list)) else v
    wide = pd.DataFrame(list(rows.values()))
    if wide.empty:
        return pd.DataFrame(columns=["uid"])
    cols = ["uid"] + sorted(c for c in wide.columns if c != "uid")
    wide = wide[cols].sort_values("uid")
    return local_time(wide, [c for c in cols if c.endswith(".time")])


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else f"export_{datetime.now():%Y%m%d_%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    con = connect_ro()
    tables = {
        "participants_wide": participants_wide(con),
        "results_long": results_long(con),
        "scans": local_time(pd.read_sql_query("SELECT * FROM scans ORDER BY scanned_at", con),
                            ["scanned_at"]),
        "raw_messages": local_time(pd.read_sql_query("SELECT * FROM raw_messages ORDER BY id", con),
                                   ["received_at"]),
    }
    for name, df in tables.items():
        df.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")
        print(f"{name}.csv: {len(df)} rows")
    print(f"-> {out.resolve()}")


if __name__ == "__main__":
    main()
