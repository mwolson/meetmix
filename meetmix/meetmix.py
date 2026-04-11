#!/usr/bin/env python3

import argparse
import datetime
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave

import pulsectl

VERSION = "0.1.1"

ALLOWED_CONF_FLAGS = {"--device-match", "--language"}

CAPTURE_SINK_DESCRIPTION = "MeetMix Capture"
CAPTURE_SINK_NAME = "meetmix_capture"
CONF_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "meetmix.conf",
)
HEADSET_PROFILES = [
    "headset-head-unit",
    "headset-head-unit-msbc",
]
LOG_DIR = os.path.expanduser("~/.minutes/logs")
MIC_VOLUME = 1.0
MODULE_PREFIX = "meetmix_"
NULL_SINK_DESCRIPTION = "MeetMix Combined"
NULL_SINK_NAME = "meetmix_combined"

_log_file = None
_stop_requested = False


class Session:
    def __init__(self):
        self.bt_sink_name = None
        self.capture_loopback = None
        self.capture_target = CAPTURE_SINK_NAME
        self.device_match = None
        self.forwarding_loopback = None
        self.keep_recording = False
        self.log_path = None
        self.modules = {}
        self.original_bt_sink_volume = None
        self.original_card_profile = None
        self.original_default_sink = None
        self.record_proc = None
        self.sco_warmup_proc = None
        self.wav_path = None


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
    global _stop_requested
    _stop_requested = False

    require_commands(["minutes", "pw-link", "pw-loopback", "pw-play", "pw-record", "wpctl"])

    device_match = args.device_match
    if not device_match:
        warn("Error: No --device-match specified.")
        warn("Run 'meetmix devices' to list available audio devices,")
        warn("then set --device-match in ~/.config/meetmix.conf or on the command line.")
        sys.exit(1)

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = setup_logging(timestamp)

    recording_dir = os.path.expanduser("~/meetings/recordings")
    os.makedirs(recording_dir, exist_ok=True)

    session = Session()
    session.keep_recording = args.keep_recording
    session.log_path = log_path
    session.wav_path = os.path.join(recording_dir, f"meetmix-{timestamp}.wav")

    prev_sigint = signal.signal(signal.SIGINT, _request_stop)
    prev_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    prev_sighup = signal.signal(signal.SIGHUP, _request_stop)

    recording_ran = False
    try:
        try:
            setup_pipeline(session, device_match)
            if not _stop_requested:
                run_recording(session)
                recording_ran = True
            else:
                log("Stop requested during setup, skipping recording.")
        finally:
            cleanup_session(session)
            signal.signal(signal.SIGINT, prev_sigint)
            signal.signal(signal.SIGTERM, prev_sigterm)
            signal.signal(signal.SIGHUP, prev_sighup)

        if recording_ran:
            process_recording(session, args)
    finally:
        _close_log_file()


def setup_pipeline(session, device_match):
    try:
        pulse = pulsectl.Pulse("meetmix")
    except Exception as e:
        warn(f"Error: Cannot connect to PulseAudio/PipeWire: {e}")
        sys.exit(1)
    try:
        cleanup_orphans(pulse)

        session.device_match = device_match
        session.original_default_sink = pulse.server_info().default_sink_name
        session.original_bt_sink_volume = save_sink_volume(
            pulse, session.original_default_sink,
        )

        card = find_bt_card(pulse, device_match)
        if card:
            session.original_card_profile = card.profile_active.name

        disable_wpctl_autoswitch()

        ensure_headset_profile(pulse, device_match)

        combined_idx = create_combined_sink(pulse)
        session.modules["combined_sink"] = combined_idx
        pulse.sink_default_set(NULL_SINK_NAME)
        move_sink_inputs(pulse, NULL_SINK_NAME)
        log(f"Default sink: {NULL_SINK_NAME} (was {session.original_default_sink})")

        if _stop_requested:
            return

        mic_source = find_mic_source(pulse, device_match)
        bt_sink = find_bt_sink(pulse, device_match)
        session.bt_sink_name = bt_sink
        bt_serial = get_node_serial(bt_sink)

        log(f"Mic source: {mic_source}")
        log(f"Speaker sink: {bt_sink}")

        if session.original_bt_sink_volume is not None:
            set_sink_volume(pulse, bt_sink, session.original_bt_sink_volume)

        prepare_source(pulse, mic_source)

        create_capture_devices(pulse, mic_source, bt_sink, session.modules)

        capture_serial = get_node_serial(CAPTURE_SINK_NAME)
        if capture_serial:
            session.capture_target = capture_serial

        session.forwarding_loopback = start_forwarding_loopback(bt_serial or bt_sink)
        wait_for_forwarding_link(session, timeout=5)
        warm_up_sco(session)
        session.capture_loopback = start_capture_loopback()

        time.sleep(0.5)
        verify_forwarding_link(pulse, session)

        card = find_bt_card(pulse, device_match)
        if card:
            profile = card.profile_active.name
            if not profile.startswith("headset-head-unit"):
                warn(f"Warning: BT profile reverted to {profile} during setup")
            else:
                log(f"BT profile verified: {profile}")
    finally:
        pulse.close()


MONITOR_INTERVAL = 8  # check every 8 * 0.25s = 2 seconds


def run_recording(session):
    record_cmd = [
        "pw-record",
        f"--target={session.capture_target}",
        "-P", "stream.capture.sink=true",
        session.wav_path,
    ]
    log(f"Recording to: {session.wav_path}")
    log(f"Capturing from: {CAPTURE_SINK_NAME} (target {session.capture_target})")
    log("Resume any paused media in your browser (the profile switch may pause it).")
    log("Press Ctrl-C to stop recording and process with minutes.")

    session.record_proc = subprocess.Popen(
        record_cmd,
        stdout=_log_file or subprocess.DEVNULL,
        stderr=_log_file or subprocess.DEVNULL,
    )
    log(f"pw-record started (pid {session.record_proc.pid})")

    mon = _RecordingMonitor(session)
    try:
        tick = 0
        while session.record_proc.poll() is None:
            if _stop_requested:
                log("Stop requested, waiting for pw-record...")
                try:
                    session.record_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    stop_process(session.record_proc)
                break
            tick += 1
            if tick % MONITOR_INTERVAL == 0:
                mon.check(session)
            time.sleep(0.25)
    finally:
        mon.close()

    exit_code = session.record_proc.returncode
    log(f"pw-record exited (code {exit_code})")
    if exit_code != 0 and not _stop_requested:
        warn(f"Warning: pw-record exited unexpectedly (code {exit_code})")


class _RecordingMonitor:
    """Periodic health checks during recording."""

    def __init__(self, session):
        self._pulse = None
        self._last_profile = None
        self._last_default_sink = None
        self._loopback_warned = False
        self._fwd_warned = False
        try:
            self._pulse = pulsectl.Pulse("meetmix-monitor")
            card = find_bt_card(self._pulse, session.device_match) if session.device_match else None
            if card:
                self._last_profile = card.profile_active.name
            self._last_default_sink = self._pulse.server_info().default_sink_name
        except Exception:
            pass

    def check(self, session):
        self._check_bt_profile(session)
        self._check_capture_loopback(session)
        self._check_forwarding_loopback(session)
        self._check_default_sink()

    def close(self):
        if self._pulse:
            try:
                self._pulse.close()
            except Exception:
                pass
            self._pulse = None

    def _check_bt_profile(self, session):
        if not self._pulse or not session.device_match:
            return
        try:
            card = find_bt_card(self._pulse, session.device_match)
        except Exception:
            return
        if not card:
            return
        profile = card.profile_active.name
        if profile != self._last_profile:
            if self._last_profile is not None:
                warn(f"BT profile changed: {self._last_profile} -> {profile}")
            else:
                log(f"BT profile: {profile}")
            self._last_profile = profile

    def _check_capture_loopback(self, session):
        if self._loopback_warned:
            return
        if session.capture_loopback and session.capture_loopback.poll() is not None:
            code = session.capture_loopback.returncode
            warn(f"pw-loopback (capture) exited unexpectedly (code {code})")
            self._loopback_warned = True

    def _check_forwarding_loopback(self, session):
        if self._fwd_warned:
            return
        if session.forwarding_loopback and session.forwarding_loopback.poll() is not None:
            code = session.forwarding_loopback.returncode
            warn(f"pw-loopback (forwarding) exited unexpectedly (code {code})")
            self._fwd_warned = True

    def _check_default_sink(self):
        if not self._pulse:
            return
        try:
            default = self._pulse.server_info().default_sink_name
        except Exception:
            return
        if default != self._last_default_sink:
            warn(f"Default sink changed: {self._last_default_sink} -> {default}")
            self._last_default_sink = default

def cleanup_session(session):
    stop_process(session.record_proc)
    stop_process(session.sco_warmup_proc)
    stop_process(session.capture_loopback)
    stop_process(session.forwarding_loopback)
    restore_default_sink(session.original_default_sink)
    unload_modules(session.modules)
    restore_bt_profile(session)
    restore_wpctl_autoswitch()
    restore_sink_volume(session.original_default_sink, session.original_bt_sink_volume)


def process_recording(session, args):
    wav_path = session.wav_path

    if not os.path.exists(wav_path):
        warn("Error: No recording file produced.")
        sys.exit(1)

    fix_wav_header(wav_path)

    file_size = os.path.getsize(wav_path)
    if file_size < 1000:
        warn(f"Warning: Recording file is very small ({file_size} bytes).")

    log_wav_stats(wav_path, file_size)

    minutes_cmd = ["minutes", "process", "--content-type", "meeting", wav_path]
    if args.language:
        minutes_cmd.extend(["--language", args.language])
    minutes_cmd.extend(args.extra_args)
    log(f"Running: {' '.join(minutes_cmd)}")

    result = run_minutes(minutes_cmd)

    if result.returncode == 0 and not session.keep_recording:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
    elif result.returncode == 0:
        log(f"Recording kept: {wav_path}")
    else:
        log(f"Recording kept (minutes exit code {result.returncode}): {wav_path}")

    sys.exit(result.returncode)


def run_minutes(cmd):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in proc.stdout or []:
        line = line.rstrip("\n")
        print(line, flush=True)
        if _log_file:
            print(f"[minutes] {line}", file=_log_file, flush=True)
    proc.wait()
    return proc


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


def _request_stop(_signum, _frame):
    global _stop_requested
    _stop_requested = True


def stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def ensure_headset_profile(pulse, device_match):
    card = find_bt_card(pulse, device_match)
    if card is None:
        return

    active = card.profile_active.name
    if active.startswith("headset-head-unit"):
        log(f"BT profile: {active}")
        return

    available = {p.name for p in card.profile_list if p.available != 0}
    for profile in HEADSET_PROFILES:
        if profile in available:
            log(f"Switching BT profile from {active} to {profile}")
            pulse.card_profile_set(card, profile)
            if wait_for_sink(pulse, device_match) is None and not _stop_requested:
                warn("Warning: HFP sink did not appear after profile switch")
            if wait_for_source(pulse, device_match) is None and not _stop_requested:
                warn("Warning: HFP source did not appear after profile switch")
            return

    warn(f"Warning: no headset profile available, staying on {active}")


def find_bt_card(pulse, device_match):
    for card in pulse.card_list():
        if matches_card(card, device_match):
            return card
    return None


def matches_card(card, pattern):
    pattern_lower = pattern.lower()
    props = card.proplist
    for key in ("device.description", "device.alias", "device.name"):
        value = props.get(key, "")
        if pattern_lower in value.lower():
            return True
    return False


def prepare_source(pulse, source_name):
    for source in pulse.source_list():
        if source.name == source_name:
            mute_state = "muted" if source.mute else "unmuted"
            log(f"Source {source_name}: {mute_state}")
            vol = source.volume
            vol.value_flat = MIC_VOLUME
            pulse.source_volume_set(source.index, vol)
            log(f"Source volume: {int(MIC_VOLUME * 100)}%")
            return


def save_sink_volume(pulse, sink_name):
    for sink in pulse.sink_list():
        if sink.name == sink_name:
            vol = sink.volume.value_flat
            log(f"Speaker volume: {vol:.0%}")
            return vol
    return None


def set_sink_volume(pulse, sink_name, volume):
    for sink in pulse.sink_list():
        if sink.name == sink_name:
            vol = sink.volume
            vol.value_flat = volume
            pulse.sink_volume_set(sink.index, vol)
            log(f"Speaker volume set: {volume:.0%} on {sink_name}")
            return


def restore_sink_volume(sink_name, volume):
    if sink_name is None or volume is None:
        return
    try:
        pulse = pulsectl.Pulse("meetmix-restore-vol")
    except Exception:
        return
    try:
        set_sink_volume(pulse, sink_name, volume)
    except pulsectl.PulseError:
        pass
    finally:
        pulse.close()


SCO_WARMUP_SECONDS = 3


def warm_up_sco(session):
    """Send silence through the forwarding path to activate the BT SCO link.

    HFP Bluetooth sinks may not produce audio until the SCO transport has been
    active for a brief period. Sending silence through the null sink wakes the
    link via the forwarding loopback.
    """
    if _stop_requested:
        return
    proc = subprocess.Popen(
        [
            "pw-play", f"--target={NULL_SINK_NAME}",
            "--format=s16", "--rate=48000", "--channels=2",
            "/dev/zero",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session.sco_warmup_proc = proc
    log(f"SCO warmup started (pid {proc.pid}, {SCO_WARMUP_SECONDS}s)")
    remaining = SCO_WARMUP_SECONDS
    while remaining > 0 and not _stop_requested:
        step = min(remaining, 0.25)
        time.sleep(step)
        remaining -= step
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    session.sco_warmup_proc = None
    log("SCO warmup complete")


def wait_for_forwarding_link(session, timeout=5):
    """Poll pw-link until the forwarding loopback is linked to the BT sink."""
    fwd_proc = session.forwarding_loopback
    bt_sink_name = session.bt_sink_name
    if fwd_proc is None or bt_sink_name is None:
        return

    loopback_id = f"pw-loopback-{fwd_proc.pid}"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline and not _stop_requested:
        if fwd_proc.poll() is not None:
            warn(f"Warning: forwarding pw-loopback exited (code {fwd_proc.returncode})")
            return
        try:
            result = subprocess.run(
                ["pw-link", "-l"], capture_output=True, text=True, timeout=3,
            )
            if _check_loopback_linked(result.stdout, loopback_id, bt_sink_name):
                log(f"Forwarding linked to {bt_sink_name} (waited "
                    f"{timeout - (deadline - time.monotonic()):.1f}s)")
                return
        except Exception:
            pass
        time.sleep(0.25)

    warn(f"Warning: forwarding loopback not linked after {timeout}s, proceeding anyway")


def _check_loopback_linked(pw_link_output, loopback_id, bt_sink_name):
    """Return True if loopback_id has an output link to bt_sink_name."""
    current_port_owner = None
    for line in pw_link_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not line[0].isspace():
            current_port_owner = stripped.split(":")[0] if ":" in stripped else None
        elif "|" in stripped:
            linked_to = stripped.split("|")[1].lstrip("-> ").lstrip("<- ")
            linked_owner = linked_to.split(":")[0] if ":" in linked_to else ""
            if current_port_owner and loopback_id in current_port_owner:
                if bt_sink_name in linked_owner:
                    return True
            elif current_port_owner and bt_sink_name in current_port_owner:
                if loopback_id in linked_owner:
                    return True
    return False


def verify_forwarding_link(pulse, session):
    """Check the forwarding loopback is linked on both ends via pw-link."""
    bt_sink_name = session.bt_sink_name
    fwd_proc = session.forwarding_loopback
    if bt_sink_name is None or fwd_proc is None:
        return

    if fwd_proc.poll() is not None:
        warn(f"Warning: forwarding pw-loopback exited (code {fwd_proc.returncode})")
        return

    for sink in pulse.sink_list():
        if sink.name == bt_sink_name:
            sink_vol = sink.volume.value_flat
            sink_mute = "muted" if sink.mute else "unmuted"
            state_names = {0: "running", 1: "idle", 2: "suspended"}
            sink_state = state_names.get(sink.state, f"unknown({sink.state})")
            log(f"BT sink: vol={sink_vol:.0%} {sink_mute} state={sink_state}")
            break

    try:
        fwd_pid = fwd_proc.pid
        loopback_id = f"pw-loopback-{fwd_pid}"
        result = subprocess.run(
            ["pw-link", "-l"], capture_output=True, text=True, timeout=5,
        )
        # pw-link -l format: port lines are unindented, link lines are indented
        # with "|-> target:port" or "|<- source:port". We need to check if the
        # loopback's output ports link to bt_sink, and if bt_sink or
        # meetmix_combined ports link to/from the loopback.
        lines = result.stdout.splitlines()
        has_input = False
        has_output = False
        current_port_owner = None
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if not line[0].isspace():
                current_port_owner = stripped.split(":")[0] if ":" in stripped else None
            elif "|" in stripped:
                linked_to = stripped.split("|")[1].lstrip("-> ").lstrip("<- ") if "|" in stripped else ""
                linked_owner = linked_to.split(":")[0] if ":" in linked_to else ""
                if current_port_owner and loopback_id in current_port_owner:
                    if bt_sink_name in linked_owner:
                        has_output = True
                    elif NULL_SINK_NAME in linked_owner:
                        has_input = True
                elif current_port_owner and bt_sink_name in current_port_owner:
                    if loopback_id in linked_owner:
                        has_output = True
                elif current_port_owner and NULL_SINK_NAME in current_port_owner:
                    if loopback_id in linked_owner:
                        has_input = True

        if has_input and has_output:
            log(f"Forwarding linked: {NULL_SINK_NAME} -> {bt_sink_name}")
        else:
            loopback_lines = []
            capture = False
            for line in lines:
                if loopback_id in line:
                    capture = True
                    loopback_lines.append(line)
                elif capture and line and line[0].isspace():
                    loopback_lines.append(line)
                else:
                    capture = False
            if loopback_lines:
                log("Forwarding pw-link state:\n" + "\n".join(loopback_lines))
            else:
                log(f"Forwarding loopback {loopback_id} not found in pw-link output")
            if not has_input:
                warn(f"Warning: forwarding input not linked to {NULL_SINK_NAME}")
            if not has_output:
                warn(f"Warning: forwarding output not linked to {bt_sink_name}")
    except Exception as e:
        warn(f"Warning: pw-link verification failed: {e}")



def move_sink_inputs(pulse, target_sink_name):
    target = None
    for sink in pulse.sink_list():
        if sink.name == target_sink_name:
            target = sink
            break
    if target is None:
        return

    for si in pulse.sink_input_list():
        if si.sink == target.index:
            continue
        try:
            pulse.sink_input_move(si.index, target.index)
            app = si.proplist.get("application.name", "unknown")
            log(f"Moved sink input: {app} -> {target_sink_name}")
        except pulsectl.PulseError:
            pass


def wait_for_sink(pulse, device_match, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _stop_requested:
        for sink in pulse.sink_list():
            if matches_device(sink, device_match):
                return sink.name
        time.sleep(0.25)
    return None


def wait_for_source(pulse, device_match, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _stop_requested:
        for source in pulse.source_list():
            if source.monitor_of_sink != 0xFFFFFFFF:
                continue
            if matches_device(source, device_match):
                return source.name
        time.sleep(0.25)
    return None


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


def find_bt_sink(pulse, device_match):
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

    return candidates[0].name


def get_node_serial(node_name):
    """Look up a PipeWire node's object.serial by its node.name."""
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5,
        )
        for node in json.loads(result.stdout):
            props = node.get("info", {}).get("props", {})
            if props.get("node.name") == node_name:
                serial = props.get("object.serial")
                if serial is not None:
                    log(f"Node {node_name}: object.serial={serial}")
                    return str(serial)
    except Exception as e:
        warn(f"Warning: could not look up node serial: {e}")
    return None


def matches_device(device, pattern):
    pattern_lower = pattern.lower()
    if pattern_lower in device.name.lower():
        return True
    if pattern_lower in device.description.lower():
        return True
    return False


def create_combined_sink(pulse):
    idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={NULL_SINK_NAME}"
        f" sink_properties=device.description={NULL_SINK_DESCRIPTION}",
    )
    log(f"Created null sink: {NULL_SINK_NAME} (module {idx})")
    return idx


def create_capture_devices(pulse, mic_source, bt_sink_name, modules=None):
    if modules is None:
        modules = {}

    idx = pulse.module_load(
        "module-null-sink",
        f"sink_name={CAPTURE_SINK_NAME}"
        f" sink_properties=device.description={CAPTURE_SINK_DESCRIPTION}",
    )
    modules["capture_sink"] = idx
    log(f"Created capture sink: {CAPTURE_SINK_NAME} (module {idx})")

    idx = pulse.module_load(
        "module-loopback",
        f"source={mic_source} sink={CAPTURE_SINK_NAME} latency_msec=1"
        " source_output_properties=media.role=Communication",
    )
    modules["mic_loopback"] = idx
    log(f"Loopback mic -> capture (module {idx})")

    return modules


def start_forwarding_loopback(bt_sink_target):
    proc = subprocess.Popen(
        [
            "pw-loopback",
            "-C", NULL_SINK_NAME,
            "--capture-props", "stream.capture.sink=true",
            "--playback-props",
            f"target.object={bt_sink_target} media.role=Communication",
        ],
        stdout=_log_file or subprocess.DEVNULL,
        stderr=_log_file or subprocess.DEVNULL,
    )
    log(f"Loopback combined -> speaker (pw-loopback pid {proc.pid}, target {bt_sink_target})")
    return proc


def start_capture_loopback():
    proc = subprocess.Popen(
        [
            "pw-loopback",
            "-C", NULL_SINK_NAME,
            "--capture-props", "stream.capture.sink=true",
            "-P", CAPTURE_SINK_NAME,
        ],
        stdout=_log_file or subprocess.DEVNULL,
        stderr=_log_file or subprocess.DEVNULL,
    )
    log(f"Loopback combined -> capture (pw-loopback pid {proc.pid})")
    return proc


def unload_modules(modules):
    if not modules:
        return
    try:
        pulse = pulsectl.Pulse("meetmix-cleanup")
    except Exception:
        return
    try:
        for name in reversed(list(modules.keys())):
            idx = modules[name]
            try:
                pulse.module_unload(idx)
            except pulsectl.PulseError:
                pass
    finally:
        pulse.close()
    modules.clear()


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


def restore_default_sink(sink_name):
    if not sink_name:
        return
    try:
        pulse = pulsectl.Pulse("meetmix-restore")
    except Exception:
        return
    try:
        pulse.sink_default_set(sink_name)
    except pulsectl.PulseError:
        pass
    finally:
        pulse.close()


WPCTL_AUTOSWITCH_KEY = "bluetooth.autoswitch-to-headset-profile"


def disable_wpctl_autoswitch():
    try:
        subprocess.run(
            ["wpctl", "settings", WPCTL_AUTOSWITCH_KEY, "false"],
            capture_output=True, timeout=5,
        )
        log("Disabled WirePlumber BT profile auto-switch")
    except Exception as e:
        warn(f"Warning: could not disable WirePlumber auto-switch: {e}")


def restore_wpctl_autoswitch():
    try:
        subprocess.run(
            ["wpctl", "settings", WPCTL_AUTOSWITCH_KEY, "true"],
            capture_output=True, timeout=5,
        )
        log("Restored WirePlumber BT profile auto-switch")
    except Exception as e:
        warn(f"Warning: could not restore WirePlumber auto-switch: {e}")


def restore_bt_profile(session):
    if not session.original_card_profile or not session.device_match:
        return
    try:
        pulse = pulsectl.Pulse("meetmix-restore-bt")
    except Exception:
        restore_default_sink(session.original_default_sink)
        return
    try:
        card = find_bt_card(pulse, session.device_match)
        if card and card.profile_active.name != session.original_card_profile:
            pulse.card_profile_set(card, session.original_card_profile)
            log(f"Restored BT profile: {session.original_card_profile}")
        # WirePlumber overrides the default sink when the A2DP sink
        # appears, whether we triggered the switch above or WirePlumber
        # already auto-switched. If the default still matches what we
        # set earlier, poll for the override before restoring.
        pre_default = pulse.server_info().default_sink_name
        if pre_default == session.original_default_sink:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if pulse.server_info().default_sink_name != pre_default:
                    break
                time.sleep(0.24)
        restore_default_sink(session.original_default_sink)
    except Exception:
        restore_default_sink(session.original_default_sink)
    finally:
        pulse.close()


def fix_wav_header(wav_path):
    try:
        file_size = os.path.getsize(wav_path)
        if file_size < 44:
            return False

        with open(wav_path, "r+b") as f:
            header = f.read(12)
            if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return False

            riff_size = struct.unpack_from("<I", header, 4)[0]
            if riff_size == file_size - 8:
                return False

            f.seek(12)
            while f.tell() < file_size - 8:
                chunk_id = f.read(4)
                if len(chunk_id) < 4:
                    break
                chunk_size_bytes = f.read(4)
                if len(chunk_size_bytes) < 4:
                    break
                chunk_size = struct.unpack("<I", chunk_size_bytes)[0]
                if chunk_id == b"data":
                    data_start = f.tell()
                    expected_data_size = file_size - data_start
                    if chunk_size != expected_data_size:
                        f.seek(data_start - 4)
                        f.write(struct.pack("<I", expected_data_size))
                        f.seek(4)
                        f.write(struct.pack("<I", file_size - 8))
                        log(f"Fixed WAV header (data size: {expected_data_size})")
                        return True
                    return False
                f.seek(chunk_size, 1)

            return False
    except Exception as e:
        warn(f"Warning: Could not fix WAV header: {e}")
        return False


def log_wav_stats(wav_path, file_size):
    try:
        with wave.open(wav_path, "rb") as w:
            channels = w.getnchannels()
            rate = w.getframerate()
            frames = w.getnframes()
            width = w.getsampwidth()
            duration = frames / rate if rate else 0
            log(f"WAV: {duration:.1f}s, {rate}Hz, {channels}ch, {width * 8}-bit, {file_size} bytes")
            raw = w.readframes(frames)
            if width == 2:
                fmt = f"<{len(raw) // 2}h"
                samples = struct.unpack(fmt, raw)
                peak = max(abs(s) for s in samples) if samples else 0
                rms = (sum(s * s for s in samples) / len(samples)) ** 0.5 if samples else 0
                log(f"WAV peak amplitude: {peak} (of 32768), RMS: {rms:.0f}")
                if peak < 100:
                    warn("Warning: Recording appears silent (peak < 100).")
            else:
                log(f"WAV sample width {width} bytes, skipping amplitude analysis")
    except Exception as e:
        warn(f"Warning: Could not analyze WAV: {e}")
    log("Processing with minutes...")


def setup_logging(timestamp):
    global _log_file
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"meetmix-{timestamp}.log")
    _log_file = open(log_path, "a", buffering=1)
    log(f"Log file: {log_path}")
    return log_path


def _close_log_file():
    global _log_file
    if _log_file:
        try:
            _log_file.close()
        except Exception:
            pass
        _log_file = None


def log(message):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(message, flush=True)
    if _log_file:
        print(f"[{ts}] {message}", file=_log_file, flush=True)


def warn(message):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(message, file=sys.stderr, flush=True)
    if _log_file:
        print(f"[{ts}] WARNING: {message}", file=_log_file, flush=True)


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
    parser.add_argument(
        "--keep-recording",
        action="store_true",
        help="keep the WAV recording after processing (for debugging)",
    )
    parser.add_argument(
        "--language",
        help="transcription language code (e.g. 'en', 'es'). Passed to minutes process.",
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

    if not args.language and "language" in file_args:
        args.language = file_args["language"]

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
        elif flag == "--language":
            result["language"] = value
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


if __name__ == "__main__":
    main()
