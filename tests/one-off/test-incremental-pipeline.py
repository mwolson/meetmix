#!/usr/bin/env python3
"""Incrementally add pipeline components to find what blocks BT HFP output.

Usage: uv run python3 tests/one-off/test-incremental-pipeline.py

Plays a bell after each addition to isolate which component kills audio:
  Step 1: HFP + null sink + forwarding loopback (known working)
  Step 2: Add capture null sink
  Step 3: Add capture loopback (pw-loopback: combined → capture)
  Step 4: Add mic loopback (module-loopback: BT source → capture)
"""

import json
import subprocess
import sys
import time

import pulsectl

DEVICE_MATCH = "AirPods"
NULL_SINK_NAME = "meetmix_combined"
CAPTURE_SINK_NAME = "meetmix_capture"
BELL = "/usr/share/sounds/freedesktop/stereo/bell.oga"


def main():
    pulse = pulsectl.Pulse("test-incremental")

    subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "false"],
        capture_output=True,
    )
    print("Disabled WirePlumber BT autoswitch")

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
        print("No BT sink found after HFP switch")
        cleanup(pulse, card, original_profile, original_volume)
        return 1

    mic_source = None
    for source in pulse.source_list():
        if source.monitor_of_sink != 0xFFFFFFFF:
            continue
        if DEVICE_MATCH.lower() in source.description.lower():
            mic_source = source.name
            break

    print(f"HFP sink: {bt_sink.name}")
    print(f"HFP mic: {mic_source}")

    if original_volume is not None:
        vol = bt_sink.volume
        vol.value_flat = original_volume
        pulse.sink_volume_set(bt_sink.index, vol)

    serial = get_node_serial(bt_sink.name)
    print(f"BT serial: {serial}")

    processes = []
    modules = []

    try:
        # === STEP 1: Base setup (known working from test-forwarding-loopback.py) ===
        print("\n=== STEP 1: Combined sink + forwarding loopback ===")
        combined_idx = pulse.module_load(
            "module-null-sink",
            f"sink_name={NULL_SINK_NAME} sink_properties=device.description=MeetMixCombined",
        )
        modules.append(combined_idx)
        print(f"  Created {NULL_SINK_NAME} (module {combined_idx})")

        fwd_loopback = subprocess.Popen([
            "pw-loopback",
            "-C", NULL_SINK_NAME,
            "--capture-props", "stream.capture.sink=true",
            "--playback-props", f"target.object={serial} media.role=Communication",
        ])
        processes.append(fwd_loopback)
        print(f"  Forwarding loopback (pid {fwd_loopback.pid})")

        time.sleep(1.5)
        play_bell("Step 1")

        # === STEP 2: Add capture null sink ===
        print("\n=== STEP 2: + Capture sink ===")
        capture_idx = pulse.module_load(
            "module-null-sink",
            f"sink_name={CAPTURE_SINK_NAME} sink_properties=device.description=MeetMixCapture",
        )
        modules.append(capture_idx)
        print(f"  Created {CAPTURE_SINK_NAME} (module {capture_idx})")

        time.sleep(0.5)
        play_bell("Step 2")

        # === STEP 3: Add capture loopback ===
        print("\n=== STEP 3: + Capture loopback (combined → capture) ===")
        capture_loopback = subprocess.Popen([
            "pw-loopback",
            "-C", NULL_SINK_NAME,
            "--capture-props", "stream.capture.sink=true",
            "-P", CAPTURE_SINK_NAME,
        ])
        processes.append(capture_loopback)
        print(f"  Capture loopback (pid {capture_loopback.pid})")

        time.sleep(1)
        play_bell("Step 3")

        # === STEP 4: Add mic loopback ===
        print("\n=== STEP 4: + Mic loopback (BT source → capture) ===")
        if mic_source:
            mic_idx = pulse.module_load(
                "module-loopback",
                f"source={mic_source} sink={CAPTURE_SINK_NAME} latency_msec=1"
                " source_output_properties=media.role=Communication",
            )
            modules.append(mic_idx)
            print(f"  Mic loopback (module {mic_idx})")
        else:
            print("  Skipped (no mic source)")

        time.sleep(1)
        play_bell("Step 4")

        # === STEP 5: Set default sink (mimics meetmix) ===
        print("\n=== STEP 5: + Set default sink to combined ===")
        pulse.sink_default_set(NULL_SINK_NAME)
        print(f"  Default sink: {NULL_SINK_NAME}")

        time.sleep(0.5)
        play_bell("Step 5")

        print("\n--- Waiting 5s for you to assess ---")
        time.sleep(5)

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

    print("\nReport which step(s) you heard the bell.")
    return 0


def play_bell(label):
    print(f"  Playing bell ({label})...")
    subprocess.run(
        ["pw-play", f"--target={NULL_SINK_NAME}", BELL],
        timeout=10,
    )
    time.sleep(2)


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
    print(f"Restored profile: {original_profile}")
    time.sleep(1)
    subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "true"],
        capture_output=True,
    )
    print("Restored WirePlumber BT autoswitch")
    if original_volume is not None:
        for sink in pulse.sink_list():
            if DEVICE_MATCH.lower() in sink.description.lower():
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
        print("\nInterrupted")
        sys.exit(130)
