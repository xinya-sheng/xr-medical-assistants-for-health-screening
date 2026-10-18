#!/usr/bin/env python3
"""Laptop station template.  Teammates: edit CONFIG + YOUR CODE only.

Flow: RFID scan -> measure(uid) -> result published -> hub stores it and replies with an ack.
Unacknowledged results are saved to disk and resent automatically until the hub confirms them,
so you can move on to the next participant even if the hub/Wi-Fi is temporarily down.
"""
import json
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

# ============================== CONFIG ==============================
BROKER_HOST = "192.168.8.10"     # hub laptop's reserved IP on the travel router
BROKER_PORT = 1883
STATION_ID = "station01"         # = MQTT username. lowercase, no spaces or / + #
MQTT_PASS = "change-me"
RFID_SOURCE = "keyboard"         # "keyboard": USB reader that types ID + Enter into this window
                                 # "mqtt":     separate device publishes to screening/<STATION_ID>/scan
TOPIC_ROOT = "screening"
RESEND_EVERY_S = 5


# ============================== YOUR CODE ===========================
def setup():
    """Runs once at start: open serial ports, connect instruments, etc."""
    pass


def measure(uid):
    """Run this station's checks for participant `uid`. Blocking is fine (can take minutes).
    Return a flat dict of ALL this station's results, e.g. {"bp_sys_mmhg": 120, "bp_dia_mmhg": 80}.
    Use consistent keys with units in names (height_cm, not height). Raise an exception to abort."""
    print(f"Measuring {uid} ...")
    value = input("  Enter value (or blank to abort): ").strip()
    if not value:
        raise RuntimeError("aborted by operator")
    return {"example_value": float(value)}


# ============================== FRAMEWORK (don't edit) ==============
BASE = f"{TOPIC_ROOT}/{STATION_ID}"
PENDING_FILE = Path(f"pending_{STATION_ID}.json")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Station:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = json.loads(PENDING_FILE.read_text()) if PENDING_FILE.exists() else {}
        self.scans = queue.Queue()
        self.busy = False
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{STATION_ID}-py")
        c.username_pw_set(STATION_ID, MQTT_PASS)
        c.will_set(f"{BASE}/status", "offline", qos=1, retain=True)
        c.on_connect, c.on_message = self.on_connect, self.on_message
        c.reconnect_delay_set(1, 10)
        self.client = c

    # ---- MQTT
    def on_connect(self, client, userdata, flags, rc, props):
        if rc.is_failure:
            print(f"!! MQTT connect failed: {rc} (check STATION_ID/MQTT_PASS)")
            return
        client.subscribe(f"{BASE}/ack", qos=1)
        if RFID_SOURCE == "mqtt":
            client.subscribe(f"{BASE}/scan", qos=1)
        self.set_status("busy" if self.busy else "online")
        print("connected to hub")

    def on_message(self, client, userdata, msg):
        try:
            body = json.loads(msg.payload)
        except ValueError:
            body = msg.payload.decode("utf-8", "replace").strip()
        if msg.topic.endswith("/ack") and isinstance(body, dict):
            with self.lock:
                done = self.pending.pop(body.get("session_id"), None)
                self._save()
            if done:
                print(f"   ✔ hub saved {body.get('uid')}" if body.get("ok")
                      else f"   ✘ hub REJECTED result: {body.get('error')}")
        elif msg.topic.endswith("/scan"):
            uid = body.get("uid") if isinstance(body, dict) else body
            if self.busy:
                print(f"!! scan {uid} ignored: still measuring previous participant")
                self.event("scan_ignored_busy", uid=uid)
            else:
                self.scans.put(str(uid).strip().upper())

    def set_status(self, s):
        return self.client.publish(f"{BASE}/status", s, qos=1, retain=True)

    def event(self, kind, **fields):
        self.client.publish(f"{BASE}/event", json.dumps({"ts": now_iso(), "type": kind, **fields}), qos=1)

    # ---- reliable results
    def _save(self):
        PENDING_FILE.write_text(json.dumps(self.pending))

    def submit(self, uid, data):
        sid = uuid.uuid4().hex
        payload = json.dumps({"uid": uid, "session_id": sid, "ts": now_iso(), "data": data})
        with self.lock:
            self.pending[sid] = payload
            self._save()
        self.client.publish(f"{BASE}/result", payload, qos=1)

    def resend_loop(self):
        while True:
            time.sleep(RESEND_EVERY_S)
            with self.lock:
                items = list(self.pending.values())
            if items and self.client.is_connected():
                print(f"   … resending {len(items)} unconfirmed result(s)")
                for p in items:
                    self.client.publish(f"{BASE}/result", p, qos=1)

    # ---- main loop
    def next_uid(self):
        if RFID_SOURCE == "keyboard":
            while True:
                uid = input("\nScan RFID card: ").strip().upper()
                if uid:
                    self.client.publish(f"{BASE}/scan", json.dumps({"uid": uid, "ts": now_iso()}), qos=1)
                    return uid
        print("\nWaiting for RFID scan ...")
        return self.scans.get()

    def run(self):
        self.client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=15)
        self.client.loop_start()
        threading.Thread(target=self.resend_loop, daemon=True).start()
        if self.pending:
            print(f"{len(self.pending)} unconfirmed result(s) from last run will be resent")
        setup()
        try:
            while True:
                uid = self.next_uid()
                print(f"Participant {uid}")
                self.busy = True
                self.set_status("busy")
                try:
                    data = measure(uid)
                    self.submit(uid, data)
                    print(f"   sent: {data}")
                except Exception as e:
                    print(f"!! measurement failed: {e}")
                    self.event("measure_failed", uid=uid, error=str(e))
                finally:
                    self.busy = False
                    self.set_status("online")
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            if self.pending:
                print(f"!! {len(self.pending)} result(s) not yet confirmed; kept in {PENDING_FILE}")
            try:
                self.set_status("offline").wait_for_publish(2)
            except Exception:
                pass
            self.client.disconnect()
            self.client.loop_stop()


if __name__ == "__main__":
    Station().run()
