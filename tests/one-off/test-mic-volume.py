#!/usr/bin/env python3
"""
One-off diagnostic: test mic capture at different volume levels.

Records 4 seconds at each volume level (25%, 50%, 75%, 100%) via
module-loopback from the BT HFP source and reports peak/RMS.
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
NULL_SINK = "meetmix_vol_test"
RECORD_SECONDS = 4
VOLUMES = [0.25, 0.50, 0.75, 1.0]


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


def main():
    pulse = pulsectl.Pulse("meetmix-vol-test")

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

    test_idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={NULL_SINK} sink_properties=device.description=VolTest",
    )

    lb_idx = pulse.module_load(
        "module-loopback",
        f"source={mic_source} sink={NULL_SINK} latency_msec=1",
    )
    time.sleep(0.5)

    tmpdir = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
    os.makedirs(tmpdir, exist_ok=True)

    print(f"\nSpeak steadily into your mic for {len(VOLUMES) * (RECORD_SECONDS + 1)}s total.\n")

    for vol in VOLUMES:
        # Set source volume
        for source in pulse.source_list():
            if source.name == mic_source:
                if source.mute:
                    pulse.source_mute(source.index, False)
                v = source.volume
                v.value_flat = vol
                pulse.source_volume_set(source.index, v)
                break

        wav_path = os.path.join(tmpdir, f"mic-vol-{int(vol * 100)}.wav")
        print(f"Volume {int(vol * 100)}%: recording {RECORD_SECONDS}s...", end=" ", flush=True)
        record_from_sink(NULL_SINK, wav_path, RECORD_SECONDS)
        dur, peak, rms = analyze_wav(wav_path)
        print(f"peak={peak:6d}  rms={rms:6.0f}  {'OK' if peak > 100 else 'LOW'}")
        time.sleep(0.5)

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
