#!/usr/bin/env python3
"""Diagnose BT HFP audio output failures.

Usage: uv run python3 tests/one-off/test-hfp-audio-diagnostic.py

Runs a sequence of checks to identify where BT HFP audio output fails:
  1. A2DP baseline (confirms BT connection and speaker work)
  2. HFP direct output (confirms SCO transport activates)
  3. HFP with forwarding loopback (confirms pw-loopback routing)
  4. Full pipeline with warmup (confirms the complete meetmix path)

Each step reports PASS/FAIL based on user feedback (pauses for confirmation).
Common failure modes:
  - Step 2 fails: BT HFP SCO transport broken (reconnect AirPods or restart PipeWire)
  - Step 3 fails: Forwarding loopback not linking (check media.role, target.object)
  - Step 4 fails: SCO warmup or ordering issue, or bluetooth-headset-manager interfering
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
    pulse = pulsectl.Pulse("hfp-diagnostic")

    card = find_bt_card(pulse)
    if not card:
        print("ERROR: No BT card matching '%s' found" % DEVICE_MATCH)
        return 1

    original_profile = card.profile_active.name
    original_default = pulse.server_info().default_sink_name
    original_volume = None
    for sink in pulse.sink_list():
        if DEVICE_MATCH.lower() in sink.description.lower():
            original_volume = sink.volume.value_flat
            break

    print(f"Card: {card.name}")
    print(f"Profile: {original_profile}")
    print(f"Default: {original_default}")
    print(f"Volume: {original_volume:.0%}" if original_volume else "Volume: unknown")

    # Check if bluetooth-headset-manager is backing off
    autoswitch = subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile"],
        capture_output=True, text=True, timeout=5,
    )
    print(f"WP autoswitch: {autoswitch.stdout.strip()}")

    results = []

    # === STEP 1: A2DP baseline ===
    print("\n" + "=" * 60)
    print("STEP 1: A2DP baseline")
    print("Playing bell through current A2DP connection...")
    pulse.sink_default_set(original_default)
    subprocess.run(["pw-play", BELL], timeout=10)
    time.sleep(1)
    answer = input("  Did you hear the bell in AirPods? [y/n]: ").strip().lower()
    results.append(("A2DP baseline", answer.startswith("y")))
    if not answer.startswith("y"):
        print("  FAIL: A2DP not working. Check BT connection.")
        print_results(results)
        pulse.close()
        return 1

    # === STEP 2: HFP direct output ===
    print("\n" + "=" * 60)
    print("STEP 2: HFP direct output")
    subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "false"],
        capture_output=True,
    )
    pulse.card_profile_set(card, "headset-head-unit")
    time.sleep(2)

    bt_sink = None
    for sink in pulse.sink_list():
        if DEVICE_MATCH.lower() in sink.description.lower():
            bt_sink = sink
            break
    if not bt_sink:
        print("  ERROR: No BT sink after HFP switch")
        restore(pulse, card, original_profile, original_default, original_volume)
        return 1

    if original_volume:
        vol = bt_sink.volume
        vol.value_flat = original_volume
        pulse.sink_volume_set(bt_sink.index, vol)

    serial = get_node_serial(bt_sink.name)
    print(f"  HFP sink: {bt_sink.name} (serial {serial})")
    print("  Playing bell directly to HFP sink...")
    subprocess.run(["pw-play", f"--target={serial}", BELL], timeout=10)
    time.sleep(1)
    answer = input("  Did you hear the bell? [y/n]: ").strip().lower()
    results.append(("HFP direct", answer.startswith("y")))
    if not answer.startswith("y"):
        print("  FAIL: HFP SCO output not working.")
        print("  Try: reconnect AirPods, restart PipeWire, or reboot.")
        restore(pulse, card, original_profile, original_default, original_volume)
        print_results(results)
        return 1

    # === STEP 3: Forwarding loopback ===
    print("\n" + "=" * 60)
    print("STEP 3: Forwarding loopback")
    combined_idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={NULL_SINK_NAME} sink_properties=device.description=MeetMixCombined",
    )
    fwd = subprocess.Popen([
        "pw-loopback", "-C", NULL_SINK_NAME,
        "--capture-props", "stream.capture.sink=true",
        "--playback-props", f"target.object={serial} media.role=Communication",
    ])
    time.sleep(2)

    linked = check_fwd_linked(fwd.pid, bt_sink.name)
    print(f"  Link state: {'LINKED' if linked else 'NOT LINKED'}")
    print("  Playing bell through forwarding...")
    subprocess.run(["pw-play", f"--target={NULL_SINK_NAME}", BELL], timeout=10)
    time.sleep(1)
    answer = input("  Did you hear the bell? [y/n]: ").strip().lower()
    results.append(("Forwarding loopback", answer.startswith("y")))

    fwd.terminate()
    fwd.wait(timeout=2)
    pulse.module_unload(combined_idx)

    if not answer.startswith("y"):
        print("  FAIL: Forwarding loopback not delivering audio.")
        if not linked:
            print("  Check: media.role=Communication, target.object serial")
        else:
            print("  Linked but no audio: possible SCO warmup issue")
        restore(pulse, card, original_profile, original_default, original_volume)
        print_results(results)
        return 1

    # === STEP 4: Full pipeline with warmup ===
    print("\n" + "=" * 60)
    print("STEP 4: Full pipeline (forwarding + capture + mic + warmup)")

    mic_source = None
    for source in pulse.source_list():
        if source.monitor_of_sink != 0xFFFFFFFF:
            continue
        if DEVICE_MATCH.lower() in source.description.lower():
            mic_source = source.name
            break

    combined_idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={NULL_SINK_NAME} sink_properties=device.description=MeetMixCombined",
    )
    capture_idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={CAPTURE_SINK_NAME} sink_properties=device.description=MeetMixCapture",
    )
    mic_idx = None
    if mic_source:
        mic_idx = pulse.module_load(
            "module-loopback",
            f"source={mic_source} sink={CAPTURE_SINK_NAME} latency_msec=1"
            " source_output_properties=media.role=Communication",
        )

    # Forwarding FIRST
    fwd = subprocess.Popen([
        "pw-loopback", "-C", NULL_SINK_NAME,
        "--capture-props", "stream.capture.sink=true",
        "--playback-props", f"target.object={serial} media.role=Communication",
    ])
    time.sleep(2)

    # SCO warmup
    print("  Warming up SCO (3s)...")
    warmup = subprocess.Popen(
        ["pw-play", f"--target={NULL_SINK_NAME}", "--format=s16",
         "--rate=48000", "--channels=2", "/dev/zero"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    warmup.terminate()
    warmup.wait(timeout=2)

    # Capture loopback SECOND
    cap = subprocess.Popen([
        "pw-loopback", "-C", NULL_SINK_NAME,
        "--capture-props", "stream.capture.sink=true",
        "-P", CAPTURE_SINK_NAME,
    ])
    pulse.sink_default_set(NULL_SINK_NAME)
    time.sleep(1)

    print("  Playing bell through full pipeline...")
    subprocess.run(["pw-play", f"--target={NULL_SINK_NAME}", BELL], timeout=10)
    time.sleep(1)
    answer = input("  Did you hear the bell? [y/n]: ").strip().lower()
    results.append(("Full pipeline", answer.startswith("y")))

    # Cleanup step 4
    fwd.terminate()
    fwd.wait(timeout=2)
    cap.terminate()
    cap.wait(timeout=2)
    if mic_idx:
        pulse.module_unload(mic_idx)
    pulse.module_unload(capture_idx)
    pulse.module_unload(combined_idx)

    if not answer.startswith("y"):
        print("  FAIL: Full pipeline not delivering audio.")
        print("  Likely: bluetooth-headset-manager interfering, or SCO warmup insufficient")

    # Final restore
    restore(pulse, card, original_profile, original_default, original_volume)
    print_results(results)
    return 0 if all(r[1] for r in results) else 1


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


def check_fwd_linked(pid, bt_sink_name):
    loopback_id = f"pw-loopback-{pid}"
    result = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=5)
    current_owner = None
    for line in result.stdout.splitlines():
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


def restore(pulse, card, original_profile, original_default, original_volume):
    pulse.card_profile_set(card, original_profile)
    time.sleep(1)
    subprocess.run(
        ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "true"],
        capture_output=True,
    )
    pulse.sink_default_set(original_default)
    if original_volume is not None:
        for sink in pulse.sink_list():
            if DEVICE_MATCH.lower() in sink.description.lower():
                vol = sink.volume
                vol.value_flat = original_volume
                pulse.sink_volume_set(sink.index, vol)
                break
    pulse.close()
    print("\nRestored original state.")


def print_results(results):
    print("\n" + "=" * 60)
    print("RESULTS:")
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted, restoring state...")
        try:
            pulse = pulsectl.Pulse("cleanup")
            card = find_bt_card(pulse)
            if card:
                pulse.card_profile_set(card, "a2dp-sink")
                time.sleep(1)
            subprocess.run(
                ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile", "true"],
                capture_output=True,
            )
            pulse.close()
        except Exception:
            pass
        sys.exit(130)
