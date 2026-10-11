"""Bambu Lab P1S web dashboard: Flask + Socket.IO + MQTT bridge."""

import colorsys
import json
import os
import ssl
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import paho.mqtt.client as mqtt
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

# --- Configuration (from environment) ---
BAMBU_IP = os.environ.get("BAMBU_IP", "")
BAMBU_SERIAL = os.environ.get("BAMBU_SERIAL", "")
BAMBU_ACCESS_CODE = os.environ.get("BAMBU_ACCESS_CODE", "")
# Set BAMBU_CONTROLS=0 to run the dashboard display-only (no printer commands).
CONTROLS_ENABLED = os.environ.get("BAMBU_CONTROLS", "1").lower() not in ("0", "false", "no", "off")
POLL_INTERVAL = 5
HISTORY_SECONDS = 30 * 60          # keep 30 minutes of temperature history
HISTORY_MIN_GAP = 2.0              # seconds between history samples
STALE_AFTER = 30                   # seconds without a report before "stale"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*")

# --- Color name lookup for AMS filaments ---

HUE_NAMES = [
    (15, "Red"), (40, "Orange"), (65, "Yellow"), (160, "Green"),
    (200, "Cyan"), (255, "Blue"), (290, "Purple"), (335, "Pink"), (360, "Red"),
]


def _color_name(hex_color):
    """Map a hex color string to a human-readable name using hue/sat/lightness."""
    if not hex_color or len(hex_color) < 6:
        return ""
    h = hex_color.lstrip("#")[:6].upper()
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return f"#{h}"
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    hue *= 360
    if light < 0.08:
        return "Black"
    if light > 0.93 and sat < 0.25:
        return "White"
    if sat < 0.12:
        if light < 0.3:
            return "Dark gray"
        if light > 0.75:
            return "Light gray"
        return "Gray"
    name = next(n for limit, n in HUE_NAMES if hue < limit)
    if name == "Orange" and light < 0.35:
        return "Brown"
    if name == "Yellow" and light < 0.4:
        return "Olive"
    if light > 0.8:
        return "Pink" if name == "Red" else f"Light {name.lower()}"
    if light < 0.25:
        return f"Dark {name.lower()}"
    return name


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

# stg_cur values reported by the printer during a job
PRINT_STAGES = {
    -1: "",
    0: "Printing",
    1: "Auto bed leveling",
    2: "Heatbed preheating",
    3: "Sweeping XY mech mode",
    4: "Changing filament",
    5: "M400 pause",
    6: "Paused: filament runout",
    7: "Heating hotend",
    8: "Calibrating extrusion",
    9: "Scanning bed surface",
    10: "Inspecting first layer",
    11: "Identifying build plate type",
    12: "Calibrating Micro Lidar",
    13: "Homing toolhead",
    14: "Cleaning nozzle tip",
    15: "Checking extruder temperature",
    16: "Paused by user",
    17: "Paused: front cover open",
    18: "Calibrating Micro Lidar",
    19: "Calibrating extrusion flow",
    20: "Paused: nozzle temperature malfunction",
    21: "Paused: heatbed temperature malfunction",
    22: "Unloading filament",
    23: "Paused: skip step",
    24: "Loading filament",
    25: "Motor noise calibration",
    26: "Paused: AMS lost",
    27: "Paused: heatbreak fan too slow",
    28: "Paused: chamber temperature error",
    29: "Cooling chamber",
    30: "Paused by user G-code",
    31: "Motor noise showoff",
    32: "Paused: nozzle filament covered",
    33: "Paused: cutter error",
    34: "Paused: first layer error",
    35: "Paused: nozzle clog",
}

SPEED_LEVELS = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}


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
        desc = f"Unknown error {full}"
    return desc, full


def _lookup_print_error(error_val):
    if not error_val:
        return None
    hex_code = f"{int(error_val):08X}"
    if hex_code == "00000000":
        return None
    desc = PRINT_ERRORS.get(hex_code, f"Print error ({hex_code})")
    return desc, hex_code


def _fan_pct(speed_val):
    if speed_val is None:
        return None
    try:
        return round(int(speed_val) / 15 * 100)
    except (TypeError, ValueError):
        return None


def _num(val):
    return val if isinstance(val, (int, float)) and not isinstance(val, bool) else None


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
_ams_meta = {}
_vt_tray = {}
_lights = {}
_state_lock = threading.Lock()

_mqtt_client = None
_printer_connected = False
_last_report_ts = None
_print_started_at = None
_last_gcode_state = None

# Temperature history: deque of (unix_ts, nozzle, bed, chamber)
_history = deque(maxlen=HISTORY_SECONDS // int(HISTORY_MIN_GAP))
_last_history_ts = 0.0

# Error tracking
_first_message_seen = False
_startup_error_codes = set()
_error_log = []


def _update_state(data):
    """Merge incoming data into persistent state."""
    global _ams_state, _ams_meta, _vt_tray, _print_started_at, _last_gcode_state
    updated = False
    for key in ("print", "system", "pushing"):
        section = data.get(key, {})
        if section and isinstance(section, dict):
            _printer_state.update(section)
            updated = True

    p = data.get("print", {})
    if isinstance(p, dict):
        ams_section = p.get("ams", {})
        if isinstance(ams_section, dict):
            ams_units = ams_section.get("ams")
            if isinstance(ams_units, list):
                _ams_state = ams_units
            for k in ("tray_now", "tray_tar", "tray_pre", "ams_exist_bits"):
                if k in ams_section:
                    _ams_meta[k] = ams_section[k]
        if isinstance(p.get("vt_tray"), dict):
            _vt_tray = p["vt_tray"]
        lights = p.get("lights_report")
        if isinstance(lights, list):
            for entry in lights:
                node = entry.get("node")
                if node:
                    _lights[node] = entry.get("mode")

        # Track when the current job started so elapsed time can be shown.
        state = p.get("gcode_state")
        if state is not None:
            if state == "RUNNING" and _last_gcode_state not in ("RUNNING", "PAUSE"):
                _print_started_at = time.time()
            elif state in ("IDLE", "FINISH", "FAILED") and _last_gcode_state in ("RUNNING", "PAUSE"):
                pass  # keep start time until the next job starts
            _last_gcode_state = state

    return updated


def _record_history():
    """Append a temperature sample (call with _state_lock held)."""
    global _last_history_ts
    now = time.time()
    if now - _last_history_ts < HISTORY_MIN_GAP:
        return
    p = _printer_state
    nozzle = _num(p.get("nozzle_temper"))
    bed = _num(p.get("bed_temper"))
    chamber = _num(p.get("chamber_temper"))
    if nozzle is None and bed is None and chamber is None:
        return
    _history.append((round(now), nozzle, bed, chamber))
    _last_history_ts = now


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


def _tray_dict(tray, slot_label, active=False):
    tray_type = tray.get("tray_type", "") or ""
    tray_color = tray.get("tray_color", "") or ""
    remain = tray.get("remain")
    try:
        remain = int(remain) if remain is not None and int(remain) >= 0 else None
    except (TypeError, ValueError):
        remain = None
    tray_id = tray.get("id")
    return {
        "id": tray_id,
        "slot": slot_label,
        "type": tray_type,
        "color": tray_color[:6] if tray_color else "",
        "color_name": _color_name(tray_color) if tray_type else "",
        "name": tray.get("tray_id_name", "") or "",
        "brand": tray.get("tray_sub_brands", "") or "",
        "remain": remain,
        "nozzle_min": _num(tray.get("nozzle_temp_min")),
        "nozzle_max": _num(tray.get("nozzle_temp_max")),
        "active": bool(active),
    }


def _build_snapshot():
    """Build a JSON-serializable snapshot of printer state for the browser."""
    with _state_lock:
        p = dict(_printer_state)
        ams = list(_ams_state)
        ams_meta = dict(_ams_meta)
        vt = dict(_vt_tray)
        lights = dict(_lights)
        errors = list(_error_log)
        last_report = _last_report_ts
        started_at = _print_started_at

    now = time.time()
    state = p.get("gcode_state", "")
    state_label = GCODE_STATES.get(state, state or "Unknown")

    # Stage text (only meaningful while a job is active)
    stage = None
    try:
        stg = int(p.get("stg_cur", -1))
        stage = PRINT_STAGES.get(stg, f"Stage {stg}") or None
    except (TypeError, ValueError):
        pass
    if state not in ("RUNNING", "PAUSE", "PREPARE"):
        stage = None

    # Fans
    fans = []
    for key, label in [
        ("cooling_fan_speed", "Part cooling"),
        ("heatbreak_fan_speed", "Hotend"),
        ("big_fan1_speed", "Auxiliary"),
        ("big_fan2_speed", "Chamber"),
    ]:
        pct = _fan_pct(p.get(key))
        if pct is not None:
            fans.append({"name": label, "pct": pct})

    # AMS
    active_tray = ams_meta.get("tray_now")
    if active_tray is not None:
        try:
            active_tray = int(active_tray)
        except (TypeError, ValueError):
            active_tray = None
    ams_data = []
    for unit in ams:
        try:
            unit_id = int(unit.get("id", 0))
        except (TypeError, ValueError):
            unit_id = 0
        trays = []
        for tray in unit.get("tray", []):
            try:
                tray_idx = int(tray.get("id", 0))
            except (TypeError, ValueError):
                tray_idx = 0
            global_id = unit_id * 4 + tray_idx
            trays.append(_tray_dict(tray, tray_idx + 1, active_tray == global_id))
        ams_data.append({
            "id": unit_id,
            "humidity": unit.get("humidity"),
            "temp": unit.get("temp"),
            "trays": trays,
        })

    external = None
    if vt and vt.get("tray_type"):
        external = _tray_dict(vt, "Ext", active_tray == 254)

    # Errors
    error_list = [
        {
            "time": stamp,
            "severity": sev,
            "description": desc,
            "code": code_str,
            "url": f"{HMS_WIKI_URL}/{code_str}" if "_" in code_str else HMS_WIKI_URL,
        }
        for stamp, sev, desc, code_str in errors
    ]

    # Timing
    remaining = _num(p.get("mc_remaining_time"))
    finish_at = None
    if remaining is not None and remaining > 0 and state == "RUNNING":
        finish_at = (datetime.now() + timedelta(minutes=remaining)).strftime("%H:%M")
    elapsed = None
    if started_at and state in ("RUNNING", "PAUSE"):
        elapsed = format_time((now - started_at) / 60)

    # Speed
    speed_level = None
    try:
        speed_level = int(p.get("spd_lvl")) if p.get("spd_lvl") is not None else None
    except (TypeError, ValueError):
        pass

    # Connection health
    age = (now - last_report) if last_report else None
    printer_online = _printer_connected and age is not None and age < STALE_AFTER

    return {
        "state": state,
        "state_label": state_label,
        "stage": stage,
        "filename": p.get("subtask_name", "") or p.get("gcode_file", ""),
        "progress": p.get("mc_percent"),
        "layer": p.get("layer_num"),
        "total_layers": p.get("total_layer_num"),
        "eta": format_time(remaining),
        "remaining_minutes": remaining,
        "finish_at": finish_at,
        "elapsed": elapsed,
        "nozzle": format_temp(p.get("nozzle_temper"), p.get("nozzle_target_temper")),
        "bed": format_temp(p.get("bed_temper"), p.get("bed_target_temper")),
        "temps": {
            "nozzle": {"actual": _num(p.get("nozzle_temper")), "target": _num(p.get("nozzle_target_temper"))},
            "bed": {"actual": _num(p.get("bed_temper")), "target": _num(p.get("bed_target_temper"))},
            "chamber": {"actual": _num(p.get("chamber_temper")), "target": None},
        },
        "chamber_temp": p.get("chamber_temper"),
        "fans": fans,
        "wifi_signal": p.get("wifi_signal"),
        "speed_level": speed_level,
        "speed_label": SPEED_LEVELS.get(speed_level, "") if speed_level else "",
        "speed_pct": p.get("spd_mag"),
        "light_on": (lights.get("chamber_light") == "on") if "chamber_light" in lights else None,
        "ams": ams_data,
        "external_spool": external,
        "errors": error_list,
        "printer_online": printer_online,
        "report_age": round(age) if age is not None else None,
        "controls_enabled": CONTROLS_ENABLED,
        "last_update": time.strftime("%H:%M:%S"),
    }


def _history_payload():
    with _state_lock:
        rows = list(_history)
    return [{"t": t, "nozzle": n, "bed": b, "chamber": c} for t, n, b, c in rows]


# --- MQTT callbacks ---
def _on_connect(client, userdata, flags, reason_code, properties):
    global _printer_connected
    if reason_code == 0:
        _printer_connected = True
        topic = f"device/{BAMBU_SERIAL}/report"
        client.subscribe(topic)
        _request_pushall(client)
        socketio.emit("printer_state", _build_snapshot())
    else:
        print(f"MQTT connection failed: {reason_code}")


def _on_disconnect(client, userdata, flags, reason_code, properties):
    global _printer_connected
    _printer_connected = False
    print(f"MQTT disconnected: {reason_code}")
    socketio.emit("printer_state", _build_snapshot())


def _on_message(client, userdata, msg):
    global _last_report_ts
    try:
        data = json.loads(msg.payload)
    except json.JSONDecodeError:
        return

    with _state_lock:
        _track_errors(data)
        _update_state(data)
        _record_history()
        _last_report_ts = time.time()

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
        # Push a heartbeat so the UI can show staleness even when the printer is silent.
        socketio.emit("printer_state", _build_snapshot())


def _start_mqtt():
    """Start the MQTT client in a background thread."""
    global _mqtt_client
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
    client.on_disconnect = _on_disconnect
    client.on_message = _on_message
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    _mqtt_client = client

    poll_thread = threading.Thread(target=_poll_loop, args=(client,), daemon=True)
    poll_thread.start()

    while True:
        try:
            client.connect(BAMBU_IP, port=8883, keepalive=60)
            client.loop_forever(retry_first_connection=True)
        except Exception as exc:  # noqa: BLE001
            print(f"MQTT connect error: {exc}; retrying in 10s")
            time.sleep(10)


# --- Printer commands ---
def _send_command(payload):
    if _mqtt_client is None or not _printer_connected:
        return False, "Printer not connected"
    topic = f"device/{BAMBU_SERIAL}/request"
    _mqtt_client.publish(topic, json.dumps(payload))
    return True, "ok"


def _run_command(action, value=None):
    """Translate a dashboard action into a printer MQTT command."""
    if not CONTROLS_ENABLED:
        return False, "Controls are disabled (BAMBU_CONTROLS=0)"

    if action == "light":
        mode = "on" if value else "off"
        ok, msg = _send_command({"system": {"sequence_id": "0", "command": "ledctrl",
                                            "led_node": "chamber_light", "led_mode": mode,
                                            "led_on_time": 500, "led_off_time": 500,
                                            "loop_times": 0, "interval_time": 0}})
        return ok, f"Chamber light {mode}" if ok else msg

    if action in ("pause", "resume", "stop"):
        ok, msg = _send_command({"print": {"sequence_id": "0", "command": action, "param": ""}})
        return ok, f"Sent {action}" if ok else msg

    if action == "speed":
        try:
            level = int(value)
        except (TypeError, ValueError):
            return False, "Invalid speed level"
        if level not in SPEED_LEVELS:
            return False, "Invalid speed level"
        ok, msg = _send_command({"print": {"sequence_id": "0", "command": "print_speed",
                                           "param": str(level)}})
        return ok, f"Speed set to {SPEED_LEVELS[level]}" if ok else msg

    if action == "pushall":
        if _mqtt_client is None:
            return False, "Printer not connected"
        _request_pushall(_mqtt_client)
        return True, "Requested full status"

    return False, f"Unknown action: {action}"


# --- Flask routes ---
@app.route("/")
def index():
    return render_template("index.html", controls_enabled=CONTROLS_ENABLED)


@app.route("/api/state")
def api_state():
    return jsonify(_build_snapshot())


@app.route("/api/history")
def api_history():
    return jsonify(_history_payload())


@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(silent=True) or {}
    ok, msg = _run_command(body.get("action"), body.get("value"))
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


# --- Socket.IO events ---
@socketio.on("connect")
def handle_connect():
    """Send current state and history to a newly connected client."""
    emit("temp_history", _history_payload())
    emit("printer_state", _build_snapshot())


@socketio.on("command")
def handle_command(data):
    data = data or {}
    ok, msg = _run_command(data.get("action"), data.get("value"))
    emit("command_result", {"ok": ok, "message": msg, "action": data.get("action")})


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

    mode = "controls enabled" if CONTROLS_ENABLED else "display only"
    print(f"Starting web dashboard ({mode}), connecting to printer at {BAMBU_IP}")
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
