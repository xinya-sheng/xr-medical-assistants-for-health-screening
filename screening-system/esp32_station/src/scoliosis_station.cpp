// ESP32-S3 RFID reader station (PlatformIO).
//
// Flow: scan card -> publish scan -> WAIT (no scanning) until the laptop station publishes
// the result for that UID and the hub acks it -> then scan the next card.
// The laptop's station_template.py must use RFID_SOURCE = "mqtt" and the SAME STATION_ID.
#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include <sys/time.h>
#include <SPI.h>
#include <MFRC522.h>

// ============================== CONFIG ==============================
const char* WIFI_SSID   = "fbiSurveillanceVan19";
const char* WIFI_PASS   = "fbiOpenUp";
const char* MQTT_HOST   = "10.118.217.173";    // hub laptop's reserved IP
const uint16_t MQTT_PORT = 1883;
const char* STATION_ID  = "station02";       // = MQTT username; must match the laptop station's ID
const char* MQTT_PASS   = "help";
const char* TOPIC_ROOT  = "screening";

const bool HAS_MEASUREMENT = false;          // reader only: the laptop does the measuring
const bool RFID_VIA_MQTT   = false;          // this ESP32 reads the cards itself
const uint32_t RESULT_WAIT_TIMEOUT_MS = 120000; // give up waiting after 2 min so it can't lock up

// ESP32-S3 RFID pins
#define SS_PIN    10
#define RST_PIN   9
#define SCK_PIN   12
#define MOSI_PIN  11
#define MISO_PIN  13

void stationDelay(uint32_t ms);

// ============================== RFID ================================
MFRC522 rfid(SS_PIN, RST_PIN);

void userSetup() {
  SPI.begin(SCK_PIN, MISO_PIN, MOSI_PIN, SS_PIN);
  rfid.PCD_Init();
  delay(100);
  rfid.PCD_AntennaOn();
  Serial.println("RFID reader ready");
}

// Returns card UID as uppercase hex, or "" if no card.
String pollRfid() {
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return "";
  String uid;
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  return uid;
}

// Unused in reader-only mode (kept so the framework compiles unchanged).
bool runMeasurement(const String& uid, JsonObject data) { return false; }

// ============================== FRAMEWORK ===========================
WiFiClient net;
PubSubClient mqtt(net);
String base;
const int MAX_PENDING = 8;
String pendingId[MAX_PENDING], pendingMsg[MAX_PENDING];
String queuedUid;
bool measuring = false;

// Gate state: a scan has been published and we're waiting for the laptop's result
String awaitingUid;          // UID we published, "" = free to scan
String awaitingSession;      // session_id of the matching result (once seen)
uint32_t awaitingSince = 0;

String topic(const char* kind) { return base + "/" + kind; }

String nowIso() {
  if (time(nullptr) < 1700000000) return "";
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

void sendEvent(const char* type, const String& uid) {
  JsonDocument d; d["type"] = type; d["uid"] = uid; publishJson("event", d);
}

void releaseGate(const char* why) {
  Serial.printf("Gate open (%s) - ready for next card\n", why);
  awaitingUid = ""; awaitingSession = "";
}

void onMqttMessage(char* t, byte* payload, unsigned int len) {
  Serial.printf("RX [%s] %.*s\n", t, len, (const char*)payload); // comment out in production
  String tp(t);
  JsonDocument doc;
  bool isJson = !deserializeJson(doc, payload, len);
  if (!isJson) return;

  if (tp.endsWith("/result")) {
    // Laptop published the measurement for the card we're waiting on
    String uid = doc["uid"] | "";
    uid.toUpperCase();
    if (awaitingUid.length() && uid == awaitingUid) {
      awaitingSession = String(doc["session_id"] | "");
      Serial.println("Result seen, waiting for hub ack");
    }
  } else if (tp.endsWith("/ack")) {
    String sid = doc["session_id"] | "";
    bool ok = doc["ok"] | false;
    if (awaitingUid.length() && sid.length() && sid == awaitingSession) {
      if (ok) releaseGate("hub saved result");
      else Serial.println("hub REJECTED result - still waiting (laptop will resend)");
    }
  }
}

void ensureConnected() {
  static uint32_t lastTry = 0;
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED || millis() - lastTry < 2000) return;
  lastTry = millis();
  String cid = String(STATION_ID) + "-esp";
  // No Last Will / status here: the laptop station owns the status topic for this ID.
  if (mqtt.connect(cid.c_str(), STATION_ID, MQTT_PASS)) {
    mqtt.subscribe(topic("ack").c_str(), 1);
    mqtt.subscribe(topic("result").c_str(), 1);
    Serial.println("MQTT connected");
  } else {
    Serial.printf("MQTT connect failed rc=%d (4/5 = bad user/password)\n", mqtt.state());
  }
}

void service() { ensureConnected(); mqtt.loop(); }

void stationDelay(uint32_t ms) {
  uint32_t start = millis();
  while (millis() - start < ms) { service(); delay(10); }
}

void setup() {
  Serial.begin(115200);
  delay(2000);
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

  // Gate closed: don't read cards until the laptop's result has been saved
  if (awaitingUid.length()) {
    if (millis() - awaitingSince > RESULT_WAIT_TIMEOUT_MS) {
      sendEvent("result_wait_timeout", awaitingUid);
      releaseGate("timeout");
    }
    delay(20);
    return;
  }

  // Gate open: scan the next card (needs MQTT up so the scan isn't lost)
  if (!mqtt.connected()) { delay(50); return; }
  String uid = pollRfid();
  if (uid.length()) {
    Serial.printf("Card %s -> publishing scan, waiting for measurement\n", uid.c_str());
    JsonDocument d; d["uid"] = uid; publishJson("scan", d);
    awaitingUid = uid; awaitingSession = ""; awaitingSince = millis();
  } else {
    delay(50);
  }
}