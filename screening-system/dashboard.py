"""Live read-only dashboard.  Run:  streamlit run dashboard.py
Open http://localhost:8501 (or http://<hub-ip>:8501 from another laptop)."""
import io
import os
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from export_csv import connect_ro, local_time, participants_wide, results_long

STALE_SCAN_MIN = int(os.getenv("STALE_SCAN_MIN", "10"))   # scan w/o result after this = flagged
OFFLINE_AFTER_MIN = 10

st.set_page_config(page_title="Screening dashboard", layout="wide")
st.title("Screening dashboard")


def q(con, sql, params=()):
    return pd.read_sql_query(sql, con, params=params)


@st.fragment(run_every=5)
def live():
    try:
        con = connect_ro()
    except Exception as e:
        st.error(f"Cannot open database ({e}). Is hub.py running in this folder?")
        return
    try:
        now = datetime.now(timezone.utc)
        stations = q(con, "SELECT station, status, last_seen FROM stations ORDER BY station")
        n_part = q(con, "SELECT COUNT(*) n FROM participants").n[0]
        n_res = q(con, "SELECT COUNT(*) n FROM results").n[0]
        n_err = q(con, "SELECT COUNT(*) n FROM raw_messages WHERE error IS NOT NULL").n[0]

        c = st.columns(4)
        c[0].metric("Participants", n_part)
        c[1].metric("Results", n_res)
        c[2].metric("Stations online", int((stations.status == "online").sum() +
                                           (stations.status == "busy").sum()))
        c[3].metric("Bad messages", n_err)

        st.subheader("Stations")
        if not stations.empty:
            ls = pd.to_datetime(stations.last_seen, utc=True)
            stations["warning"] = ""
            stations.loc[stations.status == "offline", "warning"] = "OFFLINE"
            stations.loc[(now - ls) > timedelta(minutes=OFFLINE_AFTER_MIN), "warning"] += " no msgs recently"
            st.dataframe(local_time(stations, ["last_seen"]), hide_index=True, width="stretch")

        st.subheader(f"Scans with no result after {STALE_SCAN_MIN} min")
        cutoff = (now - timedelta(minutes=STALE_SCAN_MIN)).isoformat().replace("+00:00", "Z")
        stale = q(con, """
            SELECT s.uid, s.station, MAX(s.scanned_at) scanned_at FROM scans s
            LEFT JOIN results r ON r.uid = s.uid AND r.station = s.station
            WHERE r.uid IS NULL OR r.measured_at < s.scanned_at
            GROUP BY s.uid, s.station HAVING MAX(s.scanned_at) < ?
            ORDER BY scanned_at DESC""", (cutoff,))
        st.dataframe(local_time(stale, ["scanned_at"]), hide_index=True, width="stretch")

        st.subheader("Participants (latest result per station)")
        st.dataframe(participants_wide(con), hide_index=True, width="stretch")

        left, right = st.columns(2)
        with left:
            st.subheader("Recent messages")
            st.dataframe(local_time(q(con, "SELECT received_at, topic, payload FROM raw_messages "
                                           "ORDER BY id DESC LIMIT 30"), ["received_at"]),
                         hide_index=True, width="stretch")
        with right:
            st.subheader("Bad messages")
            st.dataframe(local_time(q(con, "SELECT received_at, topic, error, payload FROM raw_messages "
                                           "WHERE error IS NOT NULL ORDER BY id DESC LIMIT 30"),
                                    ["received_at"]), hide_index=True, width="stretch")
        st.caption(f"Auto-refreshes every 5 s. Last update {datetime.now():%H:%M:%S}")
    finally:
        con.close()


live()

if st.button("Prepare CSV export"):
    con = connect_ro()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("participants_wide.csv",
                   participants_wide(con).to_csv(index=False, encoding="utf-8-sig"))
        z.writestr("results_long.csv", results_long(con).to_csv(index=False, encoding="utf-8-sig"))
    con.close()
    st.download_button("Download ZIP", buf.getvalue(),
                       file_name=f"screening_{datetime.now():%Y%m%d_%H%M}.zip")
