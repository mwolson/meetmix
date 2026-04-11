#!/usr/bin/env python3
"""
One-off test: does WirePlumber auto-restore A2DP after we drop HFP?

Steps:
1. Save current default sink and card profile
2. Switch AirPods to HFP
3. Set default sink to the HFP sink (option 3 approach)
4. Wait and poll: does WirePlumber switch back to A2DP?
5. Report what happened to the default sink
6. Restore original state if needed
"""

import sys
import time
sys.path.insert(0, ".")
from meetmix.meetmix import (
    find_bt_card, matches_device, wait_for_sink, HEADSET_PROFILES,
)
import pulsectl

DEVICE_MATCH = "AirPods"
POLL_INTERVAL = 1
MAX_WAIT = 30


def main():
    pulse = pulsectl.Pulse("meetmix-a2dp-test")

    # Save original state
    original_default = pulse.server_info().default_sink_name
    card = find_bt_card(pulse, DEVICE_MATCH)
    if card is None:
        print(f"No card matching '{DEVICE_MATCH}' found")
        pulse.close()
        return

    original_profile = card.profile_active.name
    print(f"Original default sink: {original_default}")
    print(f"Original card profile: {original_profile}")
    print()

    if original_profile.startswith("headset-head-unit"):
        print("Already in HFP mode. Switch to A2DP first, then re-run.")
        pulse.close()
        return

    # Switch to HFP
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

    print(f"Switching to HFP profile: {target_profile}")
    pulse.card_profile_set(card, target_profile)

    # Wait for HFP sink
    hfp_sink = wait_for_sink(pulse, DEVICE_MATCH)
    if hfp_sink is None:
        print("HFP sink did not appear!")
        # Try to restore
        pulse.card_profile_set(card, original_profile)
        pulse.close()
        return

    print(f"HFP sink appeared: {hfp_sink}")

    # Option 3: set default to HFP sink
    print(f"Setting default sink to HFP sink: {hfp_sink}")
    pulse.sink_default_set(hfp_sink)
    current_default = pulse.server_info().default_sink_name
    print(f"Default is now: {current_default}")
    print()

    # Now simulate meetmix cleanup: switch card back to original profile
    # (This is what WirePlumber would do automatically, but let's be explicit
    # since we have no loopbacks to trigger the auto-switch)
    print(f"Restoring card profile to: {original_profile}")
    pulse.card_profile_set(card, original_profile)

    # Poll: what happens to the default sink?
    print(f"\nPolling for default sink changes (up to {MAX_WAIT}s)...")
    start = time.monotonic()
    last_default = current_default
    while time.monotonic() - start < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        try:
            current = pulse.server_info().default_sink_name
        except Exception:
            print(f"  {time.monotonic() - start:.0f}s: pulse error, reconnecting...")
            pulse.close()
            pulse = pulsectl.Pulse("meetmix-a2dp-test")
            continue

        if current != last_default:
            print(f"  {time.monotonic() - start:.0f}s: default changed: {last_default} -> {current}")
            last_default = current

            # Check if it's the original or an A2DP sink
            if current == original_default:
                print(f"\n  DEFAULT RESTORED to original: {current}")
                break
            for sink in pulse.sink_list():
                if sink.name == current and matches_device(sink, DEVICE_MATCH):
                    print(f"\n  DEFAULT is AirPods A2DP sink: {current}")
                    break
        else:
            elapsed = time.monotonic() - start
            if elapsed % 5 < POLL_INTERVAL:
                print(f"  {elapsed:.0f}s: still {current}")

    print()
    final_default = pulse.server_info().default_sink_name
    print(f"Final default sink: {final_default}")
    print(f"Original was:       {original_default}")
    print(f"Match: {final_default == original_default}")

    # Check card profile
    card = find_bt_card(pulse, DEVICE_MATCH)
    if card:
        print(f"Final card profile: {card.profile_active.name}")

    pulse.close()


if __name__ == "__main__":
    main()
