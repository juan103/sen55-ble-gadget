#!/usr/bin/env python3
"""
SEN55 + Arduino UNO Q -> Sensirion BLE Gadget Emulator
Container side (App Lab): collects sensor data and forwards to host BLE service.

The BLE advertising runs on the host (outside the container) because
App Lab containers don't have Bluetooth access. Sensor data is forwarded
to the host BLE service via HTTP on the Docker gateway.
"""
import datetime
import json
import math
import threading
import time
import traceback
import urllib.request
import urllib.error
from collections import deque
from typing import Deque, Dict, List

print("[APP] main.py starting...")

from arduino.app_bricks.dbstorage_sqlstore import SQLStore
from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

print("[APP] Arduino imports OK")

# ============================================================
# WebUI + DB
# ============================================================
ui = WebUI()
db = SQLStore("airquality.db")
columns = {
    "ts": "INTEGER",
    "pm1p0": "REAL",
    "pm2p5": "REAL",
    "pm4p0": "REAL",
    "pm10p0": "REAL",
    "humidity": "REAL",
    "temperature": "REAL",
    "voc_index": "REAL",
    "nox_index": "REAL",
}
db.create_table("samples", columns)


def _safe_float(x, default=0.0):
    if x is None:
        return default
    try:
        f = float(x)
    except (ValueError, TypeError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _row_to_dict(r):
    if isinstance(r, dict):
        return r
    keys = [
        "ts", "pm1p0", "pm2p5", "pm4p0", "pm10p0",
        "humidity", "temperature", "voc_index", "nox_index",
    ]
    return {k: r[i] for i, k in enumerate(keys) if i < len(r)}


# ============================================================
# In-memory history
# ============================================================
HISTORY_MAX = 2000
_history: Deque[Dict] = deque(maxlen=HISTORY_MAX)
_hist_lock = threading.Lock()


def _hist_append(row: Dict):
    with _hist_lock:
        _history.append(dict(row))


def _hist_latest() -> Dict:
    with _hist_lock:
        return dict(_history[-1]) if _history else {}


def _hist_last_n(n: int) -> List[Dict]:
    n = max(0, min(HISTORY_MAX, int(n)))
    with _hist_lock:
        if n <= 0:
            return []
        return [dict(x) for x in list(_history)[-n:]]


# ============================================================
# BLE host service connection
# ============================================================
# Docker gateway IPs to try (container -> host)
BLE_HOST_CANDIDATES = ["172.17.0.1", "172.10.0.1", "host.docker.internal"]
BLE_PORT = 8321
_ble_host_url = None
_ble_connected = False


def _send_bulk_history():
    """Send stored SQLite history to the host BLE service for download."""
    if not _ble_host_url:
        return
    try:
        rows = db.read("samples", order_by="ts ASC", limit=HISTORY_MAX)
        if not rows:
            return
        history = [_row_to_dict(r) for r in rows]
        data = json.dumps(history).encode("utf-8")
        req = urllib.request.Request(
            f"{_ble_host_url}/bulk",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = resp.read().decode()
        print(f"[BLE] Sent bulk history: {result}")
    except Exception as e:
        print(f"[BLE] Bulk history send failed: {e}")


def _find_ble_host():
    """Try to find the host BLE service."""
    global _ble_host_url, _ble_connected
    for host in BLE_HOST_CANDIDATES:
        url = f"http://{host}:{BLE_PORT}/ping"
        try:
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                _ble_host_url = f"http://{host}:{BLE_PORT}"
                _ble_connected = True
                print(f"[BLE] Found host BLE service at {_ble_host_url}")
                return True
        except Exception:
            pass
    return False


def _send_to_ble_host(row: Dict):
    """Forward sensor data to the host BLE service."""
    global _ble_connected
    if not _ble_host_url:
        return
    try:
        data = json.dumps(row).encode("utf-8")
        req = urllib.request.Request(
            f"{_ble_host_url}/update",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        if _ble_connected:
            _ble_connected = False
            print("[BLE] Lost connection to host BLE service")


# ============================================================
# Bridge callback (MCU -> Python)
# ============================================================
def sensor_readings(pm1p0, pm2p5, pm4p0, pm10p0,
                    humidity, temperature, voc_index, nox_index):
    ts = int(datetime.datetime.now().timestamp() * 1000)
    row = {
        "ts": ts,
        "pm1p0": _safe_float(pm1p0),
        "pm2p5": _safe_float(pm2p5),
        "pm4p0": _safe_float(pm4p0),
        "pm10p0": _safe_float(pm10p0),
        "humidity": _safe_float(humidity),
        "temperature": _safe_float(temperature),
        "voc_index": _safe_float(voc_index),
        "nox_index": _safe_float(nox_index),
    }
    db.store("samples", row)
    _hist_append(row)
    _send_to_ble_host(row)
    ui.send_message("pm2p5", {"value": row["pm2p5"], "ts": ts})
    print(f"[DATA] pm2p5={row['pm2p5']:.1f} T={row['temperature']:.1f}")


def api_latest():
    latest = _hist_latest()
    if latest:
        return latest
    rows = db.read("samples", order_by="ts DESC", limit=1)
    return _row_to_dict(rows[0]) if rows else {}


def api_history(n: str):
    n_int = max(0, min(HISTORY_MAX, int(n)))
    return _hist_last_n(n_int)


ui.expose_api("GET", "/latest", lambda: api_latest())
ui.expose_api("GET", "/history/{n}", api_history)
print("[APP] Registering callbacks")
Bridge.provide("sensor_readings", sensor_readings)


def linux_started():
    return True


Bridge.provide("linux_started", linux_started)


# ============================================================
# BLE reconnect thread
# ============================================================
def _ble_reconnect_loop():
    """Periodically try to connect/reconnect to host BLE service."""
    while True:
        if not _ble_connected:
            if _find_ble_host():
                # Send stored history for BLE download
                _send_bulk_history()
                # Send current latest data immediately
                latest = _hist_latest()
                if latest:
                    _send_to_ble_host(latest)
            else:
                print("[BLE] Host BLE service not found. "
                      "Run ble_service.py on the host. "
                      f"Tried: {BLE_HOST_CANDIDATES} port {BLE_PORT}")
        time.sleep(10)


_ble_thread_started = False


def _start_ble_thread():
    global _ble_thread_started
    if _ble_thread_started:
        return
    _ble_thread_started = True
    threading.Thread(target=_ble_reconnect_loop, daemon=True).start()
    print("[BLE] Reconnect thread started")


# ============================================================
# App Lab entry point
# ============================================================
def user_loop():
    _start_ble_thread()
    time.sleep(0.2)


print("[APP] Starting App...")
App.run(user_loop=user_loop)
