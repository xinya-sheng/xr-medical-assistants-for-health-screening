#!/usr/bin/env python3
"""Screening hub: logs every MQTT message, records scans/results per participant in SQLite,
and acknowledges results so stations know their data is safely stored.

Run on the hub laptop:  python hub.py
Config via env vars: MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, TOPIC_ROOT, DB_PATH
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "hub")
MQTT_PASS = os.getenv("MQTT_PASS", "change-me")
ROOT = os.getenv("TOPIC_ROOT", "screening")
DB_PATH = os.getenv("DB_PATH", "screening.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hub")

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS raw_messages (      -- append-only audit log of everything
    id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, topic TEXT NOT NULL,
    payload TEXT, retained INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS stations (
    station TEXT PRIMARY KEY, status TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS participants (
    uid TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY, uid TEXT NOT NULL, station TEXT NOT NULL, scanned_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS results (            -- latest result per participant per station
    uid TEXT NOT NULL, station TEXT NOT NULL, session_id TEXT NOT NULL,
    measured_at TEXT NOT NULL, received_at TEXT NOT NULL, data TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (uid, station));
CREATE INDEX IF NOT EXISTS raw_time ON raw_messages(received_at);
CREATE INDEX IF NOT EXISTS scans_uid ON scans(uid, station);
"""

# Re-scan => new session_id => overwrite & attempts+1. Retry of same session => no-op (idempotent).
UPSERT_RESULT = """
INSERT INTO results (uid, station, session_id, measured_at, received_at, data)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (uid, station) DO UPDATE SET
    session_id = excluded.session_id, measured_at = excluded.measured_at,
    received_at = excluded.received_at, data = excluded.data, attempts = results.attempts + 1
WHERE excluded.session_id <> results.session_id AND excluded.measured_at >= results.measured_at
"""


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def norm_ts(v, fallback):
    """Device timestamp -> canonical UTC ISO string; fallback if missing/unparseable."""
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return iso(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
        except ValueError:
            pass
    return fallback


def norm_uid(v):
    return str(v).strip().upper() if v not in (None, "") else None


def open_db(path=DB_PATH):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def handle(db, topic, payload: bytes, retained=False, now=None):
    """Process one message. Returns list of (topic, payload_str) replies to publish."""
    now = iso(now or datetime.now(timezone.utc))
    text = payload.decode("utf-8", "replace")
    with db:
        raw_id = db.execute("INSERT INTO raw_messages (received_at, topic, payload, retained) "
                            "VALUES (?,?,?,?)", (now, topic, text, int(retained))).lastrowid

    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != ROOT:
        return []
    station, kind = parts[1], parts[2]
    if kind == "ack":                       # our own replies
        return []

    replies = []
    try:
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        with db:
            db.execute("INSERT INTO stations (station, last_seen) VALUES (?,?) "
                       "ON CONFLICT(station) DO UPDATE SET last_seen=excluded.last_seen",
                       (station, now))
            if kind == "status":
                db.execute("UPDATE stations SET status=? WHERE station=?", (text.strip(), station))

            elif kind == "scan" and not retained:
                uid = norm_uid(body.get("uid") if isinstance(body, dict) else text)
                if not uid:
                    raise ValueError("scan without uid")
                ts = norm_ts(body.get("ts") if isinstance(body, dict) else None, now)
                db.execute("INSERT OR IGNORE INTO participants VALUES (?,?)", (uid, ts))
                db.execute("INSERT INTO scans (uid, station, scanned_at) VALUES (?,?,?)",
                           (uid, station, ts))
                log.info("scan    %-10s %s", station, uid)

            elif kind == "result":
                if not isinstance(body, dict):
                    raise ValueError("result is not a JSON object")
                sid = body.get("session_id")
                uid = norm_uid(body.get("uid"))
                data = body.get("data")
                if not sid or not uid or not isinstance(data, dict):
                    replies.append((f"{ROOT}/{station}/ack", json.dumps(
                        {"session_id": sid, "ok": False, "error": "need uid, session_id, data{}"})))
                    raise ValueError("result missing uid/session_id/data")
                ts = norm_ts(body.get("ts"), now)
                db.execute("INSERT OR IGNORE INTO participants VALUES (?,?)", (uid, ts))
                db.execute(UPSERT_RESULT, (uid, station, sid, ts, now, json.dumps(data)))
                replies.append((f"{ROOT}/{station}/ack",
                                json.dumps({"session_id": sid, "uid": uid, "ok": True})))
                log.info("result  %-10s %s %s", station, uid, data)
    except Exception as e:
        log.warning("bad message on %s: %s", topic, e)
        with db:
            db.execute("UPDATE raw_messages SET error=? WHERE id=?", (str(e), raw_id))
    return replies


def main():
    import paho.mqtt.client as mqtt

    db = open_db()

    def on_connect(client, userdata, flags, rc, props):
        if rc.is_failure:
            log.error("MQTT connect failed: %s (check user/password in broker/passwd)", rc)
            return
        client.subscribe(f"{ROOT}/#", qos=1)
        log.info("connected to %s:%s, db=%s", MQTT_HOST, MQTT_PORT, DB_PATH)

    def on_message(client, userdata, msg):
        for t, p in handle(db, msg.topic, msg.payload, msg.retain):
            client.publish(t, p, qos=1)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hub", clean_session=False)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect, client.on_message = on_connect, on_message
    client.on_disconnect = lambda *a: log.warning("MQTT disconnected; reconnecting")
    client.reconnect_delay_set(1, 10)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        db.close()
        log.info("stopped")


if __name__ == "__main__":
    main()
