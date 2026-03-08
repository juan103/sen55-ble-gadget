# SEN55 BLE Gadget for Arduino UNO Q

Turn your **SEN55 air quality sensor** into a **Sensirion BLE Gadget** compatible with the [myAmbience](https://sensirion.com/products/catalog/SEK-SEN5x/) app (iOS/Android).

Live monitoring of temperature, humidity, VOC, NOx, and PM2.5 — with historical data download.

## Architecture

```
SEN55 sensor
    │ (I2C)
    ▼
[MCU - STM32] sketch.ino
    │ (Bridge.notify)
    ▼
[Linux Container] main.py          ← App Lab Docker container
    │ (HTTP POST to host:8321)
    ▼
[Linux Host] ble_service.py        ← Runs outside container via SSH
    │ (BlueZ D-Bus + GATT)
    ▼
BLE Advertisement + GATT Server
    │
    ▼
myAmbience app (phone)
```

> **Why the split?** The Arduino UNO Q App Lab container has no Bluetooth access (no D-Bus socket, no hcitool). BLE must run on the host, outside the container.

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `sketch.ino` | MCU (STM32) | Reads SEN55 via I2C, sends data to Linux via Bridge |
| `main.py` | Linux container (App Lab) | SQLite storage, WebUI, forwards data to host via HTTP |
| `ble_service.py` | Linux host (via SSH) | BLE advertisement + GATT download service for myAmbience |

## Setup

### Prerequisites
- Arduino UNO Q board with SEN55 connected via I2C
- Arduino App Lab account
- Sensirion myAmbience app on your phone

### Arduino Libraries (install in App Lab)
- SensirionI2CSen5x
- Sensirion Core
- Arduino_RouterBridge
- MsgPack

### Step 1: Flash the Sketch
Open `sketch.ino` in Arduino App Lab. Compile and upload.

### Step 2: Deploy main.py
Place `main.py` in your App Lab project folder. It runs automatically in the container.

### Step 3: Set Up the Host BLE Service

SSH into the Arduino UNO Q:
```bash
ssh arduino@arduinoq
# Password: arduino1
```

Install the BLE library (one time):
```bash
pip3 install dbus-next
```

Copy `ble_service.py` to the board:
```bash
scp ble_service.py arduino@arduinoq:~/
```

Run the service:
```bash
nohup python3 -u ~/ble_service.py > ~/ble_service.log 2>&1 &
```

Check the log:
```bash
tail -f ~/ble_service.log
# Should show: "BLE Gadget active! Device visible as 'S' in myAmbience"
```

### Step 4: Start the App
Start the project in Arduino App Lab. The container will automatically connect to the host BLE service and start forwarding sensor data.

### Step 5: Open myAmbience
The device appears as **"S"** in the scan list. Connect to see live data and download history.

## Technical Details

### Sensirion BLE Protocol
- Company ID: `0x06D5`
- Advertisement sample type: 24
- Download sample type: 23
- Sample: 10 bytes (5× uint16 LE): temperature, humidity, VOC, NOx, PM2.5

### Data Encoding
| Signal | Encoding |
|--------|----------|
| Temperature | `uint16(((T + 45) / 175) × 65535)` |
| Humidity | `uint16((RH / 100) × 65535)` |
| VOC / NOx | `uint16(value)` |
| PM2.5 | `uint16(value × 10)` |

### Download Header (20 bytes)
| Offset | Size | Field |
|--------|------|-------|
| 0–3 | — | Zeros (unused) |
| 4–5 | uint16 LE | Download type = 23 |
| 6–9 | uint32 LE | Interval (ms) |
| 10–13 | uint32 LE | Age of latest sample (ms) |
| 14–15 | uint16 LE | Number of samples |
| 16–19 | — | Zeros (unused) |

### dbus-next Workarounds
This project works around several dbus-next + BlueZ issues:

1. **Notifications**: `emit_properties_changed({"Value": raw_bytes}, [])` — pass raw bytes, not `Variant`
2. **Properties.Set errors**: Custom message handler intercepts BlueZ's Set calls on readonly properties
3. **Advertisement updates**: Must unregister/re-register the D-Bus advertisement every 2 seconds (hcitool gets overridden by BlueZ)

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Device not visible in myAmbience | Check log for "Advertisement registered" |
| Download fails | Ensure notifications use raw bytes, not Variant |
| Shows T=25, RH=50 (defaults) | Wait ~10-30s for container to connect and send data |
| Live values don't update | Advertisement re-registers every 2s; phone scan rate varies |
| Interval resets on restart | Settings persist to `~/.ble_gadget_settings.json` |

## License

MIT
