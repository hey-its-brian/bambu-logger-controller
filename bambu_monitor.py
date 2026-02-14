#!/usr/bin/env python3
"""Bambu Lab P1S printer monitor — streams live status over MQTT."""

import json
import os
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

# --- HMS error code lookup (common P1S codes) ---
HMS_ERRORS = {
    "0300_0100": "Heatbed temperature error: heating failed",
    "0300_0200": "Heatbed temperature error: thermal runaway",
    "0300_0300": "Heatbed temperature error: sensor abnormal",
    "0500_0100": "Nozzle temperature error: heating failed",
    "0500_0200": "Nozzle temperature error: thermal runaway",
    "0500_0300": "Nozzle temperature error: sensor abnormal",
    "0700_0100": "Motor-X error: driver abnormal",
    "0700_0200": "Motor-Y error: driver abnormal",
    "0700_0300": "Motor-Z error: driver abnormal",
    "0C00_0100": "First layer inspection failed",
    "0C00_0200": "Spaghetti/noodle detected",
    "0300_0400": "Chamber heater error",
    "0500_0400": "Nozzle clog detected",
    "0500_0500": "Filament broken or runout",
    "0700_0500": "Homing failed: axis stuck",
    "0700_0600": "Motor-E error: filament may be tangled",
    "1200_0100": "AMS communication error",
    "1200_0200": "AMS filament runout",
    "1200_0300": "AMS filament stuck or broken",
    "1200_0400": "AMS slot empty",
    "1200_1000": "AMS slot read error (RFID)",
}

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


def _render_messages():
    """Return string block for any buffered messages."""
    with _messages_lock:
        if not _messages:
            return ""
        lines = [f"\n  {BOLD}{'─' * 46}{RESET}"]
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
        lines.append(f"  {DIM}Press any key to clear messages{RESET}")
        return "\n".join(lines)


# --- Non-blocking key listener ---
_original_term_settings = None


def _key_listener():
    """Daemon thread: wait for any keypress, then clear the message buffer."""
    global _original_term_settings
    fd = sys.stdin.fileno()
    try:
        _original_term_settings = termios.tcgetattr(fd)
        tty.setraw(fd)
        while True:
            ch = os.read(fd, 1)
            if ch:
                clear_messages()
                # Ctrl-C passes through so signal handler can fire
                if ch == b"\x03":
                    os.kill(os.getpid(), signal.SIGINT)
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
    print("\033[2J\033[H", end="")


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


def print_status(data):
    """Parse and display a print status message."""
    p = data.get("print", {})
    if not p:
        return

    state = p.get("gcode_state", "")
    state_label = GCODE_STATES.get(state, state or "Unknown")

    # Color the state
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

    clear_screen()
    print(f"{BOLD}{'=' * 50}{RESET}")
    print(f"{BOLD} Bambu P1S Monitor{RESET}")
    print(f"{BOLD}{'=' * 50}{RESET}")

    # State
    print(f"\n  {BOLD}State:{RESET}  {state_str}")

    # Print job info
    filename = p.get("gcode_file", "") or p.get("subtask_name", "")
    if filename:
        # Trim long filenames
        if len(filename) > 35:
            filename = "..." + filename[-32:]
        print(f"  {BOLD}File:{RESET}   {filename}")

    progress = p.get("mc_percent")
    if progress is not None:
        bar_width = 30
        filled = int(bar_width * int(progress) / 100)
        bar = f"[{'█' * filled}{'░' * (bar_width - filled)}]"
        print(f"  {BOLD}Progress:{RESET} {bar} {progress}%")

    layer = p.get("layer_num")
    total_layers = p.get("total_layer_num")
    if layer is not None and total_layers:
        print(f"  {BOLD}Layer:{RESET}   {layer}/{total_layers}")

    remaining = p.get("mc_remaining_time")
    if remaining is not None:
        print(f"  {BOLD}ETA:{RESET}     {format_time(remaining)}")

    # Temperatures
    nozzle_temp = p.get("nozzle_temper")
    nozzle_target = p.get("nozzle_target_temper")
    bed_temp = p.get("bed_temper")
    bed_target = p.get("bed_target_temper")
    chamber_temp = p.get("chamber_temper")

    if any(v is not None for v in [nozzle_temp, bed_temp, chamber_temp]):
        print(f"\n  {BLUE}{BOLD}Temperatures{RESET}")
        if nozzle_temp is not None:
            print(f"    Nozzle:  {format_temp(nozzle_temp, nozzle_target)}")
        if bed_temp is not None:
            print(f"    Bed:     {format_temp(bed_temp, bed_target)}")
        if chamber_temp is not None:
            print(f"    Chamber: {chamber_temp}°C")

    # Fan speeds
    fan_gear = p.get("fan_gear")
    if fan_gear is not None:
        print(f"\n  {DIM}Fan: {fan_gear}{RESET}")

    # HMS errors
    hms = p.get("hms", [])
    if hms:
        print(f"\n  {RED}{BOLD}⚠ Errors / Warnings{RESET}")
        for entry in hms:
            attr = entry.get("attr", 0)
            code = entry.get("code", 0)
            # Build the code string from attr+code
            code_str = f"{attr:04X}_{code:04X}"
            desc = HMS_ERRORS.get(code_str, f"Unknown error ({code_str})")
            severity = (attr >> 16) & 0xFF
            if severity >= 3:
                print(f"    {RED}[ERROR] {desc}{RESET}")
            else:
                print(f"    {YELLOW}[WARN]  {desc}{RESET}")

    print(f"\n{DIM}  Last update: {time.strftime('%H:%M:%S')}{RESET}")
    print(f"{DIM}  Ctrl+C to exit{RESET}")

    # Persistent messages
    msg_block = _render_messages()
    if msg_block:
        print(msg_block)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        add_message("Connected to printer", "info")
        topic = f"device/{config.BAMBU_SERIAL}/report"
        client.subscribe(topic)
        add_message(f"Subscribed to {topic}", "info")

        # Request full status dump
        request_topic = f"device/{config.BAMBU_SERIAL}/request"
        push_all = json.dumps({
            "pushing": {"sequence_id": "0", "command": "pushall"}
        })
        client.publish(request_topic, push_all)
        add_message("Requested full status...", "info")
    else:
        add_message(f"Connection failed: {reason_code}", "error")


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
    except json.JSONDecodeError:
        return

    if "print" in data:
        print_status(data)


def main():
    config.validate()

    # Start non-blocking key listener (daemon thread)
    key_thread = threading.Thread(target=_key_listener, daemon=True)
    key_thread.start()

    add_message(f"Connecting to Bambu P1S at {config.BAMBU_IP}...", "info")

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
