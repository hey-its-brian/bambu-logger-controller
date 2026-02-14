#!/usr/bin/env python3
"""Bambu Lab P1S printer monitor — streams live status over MQTT."""

import json
import signal
import ssl
import sys
import time

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


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print(f"{GREEN}Connected to printer{RESET}")
        topic = f"device/{config.BAMBU_SERIAL}/report"
        client.subscribe(topic)
        print(f"{DIM}Subscribed to {topic}{RESET}")

        # Request full status dump
        request_topic = f"device/{config.BAMBU_SERIAL}/request"
        push_all = json.dumps({
            "pushing": {"sequence_id": "0", "command": "pushall"}
        })
        client.publish(request_topic, push_all)
        print(f"{DIM}Requested full status...{RESET}")
    else:
        print(f"{RED}Connection failed: {reason_code}{RESET}")
        sys.exit(1)


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
    except json.JSONDecodeError:
        return

    if "print" in data:
        print_status(data)


def main():
    config.validate()

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

    # Graceful shutdown
    def shutdown(sig, frame):
        print(f"\n{YELLOW}Disconnecting...{RESET}")
        client.disconnect()
        client.loop_stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    client.connect(config.BAMBU_IP, port=8883, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
