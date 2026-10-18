// ESP32 station (PlatformIO port of esp32_station.ino). Teammates: edit CONFIG + YOUR CODE only.
// Libraries are declared in platformio.ini (lib_deps) and install automatically on build.
//
// Flow: card scanned -> publish scan -> runMeasurement() -> publish result -> resend until hub acks.
// Can also act as a pure RFID reader for a laptop station (HAS_MEASUREMENT = false).
#include <Arduino.h>      // <-- the one line PlatformIO needs that the Arduino IDE adds for you
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include <sys/time.h>

// ============================== CONFIG ==============================
const char* WIFI_SSID   = "SINGTEL-DR77";
const char* WIFI_PASS   = "3rdf6f463v";
const char* MQTT_HOST   = "192.168.1.3";    // hub laptop's reserved IP
const uint16_t MQTT_PORT = 1883;
const char* STATION_ID  = "station02";       // = MQTT username. lowercase, no spaces or / + #
const char* MQTT_PASS   = "help";
const bool HAS_MEASUREMENT = true;           // false = only reads RFID and publishes scans
const bool RFID_VIA_MQTT  = false;           // true = scans come from another device via MQTT
const char* TOPIC_ROOT  = "screening";

void stationDelay(uint32_t ms);              // use INSTEAD of delay() inside your code

// ============================== YOUR CODE ===========================
// #include <SPI.h>
// #include <MFRC522.h>
// MFRC522 rfid(5, 22);                      // SS, RST pins

void userSetup() {
  // SPI.begin(); rfid.PCD_Init();
}

// Return card UID as hex string, or "" if no card. Framework debounces repeats.
//
// NO RFID WIRED UP YET? For a dry run, temporarily replace the body with:
//   static uint32_t last = 0;
//   if (millis() - last > 10000) { last = millis(); return "TESTCARD01"; }
//   return "";
// to fake a "card scan" every 10s and prove the MQTT round trip works end to end.

String pollRfid() {
  static uint32_t last = 0;
  if (millis() - last > 10000) {
    last = millis();
    return "TESTCARD01";
  }
  return "";
}

// String pollRfid() {
  // if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return "";
  // String uid;
  // for (byte i = 0; i < rfid.uid.size; i++) { if (rfid.uid.uidByte[i] < 0x10) uid += "0"; uid += String(rfid.uid.uidByte[i], HEX); }
  // rfid.PICC_HaltA();
  // uid.toUpperCase();
  // return uid;
//   return "";
// }

// Fill `data` with ALL of this station's results. Return false to abort (nothing is published).
// Long waits MUST use stationDelay(), not delay(), or the MQTT connection drops.
bool runMeasurement(const String& uid, JsonObject data) {
  Serial.printf("Measuring %s\n", uid.c_str());
  stationDelay(3000);                        // replace with real measurement
  data["example_value"] = 42.0;
  return true;
}

// ============================== FRAMEWORK (don't edit) ==============
WiFiClient net;
PubSubClient mqtt(net);
String base;
const int MAX_PENDING = 8;
String pendingId[MAX_PENDING], pendingMsg[MAX_PENDING];   // unacked results (RAM only)
String queuedUid;
bool measuring = false;

String topic(const char* kind) { return base + "/" + kind; }

String nowIso() {
  if (time(nullptr) < 1700000000) return "";               // NTP not synced yet
  struct timeval tv; gettimeofday(&tv, nullptr);
  struct tm t; gmtime_r(&tv.tv_sec, &t);
  char buf[32];
  strftime(buf, sizeof buf, "%Y-%m-%dT%H:%M:%S", &t);
  snprintf(buf + 19, sizeof(buf) - 19, ".%03ldZ", (long)(tv.tv_usec / 1000));
  return String(buf);
}

void publishJson(const char* kind, JsonDocument& doc, bool retain = false) {
  String ts = nowIso();
  if (ts.length()) doc["ts"] = ts;
  String out; serializeJson(doc, out);
  mqtt.publish(topic(kind).c_str(), out.c_str(), retain);
}

void setStatus(const char* s) { mqtt.publish(topic("status").c_str(), s, true); }

void sendEvent(const char* type, const String& uid) {
  JsonDocument d; d["type"] = type; d["uid"] = uid; publishJson("event", d);
}

void onMqttMessage(char* t, byte* payload, unsigned int len) {
  Serial.printf("RX [%s] %.*s\n", t, len, (const char*)payload); // For testing, comment out in production
  String tp(t);
  JsonDocument doc;
  bool isJson = !deserializeJson(doc, payload, len);
  if (tp.endsWith("/ack") && isJson) {
    String sid = doc["session_id"] | "";
    for (int i = 0; i < MAX_PENDING; i++)
      if (pendingId[i] == sid && sid.length()) {
        pendingId[i] = ""; pendingMsg[i] = "";
        Serial.println(doc["ok"] | false ? "hub saved result" : "hub REJECTED result");
      }
  } else if (tp.endsWith("/scan")) {
    String uid;
    if (isJson) uid = String(doc["uid"] | "");
    else for (unsigned int i = 0; i < len; i++) uid += (char)payload[i];
    uid.trim(); uid.toUpperCase();
    if (measuring || queuedUid.length()) sendEvent("scan_ignored_busy", uid);
    else queuedUid = uid;
  }
}

void ensureConnected() {
  static uint32_t lastTry = 0;
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED || millis() - lastTry < 2000) return;
  lastTry = millis();
  String cid = String(STATION_ID) + "-esp";
  if (mqtt.connect(cid.c_str(), STATION_ID, MQTT_PASS, topic("status").c_str(), 1, true, "offline")) {
    setStatus(measuring ? "busy" : "online");
    mqtt.subscribe(topic("ack").c_str(), 1);
    if (RFID_VIA_MQTT) mqtt.subscribe(topic("scan").c_str(), 1);
    Serial.println("MQTT connected");
  } else {
    Serial.printf("MQTT connect failed rc=%d (4/5 = bad user/password)\n", mqtt.state());
  }
}

void resendPending() {
  static uint32_t last = 0;
  if (millis() - last < 5000 || !mqtt.connected()) return;
  last = millis();
  for (int i = 0; i < MAX_PENDING; i++)
    if (pendingId[i].length()) mqtt.publish(topic("result").c_str(), pendingMsg[i].c_str());
}

void service() { ensureConnected(); mqtt.loop(); resendPending(); }

void stationDelay(uint32_t ms) {
  uint32_t start = millis();
  while (millis() - start < ms) { service(); delay(10); }
}

void submitResult(const String& uid, JsonDocument& doc) {
  String sid = String(esp_random(), HEX) + String(esp_random(), HEX);
  doc["uid"] = uid; doc["session_id"] = sid;
  String ts = nowIso(); if (ts.length()) doc["ts"] = ts;
  String out; serializeJson(doc, out);
  int slot = -1;
  for (int i = 0; i < MAX_PENDING; i++) if (!pendingId[i].length()) { slot = i; break; }
  if (slot < 0) { Serial.println("!! pending buffer full - oldest result overwritten"); slot = 0; }
  pendingId[slot] = sid; pendingMsg[slot] = out;
  mqtt.publish(topic("result").c_str(), out.c_str());
}

void setup() {
  Serial.begin(115200);
  base = String(TOPIC_ROOT) + "/" + STATION_ID;
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  configTime(0, 0, "pool.ntp.org");
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setBufferSize(1024);
  mqtt.setKeepAlive(15);
  mqtt.setSocketTimeout(5);
  userSetup();
}

void loop() {
  service();
  static String lastUid; static uint32_t lastUidAt = 0;
  if (!RFID_VIA_MQTT) {
    String uid = pollRfid();
    if (uid.length() && !(uid == lastUid && millis() - lastUidAt < 5000)) {
      lastUid = uid; lastUidAt = millis();
      JsonDocument d; d["uid"] = uid; publishJson("scan", d);
      if (HAS_MEASUREMENT) queuedUid = uid;
    }
  }
  if (HAS_MEASUREMENT && queuedUid.length()) {
    String uid = queuedUid;
    measuring = true; setStatus("busy");
    JsonDocument doc;
    if (runMeasurement(uid, doc["data"].to<JsonObject>())) submitResult(uid, doc);
    else sendEvent("measure_failed", uid);
    measuring = false; queuedUid = ""; setStatus("online");
  }
}