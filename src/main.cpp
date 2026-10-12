#include <Arduino.h>
#include <DHT.h>
#include <IRremote.hpp>
#include <ESP32Servo.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <WebServer.h>

// -------------------- Pins & Constants --------------------
constexpr int SENSOR_PIN = 34;
constexpr int DRY_VALUE = 0;
constexpr int WET_VALUE = 2100;

constexpr int DHTPIN = 17;
#define DHTTYPE DHT11

constexpr int LED_RED = 12;
constexpr int LED_GREEN = 14;
constexpr int LED_BLUE = 27;

constexpr int IR_PIN = 4;
constexpr int BUZZER_PIN = 25;
constexpr int SERVO_PIN = 18;
constexpr int WIFI_RESET_BUTTON_PIN = 32;

constexpr uint8_t IR_UP_CODE = 0x18;
constexpr uint8_t IR_DOWN_CODE = 0x52;

constexpr unsigned long WIFI_RESET_HOLD_TIME_MS = 3000;
constexpr unsigned long BUTTON_DEBOUNCE_MS = 40;
constexpr unsigned long SENSOR_PRINT_INTERVAL_MS = 2000;
constexpr unsigned long GATEWAY_SEND_INTERVAL_MS = 5000;

// -------------------- Objects --------------------
DHT dht(DHTPIN, DHTTYPE);
Servo garageServo;
Preferences preferences;
WiFiManager wm;
WebServer server(80);

// -------------------- State --------------------
bool garageOpen = false;
int currentServoAngle = 0;

unsigned long lastSensorPrint = 0;
unsigned long lastGatewaySend = 0;

bool buttonStableState = HIGH;
bool lastButtonReading = HIGH;
unsigned long lastDebounceTime = 0;
unsigned long buttonPressStart = 0;
bool wifiResetTriggered = false;

String deviceId = "esp32-node-01";
String roomName = "garage";
String gatewayHost = "";
String gatewayPort = "8080";

bool shouldSaveConfig = false;

// Push an Pi / Server
unsigned long lastPiSend = 0;
unsigned long piSendIntervalMs = 30000;  // 30 s, einstellbar 10–60 s

// Rolling buffer: letzte 5 Sensordaten
struct SensorSample {
  unsigned long ts;
  float temperature;
  float humidity;
  int soilRaw;
  int soilPercent;
  bool garageOpen;
};

constexpr int BUFFER_SIZE = 5;
SensorSample samples[BUFFER_SIZE];
uint8_t sampleIndex = 0;

// Pi / Web-Einstellungen
String piHost = "";
String piPort = "8080";
String webPass = "1234";  // Passwort für Dashboard

// Session: eingeloggter Client (einfache Implementierung: IP-basiert)
String loggedInClientIP = "";
unsigned long loginExpireMs = 0;

// -------------------- Helpers --------------------
void saveConfigCallback() {
  shouldSaveConfig = true;
}

void setColor(bool red, bool green, bool blue) {
  digitalWrite(LED_RED, red ? HIGH : LOW);
  digitalWrite(LED_GREEN, green ? HIGH : LOW);
  digitalWrite(LED_BLUE, blue ? HIGH : LOW);
}

void loadConfig() {
  preferences.begin("iotcfg", true);
  deviceId = preferences.getString("deviceId", "esp32-node-01");
  roomName = preferences.getString("roomName", "garage");
  gatewayHost = preferences.getString("gwHost", "");
  gatewayPort = preferences.getString("gwPort", "8080");

  piHost = preferences.getString("piHost", "");
  piPort = preferences.getString("piPort", "8080");
  piSendIntervalMs = preferences.getULong("piInterval", 30000);
  webPass = preferences.getString("webPass", "1234");

  preferences.end();
}

void saveConfig() {
  preferences.begin("iotcfg", false);
  preferences.putString("deviceId", deviceId);
  preferences.putString("roomName", roomName);
  preferences.putString("gwHost", gatewayHost);
  preferences.putString("gwPort", gatewayPort);

  preferences.putString("piHost", piHost);
  preferences.putString("piPort", piPort);
  preferences.putULong("piInterval", piSendIntervalMs);
  preferences.putString("webPass", webPass);

  preferences.end();

  Serial.println("Konfiguration gespeichert");
}

void clearCustomConfig() {
  preferences.begin("iotcfg", false);
  preferences.clear();
  preferences.end();

  Serial.println("Custom-Konfiguration gelöscht");
}

bool isValidPort(const String& portStr) {
  if (portStr.length() == 0) return false;

  for (size_t i = 0; i < portStr.length(); i++) {
    if (!isDigit(portStr[i])) return false;
  }

  int port = portStr.toInt();
  return port >= 1 && port <= 65535;
}

int readSoilPercent() {
  int sensorValue = analogRead(SENSOR_PIN);
  int moisturePercent = map(sensorValue, DRY_VALUE, WET_VALUE, 0, 100);
  return constrain(moisturePercent, 0, 100);
}

int readSoilRaw() {
  return analogRead(SENSOR_PIN);
}

void pushSensorSample(float temperature, float humidity, int soilRaw, int soilPercent, bool garageOpen) {
  samples[sampleIndex].ts = millis();
  samples[sampleIndex].temperature = temperature;
  samples[sampleIndex].humidity = humidity;
  samples[sampleIndex].soilRaw = soilRaw;
  samples[sampleIndex].soilPercent = soilPercent;
  samples[sampleIndex].garageOpen = garageOpen;

  sampleIndex = (sampleIndex + 1) % BUFFER_SIZE;
}

// -------------------- Servo / Garage --------------------
void moveServoSlowWithWarningBeep(int targetAngle, int stepDelayMs = 40) {
  unsigned long lastBeepToggle = millis();
  constexpr int beepOnTime = 300;
  constexpr int beepOffTime = 800;
  bool buzzerOn = false;

  if (targetAngle == currentServoAngle) {
    digitalWrite(BUZZER_PIN, LOW);
    return;
  }

  int step = (targetAngle > currentServoAngle) ? 1 : -1;

  for (int pos = currentServoAngle; pos != targetAngle + step; pos += step) {
    garageServo.write(pos);

    unsigned long now = millis();
    if (!buzzerOn && (now - lastBeepToggle >= beepOffTime)) {
      buzzerOn = true;
      lastBeepToggle = now;
      digitalWrite(BUZZER_PIN, HIGH);
    } else if (buzzerOn && (now - lastBeepToggle >= beepOnTime)) {
      buzzerOn = false;
      lastBeepToggle = now;
      digitalWrite(BUZZER_PIN, LOW);
    }

    delay(stepDelayMs);
  }

  digitalWrite(BUZZER_PIN, LOW);
  currentServoAngle = targetAngle;
}

void openGarage() {
  if (garageOpen) return;

  Serial.println("Garage wird geöffnet...");
  moveServoSlowWithWarningBeep(90, 40);
  garageOpen = true;
  Serial.println("Garage geöffnet");
}

void closeGarage() {
  if (!garageOpen) return;

  Serial.println("Garage wird geschlossen...");
  moveServoSlowWithWarningBeep(0, 40);
  garageOpen = false;
  Serial.println("Garage geschlossen");
}

// -------------------- WiFi --------------------
void setupWiFiManager() {
  WiFi.mode(WIFI_STA);
  delay(300);

  wm.setSaveConfigCallback(saveConfigCallback);
  wm.setConnectTimeout(15);
  wm.setConfigPortalTimeout(180);
  wm.setBreakAfterConfig(true);

  WiFiManagerParameter custom_device_id("deviceid", "Device ID", deviceId.c_str(), 32);
  WiFiManagerParameter custom_room("room", "Room Name", roomName.c_str(), 32);
  WiFiManagerParameter custom_gateway_host("gwhost", "Gateway Host/IP", gatewayHost.c_str(), 40);
  WiFiManagerParameter custom_gateway_port("gwport", "Gateway Port", gatewayPort.c_str(), 6);

  wm.addParameter(&custom_device_id);
  wm.addParameter(&custom_room);
  wm.addParameter(&custom_gateway_host);
  wm.addParameter(&custom_gateway_port);

  bool res = wm.autoConnect("ESP32-IoT-Setup");

  if (!res) {
    Serial.println("Keine WLAN-Verbindung. Portal beendet oder fehlgeschlagen.");
    delay(2000);
    ESP.restart();
    return;
  }

  String newDeviceId = String(custom_device_id.getValue());
  String newRoomName = String(custom_room.getValue());
  String newGatewayHost = String(custom_gateway_host.getValue());
  String newGatewayPort = String(custom_gateway_port.getValue());

  newDeviceId.trim();
  newRoomName.trim();
  newGatewayHost.trim();
  newGatewayPort.trim();

  if (newDeviceId.length() > 0) deviceId = newDeviceId;
  if (newRoomName.length() > 0) roomName = newRoomName;
  gatewayHost = newGatewayHost;
  if (isValidPort(newGatewayPort)) {
    gatewayPort = newGatewayPort;
  } else {
    gatewayPort = "8080";
  }

  saveConfig();

  Serial.println("WLAN verbunden");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());
  Serial.print("Device ID: ");
  Serial.println(deviceId);
  Serial.print("Room: ");
  Serial.println(roomName);
  Serial.print("Gateway Host: ");
  Serial.println(gatewayHost);
  Serial.print("Gateway Port: ");
  Serial.println(gatewayPort);
}

// -------------------- Button --------------------
void handleWifiResetButton() {
  int reading = digitalRead(WIFI_RESET_BUTTON_PIN);

  if (reading != lastButtonReading) {
    lastDebounceTime = millis();
  }

  if ((millis() - lastDebounceTime) > BUTTON_DEBOUNCE_MS) {
    if (reading != buttonStableState) {
      buttonStableState = reading;

      if (buttonStableState == LOW) {
        buttonPressStart = millis();
        wifiResetTriggered = false;
      } else {
        buttonPressStart = 0;
        wifiResetTriggered = false;
      }
    }
  }

  lastButtonReading = reading;

  if (buttonStableState == LOW && !wifiResetTriggered && buttonPressStart > 0) {
    if (millis() - buttonPressStart >= WIFI_RESET_HOLD_TIME_MS) {
      wifiResetTriggered = true;

      Serial.println("WLAN-Reset-Knopf lang gedrückt -> lösche WiFi und Konfiguration");
      setColor(true, false, true);

      wm.resetSettings();
      clearCustomConfig();

      delay(1000);
      ESP.restart();
    }
  }
}

// -------------------- Gateway --------------------
void sendDataToGateway(float temperature, float humidity, int soilRaw, int soilPercent) {
  if (WiFi.status() != WL_CONNECTED) return;
  if (gatewayHost.length() == 0) return;
  if (!isValidPort(gatewayPort)) return;

  HTTPClient http;
  http.setTimeout(3000);

  String url = "http://" + gatewayHost + ":" + gatewayPort + "/api/esp-data";

  if (!http.begin(url)) {
    Serial.println("HTTP begin fehlgeschlagen");
    return;
  }

  http.addHeader("Content-Type", "application/json");

  JsonDocument doc;
  doc["deviceId"] = deviceId;
  doc["room"] = roomName;
  doc["ip"] = WiFi.localIP().toString();
  doc["soilRaw"] = soilRaw;
  doc["soilPercent"] = soilPercent;
  doc["garageOpen"] = garageOpen;
  doc["uptimeMs"] = millis();

  if (isnan(temperature)) {
    doc["temperature"] = nullptr;
  } else {
    doc["temperature"] = temperature;
  }

  if (isnan(humidity)) {
    doc["humidity"] = nullptr;
  } else {
    doc["humidity"] = humidity;
  }

  String payload;
  serializeJson(doc, payload);

  int httpCode = http.POST(payload);

  Serial.print("Gateway POST -> ");
  Serial.print(url);
  Serial.print(" | HTTP Code: ");
  Serial.println(httpCode);

  if (httpCode > 0) {
    String response = http.getString();
    if (response.length() > 0) {
      Serial.print("Gateway Response: ");
      Serial.println(response);
    }
  } else {
    Serial.print("HTTP Fehler: ");
    Serial.println(http.errorToString(httpCode));
  }

  http.end();
}

// -------------------- Auth & Session --------------------
String getClientIP() {
  return server.client().remoteIP().toString();
}

bool isLoggedIn() {
  if (loggedInClientIP.length() == 0) return false;
  if (getClientIP() != loggedInClientIP) return false;
  if (millis() > loginExpireMs) {
    loggedInClientIP = "";
    loginExpireMs = 0;
    return false;
  }
  return true;
}

void setLoggedIn() {
  loggedInClientIP = getClientIP();
  loginExpireMs = millis() + 30UL * 60UL * 1000UL;  // 30 Minuten
}

void handleLoginRedirect() {
  server.sendHeader("Location", "/");
  server.send(303, "text/plain", "Redirect to login");
}

// -------------------- Login Page --------------------
void handleRoot() {
  // Wenn schon eingeloggt -> direkt zum Dashboard
  if (isLoggedIn()) {
    server.sendHeader("Location", "/dashboard");
    server.send(303, "text/plain", "Redirect to dashboard");
    return;
  }

  // Login-Formular anzeigen
  String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>ESP32 IoT Login</title></head><body>";
  html += "<h1>ESP32 IoT Login</h1>";
  html += "<form method='POST' action='/login'>";
  html += "Passwort: <input type='password' name='password'><br><br>";
  html += "<button type='submit'>Anmelden</button>";
  html += "</form></body></html>";

  server.send(200, "text/html", html);
}

void handleLoginPost() {
  if (!server.hasArg("password")) {
    server.send(400, "text/plain", "No password");
    return;
  }

  String pwd = server.arg("password");
  if (pwd == webPass) {
    setLoggedIn();
    server.sendHeader("Location", "/dashboard");
    server.send(303, "text/plain", "Login OK");
  } else {
    server.send(401, "text/plain", "Wrong password");
  }
}

// -------------------- Dashboard --------------------
void handleDashboard() {
  if (!isLoggedIn()) {
    handleLoginRedirect();
    return;
  }

  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  int soilRaw = readSoilRaw();
  int soilPercent = readSoilPercent();

  String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>ESP32 IoT Dashboard</title></head><body>";
  html += "<h1>ESP32 IoT Dashboard</h1>";

  html += "<h2>Geräte-Info</h2><ul>";
  html += "<li>Device ID: " + deviceId + "</li>";
  html += "<li>Room: " + roomName + "</li>";
  html += "<li>IP: " + WiFi.localIP().toString() + "</li>";
  html += "<li>Garage: " + String(garageOpen ? "OFFEN" : "GESCHLOSSEN") + "</li>";
  html += "</ul>";

  html += "<h2>Aktuelle Sensordaten</h2><ul>";
  html += "<li>Temperatur: " + String(isnan(temperature) ? "-" : String(temperature, 1)) + " °C</li>";
  html += "<li>Luftfeuchte: " + String(isnan(humidity) ? "-" : String(humidity, 1)) + " %</li>";
  html += "<li>Bodenfeuchte (raw): " + String(soilRaw) + "</li>";
  html += "<li>Bodenfeuchte (%): " + String(soilPercent) + " %</li>";
  html += "</ul>";

  html += "<h2>Letzte " + String(BUFFER_SIZE) + " Samples</h2><ul>";
  for (int i = 0; i < BUFFER_SIZE; i++) {
    int idx = (sampleIndex + BUFFER_SIZE - i) % BUFFER_SIZE;
    const SensorSample &s = samples[idx];
    html += "<li>[" + String(i) + "] ts=" + String(s.ts)
            + " T=" + String(isnan(s.temperature) ? 0 : s.temperature, 1)
            + " H=" + String(isnan(s.humidity) ? 0 : s.humidity, 1)
            + " soil%=" + String(s.soilPercent)
            + " garage=" + String(s.garageOpen ? "OFFEN" : "GESCHLOSSEN")
            + "</li>";
  }
  html += "</ul>";

  html += "<h2>Push-Einstellungen</h2><ul>";
  html += "<li>Pi Host: " + (piHost.length() ? piHost : "(nicht gesetzt)") + "</li>";
  html += "<li>Pi Port: " + piPort + "</li>";
  html += "<li>Push-Intervall: " + String(piSendIntervalMs / 1000) + " s</li>";
  html += "<li><a href='/settings'>Einstellungen bearbeiten</a></li>";
  html += "<li><a href='/logout'>Abmelden</a></li>";
  html += "</ul>";

  html += "</body></html>";

  server.send(200, "text/html", html);
}

// -------------------- Settings --------------------
void handleSettingsGet() {
  if (!isLoggedIn()) {
    handleLoginRedirect();
    return;
  }

  String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>ESP32 Settings</title></head><body>";
  html += "<h1>Einstellungen</h1>";
  html += "<form method='POST' action='/settings'>";

  html += "<label>Pi Host/IP:<br><input name='piHost' value='" + piHost + "'></label><br><br>";
  html += "<label>Pi Port:<br><input name='piPort' value='" + piPort + "'></label><br><br>";
  html += "<label>Push-Intervall (ms, 10000–60000):<br><input name='piSendIntervalMs' value='" + String(piSendIntervalMs) + "'></label><br><br>";
  html += "<label>Web Passwort:<br><input name='webPass' type='password' value='" + webPass + "'></label><br><br>";

  html += "<button type='submit'>Speichern</button> ";
  html += "<a href='/dashboard'>Zurück zum Dashboard</a>";
  html += "</form></body></html>";

  server.send(200, "text/html", html);
}

void handleSettingsPost() {
  if (!isLoggedIn()) {
    handleLoginRedirect();
    return;
  }

  if (server.hasArg("piHost")) piHost = server.arg("piHost");
  if (server.hasArg("piPort")) piPort = server.arg("piPort");

  if (server.hasArg("piSendIntervalMs")) {
    long val = server.arg("piSendIntervalMs").toInt();
    if (val >= 10000 && val <= 60000) {
      piSendIntervalMs = (unsigned long)val;
    }
  }

  if (server.hasArg("webPass")) {
    String newPass = server.arg("webPass");
    if (newPass.length() > 0) {
      webPass = newPass;
    }
  }

  saveConfig();

  server.sendHeader("Location", "/settings");
  server.send(303, "text/plain", "Saved");
}

// -------------------- Logout --------------------
void handleLogout() {
  loggedInClientIP = "";
  loginExpireMs = 0;
  server.sendHeader("Location", "/");
  server.send(303, "text/plain", "Logged out");
}

// -------------------- Command-Endpoint --------------------
void handleCommand() {
  if (!server.hasArg("plain") || server.arg("plain").length() == 0) {
    server.send(400, "application/json", "{\"error\":\"no body\"}");
    return;
  }

  String body = server.arg("plain");
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    server.send(400, "application/json", "{\"error\":\"invalid json\"}");
    return;
  }

  String device = doc["device"] | "";
  String action = doc["action"] | "";

  if (device == "garage") {
    if (action == "open") {
      openGarage();
    } else if (action == "close") {
      closeGarage();
    } else {
      server.send(400, "application/json", "{\"error\":\"unknown garage action\"}");
      return;
    }
  } else if (device == "led") {
    if (action == "red_on") {
      setColor(true, false, false);
    } else if (action == "green_on") {
      setColor(false, true, false);
    } else if (action == "blue_on") {
      setColor(false, false, true);
    } else if (action == "off") {
      setColor(false, false, false);
    } else {
      server.send(400, "application/json", "{\"error\":\"unknown led action\"}");
      return;
    }
  } else if (device == "buzzer") {
    if (action == "on") {
      digitalWrite(BUZZER_PIN, HIGH);
    } else if (action == "off") {
      digitalWrite(BUZZER_PIN, LOW);
    } else {
      server.send(400, "application/json", "{\"error\":\"unknown buzzer action\"}");
      return;
    }
  } else {
    server.send(400, "application/json", "{\"error\":\"unknown device\"}");
    return;
  }

  JsonDocument resp;
  resp["status"] = "ok";
  String respStr;
  serializeJson(resp, respStr);
  server.send(200, "application/json", respStr);
}

// -------------------- Setup / Loop --------------------
void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(LED_RED, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_BLUE, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(WIFI_RESET_BUTTON_PIN, INPUT_PULLUP);

  setColor(false, false, false);
  digitalWrite(BUZZER_PIN, LOW);

  dht.begin();

  garageServo.setPeriodHertz(50);
  garageServo.attach(SERVO_PIN, 500, 2400);
  garageServo.write(0);
  currentServoAngle = 0;

  IrReceiver.begin(IR_PIN, DISABLE_LED_FEEDBACK);

  loadConfig();
  setupWiFiManager();

  // Webserver-Routen
  server.on("/", HTTP_GET, handleRoot);
  server.on("/login", HTTP_POST, handleLoginPost);
  server.on("/dashboard", HTTP_GET, handleDashboard);
  server.on("/settings", HTTP_GET, handleSettingsGet);
  server.on("/settings", HTTP_POST, handleSettingsPost);
  server.on("/logout", HTTP_GET, handleLogout);
  server.on("/api/command", HTTP_POST, handleCommand);

  server.begin();

  Serial.println("System startet...");
  Serial.println("IR bereit. UP = öffnen, DOWN = schließen");
  Serial.println("WLAN-Reset: Button an GPIO32 fuer 3 Sekunden gedrueckt halten");
  Serial.print("Dashboard: http://");
  Serial.print(WiFi.localIP());
  Serial.println("/");
}

void loop() {
  handleWifiResetButton();
  server.handleClient();

  if (IrReceiver.decode()) {
    uint8_t cmd = IrReceiver.decodedIRData.command;

    Serial.print("IR Command erkannt: 0x");
    Serial.println(cmd, HEX);

    if (cmd == IR_UP_CODE) {
      openGarage();
    } else if (cmd == IR_DOWN_CODE) {
      closeGarage();
    }

    IrReceiver.resume();
  }

  int sensorValue = readSoilRaw();
  int moisturePercent = readSoilPercent();

  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();

  if (millis() - lastSensorPrint >= SENSOR_PRINT_INTERVAL_MS) {
    lastSensorPrint = millis();

    Serial.print("Raw: ");
    Serial.print(sensorValue);
    Serial.print(" | Bodenfeuchtigkeit: ");
    Serial.print(moisturePercent);
    Serial.print(" %");

    if (isnan(humidity) || isnan(temperature)) {
      Serial.println(" | DHT11 Lesefehler");
      setColor(false, false, true);
    } else {
      Serial.print(" | Raumtemperatur: ");
      Serial.print(temperature);
      Serial.print(" C | Luftfeuchtigkeit: ");
      Serial.print(humidity);
      Serial.print(" % | Garage: ");
      Serial.println(garageOpen ? "OFFEN" : "GESCHLOSSEN");

      if (temperature < 30.0) {
        setColor(false, true, false);
      } else {
        setColor(true, false, false);
      }
    }

    // Sample im Buffer speichern
    pushSensorSample(temperature, humidity, sensorValue, moisturePercent, garageOpen);
  }

  // Push an Gateway (bestehend, falls du das noch brauchst)
  if (millis() - lastGatewaySend >= GATEWAY_SEND_INTERVAL_MS) {
    lastGatewaySend = millis();
    sendDataToGateway(temperature, humidity, sensorValue, moisturePercent);
  }

  // Push an Pi
  if (millis() - lastPiSend >= piSendIntervalMs) {
    lastPiSend = millis();

    if (WiFi.status() == WL_CONNECTED && piHost.length() > 0 && isValidPort(piPort)) {
      HTTPClient http;
      http.setTimeout(3000);

      String url = "http://" + piHost + ":" + piPort + "/api/esp-data";
      if (http.begin(url)) {
        http.addHeader("Content-Type", "application/json");

        JsonDocument doc;
        doc["deviceId"] = deviceId;
        doc["room"] = roomName;
        doc["ip"] = WiFi.localIP().toString();

        // Letztes Sample
        int lastIdx = (sampleIndex + BUFFER_SIZE - 1) % BUFFER_SIZE;
        const SensorSample &s = samples[lastIdx];

        doc["temperature"] = (isnan(s.temperature) ? nullptr : s.temperature);
        doc["humidity"] = (isnan(s.humidity) ? nullptr : s.humidity);
        doc["soilRaw"] = s.soilRaw;
        doc["soilPercent"] = s.soilPercent;
        doc["garageOpen"] = s.garageOpen;

        // Buffer als Array mitschicken
        JsonArray bufferArr = doc["buffer"].to<JsonArray>();
        for (int i = 0; i < BUFFER_SIZE; i++) {
          int idx = (sampleIndex + i) % BUFFER_SIZE;
          const SensorSample &b = samples[idx];
          JsonObject bObj = bufferArr.add<JsonObject>();
          bObj["ts"] = b.ts;
          bObj["temperature"] = (isnan(b.temperature) ? nullptr : b.temperature);
          bObj["humidity"] = (isnan(b.humidity) ? nullptr : b.humidity);
          bObj["soilRaw"] = b.soilRaw;
          bObj["soilPercent"] = b.soilPercent;
          bObj["garageOpen"] = b.garageOpen;
        }

        String payload;
        serializeJson(doc, payload);

        int httpCode = http.POST(payload);
        Serial.print("Pi POST -> ");
        Serial.print(url);
        Serial.print(" | HTTP Code: ");
        Serial.println(httpCode);

        http.end();
      }
    }
  }
}