#!/usr/bin/env python3
"""Isolate pw-record as the cause of BT HFP audio silence.

Usage: uv run python3 tests/one-off/test-pw-record-isolate.py

Sets up the pipeline with CORRECT ordering (forwarding first), then:
  1. Plays bell WITHOUT pw-record → should be heard (confirms link works)
  2. Starts pw-record from capture sink
  3. Checks if forwarding link is still intact (pw-link -l)
  4. Plays bell WITH pw-record → if not heard, pw-record breaks forwarding
  5. Stops pw-record
  6. Plays bell AFTER pw-record stops → does it recover?
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import pulsectl

DEVICE_MATCH = "AirPods"
NULL_SINK_NAME = "meetmix_combined"
CAPTURE_SINK_NAME = "meetmix_capture"
BELL = "/usr/share/sounds/freedesktop/stereo/bell.oga"


def main():
    pulse = pulsectl.Pulse("test-pw-record-isolate")

    subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "false"],
        capture_output=True,
    )

    card = find_bt_card(pulse)
    if not card:
        print("No BT card found")
        return 1

    original_profile = card.profile_active.name
    original_volume = None
    for sink in pulse.sink_list():
        if DEVICE_MATCH.lower() in sink.description.lower():
            original_volume = sink.volume.value_flat
            break

    pulse.card_profile_set(card, "headset-head-unit")
    time.sleep(1)

    bt_sink = None
    for sink in pulse.sink_list():
        if DEVICE_MATCH.lower() in sink.description.lower():
            bt_sink = sink
            break
    if not bt_sink:
        print("No BT sink found")
        cleanup(pulse, card, original_profile, original_volume)
        return 1

    if original_volume is not None:
        vol = bt_sink.volume
        vol.value_flat = original_volume
        pulse.sink_volume_set(bt_sink.index, vol)

    serial = get_node_serial(bt_sink.name)
    print(f"BT sink: {bt_sink.name} (serial {serial})")

    processes = []
    modules = []

    try:
        # Create sinks
        combined_idx = pulse.module_load(
            "module-null-sink",
            f"sink_name={NULL_SINK_NAME} sink_properties=device.description=MeetMixCombined",
        )
        modules.append(combined_idx)
        capture_idx = pulse.module_load(
            "module-null-sink",
            f"sink_name={CAPTURE_SINK_NAME} sink_properties=device.description=MeetMixCapture",
        )
        modules.append(capture_idx)

        # Mic loopback
        mic_source = None
        for source in pulse.source_list():
            if source.monitor_of_sink != 0xFFFFFFFF:
                continue
            if DEVICE_MATCH.lower() in source.description.lower():
                mic_source = source.name
                break
        if mic_source:
            mic_idx = pulse.module_load(
                "module-loopback",
                f"source={mic_source} sink={CAPTURE_SINK_NAME} latency_msec=1"
                " source_output_properties=media.role=Communication",
            )
            modules.append(mic_idx)
            print(f"Mic loopback: {mic_source} -> {CAPTURE_SINK_NAME}")

        # Forwarding FIRST (correct order)
        fwd = subprocess.Popen([
            "pw-loopback",
            "-C", NULL_SINK_NAME,
            "--capture-props", "stream.capture.sink=true",
            "--playback-props", f"target.object={serial} media.role=Communication",
        ])
        processes.append(fwd)
        print(f"Forwarding loopback started (pid {fwd.pid})")

        # Wait for link
        if not wait_for_link(fwd.pid, bt_sink.name, timeout=5):
            print("FAIL: forwarding never linked")
            return 1

        # Capture loopback SECOND
        cap = subprocess.Popen([
            "pw-loopback",
            "-C", NULL_SINK_NAME,
            "--capture-props", "stream.capture.sink=true",
            "-P", CAPTURE_SINK_NAME,
        ])
        processes.append(cap)
        print(f"Capture loopback started (pid {cap.pid})")

        pulse.sink_default_set(NULL_SINK_NAME)
        time.sleep(1)

        # === TEST 1: Bell WITHOUT pw-record ===
        print("\n=== TEST 1: Bell WITHOUT pw-record ===")
        check_link_state(fwd.pid, bt_sink.name)
        subprocess.run(["pw-play", f"--target={NULL_SINK_NAME}", BELL], timeout=10)
        time.sleep(2)

        # === TEST 2: Start pw-record, check link, play bell ===
        print("\n=== TEST 2: Starting pw-record... ===")
        capture_serial = get_node_serial(CAPTURE_SINK_NAME)
        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="test-isolate-")
        os.close(wav_fd)
        record_cmd = [
            "pw-record",
            f"--target={capture_serial or CAPTURE_SINK_NAME}",
            "-P", "stream.capture.sink=true",
            wav_path,
        ]
        print(f"  cmd: {' '.join(record_cmd)}")
        record_proc = subprocess.Popen(
            record_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"  pw-record started (pid {record_proc.pid})")
        time.sleep(2)

        print("\n  Link state AFTER pw-record start:")
        check_link_state(fwd.pid, bt_sink.name)

        print("\n  Playing bell WITH pw-record...")
        subprocess.run(["pw-play", f"--target={NULL_SINK_NAME}", BELL], timeout=10)
        time.sleep(2)

        # === TEST 3: Stop pw-record, play bell again ===
        print("\n=== TEST 3: Bell AFTER stopping pw-record ===")
        record_proc.terminate()
        try:
            record_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            record_proc.kill()
            record_proc.wait()
        print("  pw-record stopped")
        time.sleep(1)

        print("  Link state AFTER pw-record stop:")
        check_link_state(fwd.pid, bt_sink.name)

        print("  Playing bell...")
        subprocess.run(["pw-play", f"--target={NULL_SINK_NAME}", BELL], timeout=10)
        time.sleep(3)

    finally:
        print("\n--- Cleanup ---")
        for proc in processes:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for mod in reversed(modules):
            try:
                pulse.module_unload(mod)
            except Exception:
                pass
        cleanup(pulse, card, original_profile, original_volume)

        if 'wav_path' in dir() and os.path.exists(wav_path):
            os.remove(wav_path)

    print("\nExpected: heard Test 1 and Test 3, not Test 2 = pw-record is the cause.")
    return 0


def wait_for_link(pid, bt_sink_name, timeout=5):
    loopback_id = f"pw-loopback-{pid}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=3)
        if check_linked(result.stdout, loopback_id, bt_sink_name):
            elapsed = timeout - (deadline - time.monotonic())
            print(f"  Linked in {elapsed:.1f}s")
            return True
        time.sleep(0.25)
    print(f"  NOT linked after {timeout}s")
    return False


def check_linked(output, loopback_id, bt_sink_name):
    current_owner = None
    for line in output.splitlines():
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
                    return True
            elif current_owner and bt_sink_name in current_owner:
                if loopback_id in linked_owner:
                    return True
    return False


def check_link_state(pid, bt_sink_name):
    loopback_id = f"pw-loopback-{pid}"
    result = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=3)
    linked = check_linked(result.stdout, loopback_id, bt_sink_name)
    print(f"  Forwarding -> BT: {'LINKED' if linked else 'NOT LINKED'}")

    # Also check node states
    result2 = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=3)
    for line in result2.stdout.splitlines():
        if bt_sink_name in line or loopback_id in line:
            print(f"    {line.strip()}")


def find_bt_card(pulse):
    for c in pulse.card_list():
        props = c.proplist
        for key in ("device.description", "device.alias"):
            if DEVICE_MATCH.lower() in props.get(key, "").lower():
                return c
    return None


def get_node_serial(node_name):
    result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5)
    for node in json.loads(result.stdout):
        props = node.get("info", {}).get("props", {})
        if props.get("node.name") == node_name:
            serial = props.get("object.serial")
            if serial is not None:
                return str(serial)
    return None


def cleanup(pulse, card, original_profile, original_volume):
    pulse.card_profile_set(card, original_profile)
    time.sleep(1)
    subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "true"],
        capture_output=True,
    )
    if original_volume is not None:
        for sink in pulse.sink_list():
            if DEVICE_MATCH.lower() in sink.description.lower():
                vol = sink.volume
                vol.value_flat = original_volume
                pulse.sink_volume_set(sink.index, vol)
                break
    pulse.close()
    print("Cleaned up.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
