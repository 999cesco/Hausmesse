// =====================================================================
//  Hausmesse Smarthome - ESP32
//  Steuert das Modellhaus und bietet zwei HTTP-Endpunkte fuer den Pi:
//    GET  /api/state    -> aktueller Zustand aller Sensoren und Geraete
//    POST /api/command  -> Befehle ausfuehren
// =====================================================================

#include <Arduino.h>
#include <DHT.h>
#include <IRremote.hpp>
#include <ESP32Servo.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <ArduinoJson.h>
#include <WebServer.h>

// -------------------- Pins --------------------
constexpr int SOIL_PIN = 34;
constexpr int DRY_VALUE = 0;
constexpr int WET_VALUE = 2100;

constexpr int DHT_PIN = 17;
#define DHTTYPE DHT11

constexpr int HEAT_PIN = 12;  // rote LED   = Heizung
constexpr int FAN_PIN = 14;   // gruene LED = Luefter
constexpr int COOL_PIN = 27;  // blaue LED  = Klima kuehlt

constexpr int IR_PIN = 4;
constexpr int BUZZER_PIN = 25;
constexpr int SERVO_PIN = 18;
constexpr int WIFI_RESET_BUTTON_PIN = 32;

constexpr uint8_t IR_UP_CODE = 0x18;
constexpr uint8_t IR_DOWN_CODE = 0x52;

// -------------------- Lichter (dimmbar) --------------------
constexpr int NUM_LIGHTS = 5;
const char* LIGHT_ROOMS[NUM_LIGHTS] = {"schlafzimmer", "arbeitszimmer", "wohnzimmer", "kueche", "garage"};
const int LIGHT_PINS[NUM_LIGHTS] = {13, 16, 19, 21, 22};
int lightBrightness[NUM_LIGHTS] = {0, 0, 0, 0, 0};  // 0..100 %
ESP32PWM lightPwm[NUM_LIGHTS];

// -------------------- Fenster --------------------
// Pin -1 = kein Sensor angeschlossen -> Fenster wird simuliert (per Befehl/Dashboard setzbar)
// Mit Reed-Kontakt: Kontakt zwischen Pin und GND, z.B. {23, 26, 33}
constexpr int NUM_WINDOWS = 3;
const char* WINDOW_ROOMS[NUM_WINDOWS] = {"wohnzimmer", "schlafzimmer", "kueche"};
const int WINDOW_PINS[NUM_WINDOWS] = {-1, -1, -1};
bool windowOpen[NUM_WINDOWS] = {false, false, false};

// -------------------- Zeiten --------------------
constexpr unsigned long SENSOR_INTERVAL_MS = 2000;
constexpr unsigned long WIFI_RESET_HOLD_TIME_MS = 3000;
constexpr unsigned long BUTTON_DEBOUNCE_MS = 40;
constexpr unsigned long SERVO_STEP_MS = 40;
constexpr unsigned long BEEP_ON_MS = 300;
constexpr unsigned long BEEP_OFF_MS = 800;

constexpr int GARAGE_OPEN_ANGLE = 90;
constexpr int GARAGE_CLOSED_ANGLE = 0;

constexpr float HYSTERESIS = 0.5;  // Grad, damit die Heizung nicht staendig an/aus springt

// -------------------- Objekte --------------------
DHT dht(DHT_PIN, DHTTYPE);
Servo garageServo;
WiFiManager wm;
WebServer server(80);

// -------------------- Zustand --------------------
String deviceId = "esp32-haus";

float lastTemperature = NAN;
float lastHumidity = NAN;
int lastSoilPercent = 0;

// Klima: Modus off / heat / cool / auto, Zieltemperatur
String climateMode = "off";
float targetTemp = 21.0;
bool heating = false;
bool cooling = false;

// Luefter: auto (laeuft mit, wenn gekuehlt wird) / on / off
String fanMode = "auto";
bool fanOn = false;

// Garage (nicht-blockierend, Servo faehrt schrittweise in loop())
int currentServoAngle = GARAGE_CLOSED_ANGLE;
int garageTargetAngle = GARAGE_CLOSED_ANGLE;
unsigned long lastServoStep = 0;
unsigned long lastBeepToggle = 0;
bool beepPhaseOn = false;

bool buzzerManual = false;

unsigned long lastSensorRead = 0;

bool buttonStableState = HIGH;
bool lastButtonReading = HIGH;
unsigned long lastDebounceTime = 0;
unsigned long buttonPressStart = 0;
bool wifiResetTriggered = false;

// =====================================================================
//  Hilfsfunktionen
// =====================================================================
int findLight(const String& room) {
  for (int i = 0; i < NUM_LIGHTS; i++) {
    if (room == LIGHT_ROOMS[i]) return i;
  }
  return -1;
}

int findWindow(const String& room) {
  for (int i = 0; i < NUM_WINDOWS; i++) {
    if (room == WINDOW_ROOMS[i]) return i;
  }
  return -1;
}

void setLight(int idx, int brightness) {
  brightness = constrain(brightness, 0, 100);
  lightBrightness[idx] = brightness;
  lightPwm[idx].writeScaled(brightness / 100.0);
}

int readSoilPercent() {
  int raw = analogRead(SOIL_PIN);
  return constrain(map(raw, DRY_VALUE, WET_VALUE, 0, 100), 0, 100);
}

// -------------------- Klima-Regelung --------------------
void updateClimate() {
  bool mayHeat = (climateMode == "heat" || climateMode == "auto");
  bool mayCool = (climateMode == "cool" || climateMode == "auto");

  if (isnan(lastTemperature) || climateMode == "off") {
    heating = false;
    cooling = false;
  } else {
    // Heizen: an, wenn deutlich zu kalt; aus, sobald Ziel erreicht
    if (!mayHeat) heating = false;
    else if (lastTemperature < targetTemp - HYSTERESIS) heating = true;
    else if (lastTemperature >= targetTemp) heating = false;

    // Kuehlen: an, wenn deutlich zu warm; aus, sobald Ziel erreicht
    if (!mayCool) cooling = false;
    else if (lastTemperature > targetTemp + HYSTERESIS) cooling = true;
    else if (lastTemperature <= targetTemp) cooling = false;
  }

  if (fanMode == "on") fanOn = true;
  else if (fanMode == "off") fanOn = false;
  else fanOn = cooling;  // auto

  digitalWrite(HEAT_PIN, heating ? HIGH : LOW);
  digitalWrite(COOL_PIN, cooling ? HIGH : LOW);
  digitalWrite(FAN_PIN, fanOn ? HIGH : LOW);
}

// -------------------- Garage --------------------
void startGarage(bool open) {
  garageTargetAngle = open ? GARAGE_OPEN_ANGLE : GARAGE_CLOSED_ANGLE;
  Serial.println(open ? "Garage oeffnet..." : "Garage schliesst...");
}

bool garageMoving() {
  return currentServoAngle != garageTargetAngle;
}

String garageState() {
  if (garageMoving()) return garageTargetAngle == GARAGE_OPEN_ANGLE ? "opening" : "closing";
  return currentServoAngle == GARAGE_OPEN_ANGLE ? "open" : "closed";
}

// Wird in jedem loop()-Durchlauf aufgerufen: ein Servo-Schritt + Warnpiepen
void updateGarage() {
  unsigned long now = millis();

  if (!garageMoving()) {
    digitalWrite(BUZZER_PIN, buzzerManual ? HIGH : LOW);
    beepPhaseOn = false;
    return;
  }

  if (now - lastServoStep >= SERVO_STEP_MS) {
    lastServoStep = now;
    currentServoAngle += (garageTargetAngle > currentServoAngle) ? 1 : -1;
    garageServo.write(currentServoAngle);
    if (!garageMoving()) Serial.println("Garage: " + garageState());
  }

  unsigned long phase = beepPhaseOn ? BEEP_ON_MS : BEEP_OFF_MS;
  if (now - lastBeepToggle >= phase) {
    lastBeepToggle = now;
    beepPhaseOn = !beepPhaseOn;
    digitalWrite(BUZZER_PIN, beepPhaseOn ? HIGH : LOW);
  }
}

// =====================================================================
//  Befehle
// =====================================================================
// Format: {"device":"...", "room":"...", "action":"...", "value":123}
// Gibt "" zurueck, wenn ok, sonst eine Fehlermeldung.
String executeCommand(JsonObject c) {
  String device = c["device"] | "";
  String room = c["room"] | "";
  String action = c["action"] | "";
  bool hasValue = !c["value"].isNull();
  float value = c["value"] | 0.0f;

  if (device == "light") {
    int brightness;
    if (action == "on") brightness = hasValue ? (int)value : 100;
    else if (action == "off") brightness = 0;
    else if (action == "set" && hasValue) brightness = (int)value;
    else return "light: action on/off/set (+value)";

    if (room == "alle") {
      for (int i = 0; i < NUM_LIGHTS; i++) setLight(i, brightness);
    } else {
      int idx = findLight(room);
      if (idx < 0) return "light: unknown room";
      setLight(idx, brightness);
    }

  } else if (device == "climate") {
    if (action != "off" && action != "heat" && action != "cool" && action != "auto") {
      return "climate: action off/heat/cool/auto";
    }
    if (hasValue) {
      if (value < 10 || value > 30) return "climate: value 10..30";
      targetTemp = value;
    }
    climateMode = action;
    updateClimate();

  } else if (device == "fan") {
    if (action != "on" && action != "off" && action != "auto") return "fan: action on/off/auto";
    fanMode = action;
    updateClimate();

  } else if (device == "garage") {
    if (action == "open") startGarage(true);
    else if (action == "close") startGarage(false);
    else return "garage: action open/close";

  } else if (device == "buzzer") {
    if (action == "on") buzzerManual = true;
    else if (action == "off") buzzerManual = false;
    else return "buzzer: action on/off";

  } else if (device == "window") {
    int idx = findWindow(room);
    if (idx < 0) return "window: unknown room";
    if (WINDOW_PINS[idx] >= 0) return "window: has real sensor";
    if (action == "open") windowOpen[idx] = true;
    else if (action == "close") windowOpen[idx] = false;
    else return "window: action open/close";

  } else {
    return "unknown device";
  }
  return "";
}

// POST /api/command  -> {"commands":[...]} oder ein einzelner Befehl
void handleCommand() {
  JsonDocument doc;
  if (deserializeJson(doc, server.arg("plain"))) {
    server.send(400, "application/json", "{\"error\":\"invalid json\"}");
    return;
  }

  JsonDocument resp;
  JsonArray results = resp["results"].to<JsonArray>();
  bool allOk = true;

  JsonArray cmds = doc["commands"].as<JsonArray>();
  if (cmds.isNull()) {
    // Einzelbefehl: das Dokument selbst ist der Befehl
    String err = executeCommand(doc.as<JsonObject>());
    JsonObject r = results.add<JsonObject>();
    r["ok"] = err.length() == 0;
    if (err.length()) { r["error"] = err; allOk = false; }
  } else {
    for (JsonObject c : cmds) {
      String err = executeCommand(c);
      JsonObject r = results.add<JsonObject>();
      r["ok"] = err.length() == 0;
      if (err.length()) { r["error"] = err; allOk = false; }
    }
  }

  resp["status"] = allOk ? "ok" : "error";
  String out;
  serializeJson(resp, out);
  server.send(allOk ? 200 : 400, "application/json", out);
}

// GET /api/state
void handleState() {
  JsonDocument doc;
  doc["deviceId"] = deviceId;
  doc["uptimeMs"] = millis();
  doc["rssi"] = WiFi.RSSI();               // WLAN-Empfang fuer den Admin-Bereich

  JsonObject sensors = doc["sensors"].to<JsonObject>();
  if (isnan(lastTemperature)) sensors["temperature"] = nullptr;
  else sensors["temperature"] = lastTemperature;
  if (isnan(lastHumidity)) sensors["humidity"] = nullptr;
  else sensors["humidity"] = lastHumidity;
  sensors["soilPercent"] = lastSoilPercent;

  JsonObject lights = doc["lights"].to<JsonObject>();
  for (int i = 0; i < NUM_LIGHTS; i++) lights[LIGHT_ROOMS[i]] = lightBrightness[i];

  JsonObject windows = doc["windows"].to<JsonObject>();
  JsonArray simulated = doc["simulatedWindows"].to<JsonArray>();
  for (int i = 0; i < NUM_WINDOWS; i++) {
    windows[WINDOW_ROOMS[i]] = windowOpen[i] ? "open" : "closed";
    if (WINDOW_PINS[i] < 0) simulated.add(WINDOW_ROOMS[i]);
  }

  JsonObject climate = doc["climate"].to<JsonObject>();
  climate["mode"] = climateMode;
  climate["target"] = targetTemp;
  climate["heating"] = heating;
  climate["cooling"] = cooling;

  JsonObject fan = doc["fan"].to<JsonObject>();
  fan["mode"] = fanMode;
  fan["on"] = fanOn;

  doc["garage"] = garageState();
  doc["buzzer"] = buzzerManual ? "on" : "off";

  String out;
  serializeJson(doc, out);
  server.send(200, "application/json", out);
}

// =====================================================================
//  WLAN & Reset-Knopf
// =====================================================================
void setupWiFi() {
  WiFi.mode(WIFI_STA);
  wm.setConnectTimeout(15);
  wm.setConfigPortalTimeout(180);

  if (!wm.autoConnect("ESP32-IoT-Setup")) {
    Serial.println("Keine WLAN-Verbindung -> Neustart");
    delay(2000);
    ESP.restart();
  }
  Serial.print("WLAN verbunden, IP: ");
  Serial.println(WiFi.localIP());
}

void handleWifiResetButton() {
  int reading = digitalRead(WIFI_RESET_BUTTON_PIN);
  if (reading != lastButtonReading) lastDebounceTime = millis();

  if (millis() - lastDebounceTime > BUTTON_DEBOUNCE_MS && reading != buttonStableState) {
    buttonStableState = reading;
    buttonPressStart = (buttonStableState == LOW) ? millis() : 0;
    wifiResetTriggered = false;
  }
  lastButtonReading = reading;

  if (buttonStableState == LOW && !wifiResetTriggered && buttonPressStart > 0 &&
      millis() - buttonPressStart >= WIFI_RESET_HOLD_TIME_MS) {
    wifiResetTriggered = true;
    Serial.println("WLAN-Reset -> loesche WLAN-Daten und starte neu");
    digitalWrite(HEAT_PIN, HIGH);
    digitalWrite(COOL_PIN, HIGH);  // rot+blau = Reset-Signal
    wm.resetSettings();
    delay(1000);
    ESP.restart();
  }
}

// =====================================================================
//  Setup / Loop
// =====================================================================
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(HEAT_PIN, OUTPUT);
  pinMode(FAN_PIN, OUTPUT);
  pinMode(COOL_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(WIFI_RESET_BUTTON_PIN, INPUT_PULLUP);
  digitalWrite(BUZZER_PIN, LOW);

  for (int i = 0; i < NUM_WINDOWS; i++) {
    if (WINDOW_PINS[i] >= 0) pinMode(WINDOW_PINS[i], INPUT_PULLUP);
  }

  // PWM-Timer fuer Servo und dimmbare LEDs reservieren
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  garageServo.setPeriodHertz(50);
  garageServo.attach(SERVO_PIN, 500, 2400);
  garageServo.write(currentServoAngle);

  for (int i = 0; i < NUM_LIGHTS; i++) {
    lightPwm[i].attachPin(LIGHT_PINS[i], 1000, 10);  // 1 kHz, 10 Bit
    setLight(i, 0);
  }

  dht.begin();
  IrReceiver.begin(IR_PIN, DISABLE_LED_FEEDBACK);
  updateClimate();

  setupWiFi();

  server.on("/api/state", HTTP_GET, handleState);
  server.on("/api/command", HTTP_POST, handleCommand);
  server.begin();

  Serial.println("Bereit. State: http://" + WiFi.localIP().toString() + "/api/state");
}

void loop() {
  handleWifiResetButton();
  server.handleClient();
  updateGarage();

  // IR-Fernbedienung: hoch = Garage auf, runter = Garage zu
  if (IrReceiver.decode()) {
    uint8_t cmd = IrReceiver.decodedIRData.command;
    if (cmd == IR_UP_CODE) startGarage(true);
    else if (cmd == IR_DOWN_CODE) startGarage(false);
    IrReceiver.resume();
  }

  // Sensoren alle 2 s lesen
  if (millis() - lastSensorRead >= SENSOR_INTERVAL_MS) {
    lastSensorRead = millis();

    lastSoilPercent = readSoilPercent();
    lastHumidity = dht.readHumidity();
    lastTemperature = dht.readTemperature();

    for (int i = 0; i < NUM_WINDOWS; i++) {
      if (WINDOW_PINS[i] >= 0) {
        // INPUT_PULLUP: Magnet am Kontakt (Fenster zu) = LOW
        windowOpen[i] = digitalRead(WINDOW_PINS[i]) == HIGH;
      }
    }

    updateClimate();

    Serial.printf("T=%.1f C  H=%.0f %%  Boden=%d %%  Klima=%s/%.1f  Heizt=%d Kuehlt=%d  Garage=%s\n",
                  lastTemperature, lastHumidity, lastSoilPercent, climateMode.c_str(), targetTemp,
                  heating, cooling, garageState().c_str());
  }
}
