# Health screening station network

```
[RFID] -> station (laptop/ESP32) --scan/result--> Mosquitto broker <--> hub.py --> screening.db (SQLite)
                     ^------------------- ack -------------------|        |
                                                       dashboard.py (live) + export_csv.py (CSV)
```
Everything runs on one **hub laptop** (broker + hub.py + dashboard + its own station script) connected to your own **travel router**.

## Files
| File | Runs on | What it does |
|---|---|---|
| `hub.py` | Hub laptop | The overseer. Subscribes to `screening/#`, logs every message, stores scans and results per participant in `screening.db`, and sends an ack back to the station once a result is saved. |
| `dashboard.py` | Hub laptop | Live read-only web view (`streamlit run dashboard.py`, port 8501). Shows station status, scans with no result yet, latest results per participant and bad messages, plus a CSV download button. |
| `export_csv.py` | Hub laptop | Exports CSVs after the event: `participants_wide.csv` (1 row per participant, columns `<station>.<metric>`), `results_long.csv`, `scans.csv`, `raw_messages.csv`. Times are in SGT. |
| `station_template.py` | Laptop stations | Teammates edit CONFIG + `setup()` + `measure()` only. Handles the RFID input (USB keyboard reader or via MQTT), online/busy status, and resending results until the hub confirms them (kept in `pending_<id>.json` across restarts). |
| `esp32_station/esp32_station.ino` | ESP32 stations | Teammates edit CONFIG + `userSetup()` + `pollRfid()` + `runMeasurement()` only. Can also act as an RFID-only reader for a laptop station. |
| `broker/mosquitto.conf` | Hub laptop | Mosquitto config: listens on the LAN, requires login, keeps queued messages across restarts. |
| `broker/acl` | Hub laptop | Permissions: each station can only use `screening/<its own id>/...`, while the hub can use everything. |
| `requirements.txt` | All laptops | Python libraries (`pip install -r requirements.txt`). |

## Topics (all under `screening/<station_id>/`)
| Topic | Sent by | Payload |
|---|---|---|
| `scan` | RFID reader / station | `{"uid":"04A1B2C3","ts":"...Z"}` (plain-text UID also accepted) |
| `result` | station | `{"uid":..., "session_id":<unique per measurement>, "ts":"...Z", "data":{...}}` |
| `ack` | hub | `{"session_id":..., "ok":true}` once saved. The station resends until it gets this |
| `status` | station (auto) | `online` / `busy` / `offline` (retained + Last Will) |
| `event` | station | errors, ignored scans, etc. |

**Why the ID is inside every result:** each result message carries the participant's `uid` and *all* of that station's metrics together:
```json
{"uid":"04A1B2C3","session_id":"9f2c...","ts":"2026-10-14T02:03:00Z",
 "data":{"bp_sys_mmhg":120,"bp_dia_mmhg":80,"pulse_bpm":72}}
```
The hub never matches results to scans using timings or message order. If it did, a missed or reordered message could assign measurements to the wrong person without any error. Scan messages are only used for tracking on the dashboard. `measure()` / `runMeasurement()` should return all of a station's metrics in one go.

**Overwrite rule:** the database keeps one row per (participant, station). A re-scan creates a new `session_id`, which replaces that row and adds 1 to `attempts`. Resending the same `session_id` changes nothing. Every original message stays in `raw_messages`, so overwritten data can still be recovered.

## One-time setup (hub laptop)
1. Install [Mosquitto](https://mosquitto.org/download/) and Python 3.10+, then run `pip install -r requirements.txt`.
2. Create the accounts. The username **must equal** the station ID:
   ```
   cd broker
   mosquitto_passwd -c passwd hub
   mosquitto_passwd passwd station01      # repeat for each station
   ```
3. Travel router: give the hub laptop a **DHCP reservation** (e.g. 192.168.8.10). Station configs then never change between schools.
4. Hub laptop: disable sleep, and allow inbound TCP 1883 (and 8501 if other laptops view the dashboard) through the firewall.

## Each screening day (hub laptop, 4 terminals)
```
cd broker && mosquitto -c mosquitto.conf -v
set MQTT_PASS=<hub password> && python hub.py          # macOS/Linux: export MQTT_PASS=...
streamlit run dashboard.py
python station_template.py                             # the hub laptop's own station
```
Afterwards: run `python export_csv.py`, then back up `screening.db` and the export folder.

Use a separate folder or `DB_PATH` per school/day (e.g. `set DB_PATH=schoolA_2026-10-14.db`) so events don't mix.

## Teammates
- **Laptop station:** copy `station_template.py`. Edit CONFIG and `measure()`. Set `RFID_SOURCE` to `keyboard` for a USB reader or `mqtt` if an ESP32 reads the cards.
- **ESP32 station:** copy `esp32_station/`. Edit CONFIG, `pollRfid()` and `runMeasurement()`. Use `stationDelay()` instead of `delay()`.
- **ESP32 used only as an RFID reader for a laptop:** set `HAS_MEASUREMENT=false` on the ESP32 and `RFID_SOURCE="mqtt"` on the laptop, with the same `STATION_ID`.
- Result keys should be flat, with units in the name: `height_cm`, `bp_systolic_mmhg`.

## Gotchas
- **All RFID readers must output the same UID format.** USB "keyboard" readers often output decimal, while the RC522 code here outputs hex. The same card would then show up as two different participants. Test one card on every station before the event.
- A USB keyboard reader types into whichever window is focused. Keep the station terminal focused.
- ESP32 holds up to 8 unconfirmed results in RAM, which are lost if it reboots. Laptop stations save theirs to disk.
- This is personal health data (PDPA). Store only card UIDs in this system, and keep the UID-to-name list separately. Encrypt the hub laptop's disk and follow each school's consent and retention rules.
- Tested: hub logic, CSV export and dashboard (with simulated messages). Not yet tested: a live broker connection and compiling the ESP32 sketch. Do a full dry run before the first school.
