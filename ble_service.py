#!/usr/bin/env python3
"""
Sensirion BLE Gadget Service — runs on the Arduino UNO Q HOST (not in container).

Receives sensor data from the App Lab container via HTTP and broadcasts
it as a Sensirion MyAmbience-compatible BLE advertisement + GATT server.

SETUP:
  1. SSH into the Arduino UNO Q:  ssh user@<board-ip>
  2. Copy this file:              scp ble_service.py user@<board-ip>:~/
  3. Install dbus-next:           pip3 install dbus-next
  4. Run:                         python3 ~/ble_service.py
     Or as a background service:  nohup python3 ~/ble_service.py &

The container's main.py will automatically find and connect to this service.
"""
import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Deque, Dict, List, Optional, Tuple

# ============================================================
# Configuration
# ============================================================
HTTP_PORT = 8321
HCI_DEV = os.environ.get("HCI_DEV", "hci0")
HISTORY_MAX = 2000
DEFAULT_HISTORY_INTERVAL_MS = 60_000
SETTINGS_FILE = os.path.expanduser("~/.ble_gadget_settings.json")

# ============================================================
# BLE constants (Sensirion protocol)
# ============================================================
COMPANY_ID = 0x06D5
ADV_TYPE = 0x00
SAMPLE_TYPE = 24
DOWNLOAD_TYPE = 23  # downloadType for T_RH_VOC_NOX_PM25 (distinct from sampleType=24)
SAMPLE_SIZE_BYTES = 10

DOWNLOAD_SERVICE_UUID = "00008000-b38d-4985-720e-0f993a68ee41"
SAMPLE_HISTORY_INTERVAL_UUID = "00008001-b38d-4985-720e-0f993a68ee41"
NUMBER_OF_SAMPLES_UUID = "00008002-b38d-4985-720e-0f993a68ee41"
REQUESTED_SAMPLES_UUID = "00008003-b38d-4985-720e-0f993a68ee41"
DOWNLOAD_PACKET_UUID = "00008004-b38d-4985-720e-0f993a68ee41"

SETTINGS_SERVICE_UUID = "00008100-b38d-4985-720e-0f993a68ee41"
WIFI_SSID_UUID = "00008171-b38d-4985-720e-0f993a68ee41"
WIFI_PWD_UUID = "00008172-b38d-4985-720e-0f993a68ee41"
ALT_DEVICE_NAME_UUID = "00008120-b38d-4985-720e-0f993a68ee41"

AD_FLAGS = 0x01
AD_COMPLETE_NAME = 0x09
AD_MANUFACTURER_DATA = 0xFF
AD_FLAGS_VALUE = 0x06
GADGET_NAME = "S"

# BlueZ D-Bus constants
BLUEZ_SERVICE = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"
ADV_PATH = "/com/sensirion/gadget/adv0"
APP_PATH = "/com/sensirion/gadget/app"
SVC_DL_PATH = APP_PATH + "/service_dl"
SVC_SET_PATH = APP_PATH + "/service_set"

IFACE_ADAPTER = "org.bluez.Adapter1"
IFACE_ADV = "org.bluez.LEAdvertisement1"
IFACE_OM = "org.freedesktop.DBus.ObjectManager"
IFACE_GATT_MGR = "org.bluez.GattManager1"
IFACE_ADV_MGR = "org.bluez.LEAdvertisingManager1"
IFACE_SVC = "org.bluez.GattService1"
IFACE_CHR = "org.bluez.GattCharacteristic1"
IFACE_PROPS = "org.freedesktop.DBus.Properties"


# ============================================================
# Encoding functions (match Sensirion BLEProtocol.cpp)
# ============================================================
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


def clamp_u16(v):
    return max(0, min(65535, int(v)))


def encode_temperature_v1(t_c):
    t_c = _safe_float(t_c, 25.0)
    return clamp_u16(((t_c + 45.0) / 175.0) * 65535.0 + 0.5)


def encode_humidity_v1(rh):
    rh = _safe_float(rh, 50.0)
    return clamp_u16((rh / 100.0) * 65535.0 + 0.5)


def encode_simple(x):
    return clamp_u16(_safe_float(x) + 0.5)


def encode_pm_v2(pm):
    return clamp_u16(_safe_float(pm) * 10.0 + 0.5)


def build_sample_bytes(t, rh, voc, nox, pm25):
    return struct.pack(
        "<HHHHH",
        encode_temperature_v1(t),
        encode_humidity_v1(rh),
        encode_simple(voc),
        encode_simple(nox),
        encode_pm_v2(pm25),
    )


def get_dev_id_from_mac(mac):
    parts = mac.strip().split(":")
    if len(parts) != 6:
        return (0x00, 0x00)
    return (int(parts[4], 16), int(parts[5], 16))


def build_mfg_payload(dev_hi, dev_lo, t, rh, voc, nox, pm25):
    sample = build_sample_bytes(t, rh, voc, nox, pm25)
    return bytes([ADV_TYPE, SAMPLE_TYPE, dev_hi, dev_lo]) + sample


# ============================================================
# Shared state (thread-safe)
# ============================================================
class SensorState:
    def __init__(self):
        self.lock = threading.Lock()
        self.temperature = 25.0
        self.humidity = 50.0
        self.voc_index = 100.0
        self.nox_index = 1.0
        self.pm2p5 = 0.0
        self.updated = False

        # History for BLE download
        self.nr_of_samples_requested = 0
        self._sample_ring: Deque[bytes] = deque(maxlen=HISTORY_MAX)
        self._last_store_ms = 0

        # Load persisted settings
        self.history_interval_ms = DEFAULT_HISTORY_INTERVAL_MS
        self.alt_name = "SEN55-UNOQ"
        self._load_settings()

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, "r") as f:
                s = json.loads(f.read())
                self.history_interval_ms = s.get("interval_ms", self.history_interval_ms)
                self.alt_name = s.get("alt_name", self.alt_name)
                print(f"[BLE] Loaded settings: interval={self.history_interval_ms}ms, name={self.alt_name}")
        except Exception:
            pass

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, "w") as f:
                f.write(json.dumps({"interval_ms": self.history_interval_ms, "alt_name": self.alt_name}))
        except Exception:
            pass

    def update(self, data: Dict):
        with self.lock:
            self.temperature = _safe_float(data.get("temperature"), self.temperature)
            self.humidity = _safe_float(data.get("humidity"), self.humidity)
            self.voc_index = _safe_float(data.get("voc_index"), self.voc_index)
            self.nox_index = _safe_float(data.get("nox_index"), self.nox_index)
            self.pm2p5 = _safe_float(data.get("pm2p5"), self.pm2p5)
            self.updated = True

            # Store sample in ring buffer if enough time has passed
            now_ms = int(time.time() * 1000)
            if (now_ms - self._last_store_ms) >= self.history_interval_ms:
                self._last_store_ms = now_ms
                sample = build_sample_bytes(
                    self.temperature, self.humidity,
                    self.voc_index, self.nox_index, self.pm2p5
                )
                self._sample_ring.append(sample)

    def load_history(self, rows):
        """Load historical samples from SQLite bulk data (oldest first)."""
        with self.lock:
            loaded = 0
            for r in rows:
                t = _safe_float(r.get("temperature"), 25.0)
                rh = _safe_float(r.get("humidity"), 50.0)
                voc = _safe_float(r.get("voc_index"), 0.0)
                nox = _safe_float(r.get("nox_index"), 0.0)
                pm25 = _safe_float(r.get("pm2p5"), 0.0)
                sample = build_sample_bytes(t, rh, voc, nox, pm25)
                self._sample_ring.append(sample)
                loaded += 1
            # Update current values from the most recent row
            if rows:
                last = rows[-1]
                self.temperature = _safe_float(last.get("temperature"), self.temperature)
                self.humidity = _safe_float(last.get("humidity"), self.humidity)
                self.voc_index = _safe_float(last.get("voc_index"), self.voc_index)
                self.nox_index = _safe_float(last.get("nox_index"), self.nox_index)
                self.pm2p5 = _safe_float(last.get("pm2p5"), self.pm2p5)
                self.updated = True
            if loaded:
                print(f"[BLE] Loaded {loaded} historical samples from container")
            return loaded

    def get_values(self):
        with self.lock:
            return (self.temperature, self.humidity,
                    self.voc_index, self.nox_index, self.pm2p5)

    def samples_count(self):
        with self.lock:
            return len(self._sample_ring)

    def get_samples(self, count):
        with self.lock:
            n = min(count, len(self._sample_ring))
            if n <= 0:
                return []
            return list(self._sample_ring)[-n:]


state = SensorState()


# ============================================================
# HTTP Server (receives data from container)
# ============================================================
class BLEHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/update":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                state.update(data)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif self.path == "/bulk":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                rows = json.loads(body)
                if rows:
                    print(f"[HTTP] Bulk: {len(rows)} rows, first={rows[0]}, last={rows[-1]}")
                count = state.load_history(rows)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(f"loaded {count}".encode())
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logging


def start_http_server():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), BLEHandler)
    print(f"[HTTP] Listening on port {HTTP_PORT}")
    server.serve_forever()


# ============================================================
# BLE via D-Bus (preferred, requires dbus-next)
# ============================================================
def try_dbus_ble():
    """Try to use dbus-next for full BLE support (adv + GATT)."""
    try:
        from dbus_next.aio import MessageBus
        from dbus_next.service import (
            ServiceInterface, method, dbus_property, PropertyAccess,
        )
        from dbus_next import Variant, BusType, Message
        print("[BLE] dbus-next available, using D-Bus BLE")
    except ImportError:
        print("[BLE] dbus-next not installed. Install with: pip3 install dbus-next")
        return False

    # ---- D-Bus classes (defined inside function to avoid import errors) ----

    class Advertisement(ServiceInterface):
        def __init__(self, payload):
            super().__init__(IFACE_ADV)
            self._type = "peripheral"
            self._local_name = GADGET_NAME
            self._payload = payload

        @method()
        def Release(self):
            print("[BLE] Advertisement released")

        def update_payload(self, payload):
            self._payload = payload

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> "s":
            return self._type

        @dbus_property(access=PropertyAccess.READ)
        def LocalName(self) -> "s":
            return self._local_name

        @dbus_property(access=PropertyAccess.READ)
        def ManufacturerData(self) -> "a{qv}":
            return {COMPANY_ID: Variant("ay", self._payload)}

        @dbus_property(access=PropertyAccess.READ)
        def TxPower(self) -> "n":
            return 0

    class GattApp:
        """Manages GATT services and handles GetManagedObjects via message handler."""
        def __init__(self):
            self._services = []

        def add_service(self, svc):
            self._services.append(svc)

        def get_managed_objects(self):
            managed = {}
            for svc in self._services:
                managed[svc.path] = {
                    IFACE_SVC: {
                        "UUID": Variant("s", svc.uuid),
                        "Primary": Variant("b", True),
                        "Characteristics": Variant(
                            "ao", [c.path for c in svc.chars]
                        ),
                    }
                }
                for ch in svc.chars:
                    managed[ch.path] = {
                        IFACE_CHR: {
                            "UUID": Variant("s", ch.uuid),
                            "Service": Variant("o", svc.path),
                            "Flags": Variant("as", ch.flags),
                        }
                    }
            return managed

    class GattSvc(ServiceInterface):
        def __init__(self, path, uuid):
            super().__init__(IFACE_SVC)
            self.path = path
            self.uuid = uuid
            self.chars = []

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":
            return self.uuid

        @dbus_property(access=PropertyAccess.READ)
        def Primary(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def Characteristics(self) -> "ao":
            return [c.path for c in self.chars]

    class GattChr(ServiceInterface):
        def __init__(self, path, uuid, svc_path, flags,
                     read_cb=None, write_cb=None, notify_cb=None):
            super().__init__(IFACE_CHR)
            self.path = path
            self.uuid = uuid
            self._svc_path = svc_path
            self.flags = flags
            self._read_cb = read_cb
            self._write_cb = write_cb
            self._notify_cb = notify_cb
            self._notifying = False
            self._value = b""

        @dbus_property(access=PropertyAccess.READ)
        def UUID(self) -> "s":
            return self.uuid

        @dbus_property(access=PropertyAccess.READ)
        def Service(self) -> "o":
            return self._svc_path

        @dbus_property(access=PropertyAccess.READ)
        def Flags(self) -> "as":
            return self.flags

        @dbus_property(access=PropertyAccess.READ)
        def Value(self) -> "ay":
            return self._value

        @method()
        def ReadValue(self, options: "a{sv}") -> "ay":
            if self._read_cb:
                self._value = self._read_cb()
            print(f"[BLE] ReadValue {self.uuid[-8:]}: {self._value.hex()}")
            return self._value

        @method()
        def WriteValue(self, value: "ay", options: "a{sv}"):
            print(f"[BLE] WriteValue {self.uuid[-8:]}: {bytes(value).hex()}")
            if self._write_cb:
                self._write_cb(bytes(value))

        @method()
        def StartNotify(self):
            print(f"[BLE] StartNotify called on {self.uuid[-8:]}")
            self._notifying = True
            if self._notify_cb:
                self._notify_cb(self)

        @method()
        def StopNotify(self):
            self._notifying = False

        def set_value_and_notify(self, value):
            self._value = value
            try:
                self.emit_properties_changed({"Value": value}, [])
            except Exception as e:
                print(f"[BLE] Notify FAILED on {self.uuid}: {e}")

    # ---- Async BLE main ----

    async def ble_main():
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        print("[BLE] Connected to system D-Bus")

        # Power on and set discoverable
        try:
            intro = await bus.introspect(BLUEZ_SERVICE, ADAPTER_PATH)
            adapter = bus.get_proxy_object(BLUEZ_SERVICE, ADAPTER_PATH, intro)
            props = adapter.get_interface(IFACE_PROPS)

            await props.call_set(IFACE_ADAPTER, "Powered", Variant("b", True))
            await asyncio.sleep(0.3)
            await props.call_set(IFACE_ADAPTER, "Discoverable", Variant("b", True))
            await props.call_set(IFACE_ADAPTER, "DiscoverableTimeout", Variant("u", 0))
            print("[BLE] Adapter powered on and discoverable")

            mac_v = await props.call_get(IFACE_ADAPTER, "Address")
            mac = mac_v.value if hasattr(mac_v, "value") else str(mac_v)
        except Exception as e:
            print(f"[BLE] Adapter setup error: {e}")
            mac = "00:00:00:00:00:00"

        dev_hi, dev_lo = get_dev_id_from_mac(mac)
        print(f"[BLE] MAC: {mac}, DevID: {dev_hi:02X}:{dev_lo:02X}")

        # Initial payload
        t, rh, voc, nox, pm25 = state.get_values()
        payload = build_mfg_payload(dev_hi, dev_lo, t, rh, voc, nox, pm25)

        # Register D-Bus advertisement so BlueZ enables LE advertising
        adv = Advertisement(payload)
        bus.export(ADV_PATH, adv)

        # Download service
        svc_dl = GattSvc(SVC_DL_PATH, DOWNLOAD_SERVICE_UUID)

        ch_interval = GattChr(
            SVC_DL_PATH + "/char0", SAMPLE_HISTORY_INTERVAL_UUID, svc_dl.path,
            ["read", "write"],
            read_cb=lambda: struct.pack("<I", state.history_interval_ms),
            write_cb=lambda raw: _set_interval(raw),
        )

        def _set_interval(raw):
            if len(raw) >= 4:
                val = int.from_bytes(raw[:4], "little", signed=False)
                if val > 0:
                    state.history_interval_ms = val
                    state.save_settings()
                    print(f"[BLE] History interval: {val} ms (saved)")

        def _read_count():
            c = state.samples_count()
            print(f"[BLE] App read sample count: {c}")
            return struct.pack("<I", c)

        ch_count = GattChr(
            SVC_DL_PATH + "/char1", NUMBER_OF_SAMPLES_UUID, svc_dl.path,
            ["read"],
            read_cb=_read_count,
        )

        def _write_requested(raw):
            val = int.from_bytes(raw[:4], "little", signed=False) if len(raw) >= 4 else 0
            state.nr_of_samples_requested = val
            print(f"[BLE] App requested {val} samples")

        ch_req = GattChr(
            SVC_DL_PATH + "/char2", REQUESTED_SAMPLES_UUID, svc_dl.path,
            ["write"],
            write_cb=_write_requested,
        )

        _download_task = [None]

        def _on_download_notify(ch):
            print(f"[BLE] Download notify started (requested={state.nr_of_samples_requested})")
            if _download_task[0] and not _download_task[0].done():
                print("[BLE] Download already in progress, skipping")
                return
            _download_task[0] = asyncio.get_running_loop().create_task(
                _run_download(ch)
            )

        async def _run_download(ch):
            avail = state.samples_count()
            req = state.nr_of_samples_requested
            count = req if 0 < req <= avail else avail
            # Cap for safety
            count = min(count, 500)
            if count == 0:
                print("[BLE] Download: no samples available")
                return
            samples = state.get_samples(count)
            n = len(samples)
            interval = state.history_interval_ms
            # Age of latest sample (time since most recent sample)
            age_latest = interval  # most recent sample is ~1 interval old
            print(f"[BLE] Download: {n} samples, interval={interval}ms, age_latest={age_latest}ms")

            # Header packet (20 bytes) - Sensirion format:
            #   0-3:   zeros (unused)
            #   4-5:   download_sample_type (uint16 LE) = 23
            #   6-9:   interval_ms (uint32 LE)
            #   10-13: age_of_latest_sample_ms (uint32 LE)
            #   14-15: number_of_samples (uint16 LE)
            #   16-19: zeros (unused)
            header = bytearray(20)
            struct.pack_into("<H", header, 4, DOWNLOAD_TYPE)
            struct.pack_into("<I", header, 6, interval)
            struct.pack_into("<I", header, 10, age_latest)
            struct.pack_into("<H", header, 14, n)
            print(f"[BLE] Header: {header.hex()}")
            ch.set_value_and_notify(bytes(header))
            await asyncio.sleep(0.1)

            for i, s in enumerate(samples):
                if not ch._notifying:
                    print(f"[BLE] Download aborted at sample {i}")
                    break
                pkt = bytearray(20)
                struct.pack_into("<H", pkt, 0, i + 1)  # seq starts at 1 (header is 0)
                pkt[2:2 + SAMPLE_SIZE_BYTES] = s
                ch.set_value_and_notify(bytes(pkt))
                await asyncio.sleep(0.05)
            print(f"[BLE] Download complete: {n} samples sent")

        ch_dl = GattChr(
            SVC_DL_PATH + "/char3", DOWNLOAD_PACKET_UUID, svc_dl.path,
            ["notify", "indicate", "read"],
            read_cb=lambda: b"",
            notify_cb=_on_download_notify,
        )

        svc_dl.chars = [ch_interval, ch_count, ch_req, ch_dl]

        # Settings service
        svc_set = GattSvc(SVC_SET_PATH, SETTINGS_SERVICE_UUID)
        ch_name = GattChr(
            SVC_SET_PATH + "/char0", ALT_DEVICE_NAME_UUID, svc_set.path,
            ["read", "write"],
            read_cb=lambda: state.alt_name.encode("utf-8"),
            write_cb=lambda raw: (
                setattr(state, "alt_name", raw.decode("utf-8", errors="ignore")),
                state.save_settings(),
            ),
        )
        ch_ssid = GattChr(
            SVC_SET_PATH + "/char1", WIFI_SSID_UUID, svc_set.path, ["write"],
        )
        ch_pwd = GattChr(
            SVC_SET_PATH + "/char2", WIFI_PWD_UUID, svc_set.path, ["write"],
        )
        svc_set.chars = [ch_name, ch_ssid, ch_pwd]

        # Register with BlueZ
        gatt_app = GattApp()
        gatt_app.add_service(svc_dl)
        gatt_app.add_service(svc_set)

        # Export service and characteristic interfaces
        bus.export(svc_dl.path, svc_dl)
        bus.export(svc_set.path, svc_set)
        for c in svc_dl.chars + svc_set.chars:
            bus.export(c.path, c)

        # Handle GetManagedObjects and Properties.Set via low-level message handler
        # (dbus-next doesn't dispatch ObjectManager to ServiceInterface,
        #  and its default Properties.Set handler rejects readonly properties)
        def _msg_handler(msg):
            if (msg.interface == IFACE_OM
                    and msg.member == "GetManagedObjects"
                    and msg.path == APP_PATH):
                managed = gatt_app.get_managed_objects()
                print(f"[BLE] GetManagedObjects called, returning {len(managed)} objects")
                reply = Message.new_method_return(
                    msg, "a{oa{sa{sv}}}", [managed]
                )
                bus.send(reply)
                return True
            # Intercept Properties.Set on our objects — accept silently
            if (msg.interface == IFACE_PROPS
                    and msg.member == "Set"
                    and msg.path and msg.path.startswith("/com/sensirion/")):
                reply = Message.new_method_return(msg, "", [])
                bus.send(reply)
                return True
            return False

        bus.add_message_handler(_msg_handler)

        intro = await bus.introspect(BLUEZ_SERVICE, ADAPTER_PATH)
        adapter = bus.get_proxy_object(BLUEZ_SERVICE, ADAPTER_PATH, intro)
        adv_mgr = adapter.get_interface(IFACE_ADV_MGR)
        gatt_mgr = adapter.get_interface(IFACE_GATT_MGR)

        try:
            await gatt_mgr.call_register_application(APP_PATH, {})
            print("[BLE] GATT registered")
        except Exception as e:
            print(f"[BLE] GATT registration failed: {e}")

        # Register D-Bus advertisement for GATT discoverability
        try:
            await adv_mgr.call_register_advertisement(ADV_PATH, {})
            print("[BLE] Advertisement registered via D-Bus")
        except Exception as e:
            print(f"[BLE] Advertisement registration failed: {e}")
            return

        # Ensure LE advertising is enabled (BlueZ may not enable it automatically)
        proc = await asyncio.create_subprocess_shell(
            f"hciconfig {HCI_DEV} leadv 0",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(0.3)
        print("[BLE] BLE Gadget active! Device visible as 'S' in myAmbience")

        # Update loop: unregister/re-register advertisement to update payload
        # This is the only reliable way to update BlueZ managed advertisement data
        update_count = 0
        while True:
            t, rh, voc, nox, pm25 = state.get_values()
            mfg_payload = build_mfg_payload(dev_hi, dev_lo, t, rh, voc, nox, pm25)
            adv.update_payload(mfg_payload)

            # Re-register to push new data to BlueZ
            try:
                await adv_mgr.call_unregister_advertisement(ADV_PATH)
            except Exception:
                pass
            try:
                await adv_mgr.call_register_advertisement(ADV_PATH, {})
            except Exception:
                pass

            update_count += 1
            if update_count % 10 == 1:
                print(f"[BLE] Adv update #{update_count}: "
                      f"T={t:.1f} RH={rh:.0f} PM2.5={pm25:.1f}")

            await asyncio.sleep(2.0)

    # Run the async BLE main
    def run_ble():
        try:
            asyncio.run(ble_main())
        except Exception:
            print("[BLE] CRASHED:\n" + traceback.format_exc())

    threading.Thread(target=run_ble, daemon=True).start()
    return True


# ============================================================
# BLE via hcitool fallback (if dbus-next not available)
# ============================================================
def try_hcitool_ble():
    """Fallback: use hcitool for advertisement-only BLE."""
    # Check for tools
    for tool in ["hcitool", "btmgmt", "hciconfig"]:
        r = subprocess.run(["which", tool], capture_output=True)
        if r.returncode == 0:
            print(f"[BLE] Found {tool}, using subprocess BLE")
            break
    else:
        print("[BLE] No BLE tools found on host either!")
        return False

    def _run(cmd):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            return r.returncode == 0
        except Exception:
            return False

    # Get MAC
    mac = "00:00:00:00:00:00"
    r = subprocess.run(f"hciconfig {HCI_DEV}", shell=True, capture_output=True, text=True)
    if r.returncode == 0:
        for line in r.stdout.split("\n"):
            if "BD Address:" in line:
                mac = line.split("BD Address:")[1].strip().split()[0]

    dev_hi, dev_lo = get_dev_id_from_mac(mac)
    print(f"[BLE] MAC: {mac}, DevID: {dev_hi:02X}:{dev_lo:02X}")

    _run(f"hciconfig {HCI_DEV} up")
    _run(f"hciconfig {HCI_DEV} leadv 0")  # Enable LE advertising

    def update_loop():
        while True:
            t, rh, voc, nox, pm25 = state.get_values()
            sample = build_sample_bytes(t, rh, voc, nox, pm25)
            mfg_payload = bytes([ADV_TYPE, SAMPLE_TYPE, dev_hi, dev_lo]) + sample
            company_le = struct.pack("<H", COMPANY_ID)
            mfg_data = company_le + mfg_payload

            # Build AD structures
            ad = bytearray()
            ad += bytes([0x02, AD_FLAGS, AD_FLAGS_VALUE])
            name_b = GADGET_NAME.encode("ascii")
            ad += bytes([1 + len(name_b), AD_COMPLETE_NAME]) + name_b
            ad += bytes([1 + len(mfg_data), AD_MANUFACTURER_DATA]) + mfg_data

            # Set advertising data via HCI
            padded = ad + b'\x00' * (31 - len(ad))
            param_hex = f"{len(ad):02X} " + " ".join(f"{b:02X}" for b in padded)
            _run(f"hcitool -i {HCI_DEV} cmd 0x08 0x0008 {param_hex}")

            time.sleep(5)

    threading.Thread(target=update_loop, daemon=True).start()
    print("[BLE] hcitool advertising started (no GATT/download support)")
    return True


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Sensirion BLE Gadget Service for Arduino UNO Q")
    print(f"HTTP port: {HTTP_PORT}")
    print("=" * 60)

    # Start HTTP server in background
    threading.Thread(target=start_http_server, daemon=True).start()

    # Try D-Bus BLE first, then hcitool fallback
    if not try_dbus_ble():
        print("[BLE] D-Bus BLE failed, trying hcitool fallback...")
        if not try_hcitool_ble():
            print("[BLE] WARNING: No BLE method available!")
            print("[BLE] Install dbus-next: pip3 install dbus-next")
            print("[BLE] Or install BlueZ tools: apt install bluez")

    print("[SVC] Service running. Waiting for sensor data from container...")

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
            t, rh, voc, nox, pm25 = state.get_values()
            samples = state.samples_count()
            print(f"[SVC] T={t:.1f} RH={rh:.0f}% VOC={voc:.0f} "
                  f"NOx={nox:.0f} PM2.5={pm25:.1f} | {samples} stored samples")
    except KeyboardInterrupt:
        print("\n[SVC] Shutting down")


if __name__ == "__main__":
    main()
