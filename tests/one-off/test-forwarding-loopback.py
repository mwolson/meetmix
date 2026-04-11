#!/usr/bin/env python3
"""Test whether pw-loopback can forward audio to an HFP BT output sink.

Usage: uv run python3 tests/one-off/test-forwarding-loopback.py

Switches to HFP, creates a null sink, starts a pw-loopback with
target.object=<serial> and media.role=Communication, plays a bell sound,
verifies linking via pw-link, and restores original state.

Finding: pw-loopback requires both target.object (serial) and
media.role=Communication in --playback-props for HFP sinks. Using -P with a
separate --playback-props causes the target to be overridden. WirePlumber's
intended-roles policy blocks streams without media.role=Communication from
connecting to HFP sinks.
"""

import json
import subprocess
import sys
import time

import pulsectl

DEVICE_MATCH = "AirPods"


def main():
    pulse = pulsectl.Pulse("test-fwd-loopback")

    subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "false"],
        capture_output=True,
    )
    print("Disabled WirePlumber BT autoswitch")

    card = None
    for c in pulse.card_list():
        props = c.proplist
        for key in ("device.description", "device.alias"):
            if DEVICE_MATCH.lower() in props.get(key, "").lower():
                card = c
                break
    if not card:
        print("No BT card found")
        return 1

    original_profile = card.profile_active.name
    original_volume = None
    print(f"Original profile: {original_profile}")

    # Save original A2DP volume
    for sink in pulse.sink_list():
        if DEVICE_MATCH.lower() in sink.description.lower():
            original_volume = sink.volume.value_flat
            print(f"Original volume: {original_volume:.0%}")
            break

    pulse.card_profile_set(card, "headset-head-unit")
    time.sleep(1)

    bt_sink = None
    for sink in pulse.sink_list():
        if DEVICE_MATCH.lower() in sink.description.lower():
            bt_sink = sink
            break
    if not bt_sink:
        print("No BT sink found after HFP switch")
        cleanup(pulse, card, original_profile, None, None)
        return 1

    print(f"HFP sink: {bt_sink.name}")
    print(f"  volume after switch: {bt_sink.volume.value_flat:.0%}")

    # Restore volume to original level
    if original_volume is not None:
        vol = bt_sink.volume
        vol.value_flat = original_volume
        pulse.sink_volume_set(bt_sink.index, vol)
        print(f"  volume restored to: {original_volume:.0%}")

    # Get object.serial for the HFP sink
    serial = get_node_serial(bt_sink.name)
    if not serial:
        print("Could not find object.serial for HFP sink")
        cleanup(pulse, card, original_profile, bt_sink.name, original_volume)
        return 1
    print(f"  object.serial: {serial}")

    # Create test null sink
    null_idx = pulse.module_load(
        "module-null-sink", "sink_name=test_fwd sink_properties=device.description=TestFwd"
    )
    print(f"Created null sink test_fwd (module {null_idx})")

    # Start pw-loopback with target.object and media.role in --playback-props
    loopback_proc = subprocess.Popen(
        [
            "pw-loopback",
            "-C", "test_fwd",
            "--capture-props", "stream.capture.sink=true",
            "--playback-props", f"target.object={serial} media.role=Communication",
        ],
    )
    print(f"Started pw-loopback (pid {loopback_proc.pid})")
    time.sleep(1)

    # Verify links
    verify_links(loopback_proc.pid, bt_sink.name)

    # Play bell
    print("\n--- Playing bell ---")
    subprocess.run(
        ["pw-play", "--target=test_fwd", "/usr/share/sounds/freedesktop/stereo/bell.oga"],
    )
    time.sleep(3)

    # Cleanup
    loopback_proc.terminate()
    loopback_proc.wait(timeout=2)
    pulse.module_unload(null_idx)
    cleanup(pulse, card, original_profile, bt_sink.name, original_volume)
    return 0


def get_node_serial(node_name):
    result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
    for node in json.loads(result.stdout):
        props = node.get("info", {}).get("props", {})
        if props.get("node.name") == node_name:
            serial = props.get("object.serial")
            if serial is not None:
                return str(serial)
    return None


def verify_links(pid, bt_sink_name):
    result = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=5)
    lines = result.stdout.splitlines()
    loopback_id = f"pw-loopback-{pid}"

    has_input = False
    has_output = False
    current_owner = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not line[0].isspace():
            current_owner = stripped.split(":")[0] if ":" in stripped else None
        elif "|" in stripped:
            linked_to = stripped.split("|")[1].lstrip("-> ").lstrip("<- ")
            linked_owner = linked_to.split(":")[0] if ":" in linked_to else ""
            if current_owner and loopback_id in current_owner:
                if bt_sink_name in linked_owner:
                    has_output = True
                elif "test_fwd" in linked_owner:
                    has_input = True
            elif current_owner and bt_sink_name in current_owner:
                if loopback_id in linked_owner:
                    has_output = True
            elif current_owner and "test_fwd" in current_owner:
                if loopback_id in linked_owner:
                    has_input = True

    print(f"  input linked (test_fwd -> loopback): {has_input}")
    print(f"  output linked (loopback -> {bt_sink_name}): {has_output}")
    if has_input and has_output:
        print("  PASS: fully linked")
    else:
        print("  FAIL: not fully linked")
        # Dump relevant lines
        for line in lines:
            if loopback_id in line or "test_fwd" in line:
                print(f"    {line}")


def cleanup(pulse, card, original_profile, sink_name, original_volume):
    subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "true"],
        capture_output=True,
    )
    print("\nRestored WirePlumber BT autoswitch")
    pulse.card_profile_set(card, original_profile)
    print(f"Restored profile: {original_profile}")
    if sink_name and original_volume is not None:
        time.sleep(1)
        for sink in pulse.sink_list():
            if sink.name == sink_name:
                vol = sink.volume
                vol.value_flat = original_volume
                pulse.sink_volume_set(sink.index, vol)
                print(f"Restored volume: {original_volume:.0%}")
                break
    pulse.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
