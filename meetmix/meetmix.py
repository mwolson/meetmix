#!/usr/bin/env python3

import argparse
import atexit
import datetime
import os
import re
import shutil
import signal
import subprocess
import sys

import pulsectl

VERSION = "0.1.1"

ALLOWED_CONF_FLAGS = {"--device-match"}

CONF_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "meetmix.conf",
)

MODULE_PREFIX = "meetmix_"
NULL_SINK_NAME = "meetmix_combined"
NULL_SINK_DESCRIPTION = "MeetMix Combined"


def main():
    file_args = load_conf()
    args = parse_args(file_args)

    command = getattr(args, "command", None)
    if command == "devices":
        run_devices(args)
    elif command == "cleanup":
        run_cleanup()
    else:
        run_record(args)


def run_record(args):
    require_commands(["minutes", "pw-record"])

    device_match = args.device_match
    if not device_match:
        warn("Error: No --device-match specified.")
        warn("Run 'meetmix devices' to list available audio devices,")
        warn("then set --device-match in ~/.config/meetmix.conf or on the command line.")
        sys.exit(1)

    pulse = pulsectl.Pulse("meetmix")

    cleanup_orphans(pulse)

    mic_source = find_mic_source(pulse, device_match)
    speaker_monitor = find_speaker_monitor(pulse, device_match)

    log(f"Mic source: {mic_source}")
    log(f"Speaker monitor: {speaker_monitor}")

    module_indices = create_virtual_devices(pulse, mic_source, speaker_monitor)

    monitor_source = f"{NULL_SINK_NAME}.monitor"
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    wav_path = os.path.join(
        os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
        f"meetmix-{timestamp}.wav",
    )

    setup_cleanup(module_indices, wav_path)

    record_cmd = [
        "pw-record",
        f"--target={monitor_source}",
        wav_path,
    ]
    log(f"Recording to: {wav_path}")
    log(f"Capturing from: {monitor_source}")
    log("Press Ctrl-C to stop recording and process with minutes.")

    prev_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        subprocess.run(record_cmd, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGINT, prev_handler)

    if not os.path.exists(wav_path):
        warn("Error: No recording file produced.")
        sys.exit(1)

    file_size = os.path.getsize(wav_path)
    if file_size < 1000:
        warn(f"Warning: Recording file is very small ({file_size} bytes).")

    log(f"Recording saved ({file_size} bytes). Processing with minutes...")
    minutes_cmd = ["minutes", "process", "--content-type", "meeting", wav_path]
    minutes_cmd.extend(args.extra_args)
    log(f"Running: {' '.join(minutes_cmd)}")

    result = subprocess.run(minutes_cmd)

    if result.returncode == 0:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

    sys.exit(result.returncode)


def run_devices(args):
    pulse = pulsectl.Pulse("meetmix")
    device_match = args.device_match

    print("Sources (microphones):")
    for source in pulse.source_list():
        if source.monitor_of_sink != 0xFFFFFFFF:
            continue
        marker = ""
        if device_match and matches_device(source, device_match):
            marker = " *"
        print(f"  {source.name}  ({source.description}){marker}")

    print()
    print("Sinks (speakers):")
    for sink in pulse.sink_list():
        marker = ""
        if device_match and matches_device(sink, device_match):
            marker = " *"
        print(f"  {sink.name}  ({sink.description})")
        print(f"    monitor: {sink.monitor_source_name}{marker}")

    pulse.close()


def run_cleanup():
    pulse = pulsectl.Pulse("meetmix")
    count = cleanup_orphans(pulse)
    if count == 0:
        log("No orphaned meetmix modules found.")
    else:
        log(f"Cleaned up {count} orphaned module(s).")
    pulse.close()


def find_mic_source(pulse, device_match):
    candidates = []
    for source in pulse.source_list():
        if source.monitor_of_sink != 0xFFFFFFFF:
            continue
        if matches_device(source, device_match):
            candidates.append(source)

    if not candidates:
        warn(f"Error: No microphone source matching '{device_match}' found.")
        warn("Available sources:")
        for source in pulse.source_list():
            if source.monitor_of_sink != 0xFFFFFFFF:
                continue
            warn(f"  {source.name}  ({source.description})")
        sys.exit(1)

    if len(candidates) > 1:
        warn(f"Error: Multiple microphone sources match '{device_match}':")
        for source in candidates:
            warn(f"  {source.name}  ({source.description})")
        warn("Use a more specific --device-match value.")
        sys.exit(1)

    return candidates[0].name


def find_speaker_monitor(pulse, device_match):
    candidates = []
    for sink in pulse.sink_list():
        if matches_device(sink, device_match):
            candidates.append(sink)

    if not candidates:
        warn(f"Error: No speaker sink matching '{device_match}' found.")
        warn("Available sinks:")
        for sink in pulse.sink_list():
            warn(f"  {sink.name}  ({sink.description})")
        sys.exit(1)

    if len(candidates) > 1:
        warn(f"Error: Multiple speaker sinks match '{device_match}':")
        for sink in candidates:
            warn(f"  {sink.name}  ({sink.description})")
        warn("Use a more specific --device-match value.")
        sys.exit(1)

    return candidates[0].monitor_source_name


def matches_device(device, pattern):
    pattern_lower = pattern.lower()
    if pattern_lower in device.name.lower():
        return True
    if pattern_lower in device.description.lower():
        return True
    return False


def create_virtual_devices(pulse, mic_source, speaker_monitor):
    module_indices = []

    idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={NULL_SINK_NAME}"
        f" sink_properties=device.description={NULL_SINK_DESCRIPTION}",
    )
    module_indices.append(idx)
    log(f"Created null sink: {NULL_SINK_NAME} (module {idx})")

    idx = pulse.module_load(
        "module-loopback",
        f"source={mic_source} sink={NULL_SINK_NAME} latency_msec=1",
    )
    module_indices.append(idx)
    log(f"Loopback mic -> combined (module {idx})")

    idx = pulse.module_load(
        "module-loopback",
        f"source={speaker_monitor} sink={NULL_SINK_NAME} latency_msec=1",
    )
    module_indices.append(idx)
    log(f"Loopback speaker -> combined (module {idx})")

    return module_indices


def cleanup_modules(module_indices):
    if not module_indices:
        return
    try:
        pulse = pulsectl.Pulse("meetmix-cleanup")
    except Exception:
        return
    try:
        for idx in reversed(module_indices):
            try:
                pulse.module_unload(idx)
            except pulsectl.PulseError:
                pass
    finally:
        pulse.close()
    module_indices.clear()


def cleanup_orphans(pulse):
    count = 0
    for module in pulse.module_list():
        if module.argument and MODULE_PREFIX in module.argument:
            try:
                pulse.module_unload(module.index)
                log(f"Unloaded orphaned module: {module.name} (index {module.index})")
                count += 1
            except pulsectl.PulseError:
                pass
    return count


_cleanup_indices = []
_cleanup_wav_path = None


def setup_cleanup(module_indices, wav_path=None):
    global _cleanup_indices, _cleanup_wav_path
    _cleanup_indices = module_indices
    _cleanup_wav_path = wav_path
    atexit.register(_atexit_cleanup)
    signal.signal(signal.SIGINT, _signal_cleanup)
    signal.signal(signal.SIGTERM, _signal_cleanup)


def _atexit_cleanup():
    cleanup_modules(_cleanup_indices)


def _signal_cleanup(signum, _frame):
    cleanup_modules(_cleanup_indices)
    if signum == signal.SIGTERM and _cleanup_wav_path:
        try:
            os.unlink(_cleanup_wav_path)
        except OSError:
            pass
        sys.exit(128 + signum)


def parse_args(file_args):
    parser = argparse.ArgumentParser(
        prog="meetmix",
        description=(
            "Combine mic + speaker monitor into a single PipeWire source "
            "for minutes record. With no command, runs record."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--device-match",
        help="substring to match Bluetooth device name or description (e.g. 'AirPods')",
    )

    sub = parser.add_subparsers(dest="command", title="commands")
    sub.add_parser("cleanup", help="remove orphaned meetmix PipeWire modules")
    sub.add_parser("devices", help="list audio sources and sinks")
    sub.add_parser(
        "record",
        help="create virtual devices and run minutes record (default)",
    )

    args, extra = parser.parse_known_args()

    # Strip leading '--' separator if present
    if extra and extra[0] == "--":
        extra = extra[1:]

    args.extra_args = extra if args.command in (None, "record") else []

    args.cli_device_match = args.device_match
    if not args.device_match and "device_match" in file_args:
        args.device_match = file_args["device_match"]

    return args


def load_conf():
    if not os.path.exists(CONF_PATH):
        return {}

    result = {}
    for flag, value in iter_conf_entries(CONF_PATH):
        if flag not in ALLOWED_CONF_FLAGS:
            warn(f"Error: Unsupported flag '{flag}' in {CONF_PATH}")
            sys.exit(1)
        if flag == "--device-match":
            result["device_match"] = value
    return result


def iter_conf_entries(path):
    with open(path) as f:
        for line_num, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^(--[a-z][a-z0-9-]*)=(.+)$", line)
            if not match:
                warn(f"Error: Malformed line {line_num} in {path}: {line}")
                sys.exit(1)
            yield match.group(1), match.group(2)


def require_commands(commands):
    missing = [command for command in commands if not shutil.which(command)]
    if missing:
        for command in missing:
            warn(f"Error: '{command}' is required but not found in PATH.")
        sys.exit(1)


def log(message):
    print(message, flush=True)


def warn(message):
    print(message, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
