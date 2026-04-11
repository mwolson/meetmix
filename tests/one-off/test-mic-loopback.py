#!/usr/bin/env python3
"""
One-off diagnostic: does module-loopback carry BT mic audio to a null sink?

Compares two approaches:
  A) PulseAudio module-loopback: source=BT_mic sink=null_sink
  B) PipeWire-native pw-loopback: --capture-props node.target=BT_mic
     --playback-props node.target=null_sink

For each approach, records 5 seconds from the null sink with pw-record
and reports peak/RMS amplitude.

Steps:
1. Switch to HFP, create a test null sink
2. Test A: module-loopback mic -> null sink, record 5s, report amplitude
3. Test B: pw-loopback mic -> null sink, record 5s, report amplitude
4. Restore original state
"""

import os
import struct
import subprocess
import sys
import time
import wave
sys.path.insert(0, ".")
from meetmix.meetmix import (
    find_bt_card, find_mic_source, ensure_headset_profile,
    prepare_source,
)
import pulsectl

DEVICE_MATCH = "AirPods"
NULL_SINK = "meetmix_mic_test"
RECORD_SECONDS = 5


def analyze_wav(path):
    try:
        with wave.open(path, "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            width = w.getsampwidth()
            duration = frames / rate if rate else 0
            raw = w.readframes(frames)
            if width == 2 and raw:
                samples = struct.unpack(f"<{len(raw) // 2}h", raw)
                peak = max(abs(s) for s in samples) if samples else 0
                rms = (sum(s * s for s in samples) / len(samples)) ** 0.5 if samples else 0
                return duration, peak, rms
            return duration, 0, 0
    except Exception as e:
        print(f"  Error analyzing: {e}")
        return 0, 0, 0


def record_from_sink(sink_name, wav_path, seconds):
    proc = subprocess.Popen(
        ["pw-record", f"--target={sink_name}",
         "-P", "stream.capture.sink=true", wav_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(seconds)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return proc.returncode


def main():
    pulse = pulsectl.Pulse("meetmix-mic-test")

    card = find_bt_card(pulse, DEVICE_MATCH)
    if card is None:
        print(f"No card matching '{DEVICE_MATCH}' found")
        pulse.close()
        return

    original_profile = card.profile_active.name
    print(f"Original card profile: {original_profile}")

    # Switch to HFP if needed
    if not original_profile.startswith("headset-head-unit"):
        print("Switching to HFP...")
        ensure_headset_profile(pulse, DEVICE_MATCH)
        time.sleep(0.5)

    mic_source = find_mic_source(pulse, DEVICE_MATCH)
    print(f"Mic source: {mic_source}")
    prepare_source(pulse, mic_source)

    # Create test null sink
    test_idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={NULL_SINK} sink_properties=device.description=MicTest",
    )
    print(f"Created test sink: {NULL_SINK} (module {test_idx})")

    tmpdir = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
    os.makedirs(tmpdir, exist_ok=True)

    # --- Test A: module-loopback ---
    print(f"\n=== Test A: module-loopback (speak into mic for {RECORD_SECONDS}s) ===")
    lb_idx = pulse.module_load(
        "module-loopback",
        f"source={mic_source} sink={NULL_SINK} latency_msec=1",
    )
    print(f"Created loopback (module {lb_idx})")
    time.sleep(0.5)

    wav_a = os.path.join(tmpdir, "mic-test-module-loopback.wav")
    print(f"Recording {RECORD_SECONDS}s to {wav_a}...")
    record_from_sink(NULL_SINK, wav_a, RECORD_SECONDS)

    pulse.module_unload(lb_idx)

    dur, peak, rms = analyze_wav(wav_a)
    print(f"  Duration: {dur:.1f}s, Peak: {peak}, RMS: {rms:.0f}")
    if peak < 100:
        print("  RESULT: SILENT (module-loopback NOT carrying mic audio)")
    else:
        print("  RESULT: AUDIO DETECTED (module-loopback works)")

    # --- Test B: pw-loopback ---
    print(f"\n=== Test B: pw-loopback (speak into mic for {RECORD_SECONDS}s) ===")
    pw_lb = subprocess.Popen(
        [
            "pw-loopback",
            "--capture-props", f"node.target={mic_source}",
            "--playback-props", f"node.target={NULL_SINK}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"Started pw-loopback (pid {pw_lb.pid})")
    time.sleep(0.5)

    wav_b = os.path.join(tmpdir, "mic-test-pw-loopback.wav")
    print(f"Recording {RECORD_SECONDS}s to {wav_b}...")
    record_from_sink(NULL_SINK, wav_b, RECORD_SECONDS)

    pw_lb.terminate()
    try:
        pw_lb.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pw_lb.kill()

    dur, peak, rms = analyze_wav(wav_b)
    print(f"  Duration: {dur:.1f}s, Peak: {peak}, RMS: {rms:.0f}")
    if peak < 100:
        print("  RESULT: SILENT (pw-loopback NOT carrying mic audio)")
    else:
        print("  RESULT: AUDIO DETECTED (pw-loopback works)")

    # Cleanup
    print("\nCleaning up...")
    try:
        pulse.module_unload(test_idx)
    except Exception:
        pass

    if not original_profile.startswith("headset-head-unit"):
        card = find_bt_card(pulse, DEVICE_MATCH)
        if card:
            pulse.card_profile_set(card, original_profile)
            print(f"Restored profile: {original_profile}")

    pulse.close()
    print("Done.")


if __name__ == "__main__":
    main()
