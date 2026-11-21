#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <time.h>
#include "secrets.h"

const int   RELAY_GPIO             = 13;
const bool  RELAY_ACTIVE_HIGH      = true;
const bool  LOAD_ON_WHEN_ENERGIZED = false;
const int   ADC_GPIO               = 34;

const float SENSITIVITY_V_PER_A = 0.100f;
const float DIVIDER_GAIN        = 1.0f;
const float ACS_IDLE_V          = 2.5f;
const float IDLE_TOLERANCE_V    = 0.35f;
const float LOAD_VOLTAGE_V      = 3.3f;
const float DEADBAND_A          = 0.005f;

const float OVERCURRENT_A     = 0.30f;
const int   OVERCURRENT_READS = 2;

const char* HOUSE  = "H01";
const char* DEVICE = "water_heater_01";

const float SENSE_PERIOD_S     = 0.5f;
const float TELEMETRY_PERIOD_S = 5.0f;
const float HEARTBEAT_PERIOD_S = 30.0f;
const float RELAY_SETTLE_S     = 1.5f;
const float WIFI_RETRY_S       = 10.0f;
const float MQTT_RETRY_S       = 5.0f;
const long  TZ_OFFSET_S        = 7 * 3600;
const char* TZ_SUFFIX          = "+07:00";

const bool  DEBUG_SENSE     = true;
const bool  NETWORK_ENABLED = true;

const float BASELINE_TAU_S  = 20.0f;
const float BASELINE_GATE_A = 0.20f;
const float BENCH_CYCLE_S   = 0.0f;

// Forced cycle (ignores the schedule): load ON for FORCE_ON_S, then OFF for
// FORCE_OFF_S, repeating. Set FORCE_ON to false to go back to schedule control.
const bool  FORCE_ON        = true;
const float FORCE_ON_S      = 600.0f;  // 10 minutes on
const float FORCE_OFF_S     = 60.0f;   // 1 minute off

struct Schedule { char id[24]; time_t start; time_t end; bool valid; };
struct Sample { time_t ts; float watts; float amps; bool relay; };

uint32_t toMs(float seconds) { return (uint32_t)(seconds * 1000.0f + 0.5f); }
float sinceS(uint32_t sinceMs) { return (uint32_t)(millis() - sinceMs) / 1000.0f; }

int64_t daysFromCivil(int y, int m, int d) {
  y -= (m <= 2);
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned doy = (153 * (m > 2 ? m - 3 : m + 9) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return (int64_t)era * 146097 + (int64_t)doe - 719468;
}

bool parseIso(const char* s, time_t& out) {
  int Y, M, D, h, mi, sec;
  if (!s || strlen(s) < 19) return false;
  if (sscanf(s, "%d-%d-%dT%d:%d:%d", &Y, &M, &D, &h, &mi, &sec) != 6) return false;
  const char* p = s + 19;
  if (*p == '.') {
    while (*p && *p != '+' && *p != '-' && *p != 'Z') p++;
  }
  long offSec = 0;
  if (*p == '+' || *p == '-') {
    int oh = 0, om = 0;
    sscanf(p + 1, "%d:%d", &oh, &om);
    offSec = (oh * 3600L + om * 60L) * (*p == '-' ? -1 : 1);
  }
  out = (time_t)(daysFromCivil(Y, M, D) * 86400LL + h * 3600LL + mi * 60LL + sec - offSec);
  return true;
}

void fmtIso(time_t t, char* out, size_t n) {
  time_t local = t + TZ_OFFSET_S;
  struct tm tmv;
  gmtime_r(&local, &tmv);
  size_t k = strftime(out, n, "%Y-%m-%dT%H:%M:%S", &tmv);
  snprintf(out + k, n - k, "%s", TZ_SUFFIX);
}

bool timeValid() { return time(nullptr) > 1700000000; }

WiFiClient   net;
PubSubClient mqtt(net);
Preferences  prefs;

char TOPIC_TELEMETRY[96], TOPIC_STATUS[96], TOPIC_SCHEDULE[96], TOPIC_OVERRIDE[96], CLIENT_ID[64];

float    zeroPinV = 1.65f;
bool     relayOn = false;
uint32_t relayChangedMs = 0;
bool     latched = false;
int      overCount = 0;
bool     statusDirty = true;

enum Manual { MANUAL_AUTO, MANUAL_ON, MANUAL_OFF };
Manual   manual = MANUAL_AUTO;
uint32_t manualUntilMs = 0;

// Forced on/off cycle state (used when FORCE_ON is true)
bool     cycleOn = true;
uint32_t cycleChangedMs = 0;

Schedule sched = {"", 0, 0, false};

float lastAmps = 0.0f;
float accAmps = 0.0f;
int   accN = 0;
bool  ntpStarted = false;

const int BUF_N = 240;
Sample buf[BUF_N];
int bufHead = 0, bufCount = 0;

void bufPush(const Sample& s) {
  buf[(bufHead + bufCount) % BUF_N] = s;
  if (bufCount < BUF_N) bufCount++;
  else bufHead = (bufHead + 1) % BUF_N;
}

void setRelay(bool on) {
  bool coil = (on == LOAD_ON_WHEN_ENERGIZED);
  digitalWrite(RELAY_GPIO, (coil == RELAY_ACTIVE_HIGH) ? HIGH : LOW);
  if (on != relayOn) {
    relayOn = on;
    relayChangedMs = millis();
    statusDirty = true;
    Serial.printf("load -> %s\n", on ? "ON" : "OFF");
  }
}

float readPinVolts(int n) {
  uint32_t sumMv = 0;
  for (int i = 0; i < n; i++) {
    sumMv += analogReadMilliVolts(ADC_GPIO);
    delayMicroseconds(150);
  }
  return ((float)sumMv / n) / 1000.0f;
}

float voltsToAmps(float pinV, float zeroV) {
  return (pinV - zeroV) / (DIVIDER_GAIN * SENSITIVITY_V_PER_A);
}

void calibrate() {
  setRelay(false);
  delay(toMs(RELAY_SETTLE_S));
  zeroPinV = readPinVolts(4000);

  float expectedV = ACS_IDLE_V * DIVIDER_GAIN;
  Serial.printf("sensor idle level: %.4f V (expected about %.4f V)\n", zeroPinV, expectedV);
  if (fabsf(zeroPinV - expectedV) > IDLE_TOLERANCE_V) {
    Serial.println("WARNING: idle level far from expected. Check ACS712 5 V/GND and the OUT wire to the ADC pin.");
  }

  setRelay(true);
  delay(toMs(RELAY_SETTLE_S));
  float loadedV = readPinVolts(4000);
  setRelay(false);

  float testAmps = voltsToAmps(loadedV, zeroPinV);
  Serial.printf("self-test: loaded level %.4f V, current %.4f A\n", loadedV, testAmps);
  if (testAmps < -DEADBAND_A) {
    Serial.println("WARNING: negative current. If the LED was lit during the idle reading, set RELAY_ACTIVE_HIGH to the opposite value.");
  } else if (testAmps < DEADBAND_A) {
    Serial.println("WARNING: no current detected with the load on. Check the wiring and the relay polarity.");
  }
}

// Called at the end of each OFF window, while the load has been off for a
// while, so the sensor reading is a clean zero-current reference.
void snapZero() {
  float v = readPinVolts(4000);
  float expectedV = ACS_IDLE_V * DIVIDER_GAIN;
  if (fabsf(v - expectedV) <= IDLE_TOLERANCE_V) {
    Serial.printf("re-zero: %.4f V (was %.4f V, drift %+.1f mV)\n", v, zeroPinV, (v - zeroPinV) * 1000.0f);
    zeroPinV = v;
  } else {
    Serial.println("re-zero skipped: idle level out of range");
  }
  accAmps = 0.0f;
  accN = 0;
}

void senseOnce() {
  float pinV = readPinVolts(800);
  float a = voltsToAmps(pinV, zeroPinV);

  bool settled = !relayOn && sinceS(relayChangedMs) > 2.0f * RELAY_SETTLE_S;
  if (settled && fabsf(a) < BASELINE_GATE_A) {
    zeroPinV += (pinV - zeroPinV) * (SENSE_PERIOD_S / BASELINE_TAU_S);
    a = voltsToAmps(pinV, zeroPinV);
  }

  lastAmps = fabsf(a);
  accAmps += a;
  accN++;

  if (DEBUG_SENSE) {
    Serial.printf("pin=%.4f V  zero=%.4f V  I=%.4f A  load=%d\n", pinV, zeroPinV, a, relayOn ? 1 : 0);
  }
}

const char* safetyState() {
  if (latched) return "overcurrent";
  if (manual == MANUAL_OFF) return "forced_off";
  return "ok";
}

void applyRules() {
  if (!latched && relayOn && lastAmps > OVERCURRENT_A) {
    if (++overCount >= OVERCURRENT_READS) {
      latched = true;
      statusDirty = true;
      Serial.println("SAFETY: overcurrent, relay forced off until reset");
    }
  } else {
    overCount = 0;
  }

  bool want = false;
  if (!latched) {
    if (manual == MANUAL_ON && (int32_t)(millis() - manualUntilMs) >= 0) {
      manual = MANUAL_AUTO;
      statusDirty = true;
    }
    if (manual == MANUAL_ON) {
      want = true;
    } else if (manual == MANUAL_AUTO) {
      if (BENCH_CYCLE_S > 0.0f) {
        want = fmodf(millis() / 1000.0f, 2.0f * BENCH_CYCLE_S) < BENCH_CYCLE_S;
      } else {
        time_t now = time(nullptr);
        want = sched.valid && timeValid() && now >= sched.start && now < sched.end;
      }
    }
  }

  if (FORCE_ON && !latched && manual != MANUAL_OFF) {
    float phaseS = cycleOn ? FORCE_ON_S : FORCE_OFF_S;
    if (sinceS(cycleChangedMs) >= phaseS) {
      if (!cycleOn) snapZero();          // end of OFF window: refresh the zero reference
      cycleOn = !cycleOn;
      cycleChangedMs = millis();
      Serial.printf("cycle: load %s for %.0f s\n", cycleOn ? "ON" : "OFF", cycleOn ? FORCE_ON_S : FORCE_OFF_S);
    }
    want = cycleOn;
  } else {
    // Not forcing right now (latched or manual OFF): next time forcing resumes,
    // start a fresh ON period.
    cycleOn = true;
    cycleChangedMs = millis();
  }
  setRelay(want);
}

void saveSchedule() {
  prefs.begin("sched", false);
  prefs.putString("id", sched.id);
  prefs.putLong64("start", (int64_t)sched.start);
  prefs.putLong64("end", (int64_t)sched.end);
  prefs.end();
}

void loadSchedule() {
  prefs.begin("sched", false);
  String id = prefs.getString("id", "");
  int64_t st = prefs.getLong64("start", 0);
  int64_t en = prefs.getLong64("end", 0);
  prefs.end();
  if (id.length() && en > st) {
    strlcpy(sched.id, id.c_str(), sizeof(sched.id));
    sched.start = (time_t)st;
    sched.end = (time_t)en;
    sched.valid = true;
    Serial.printf("restored schedule %s from flash\n", sched.id);
  }
}

void publishStatus() {
  if (!mqtt.connected()) return;
  char ts[40], p[192];
  fmtIso(time(nullptr), ts, sizeof(ts));
  snprintf(p, sizeof(p), "{\"online\":true,\"safety_state\":\"%s\",\"relay\":%d,\"ts\":\"%s\"}",
           safetyState(), relayOn ? 1 : 0, ts);
  mqtt.publish(TOPIC_STATUS, p, true);
}

bool publishSample(const Sample& s) {
  char ts[40], p[192];
  fmtIso(s.ts, ts, sizeof(ts));
  snprintf(p, sizeof(p), "{\"ts\":\"%s\",\"watts\":%.3f,\"amps\":%.4f,\"relay\":%d}",
           ts, s.watts, s.amps, s.relay ? 1 : 0);
  return mqtt.publish(TOPIC_TELEMETRY, p);
}

void flushBuffer() {
  int sent = 0;
  while (mqtt.connected() && bufCount > 0 && sent < 10) {
    if (!publishSample(buf[bufHead])) break;
    bufHead = (bufHead + 1) % BUF_N;
    bufCount--;
    sent++;
  }
}

void handleSchedule(JsonDocument& d) {
  const char* status = d["status"] | "";
  if (strcmp(status, "scheduled") != 0) return;
  const char* id = d["schedule_id"] | "";
  time_t st, en;
  if (!parseIso(d["start"] | "", st) || !parseIso(d["end"] | "", en) || en <= st) {
    Serial.println("ignored schedule: bad times");
    return;
  }
  if (sched.valid && strcmp(sched.id, id) == 0 && sched.start == st && sched.end == en) return;
  strlcpy(sched.id, id, sizeof(sched.id));
  sched.start = st;
  sched.end = en;
  sched.valid = true;
  saveSchedule();
  char a[40], b[40];
  fmtIso(st, a, sizeof(a));
  fmtIso(en, b, sizeof(b));
  Serial.printf("new schedule %s: %s to %s\n", sched.id, a, b);
}

void handleOverride(JsonDocument& d) {
  const char* action = d["action"] | "";
  if (!strcmp(action, "on")) {
    float durationS = (d["minutes"] | 10.0f) * 60.0f;
    manual = MANUAL_ON;
    manualUntilMs = millis() + toMs(durationS);
  } else if (!strcmp(action, "off")) {
    manual = MANUAL_OFF;
  } else if (!strcmp(action, "auto")) {
    manual = MANUAL_AUTO;
  } else if (!strcmp(action, "reset")) {
    latched = false;
    overCount = 0;
  }
  statusDirty = true;
  Serial.printf("override: %s\n", action);
}

void onMessage(char* topic, byte* payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) {
    Serial.println("ignored message: bad JSON");
    return;
  }
  if (strcmp(topic, TOPIC_SCHEDULE) == 0) handleSchedule(doc);
  else if (strcmp(topic, TOPIC_OVERRIDE) == 0) handleOverride(doc);
}

void wifiTick() {
  static uint32_t lastMs = 0;
  static bool tried = false;
  if (WiFi.status() == WL_CONNECTED) return;
  if (tried && sinceS(lastMs) < WIFI_RETRY_S) return;
  tried = true;
  lastMs = millis();
  Serial.println("Wi-Fi connecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void ntpTick() {
  if (ntpStarted || WiFi.status() != WL_CONNECTED) return;
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  ntpStarted = true;
  Serial.println("NTP started");
}

void mqttTick() {
  if (mqtt.connected()) {
    mqtt.loop();
    return;
  }
  static uint32_t lastMs = 0;
  static bool tried = false;
  if (WiFi.status() != WL_CONNECTED) return;
  if (tried && sinceS(lastMs) < MQTT_RETRY_S) return;
  tried = true;
  lastMs = millis();
  Serial.printf("MQTT connecting to %s:%d ... ", MQTT_HOST, MQTT_PORT);
  if (mqtt.connect(CLIENT_ID, MQTT_USER, MQTT_PASS, TOPIC_STATUS, 1, true, "{\"online\":false}")) {
    Serial.println("ok");
    mqtt.subscribe(TOPIC_SCHEDULE, 1);
    mqtt.subscribe(TOPIC_OVERRIDE, 1);
    statusDirty = true;
  } else {
    Serial.printf("failed, rc=%d\n", mqtt.state());
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nI-Kit edge node starting");

  snprintf(TOPIC_TELEMETRY, sizeof(TOPIC_TELEMETRY), "home/%s/device/%s/telemetry/power", HOUSE, DEVICE);
  snprintf(TOPIC_STATUS,    sizeof(TOPIC_STATUS),    "home/%s/device/%s/status", HOUSE, DEVICE);
  snprintf(TOPIC_SCHEDULE,  sizeof(TOPIC_SCHEDULE),  "home/%s/device/%s/command/schedule", HOUSE, DEVICE);
  snprintf(TOPIC_OVERRIDE,  sizeof(TOPIC_OVERRIDE),  "home/%s/device/%s/command/override", HOUSE, DEVICE);
  snprintf(CLIENT_ID,       sizeof(CLIENT_ID),       "esp32-%s-%s", HOUSE, DEVICE);

  pinMode(RELAY_GPIO, OUTPUT);
  setRelay(false);
  analogReadResolution(12);
  analogSetPinAttenuation(ADC_GPIO, ADC_11db);
  calibrate();
  loadSchedule();

  // Start the first full ON period now (calibration takes a few seconds).
  cycleOn = true;
  cycleChangedMs = millis();

  if (NETWORK_ENABLED) WiFi.mode(WIFI_STA);
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);
  mqtt.setSocketTimeout(3);
}

void loop() {
  static uint32_t lastSenseMs = 0, lastTelemetryMs = 0, lastStatusMs = 0;

  if (NETWORK_ENABLED) {
    wifiTick();
    ntpTick();
    mqttTick();
  }

  if (sinceS(lastSenseMs) >= SENSE_PERIOD_S) {
    lastSenseMs = millis();
    senseOnce();
    applyRules();
  }

  if (sinceS(lastTelemetryMs) >= TELEMETRY_PERIOD_S) {
    lastTelemetryMs = millis();
    if (accN > 0) {
      static float ampsEma = 0.0f;
      static bool  lastLoad = false;
      float raw = fabsf(accAmps / accN);
      if (relayOn != lastLoad) { ampsEma = raw; lastLoad = relayOn; }
      ampsEma += 0.2f * (raw - ampsEma);
      float amps = ampsEma;
      if (amps < DEADBAND_A) amps = 0.0f;
      accAmps = 0.0f;
      accN = 0;
      float watts = amps * LOAD_VOLTAGE_V;
      Serial.printf("avg %.4f A  %.4f W  load=%d  wifi=%d  mqtt=%d  buffered=%d  time=%d\n",
                    amps, watts, relayOn ? 1 : 0, WiFi.status() == WL_CONNECTED ? 1 : 0,
                    mqtt.connected() ? 1 : 0, bufCount, timeValid() ? 1 : 0);
      if (timeValid()) {
        Sample s = {time(nullptr), watts, amps, relayOn};
        bufPush(s);
      }
    }
  }

  flushBuffer();

  if (mqtt.connected() && (statusDirty || sinceS(lastStatusMs) >= HEARTBEAT_PERIOD_S)) {
    publishStatus();
    statusDirty = false;
    lastStatusMs = millis();
  }
}
