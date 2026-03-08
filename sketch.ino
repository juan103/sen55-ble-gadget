// SEN55 + Arduino UNO Q — MCU-side sketch
// Reads SEN55 via I2C and sends data to the Linux/Python side via Bridge.notify
//
// SPDX-License-Identifier: MPL-2.0

#include <Arduino.h>
#include <Wire.h>
#include <SensirionI2CSen5x.h>
#include <Arduino_RouterBridge.h>

SensirionI2CSen5x sen5x;

unsigned long previousMillis = 0;
const unsigned long interval = 1000; // 1 second

// Print product info only if the I2C buffer is large enough
#define MAXBUF_REQUIREMENT 48
#if (defined(I2C_BUFFER_LENGTH) && (I2C_BUFFER_LENGTH >= MAXBUF_REQUIREMENT)) || \
    (defined(BUFFER_LENGTH) && (BUFFER_LENGTH >= MAXBUF_REQUIREMENT))
#define USE_PRODUCT_INFO
#endif

static void printModuleVersions() {
  uint16_t error;
  char errorMessage[256];

  unsigned char productName[32];
  uint8_t productNameSize = 32;

  error = sen5x.getProductName(productName, productNameSize);
  if (error) {
    Serial.print("Error getProductName(): ");
    errorToString(error, errorMessage, sizeof(errorMessage));
    Serial.println(errorMessage);
  } else {
    Serial.print("ProductName: ");
    Serial.println((char*)productName);
  }

  uint8_t firmwareMajor, firmwareMinor;
  bool firmwareDebug;
  uint8_t hardwareMajor, hardwareMinor;
  uint8_t protocolMajor, protocolMinor;

  error = sen5x.getVersion(firmwareMajor, firmwareMinor, firmwareDebug,
                           hardwareMajor, hardwareMinor,
                           protocolMajor, protocolMinor);
  if (error) {
    Serial.print("Error getVersion(): ");
    errorToString(error, errorMessage, sizeof(errorMessage));
    Serial.println(errorMessage);
  } else {
    Serial.print("Firmware: ");
    Serial.print(firmwareMajor);
    Serial.print(".");
    Serial.print(firmwareMinor);
    Serial.print(firmwareDebug ? " (debug)" : "");
    Serial.print(" | Hardware: ");
    Serial.print(hardwareMajor);
    Serial.print(".");
    Serial.println(hardwareMinor);
  }
}

static void printSerialNumber() {
  uint16_t error;
  char errorMessage[256];

  unsigned char serialNumber[32];
  uint8_t serialNumberSize = 32;

  error = sen5x.getSerialNumber(serialNumber, serialNumberSize);
  if (error) {
    Serial.print("Error getSerialNumber(): ");
    errorToString(error, errorMessage, sizeof(errorMessage));
    Serial.println(errorMessage);
  } else {
    Serial.print("SerialNumber: ");
    Serial.println((char*)serialNumber);
  }
}

void setup() {
  Serial.begin(115200);

  // Wait for serial with a timeout (don't block forever on UNO Q)
  unsigned long serialStart = millis();
  while (!Serial && (millis() - serialStart < 3000)) {
    delay(50);
  }

  Bridge.begin();

  Wire.begin();
  sen5x.begin(Wire);

  uint16_t error;
  char errorMessage[256];

  error = sen5x.deviceReset();
  if (error) {
    Serial.print("Error deviceReset(): ");
    errorToString(error, errorMessage, sizeof(errorMessage));
    Serial.println(errorMessage);
  }

#ifdef USE_PRODUCT_INFO
  printSerialNumber();
  printModuleVersions();
#endif

  // Optional temperature offset calibration
  float tempOffset = 0.0f;
  error = sen5x.setTemperatureOffsetSimple(tempOffset);
  if (error) {
    Serial.print("Error setTemperatureOffsetSimple(): ");
    errorToString(error, errorMessage, sizeof(errorMessage));
    Serial.println(errorMessage);
  }

  error = sen5x.startMeasurement();
  if (error) {
    Serial.print("Error startMeasurement(): ");
    errorToString(error, errorMessage, sizeof(errorMessage));
    Serial.println(errorMessage);
  } else {
    Serial.println("SEN55 measurement started.");
  }
}

void loop() {
  unsigned long currentMillis = millis();
  if (currentMillis - previousMillis < interval) return;
  previousMillis = currentMillis;

  uint16_t error;
  char errorMessage[256];

  float pm1 = NAN, pm25 = NAN, pm4 = NAN, pm10 = NAN;
  float rh = NAN, t = NAN, voc = NAN, nox = NAN;

  error = sen5x.readMeasuredValues(pm1, pm25, pm4, pm10, rh, t, voc, nox);
  if (error) {
    Serial.print("Error readMeasuredValues(): ");
    errorToString(error, errorMessage, sizeof(errorMessage));
    Serial.println(errorMessage);
    return;
  }

  // Replace NaN with 0 before sending (MsgPack handles floats but NaN
  // can cause issues on the Python side encoding for BLE)
  if (isnan(pm1))  pm1  = 0.0f;
  if (isnan(pm25)) pm25 = 0.0f;
  if (isnan(pm4))  pm4  = 0.0f;
  if (isnan(pm10)) pm10 = 0.0f;
  if (isnan(rh))   rh   = 0.0f;
  if (isnan(t))    t    = 0.0f;
  if (isnan(voc))  voc  = 0.0f;
  if (isnan(nox))  nox  = 0.0f;

  Serial.print("pm2p5=");
  Serial.print(pm25);
  Serial.print(" T=");
  Serial.print(t);
  Serial.print(" RH=");
  Serial.println(rh);

  // Send all 8 values to the Linux/Python side
  Bridge.notify("sensor_readings",
                pm1, pm25, pm4, pm10,
                rh, t, voc, nox);
}
