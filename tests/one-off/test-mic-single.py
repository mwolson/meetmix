#!/usr/bin/env python3
"""
One-off diagnostic: single continuous recording from BT mic via module-loopback.

Matches the real meetmix architecture as closely as possible:
- Creates null sink, module-loopback from mic, pw-record captures from null sink
- Single pw-record session (no graph disruption)
- Tests at 100% volume
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
)
import pulsectl

DEVICE_MATCH = "AirPods"
NULL_SINK = "meetmix_mic_single"
RECORD_SECONDS = 8


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
        print(f"  Error: {e}")
        return 0, 0, 0


def main():
    pulse = pulsectl.Pulse("meetmix-mic-single")

    card = find_bt_card(pulse, DEVICE_MATCH)
    if card is None:
        print(f"No card matching '{DEVICE_MATCH}' found")
        pulse.close()
        return

    original_profile = card.profile_active.name

    if not original_profile.startswith("headset-head-unit"):
        print("Switching to HFP...")
        ensure_headset_profile(pulse, DEVICE_MATCH)
        time.sleep(0.5)

    mic_source = find_mic_source(pulse, DEVICE_MATCH)
    print(f"Mic source: {mic_source}")

    # Unmute and set to 100%
    for source in pulse.source_list():
        if source.name == mic_source:
            if source.mute:
                pulse.source_mute(source.index, False)
                print("Unmuted source")
            v = source.volume
            v.value_flat = 1.0
            pulse.source_volume_set(source.index, v)
            print("Source volume: 100%")
            break

    # Create null sink and module-loopback
    test_idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={NULL_SINK} sink_properties=device.description=MicSingle",
    )
    lb_idx = pulse.module_load(
        "module-loopback",
        f"source={mic_source} sink={NULL_SINK} latency_msec=1",
    )
    print("Created null sink and loopback")
    time.sleep(1)  # Let graph settle

    tmpdir = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    wav_path = os.path.join(tmpdir, "mic-single-100pct.wav")

    print(f"\nSpeak into your mic for {RECORD_SECONDS} seconds...")
    proc = subprocess.Popen(
        ["pw-record", f"--target={NULL_SINK}",
         "-P", "stream.capture.sink=true", wav_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(RECORD_SECONDS)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    dur, peak, rms = analyze_wav(wav_path)
    print(f"\nResult: {dur:.1f}s, peak={peak}, rms={rms:.0f}")
    if peak < 100:
        print("SILENT - module-loopback not carrying mic audio")
    elif peak < 1000:
        print(f"VERY QUIET - mic captured but level is low ({peak}/32768 = {peak/32768*100:.1f}%)")
    else:
        print(f"GOOD - mic audio captured (peak {peak}/32768 = {peak/32768*100:.1f}%)")

    # Cleanup
    print("\nCleaning up...")
    try:
        pulse.module_unload(lb_idx)
    except Exception:
        pass
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
