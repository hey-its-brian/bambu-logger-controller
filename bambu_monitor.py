#!/usr/bin/env python3
"""Bambu Lab P1S printer monitor — streams live status over MQTT."""

import json
import math
import os
import re
import select
import signal
import ssl
import sys
import termios
import threading
import time
import tty

import paho.mqtt.client as mqtt

import config

# --- ANSI colors ---
RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
DIM = "\033[2m"

# --- Layout helpers ---
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _get_term_width():
    """Get terminal width, defaulting to 80."""
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _visible_len(s):
    """String length ignoring ANSI escape codes."""
    return len(_ANSI_RE.sub("", s))


def _render_columns(left, right, left_width, gap=3):
    """Merge two lists of strings side-by-side with padding."""
    lines = []
    height = max(len(left), len(right))
    spacer = " " * gap
    for i in range(height):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        pad = left_width - _visible_len(l)
        lines.append(l + " " * max(pad, 0) + spacer + r)
    return lines


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
    # Normalize: take first 6 chars, uppercase, strip leading #
    h = hex_color.lstrip("#")[:6].upper()
    if h in COLOR_NAMES:
        return COLOR_NAMES[h]
    # Nearest-neighbor search
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
# HMS errors: keyed by full 16-char code (XXXX_XXXX_XXXX_XXXX)
# Falls back to partial match on first 8 chars (XXXX_XXXX)
HMS_ERRORS = {
    # Heatbed
    "0300_0100": "Heatbed temperature error: heating failed",
    "0300_0200": "Heatbed temperature error: thermal runaway",
    "0300_0300": "Heatbed temperature error: sensor abnormal",
    "0300_0400": "Chamber heater error",
    # Nozzle
    "0500_0100": "Nozzle temperature error: heating failed",
    "0500_0200": "Nozzle temperature error: thermal runaway",
    "0500_0300": "Nozzle temperature error: sensor abnormal",
    "0500_0400": "Nozzle clog detected",
    "0500_0500_0001_0007": "MQTT command verification failed (update firmware/Studio)",
    "0500_0500": "Filament broken or runout",
    # Motors
    "0700_0100": "Motor-X error: driver abnormal",
    "0700_0200": "Motor-Y error: driver abnormal",
    "0700_0300": "Motor-Z error: driver abnormal",
    "0700_0500": "Homing failed: axis stuck",
    "0700_0600": "Motor-E error: filament may be tangled",
    # Heatbed homing
    "0300_0D00_0002_0001": "Heatbed homing abnormal: bulge on heatbed or dirty nozzle tip",
    "0300_0D00_0001_0003": "Build plate may not be properly placed",
    # First layer / detection
    "0C00_0100": "First layer inspection failed",
    "0C00_0200": "Spaghetti/noodle detected",
    "0C00_0300": "First layer inspection: AMS filament stuck or broken",
    # AMS
    "1200_0100": "AMS communication error",
    "1200_0200": "AMS filament runout",
    "1200_0300": "AMS filament stuck or broken",
    "1200_0400": "AMS slot empty",
    "1200_1000": "AMS slot read error (RFID)",
}

# print_error: keyed by 8-char hex (from decimal print_error field)
PRINT_ERRORS = {
    "0300840C": "Print canceled by user",
    "03008400": "Print error (generic)",
}

# Wiki URL for looking up unknown HMS codes
HMS_WIKI_URL = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode"

# --- Printer state names ---
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


# --- Error tracking ---
_first_message_seen = False
_startup_error_codes = set()  # codes present on first message (shown dimmed)
_error_log = []  # list of (timestamp, severity, description, code_str) — append-only


def _hms_code_full(attr, code):
    """Build the full 16-char HMS code string from attr and code integers."""
    return (
        f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}_"
        f"{(code >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"
    )


def _hms_code_short(attr, code):
    """Build the short 8-char HMS code string (first two segments)."""
    return f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}"


def _lookup_hms(attr, code):
    """Look up an HMS error description. Returns (description, code_str)."""
    full = _hms_code_full(attr, code)
    short = _hms_code_short(attr, code)
    desc = HMS_ERRORS.get(full) or HMS_ERRORS.get(short)
    if not desc:
        desc = f"Unknown error — see {HMS_WIKI_URL}/{full}"
    return desc, full


def _lookup_print_error(error_val):
    """Decode a decimal print_error value. Returns (description, hex_code) or None."""
    if not error_val:
        return None
    hex_code = f"{int(error_val):08X}"
    if hex_code == "00000000":
        return None
    desc = PRINT_ERRORS.get(hex_code, f"Print error ({hex_code}) — see {HMS_WIKI_URL}")
    return desc, hex_code


def _fan_pct(speed_val):
    """Convert fan speed (0-15 string or int) to a percentage string."""
    if speed_val is None:
        return "--"
    pct = round(int(speed_val) / 15 * 100)
    return f"{pct}%"


# --- MQTT command support ---
_mqtt_client = None
_light_on = True

# --- Persistent message buffer ---
_messages = []
_messages_lock = threading.Lock()


def add_message(text, level="info"):
    """Append a persistent message (shown until dismissed by keypress)."""
    stamp = time.strftime("%H:%M:%S")
    with _messages_lock:
        _messages.append({"text": text, "level": level, "time": stamp})


def clear_messages():
    """Clear all persistent messages."""
    with _messages_lock:
        _messages.clear()


def _send_command(payload):
    """Publish a command to the printer via MQTT."""
    if _mqtt_client is None:
        add_message("Not connected to printer", "error")
        return
    topic = f"device/{config.BAMBU_SERIAL}/request"
    _mqtt_client.publish(topic, json.dumps(payload))


def _render_messages_lines():
    """Return list of message lines for display."""
    with _messages_lock:
        if not _messages:
            return []
        lines = []
        for msg in _messages:
            level = msg["level"]
            stamp = msg["time"]
            text = msg["text"]
            if level == "error":
                lines.append(f"  {RED}[{stamp}] {text}{RESET}")
            elif level == "warning":
                lines.append(f"  {YELLOW}[{stamp}] {text}{RESET}")
            else:
                lines.append(f"  {DIM}[{stamp}] {text}{RESET}")
        lines.append(f"  {DIM}Press any key to clear{RESET}")
        return lines


# --- Non-blocking key listener ---
_original_term_settings = None


def _handle_key(key):
    """Process a single keypress: dispatch commands or clear messages."""
    global _light_on

    # Light toggle
    if key == "l":
        _light_on = not _light_on
        mode = "on" if _light_on else "off"
        _send_command({"system": {"sequence_id": "0", "command": "ledctrl",
                                  "led_node": "chamber_light", "led_mode": mode}})
        add_message(f"Light: {mode}")
        return

    # Any other key: just clear messages
    clear_messages()


def _key_listener():
    """Daemon thread: wait for keypresses and dispatch commands."""
    global _original_term_settings
    fd = sys.stdin.fileno()
    try:
        _original_term_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        # Drain any buffered input (e.g. Enter from launching the command)
        while select.select([fd], [], [], 0)[0]:
            os.read(fd, 1024)
        while True:
            ch = os.read(fd, 1)
            if ch:
                # Ctrl-C passes through so signal handler can fire
                if ch == b"\x03":
                    os.kill(os.getpid(), signal.SIGINT)
                else:
                    _handle_key(ch.decode("ascii", errors="ignore"))
    except OSError:
        pass
    finally:
        if _original_term_settings:
            termios.tcsetattr(fd, termios.TCSADRAIN, _original_term_settings)


def _restore_terminal():
    """Restore terminal settings (called on shutdown)."""
    if _original_term_settings:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _original_term_settings)
        except OSError:
            pass


def clear_screen():
    print("\033[2J\033[3J\033[H", end="")


def format_time(minutes):
    """Format minutes into h:mm."""
    if not minutes or minutes < 0:
        return "--:--"
    h, m = divmod(int(minutes), 60)
    return f"{h}:{m:02d}"


def format_temp(actual, target):
    """Format temperature as 'actual/target°C'."""
    a = f"{actual:.1f}" if isinstance(actual, (int, float)) else "--"
    t = f"{target:.0f}" if isinstance(target, (int, float)) and target > 0 else "--"
    return f"{a}/{t}°C"


# --- Persistent printer state (accumulates across MQTT messages) ---
_printer_state = {}
_ams_state = []  # list of AMS units with trays


def _update_state(data):
    """Merge incoming data into persistent state."""
    global _ams_state
    updated = False
    for key in ("print", "system", "pushing"):
        section = data.get(key, {})
        if section and isinstance(section, dict):
            _printer_state.update(section)
            updated = True
    # Extract AMS data
    ams_section = data.get("print", {}).get("ams", {})
    if isinstance(ams_section, dict):
        ams_units = ams_section.get("ams")
        if isinstance(ams_units, list):
            _ams_state = ams_units
    return updated


def _track_errors(data):
    """Extract errors from raw MQTT data BEFORE it gets merged into state.

    On the first message, just record which codes exist (startup errors).
    On subsequent messages, append any new errors to the persistent log.
    """
    global _first_message_seen

    p = data.get("print", {})
    if not p:
        return

    # Collect error codes from this message
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

    # Append new errors to the log (skip startup codes and duplicates)
    logged_codes = {entry[3] for entry in _error_log}
    stamp = time.strftime("%H:%M:%S")
    for code, (severity, desc, code_str) in current.items():
        if code not in _startup_error_codes and code not in logged_codes:
            _error_log.append((stamp, severity, desc, code_str))


def _render_header(term_width):
    """Return header lines with WiFi signal right-aligned."""
    p = _printer_state
    wifi = p.get("wifi_signal")
    title = " Bambu P1S Monitor"
    lines = [f"{BOLD}{'=' * term_width}{RESET}"]
    if wifi is not None:
        wifi_val = str(wifi).replace("dBm", "")
        wifi_str = f"WiFi: {wifi_val}dBm"
        pad = term_width - len(title) - len(wifi_str) - 1
        lines.append(f"{BOLD}{title}{' ' * max(pad, 1)}{RESET}{DIM}{wifi_str}{RESET}")
    else:
        lines.append(f"{BOLD}{title}{RESET}")
    lines.append(f"{BOLD}{'=' * term_width}{RESET}")
    return lines


def _build_print_info(p, state, state_str, col_width):
    """Build left column: state, file, progress, layer, ETA."""
    lines = [f"  {BOLD}State:{RESET}  {state_str}"]
    if state not in ("IDLE", ""):
        filename = p.get("gcode_file", "") or p.get("subtask_name", "")
        if filename:
            max_fn = col_width - 12
            if max_fn < 10:
                max_fn = 10
            if len(filename) > max_fn:
                filename = "..." + filename[-(max_fn - 3):]
            lines.append(f"  {BOLD}File:{RESET}   {filename}")

        progress = p.get("mc_percent")
        if progress is not None:
            bar_width = max(col_width - 22, 10)
            filled = int(bar_width * int(progress) / 100)
            bar = f"[{'█' * filled}{'░' * (bar_width - filled)}]"
            lines.append(f"  {BOLD}Progress:{RESET} {bar} {progress}%")

        layer = p.get("layer_num")
        total_layers = p.get("total_layer_num")
        if layer is not None and total_layers:
            lines.append(f"  {BOLD}Layer:{RESET}   {layer}/{total_layers}")

        remaining = p.get("mc_remaining_time")
        if remaining is not None:
            lines.append(f"  {BOLD}ETA:{RESET}     {format_time(remaining)}")
    return lines


def _build_temp_fans(p):
    """Build right column: temperatures and fan speeds."""
    lines = []
    nozzle_temp = p.get("nozzle_temper")
    nozzle_target = p.get("nozzle_target_temper")
    bed_temp = p.get("bed_temper")
    bed_target = p.get("bed_target_temper")
    chamber_temp = p.get("chamber_temper")

    if any(v is not None for v in [nozzle_temp, bed_temp, chamber_temp]):
        lines.append(f"{BLUE}{BOLD}Temperatures{RESET}")
        if nozzle_temp is not None:
            lines.append(f"  Nozzle:  {format_temp(nozzle_temp, nozzle_target)}")
        if bed_temp is not None:
            lines.append(f"  Bed:     {format_temp(bed_temp, bed_target)}")
        if chamber_temp is not None:
            lines.append(f"  Chamber: {chamber_temp}°C")

    part_fan = p.get("cooling_fan_speed")
    hotend_fan = p.get("heatbreak_fan_speed")
    aux_fan = p.get("big_fan1_speed")
    chamber_fan = p.get("big_fan2_speed")

    if any(v is not None for v in [part_fan, hotend_fan, aux_fan, chamber_fan]):
        lines.append(f"{BLUE}{BOLD}Fans{RESET}")
        if part_fan is not None:
            lines.append(f"  Part cooling: {_fan_pct(part_fan)}")
        if hotend_fan is not None:
            lines.append(f"  Hotend:       {_fan_pct(hotend_fan)}")
        if aux_fan is not None:
            lines.append(f"  Auxiliary:    {_fan_pct(aux_fan)}")
        if chamber_fan is not None:
            lines.append(f"  Chamber:      {_fan_pct(chamber_fan)}")
    return lines


def _wrap_text(text, width):
    """Wrap text to fit within width, breaking on spaces or forcing breaks."""
    if len(text) <= width:
        return [text]
    result = []
    while len(text) > width:
        # Try to break at a space
        brk = text.rfind(" ", 0, width)
        if brk <= 0:
            brk = width  # Force break mid-word/URL
        result.append(text[:brk])
        text = text[brk:].lstrip()
    if text:
        result.append(text)
    return result


def _build_errors_and_messages(col_width=None):
    """Build bottom-left: error log + persistent messages."""
    lines = []
    if _error_log:
        lines.append(f"  {RED}{BOLD}⚠ Errors (this session){RESET}")
        # "    [HH:MM:SS] " = 4 + 10 + 2 = 16 chars total
        prefix_len = 16
        max_desc = (col_width - prefix_len) if col_width and col_width > 24 else 60
        # Continuation lines align under the description text
        cont_pad = " " * (prefix_len - 4)  # 4 leading spaces added separately
        for stamp, severity, desc, code_str in _error_log:
            color = RED if severity >= 3 else YELLOW
            wrapped = _wrap_text(desc, max_desc)
            lines.append(f"    {color}[{stamp}] {wrapped[0]}{RESET}")
            for cont in wrapped[1:]:
                lines.append(f"    {color}{cont_pad}{cont}{RESET}")

    msg_lines = _render_messages_lines()
    if msg_lines:
        if lines:
            lines.append("")
        lines.extend(msg_lines)
    return lines


def _build_ams_info():
    """Build bottom-right: AMS filament slots with type and color."""
    if not _ams_state:
        return []
    lines = [f"{BLUE}{BOLD}AMS Filaments{RESET}"]
    for unit in _ams_state:
        humidity = unit.get("humidity")
        temp = unit.get("temp")
        meta = []
        if humidity is not None:
            meta.append(f"H:{humidity}%")
        if temp is not None:
            meta.append(f"T:{temp}°C")
        if meta:
            lines.append(f"  {DIM}{' '.join(meta)}{RESET}")
        for tray in unit.get("tray", []):
            slot_id = int(tray.get("id", 0)) + 1
            tray_type = tray.get("tray_type", "")
            tray_color = tray.get("tray_color", "")
            if not tray_type:
                lines.append(f"  Slot {slot_id}: {DIM}(empty){RESET}")
            else:
                cname = _color_name(tray_color)
                if cname:
                    lines.append(f"  Slot {slot_id}: {tray_type} ({cname})")
                else:
                    lines.append(f"  Slot {slot_id}: {tray_type}")
    return lines


def _render_footer():
    """Return footer lines."""
    return [
        "",
        f"{DIM}  Last update: {time.strftime('%H:%M:%S')}{RESET}",
        f"{DIM}  l:toggle light{RESET}",
        f"{DIM}  Ctrl+C to exit{RESET}",
    ]


def print_status():
    """Render the current printer state to screen."""
    p = _printer_state
    if not p:
        return

    state = p.get("gcode_state", "")
    state_label = GCODE_STATES.get(state, state or "Unknown")

    if state == "RUNNING":
        state_str = f"{GREEN}{BOLD}{state_label}{RESET}"
    elif state in ("PAUSE", "PREPARE"):
        state_str = f"{YELLOW}{BOLD}{state_label}{RESET}"
    elif state == "FAILED":
        state_str = f"{RED}{BOLD}{state_label}{RESET}"
    elif state == "FINISH":
        state_str = f"{CYAN}{BOLD}{state_label}{RESET}"
    else:
        state_str = f"{BOLD}{state_label}{RESET}"

    term_width = _get_term_width()
    narrow = term_width < 60

    clear_screen()

    # Header
    for line in _render_header(term_width):
        print(line)

    if narrow:
        # Single-column fallback
        col_width = term_width - 4
        for line in _build_print_info(p, state, state_str, col_width):
            print(line)
        print()
        right = _build_temp_fans(p)
        for line in right:
            print(f"  {line}")
        errors = _build_errors_and_messages(col_width)
        if errors:
            print()
            for line in errors:
                print(line)
        ams = _build_ams_info()
        if ams:
            print()
            for line in ams:
                print(f"  {line}")
    else:
        # Two-column layout
        col_width = (term_width - 6) // 2
        left_top = _build_print_info(p, state, state_str, col_width)
        right_top = _build_temp_fans(p)
        print()
        for line in _render_columns(left_top, right_top, col_width):
            print(line)

        left_bot = _build_errors_and_messages(col_width)
        right_bot = _build_ams_info()
        if left_bot or right_bot:
            print()
            for line in _render_columns(left_bot, right_bot, col_width):
                print(line)

    for line in _render_footer():
        print(line)


POLL_INTERVAL = 5  # seconds between pushall requests


def _request_pushall(client):
    """Send a pushall request to the printer."""
    request_topic = f"device/{config.BAMBU_SERIAL}/request"
    push_all = json.dumps({
        "pushing": {"sequence_id": "0", "command": "pushall"}
    })
    client.publish(request_topic, push_all)


def _poll_loop(client):
    """Daemon thread: periodically request full status from the printer."""
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            _request_pushall(client)
        except Exception:
            pass


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        topic = f"device/{config.BAMBU_SERIAL}/report"
        client.subscribe(topic)
        _request_pushall(client)
    else:
        add_message(f"Connection failed: {reason_code}", "error")


DEBUG_LOG = os.environ.get("BAMBU_DEBUG")


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
    except json.JSONDecodeError:
        return

    if DEBUG_LOG:
        with open(DEBUG_LOG, "a") as f:
            f.write(json.dumps(data, indent=2) + "\n---\n")

    _track_errors(data)
    _update_state(data)
    print_status()


def main():
    config.validate()

    # Start non-blocking key listener (daemon thread)
    key_thread = threading.Thread(target=_key_listener, daemon=True)
    key_thread.start()

    clear_screen()
    print(f"{BOLD}Connecting to Bambu P1S at {config.BAMBU_IP}...{RESET}")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="bambu_monitor",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set("bblp", config.BAMBU_ACCESS_CODE)

    # TLS with no cert verification (printer uses self-signed cert)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ctx)

    client.on_connect = on_connect
    client.on_message = on_message

    global _mqtt_client
    _mqtt_client = client

    # Start periodic polling thread
    poll_thread = threading.Thread(target=_poll_loop, args=(client,), daemon=True)
    poll_thread.start()

    # Graceful shutdown
    def shutdown(sig, frame):
        _restore_terminal()
        print(f"\n{YELLOW}Disconnecting...{RESET}")
        client.disconnect()
        client.loop_stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        client.connect(config.BAMBU_IP, port=8883, keepalive=60)
        client.loop_forever()
    except Exception as e:
        add_message(f"Connection error: {e}", "error")
        # Keep running briefly so the user can see the error
        time.sleep(30)
    finally:
        _restore_terminal()


if __name__ == "__main__":
    main()
