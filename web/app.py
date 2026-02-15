"""Bambu Lab P1S web dashboard — Flask + Socket.IO + MQTT bridge."""

import json
import math
import os
import ssl
import threading
import time

import paho.mqtt.client as mqtt
from flask import Flask, render_template
from flask_socketio import SocketIO

# --- Configuration (from environment) ---
BAMBU_IP = os.environ.get("BAMBU_IP", "")
BAMBU_SERIAL = os.environ.get("BAMBU_SERIAL", "")
BAMBU_ACCESS_CODE = os.environ.get("BAMBU_ACCESS_CODE", "")
POLL_INTERVAL = 5

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*")

# --- Color name lookup for AMS filaments ---
COLOR_NAMES = {
    "000000": "Black",
    "FFFFFF": "White",
    "FF0000": "Red",
    "00FF00": "Green",
    "0000FF": "Blue",
    "FFFF00": "Yellow",
    "FF00FF": "Magenta",
    "00FFFF": "Cyan",
    "FF8000": "Orange",
    "800080": "Purple",
    "FFC0CB": "Pink",
    "A52A2A": "Brown",
    "808080": "Gray",
    "C0C0C0": "Silver",
}


def _color_name(hex_color):
    """Map a hex color string to a human-readable name."""
    if not hex_color or len(hex_color) < 6:
        return ""
    h = hex_color.lstrip("#")[:6].upper()
    if h in COLOR_NAMES:
        return COLOR_NAMES[h]
    try:
        r1, g1, b1 = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return f"#{h}"
    best_name, best_dist = None, float("inf")
    for ref_hex, name in COLOR_NAMES.items():
        r2, g2, b2 = int(ref_hex[0:2], 16), int(ref_hex[2:4], 16), int(ref_hex[4:6], 16)
        dist = math.sqrt((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2)
        if dist < best_dist:
            best_dist = dist
            best_name = name
    if best_dist < 100:
        return best_name
    return f"#{h}"


# --- Error code lookups ---
HMS_ERRORS = {
    "0300_0100": "Heatbed temperature error: heating failed",
    "0300_0200": "Heatbed temperature error: thermal runaway",
    "0300_0300": "Heatbed temperature error: sensor abnormal",
    "0300_0400": "Chamber heater error",
    "0500_0100": "Nozzle temperature error: heating failed",
    "0500_0200": "Nozzle temperature error: thermal runaway",
    "0500_0300": "Nozzle temperature error: sensor abnormal",
    "0500_0400": "Nozzle clog detected",
    "0500_0500_0001_0007": "MQTT command verification failed (update firmware/Studio)",
    "0500_0500": "Filament broken or runout",
    "0700_0100": "Motor-X error: driver abnormal",
    "0700_0200": "Motor-Y error: driver abnormal",
    "0700_0300": "Motor-Z error: driver abnormal",
    "0700_0500": "Homing failed: axis stuck",
    "0700_0600": "Motor-E error: filament may be tangled",
    "0300_0D00_0002_0001": "Heatbed homing abnormal: bulge on heatbed or dirty nozzle tip",
    "0300_0D00_0001_0003": "Build plate may not be properly placed",
    "0C00_0100": "First layer inspection failed",
    "0C00_0200": "Spaghetti/noodle detected",
    "0C00_0300": "First layer inspection: AMS filament stuck or broken",
    "1200_0100": "AMS communication error",
    "1200_0200": "AMS filament runout",
    "1200_0300": "AMS filament stuck or broken",
    "1200_0400": "AMS slot empty",
    "1200_1000": "AMS slot read error (RFID)",
}

PRINT_ERRORS = {
    "0300840C": "Print canceled by user",
    "03008400": "Print error (generic)",
}

HMS_WIKI_URL = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode"

GCODE_STATES = {
    "IDLE": "Idle",
    "RUNNING": "Printing",
    "PAUSE": "Paused",
    "FINISH": "Finished",
    "FAILED": "Failed",
    "PREPARE": "Preparing",
    "SLICING": "Slicing",
    "UNKNOWN": "Unknown",
}


# --- Helper functions ---
def _hms_code_full(attr, code):
    return (
        f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}_"
        f"{(code >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"
    )


def _hms_code_short(attr, code):
    return f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}"


def _lookup_hms(attr, code):
    full = _hms_code_full(attr, code)
    short = _hms_code_short(attr, code)
    desc = HMS_ERRORS.get(full) or HMS_ERRORS.get(short)
    if not desc:
        desc = f"Unknown error — see {HMS_WIKI_URL}/{full}"
    return desc, full


def _lookup_print_error(error_val):
    if not error_val:
        return None
    hex_code = f"{int(error_val):08X}"
    if hex_code == "00000000":
        return None
    desc = PRINT_ERRORS.get(hex_code, f"Print error ({hex_code}) — see {HMS_WIKI_URL}")
    return desc, hex_code


def _fan_pct(speed_val):
    if speed_val is None:
        return "--"
    pct = round(int(speed_val) / 15 * 100)
    return f"{pct}%"


def format_temp(actual, target):
    a = f"{actual:.1f}" if isinstance(actual, (int, float)) else "--"
    t = f"{target:.0f}" if isinstance(target, (int, float)) and target > 0 else "--"
    return f"{a}/{t}°C"


def format_time(minutes):
    if not minutes or minutes < 0:
        return "--:--"
    h, m = divmod(int(minutes), 60)
    return f"{h}:{m:02d}"


# --- Persistent printer state ---
_printer_state = {}
_ams_state = []
_state_lock = threading.Lock()

# Error tracking
_first_message_seen = False
_startup_error_codes = set()
_error_log = []


def _update_state(data):
    """Merge incoming data into persistent state."""
    global _ams_state
    updated = False
    for key in ("print", "system", "pushing"):
        section = data.get(key, {})
        if section and isinstance(section, dict):
            _printer_state.update(section)
            updated = True
    ams_section = data.get("print", {}).get("ams", {})
    if isinstance(ams_section, dict):
        ams_units = ams_section.get("ams")
        if isinstance(ams_units, list):
            _ams_state = ams_units
    return updated


def _track_errors(data):
    """Extract errors from raw MQTT data BEFORE merging into state."""
    global _first_message_seen

    p = data.get("print", {})
    if not p:
        return

    current = {}
    for entry in p.get("hms", []):
        attr = entry.get("attr", 0)
        code = entry.get("code", 0)
        full_code = _hms_code_full(attr, code)
        desc, code_str = _lookup_hms(attr, code)
        severity = (code >> 16) & 0xFFFF
        current[full_code] = (severity, desc, code_str)

    pe_info = _lookup_print_error(p.get("print_error"))
    if pe_info:
        current[pe_info[1]] = (3, pe_info[0], pe_info[1])

    if not _first_message_seen:
        _first_message_seen = True
        _startup_error_codes.update(current.keys())
        return

    logged_codes = {entry[3] for entry in _error_log}
    stamp = time.strftime("%H:%M:%S")
    for code, (severity, desc, code_str) in current.items():
        if code not in _startup_error_codes and code not in logged_codes:
            _error_log.append((stamp, severity, desc, code_str))


def _build_snapshot():
    """Build a JSON-serializable snapshot of printer state for the browser."""
    with _state_lock:
        p = dict(_printer_state)
        ams = list(_ams_state)
        errors = list(_error_log)

    state = p.get("gcode_state", "")
    state_label = GCODE_STATES.get(state, state or "Unknown")

    # Fans
    fans = {}
    for key, label in [
        ("cooling_fan_speed", "Part cooling"),
        ("heatbreak_fan_speed", "Hotend"),
        ("big_fan1_speed", "Auxiliary"),
        ("big_fan2_speed", "Chamber"),
    ]:
        val = p.get(key)
        if val is not None:
            fans[label] = _fan_pct(val)

    # AMS
    ams_data = []
    for unit in ams:
        trays = []
        for tray in unit.get("tray", []):
            slot_id = int(tray.get("id", 0)) + 1
            tray_type = tray.get("tray_type", "")
            tray_color = tray.get("tray_color", "")
            color_name = _color_name(tray_color) if tray_type else ""
            trays.append({
                "slot": slot_id,
                "type": tray_type,
                "color": tray_color[:6] if tray_color else "",
                "color_name": color_name,
            })
        ams_data.append({
            "humidity": unit.get("humidity"),
            "temp": unit.get("temp"),
            "trays": trays,
        })

    # Errors
    error_list = [
        {"time": stamp, "severity": sev, "description": desc, "code": code_str}
        for stamp, sev, desc, code_str in errors
    ]

    return {
        "state": state,
        "state_label": state_label,
        "filename": p.get("gcode_file", "") or p.get("subtask_name", ""),
        "progress": p.get("mc_percent"),
        "layer": p.get("layer_num"),
        "total_layers": p.get("total_layer_num"),
        "eta": format_time(p.get("mc_remaining_time")),
        "nozzle": format_temp(p.get("nozzle_temper"), p.get("nozzle_target_temper")),
        "bed": format_temp(p.get("bed_temper"), p.get("bed_target_temper")),
        "chamber_temp": p.get("chamber_temper"),
        "fans": fans,
        "wifi_signal": p.get("wifi_signal"),
        "ams": ams_data,
        "errors": error_list,
        "last_update": time.strftime("%H:%M:%S"),
    }


# --- MQTT callbacks ---
def _on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        topic = f"device/{BAMBU_SERIAL}/report"
        client.subscribe(topic)
        _request_pushall(client)
    else:
        print(f"MQTT connection failed: {reason_code}")


def _on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
    except json.JSONDecodeError:
        return

    with _state_lock:
        _track_errors(data)
        _update_state(data)

    snapshot = _build_snapshot()
    socketio.emit("printer_state", snapshot)


def _request_pushall(client):
    request_topic = f"device/{BAMBU_SERIAL}/request"
    payload = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})
    client.publish(request_topic, payload)


def _poll_loop(client):
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            _request_pushall(client)
        except Exception:
            pass


def _start_mqtt():
    """Start the MQTT client in a background thread."""
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="bambu_web_monitor",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set("bblp", BAMBU_ACCESS_CODE)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ctx)

    client.on_connect = _on_connect
    client.on_message = _on_message

    poll_thread = threading.Thread(target=_poll_loop, args=(client,), daemon=True)
    poll_thread.start()

    client.connect(BAMBU_IP, port=8883, keepalive=60)
    client.loop_forever()


# --- Flask routes ---
@app.route("/")
def index():
    return render_template("index.html")


# --- Socket.IO events ---
@socketio.on("connect")
def handle_connect():
    """Send current state to newly connected client."""
    if _printer_state:
        snapshot = _build_snapshot()
        socketio.emit("printer_state", snapshot)


# --- Entry point ---
def main():
    missing = []
    if not BAMBU_IP:
        missing.append("BAMBU_IP")
    if not BAMBU_SERIAL:
        missing.append("BAMBU_SERIAL")
    if not BAMBU_ACCESS_CODE:
        missing.append("BAMBU_ACCESS_CODE")
    if missing:
        print(f"Error: Missing required env vars: {', '.join(missing)}")
        print("Set BAMBU_IP, BAMBU_SERIAL, and BAMBU_ACCESS_CODE.")
        raise SystemExit(1)

    mqtt_thread = threading.Thread(target=_start_mqtt, daemon=True)
    mqtt_thread.start()

    print(f"Starting web dashboard — connecting to printer at {BAMBU_IP}")
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
