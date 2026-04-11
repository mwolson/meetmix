#!/usr/bin/env python3
"""
One-off test: compare the A2DP stub source vs the real HFP source.

The A2DP stub `bluez_input.*` exists in A2DP mode but produces silence.
This test captures its properties, switches to HFP, captures the real
source properties, and reports differences. The goal is to find a
reliable way to distinguish them so `wait_for_source()` doesn't return
the stub.

Steps:
1. In A2DP mode, find the stub source and dump its properties
2. Switch to HFP
3. Find the HFP source and dump its properties
4. Compare and report differences
5. Restore original state
"""

import sys
import time
sys.path.insert(0, ".")
from meetmix.meetmix import (
    find_bt_card, matches_device, wait_for_source, HEADSET_PROFILES,
)
import pulsectl

DEVICE_MATCH = "AirPods"


def dump_source(source, label):
    print(f"\n=== {label} ===")
    print(f"  name: {source.name}")
    print(f"  description: {source.description}")
    print(f"  index: {source.index}")
    print(f"  monitor_of_sink: {source.monitor_of_sink}")
    print(f"  mute: {source.mute}")
    print(f"  volume: {source.volume.values}")
    print(f"  channel_count: {source.channel_count}")
    print(f"  proplist keys: {sorted(source.proplist.keys())}")
    for key in sorted(source.proplist.keys()):
        print(f"    {key} = {source.proplist[key]}")


def find_matching_source(pulse, device_match):
    for source in pulse.source_list():
        if source.monitor_of_sink != 0xFFFFFFFF:
            continue
        if matches_device(source, device_match):
            return source
    return None


def main():
    pulse = pulsectl.Pulse("meetmix-stub-test")

    card = find_bt_card(pulse, DEVICE_MATCH)
    if card is None:
        print(f"No card matching '{DEVICE_MATCH}' found")
        pulse.close()
        return

    original_profile = card.profile_active.name
    print(f"Current card profile: {original_profile}")

    if original_profile.startswith("headset-head-unit"):
        print("Already in HFP mode. Switch to A2DP first, then re-run.")
        pulse.close()
        return

    # Phase 1: capture A2DP stub source properties
    stub = find_matching_source(pulse, DEVICE_MATCH)
    if stub is None:
        print("No source matching device in A2DP mode (no stub found)")
    else:
        dump_source(stub, "A2DP STUB SOURCE")

    # Phase 2: switch to HFP and capture real source
    available = {p.name for p in card.profile_list if p.available != 0}
    target_profile = None
    for profile in HEADSET_PROFILES:
        if profile in available:
            target_profile = profile
            break

    if not target_profile:
        print("No HFP profile available")
        pulse.close()
        return

    print(f"\nSwitching to HFP profile: {target_profile}")
    pulse.card_profile_set(card, target_profile)

    hfp_source_name = wait_for_source(pulse, DEVICE_MATCH)
    if hfp_source_name is None:
        print("HFP source did not appear!")
        pulse.card_profile_set(card, original_profile)
        pulse.close()
        return

    hfp = find_matching_source(pulse, DEVICE_MATCH)
    if hfp:
        dump_source(hfp, "HFP REAL SOURCE")

    # Phase 3: compare
    if stub and hfp:
        print("\n=== COMPARISON ===")
        print(f"  Name changed: {stub.name} -> {hfp.name}")
        print(f"  Same name: {stub.name == hfp.name}")
        print(f"  Same index: {stub.index == hfp.index}")
        print(f"  Description changed: {stub.description} -> {hfp.description}")

        stub_keys = set(stub.proplist.keys())
        hfp_keys = set(hfp.proplist.keys())
        added = hfp_keys - stub_keys
        removed = stub_keys - hfp_keys
        common = stub_keys & hfp_keys
        changed = {k for k in common if stub.proplist[k] != hfp.proplist[k]}

        if added:
            print(f"\n  Props only in HFP: {sorted(added)}")
            for k in sorted(added):
                print(f"    {k} = {hfp.proplist[k]}")
        if removed:
            print(f"\n  Props only in A2DP stub: {sorted(removed)}")
            for k in sorted(removed):
                print(f"    {k} = {stub.proplist[k]}")
        if changed:
            print("\n  Props that differ:")
            for k in sorted(changed):
                print(f"    {k}: {stub.proplist[k]} -> {hfp.proplist[k]}")

    # Phase 4: restore
    print(f"\nRestoring card profile to: {original_profile}")
    pulse.card_profile_set(card, original_profile)
    time.sleep(1)

    print("Done.")
    pulse.close()


if __name__ == "__main__":
    main()
