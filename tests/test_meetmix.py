import importlib.machinery
import importlib.util
import os
import pathlib
import signal
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module():
    loader = importlib.machinery.SourceFileLoader("meetmix_module", str(ROOT / "meetmix" / "meetmix.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Failed to create import spec for meetmix")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


MEETMIX = load_module()


def make_source(name="src", description="Source", monitor_of_sink=0xFFFFFFFF):
    source = mock.Mock()
    source.name = name
    source.description = description
    source.monitor_of_sink = monitor_of_sink
    return source


def make_sink(name="sink", description="Sink", monitor_source_name="sink.monitor"):
    sink = mock.Mock()
    sink.name = name
    sink.description = description
    sink.monitor_source_name = monitor_source_name
    return sink


def make_card(name="card", description="Card", active_profile="a2dp-sink", profiles=None):
    card = mock.Mock()
    card.name = name
    card.proplist = {"device.description": description, "device.name": name}
    card.profile_active = mock.Mock()
    card.profile_active.name = active_profile
    if profiles is None:
        profiles = []
    card.profile_list = profiles
    return card


def make_profile(name, available=1):
    profile = mock.Mock()
    profile.name = name
    profile.available = available
    return profile


class CleanupSessionTests(unittest.TestCase):
    @mock.patch.object(MEETMIX, "restore_sink_volume")
    @mock.patch.object(MEETMIX, "restore_bt_profile")
    @mock.patch.object(MEETMIX, "restore_wpctl_autoswitch")
    @mock.patch.object(MEETMIX, "unload_modules")
    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "stop_process")
    def test_stops_all_subprocesses(self, stop_proc, _restore, _unload, _wpctl, _restore_bt, _vol):
        session = MEETMIX.Session()
        session.record_proc = mock.Mock()
        session.sco_warmup_proc = mock.Mock()
        session.capture_loopback = mock.Mock()
        session.forwarding_loopback = mock.Mock()
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.cleanup_session(session)
        self.assertEqual(4, stop_proc.call_count)
        stop_proc.assert_any_call(session.record_proc)
        stop_proc.assert_any_call(session.sco_warmup_proc)
        stop_proc.assert_any_call(session.capture_loopback)
        stop_proc.assert_any_call(session.forwarding_loopback)

    @mock.patch.object(MEETMIX, "restore_sink_volume")
    @mock.patch.object(MEETMIX, "restore_bt_profile")
    @mock.patch.object(MEETMIX, "restore_wpctl_autoswitch")
    @mock.patch.object(MEETMIX, "unload_modules")
    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "stop_process")
    def test_cleanup_order(self, stop_proc, restore, unload, wpctl, restore_bt, restore_vol):
        call_order = []
        stop_proc.side_effect = lambda p: call_order.append(("stop", getattr(p, "_name", "none")))
        restore.side_effect = lambda s: call_order.append(("restore", s))
        unload.side_effect = lambda m: call_order.append(("unload",))
        wpctl.side_effect = lambda: call_order.append(("wpctl_restore",))
        restore_bt.side_effect = lambda s: call_order.append(("restore_bt",))
        restore_vol.side_effect = lambda n, v: call_order.append(("restore_vol",))
        session = MEETMIX.Session()
        rec = mock.Mock(_name="record")
        warmup = mock.Mock(_name="warmup")
        lb = mock.Mock(_name="loopback")
        fwd = mock.Mock(_name="forwarding")
        session.record_proc = rec
        session.sco_warmup_proc = warmup
        session.capture_loopback = lb
        session.forwarding_loopback = fwd
        session.original_default_sink = "alsa_output.hdmi"
        session.modules = {"capture_sink": 100}
        MEETMIX.cleanup_session(session)
        self.assertEqual(
            [
                ("stop", "record"),
                ("stop", "warmup"),
                ("stop", "loopback"),
                ("stop", "forwarding"),
                ("restore", "alsa_output.hdmi"),
                ("unload",),
                ("restore_bt",),
                ("wpctl_restore",),
                ("restore_vol",),
            ],
            call_order,
        )


class CleanupOrphansTests(unittest.TestCase):
    @mock.patch("builtins.print")
    def test_unloads_orphaned_modules(self, _print):
        pulse = mock.Mock()
        m1 = mock.Mock(name="module-null-sink", argument="sink_name=meetmix_combined", index=50)
        m1.name = "module-null-sink"
        m2 = mock.Mock(name="module-loopback", argument="source=mic sink=meetmix_combined", index=51)
        m2.name = "module-loopback"
        m3 = mock.Mock(name="module-null-sink", argument="sink_name=other_thing", index=52)
        m3.name = "module-null-sink"
        pulse.module_list.return_value = [m1, m2, m3]
        count = MEETMIX.cleanup_orphans(pulse)
        self.assertEqual(2, count)
        pulse.module_unload.assert_any_call(50)
        pulse.module_unload.assert_any_call(51)

    def test_returns_zero_when_no_orphans(self):
        pulse = mock.Mock()
        m1 = mock.Mock(argument="sink_name=other_thing", index=52)
        m1.name = "module-null-sink"
        pulse.module_list.return_value = [m1]
        count = MEETMIX.cleanup_orphans(pulse)
        self.assertEqual(0, count)


class ConfTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_conf_path = MEETMIX.CONF_PATH
        MEETMIX.CONF_PATH = os.path.join(self._tmpdir, "meetmix.conf")

    def tearDown(self):
        MEETMIX.CONF_PATH = self._orig_conf_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_conf_returns_empty_when_no_file(self):
        self.assertEqual({}, MEETMIX.load_conf())

    def test_load_conf_reads_device_match(self):
        with open(MEETMIX.CONF_PATH, "w") as f:
            f.write("--device-match=AirPods\n")
        self.assertEqual({"device_match": "AirPods"}, MEETMIX.load_conf())

    def test_load_conf_ignores_comments_and_blank_lines(self):
        with open(MEETMIX.CONF_PATH, "w") as f:
            f.write("# match my headphones\n\n--device-match=AirPods Pro\n")
        self.assertEqual({"device_match": "AirPods Pro"}, MEETMIX.load_conf())

    @mock.patch.object(MEETMIX, "warn")
    def test_load_conf_rejects_unknown_flags(self, _warn):
        with open(MEETMIX.CONF_PATH, "w") as f:
            f.write("--foobar=baz\n")
        with self.assertRaises(SystemExit):
            MEETMIX.load_conf()

    @mock.patch.object(MEETMIX, "warn")
    def test_load_conf_rejects_malformed_lines(self, _warn):
        with open(MEETMIX.CONF_PATH, "w") as f:
            f.write("garbage\n")
        with self.assertRaises(SystemExit):
            MEETMIX.load_conf()


class CreateCaptureDevicesTests(unittest.TestCase):
    def setUp(self):
        self.pulse = mock.Mock()
        self.pulse.module_load.side_effect = [101, 102]

    @mock.patch("builtins.print")
    def test_returns_dict_with_two_modules(self, _print):
        modules = MEETMIX.create_capture_devices(self.pulse, "mic_src", "bt_sink")
        self.assertEqual(2, self.pulse.module_load.call_count)
        self.assertEqual(
            {"capture_sink": 101, "mic_loopback": 102},
            modules,
        )

    @mock.patch("builtins.print")
    def test_populates_passed_in_dict(self, _print):
        existing = {"combined_sink": 100}
        MEETMIX.create_capture_devices(self.pulse, "mic_src", "bt_sink", existing)
        self.assertEqual(100, existing["combined_sink"])
        self.assertEqual(101, existing["capture_sink"])
        self.assertEqual(102, existing["mic_loopback"])

    @mock.patch("builtins.print")
    def test_partial_failure_records_created_modules(self, _print):
        self.pulse.module_load.side_effect = [101, MEETMIX.pulsectl.PulseError("fail")]
        modules = {"combined_sink": 100}
        with self.assertRaises(MEETMIX.pulsectl.PulseError):
            MEETMIX.create_capture_devices(self.pulse, "mic_src", "bt_sink", modules)
        self.assertIn("capture_sink", modules)
        self.assertEqual(101, modules["capture_sink"])
        self.assertNotIn("mic_loopback", modules)

    @mock.patch("builtins.print")
    def test_capture_sink_args(self, _print):
        MEETMIX.create_capture_devices(self.pulse, "mic_src", "bt_sink")
        call = self.pulse.module_load.call_args_list[0]
        self.assertEqual("module-null-sink", call.args[0])
        self.assertIn("sink_name=meetmix_capture", call.args[1])

    @mock.patch("builtins.print")
    def test_loopback_mic_args(self, _print):
        MEETMIX.create_capture_devices(self.pulse, "mic_src", "bt_sink")
        call = self.pulse.module_load.call_args_list[1]
        self.assertEqual("module-loopback", call.args[0])
        self.assertIn("source=mic_src", call.args[1])
        self.assertIn("sink=meetmix_capture", call.args[1])


class StartForwardingLoopbackTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    @mock.patch("builtins.print")
    def test_spawns_pw_loopback(self, _print, popen_mock):
        proc = mock.Mock(pid=5678)
        popen_mock.return_value = proc
        result = MEETMIX.start_forwarding_loopback("99999")
        self.assertEqual(proc, result)
        cmd = popen_mock.call_args.args[0]
        self.assertEqual("pw-loopback", cmd[0])
        self.assertIn("-C", cmd)
        joined = " ".join(cmd)
        self.assertIn("stream.capture.sink=true", joined)
        self.assertIn("target.object=99999", joined)
        self.assertIn("media.role=Communication", joined)


class CreateCombinedSinkTests(unittest.TestCase):
    def setUp(self):
        self.pulse = mock.Mock()
        self.pulse.module_load.return_value = 100

    @mock.patch("builtins.print")
    def test_loads_one_module(self, _print):
        idx = MEETMIX.create_combined_sink(self.pulse)
        self.assertEqual(1, self.pulse.module_load.call_count)
        self.assertEqual(100, idx)

    @mock.patch("builtins.print")
    def test_null_sink_args(self, _print):
        MEETMIX.create_combined_sink(self.pulse)
        call = self.pulse.module_load.call_args
        self.assertEqual("module-null-sink", call.args[0])
        self.assertIn("sink_name=meetmix_combined", call.args[1])


class DisableWpctlAutoswitchTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.subprocess, "run")
    @mock.patch("builtins.print")
    def test_calls_wpctl(self, _print, run_mock):
        MEETMIX.disable_wpctl_autoswitch()
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        self.assertEqual(["wpctl", "settings", MEETMIX.WPCTL_AUTOSWITCH_KEY, "false"], cmd)

    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.subprocess, "run", side_effect=FileNotFoundError("wpctl"))
    @mock.patch("builtins.print")
    def test_warns_on_failure(self, _print, _run, warn_mock):
        MEETMIX.disable_wpctl_autoswitch()
        warn_mock.assert_called_once()
        self.assertIn("could not disable", warn_mock.call_args.args[0])


class RestoreWpctlAutoswitchTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.subprocess, "run")
    @mock.patch("builtins.print")
    def test_calls_wpctl(self, _print, run_mock):
        MEETMIX.restore_wpctl_autoswitch()
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        self.assertEqual(["wpctl", "settings", MEETMIX.WPCTL_AUTOSWITCH_KEY, "true"], cmd)

    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.subprocess, "run", side_effect=FileNotFoundError("wpctl"))
    @mock.patch("builtins.print")
    def test_warns_on_failure(self, _print, _run, warn_mock):
        MEETMIX.restore_wpctl_autoswitch()
        warn_mock.assert_called_once()
        self.assertIn("could not restore", warn_mock.call_args.args[0])


class EnsureHeadsetProfileTests(unittest.TestCase):
    def test_noop_when_already_headset(self):
        card = make_card(
            active_profile="headset-head-unit",
            profiles=[make_profile("headset-head-unit")],
        )
        pulse = mock.Mock()
        pulse.card_list.return_value = [card]
        MEETMIX.ensure_headset_profile(pulse, "Card")
        pulse.card_profile_set.assert_not_called()

    def test_switches_to_headset_profile(self):
        card = make_card(
            description="AirPods Pro",
            active_profile="a2dp-sink",
            profiles=[
                make_profile("a2dp-sink"),
                make_profile("headset-head-unit"),
                make_profile("headset-head-unit-msbc"),
            ],
        )
        pulse = mock.Mock()
        pulse.card_list.return_value = [card]
        pulse.sink_list.return_value = [
            make_sink("bluez_output.abc", "AirPods Pro"),
        ]
        pulse.source_list.return_value = [
            make_source("bluez_input.abc", "AirPods Pro"),
        ]
        MEETMIX.ensure_headset_profile(pulse, "AirPods")
        pulse.card_profile_set.assert_called_once_with(card, "headset-head-unit")

    def test_falls_back_to_msbc(self):
        card = make_card(
            description="AirPods Pro",
            active_profile="a2dp-sink",
            profiles=[
                make_profile("a2dp-sink"),
                make_profile("headset-head-unit", available=0),
                make_profile("headset-head-unit-msbc"),
            ],
        )
        pulse = mock.Mock()
        pulse.card_list.return_value = [card]
        pulse.sink_list.return_value = [
            make_sink("bluez_output.abc", "AirPods Pro"),
        ]
        pulse.source_list.return_value = [
            make_source("bluez_input.abc", "AirPods Pro"),
        ]
        MEETMIX.ensure_headset_profile(pulse, "AirPods")
        pulse.card_profile_set.assert_called_once_with(card, "headset-head-unit-msbc")

    def test_noop_when_no_matching_card(self):
        pulse = mock.Mock()
        pulse.card_list.return_value = []
        MEETMIX.ensure_headset_profile(pulse, "AirPods")
        pulse.card_profile_set.assert_not_called()

    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.time, "monotonic", side_effect=[0, 6, 0, 6])
    @mock.patch.object(MEETMIX.time, "sleep")
    def test_warns_when_sink_or_source_timeout(self, _sleep, _monotonic, warn_mock):
        card = make_card(
            description="AirPods Pro",
            active_profile="a2dp-sink",
            profiles=[make_profile("headset-head-unit")],
        )
        pulse = mock.Mock()
        pulse.card_list.return_value = [card]
        pulse.sink_list.return_value = []
        pulse.source_list.return_value = []
        MEETMIX.ensure_headset_profile(pulse, "AirPods")
        pulse.card_profile_set.assert_called_once()
        warnings = [c.args[0] for c in warn_mock.call_args_list]
        self.assertTrue(any("HFP sink" in w for w in warnings))
        self.assertTrue(any("HFP source" in w for w in warnings))


class FindBtSinkTests(unittest.TestCase):
    def test_finds_matching_sink(self):
        pulse = mock.Mock()
        pulse.sink_list.return_value = [
            make_sink("alsa_output.usb", "USB Speakers", "alsa_output.usb.monitor"),
            make_sink("bluez_output.abc", "AirPods Pro", "bluez_output.abc.monitor"),
        ]
        result = MEETMIX.find_bt_sink(pulse, "AirPods")
        self.assertEqual("bluez_output.abc", result)

    @mock.patch.object(MEETMIX, "warn")
    def test_exits_on_no_match(self, _warn):
        pulse = mock.Mock()
        pulse.sink_list.return_value = [
            make_sink("alsa_output.usb", "USB Speakers"),
        ]
        with self.assertRaises(SystemExit):
            MEETMIX.find_bt_sink(pulse, "AirPods")

    @mock.patch.object(MEETMIX, "warn")
    def test_exits_on_multiple_matches(self, _warn):
        pulse = mock.Mock()
        pulse.sink_list.return_value = [
            make_sink("bluez_output.abc", "AirPods Pro"),
            make_sink("bluez_output.def", "AirPods Max"),
        ]
        with self.assertRaises(SystemExit):
            MEETMIX.find_bt_sink(pulse, "AirPods")


class FindMicSourceTests(unittest.TestCase):
    def test_finds_matching_source(self):
        pulse = mock.Mock()
        pulse.source_list.return_value = [
            make_source("alsa_input.usb", "USB Mic"),
            make_source("bluez_input.abc", "AirPods Pro"),
        ]
        result = MEETMIX.find_mic_source(pulse, "AirPods")
        self.assertEqual("bluez_input.abc", result)

    def test_excludes_monitor_sources(self):
        pulse = mock.Mock()
        pulse.source_list.return_value = [
            make_source("bluez_output.abc.monitor", "Monitor of AirPods Pro", monitor_of_sink=42),
            make_source("bluez_input.abc", "AirPods Pro"),
        ]
        result = MEETMIX.find_mic_source(pulse, "AirPods")
        self.assertEqual("bluez_input.abc", result)

    @mock.patch.object(MEETMIX, "warn")
    def test_exits_on_no_match(self, _warn):
        pulse = mock.Mock()
        pulse.source_list.return_value = [
            make_source("alsa_input.usb", "USB Mic"),
        ]
        with self.assertRaises(SystemExit):
            MEETMIX.find_mic_source(pulse, "AirPods")

    @mock.patch.object(MEETMIX, "warn")
    def test_exits_on_multiple_matches(self, _warn):
        pulse = mock.Mock()
        pulse.source_list.return_value = [
            make_source("bluez_input.abc", "AirPods Pro"),
            make_source("bluez_input.def", "AirPods Max"),
        ]
        with self.assertRaises(SystemExit):
            MEETMIX.find_mic_source(pulse, "AirPods")


class FixWavHeaderTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _wav_path(self, name="test.wav"):
        return os.path.join(self._tmpdir, name)

    def _build_wav(self, data_size, data_size_in_header=None, riff_size_in_header=None):
        if data_size_in_header is None:
            data_size_in_header = data_size
        fmt_chunk = struct.pack(
            "<4sIHHIIHH",
            b"fmt ", 16, 1, 1, 16000, 32000, 2, 16,
        )
        data_header = struct.pack("<4sI", b"data", data_size_in_header)
        data = b"\x80" * data_size
        riff_payload = b"WAVE" + fmt_chunk + data_header + data
        if riff_size_in_header is None:
            riff_size_in_header = len(riff_payload)
        return b"RIFF" + struct.pack("<I", riff_size_in_header) + riff_payload

    @mock.patch("builtins.print")
    def test_fixes_truncated_wav(self, _print):
        wav_bytes = self._build_wav(
            data_size=100,
            data_size_in_header=1000,
            riff_size_in_header=1036,
        )
        path = self._wav_path()
        with open(path, "wb") as f:
            f.write(wav_bytes)
        result = MEETMIX.fix_wav_header(path)
        self.assertTrue(result)
        with open(path, "rb") as f:
            f.seek(4)
            riff_size = struct.unpack("<I", f.read(4))[0]
            self.assertEqual(os.path.getsize(path) - 8, riff_size)
            f.seek(40)
            data_size = struct.unpack("<I", f.read(4))[0]
            self.assertEqual(100, data_size)

    @mock.patch("builtins.print")
    def test_noop_on_valid_wav(self, _print):
        wav_bytes = self._build_wav(data_size=100)
        path = self._wav_path()
        with open(path, "wb") as f:
            f.write(wav_bytes)
        result = MEETMIX.fix_wav_header(path)
        self.assertFalse(result)

    def test_noop_on_non_wav_file(self):
        path = self._wav_path()
        with open(path, "wb") as f:
            f.write(b"NOT A WAV FILE" + b"\x00" * 100)
        result = MEETMIX.fix_wav_header(path)
        self.assertFalse(result)

    def test_noop_on_small_file(self):
        path = self._wav_path()
        with open(path, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 10)
        result = MEETMIX.fix_wav_header(path)
        self.assertFalse(result)

    @mock.patch.object(MEETMIX, "warn")
    def test_handles_nonexistent_file(self, mock_warn):
        result = MEETMIX.fix_wav_header("/nonexistent/file.wav")
        self.assertFalse(result)
        mock_warn.assert_called_once()


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_log_file = MEETMIX._log_file
        self._log_path = os.path.join(self._tmpdir, "test.log")
        MEETMIX._log_file = open(self._log_path, "a", buffering=1)

    def tearDown(self):
        if MEETMIX._log_file and MEETMIX._log_file != self._orig_log_file:
            MEETMIX._log_file.close()
        MEETMIX._log_file = self._orig_log_file
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_log_writes_to_file(self):
        MEETMIX.log("test message")
        with open(self._log_path) as f:
            content = f.read()
        self.assertIn("test message", content)

    def test_warn_writes_to_file_with_prefix(self):
        MEETMIX.warn("warning message")
        with open(self._log_path) as f:
            content = f.read()
        self.assertIn("WARNING: warning message", content)

    def test_log_path_with_spaces(self):
        MEETMIX.log("Recording to: /home/user/my meetings/session 1.wav")
        with open(self._log_path) as f:
            content = f.read()
        self.assertIn("/home/user/my meetings/session 1.wav", content)

    def test_log_single_and_double_quotes(self):
        MEETMIX.log("Device: 'AirPods Pro' matched \"AirPods\"")
        with open(self._log_path) as f:
            content = f.read()
        self.assertIn("'AirPods Pro'", content)
        self.assertIn('"AirPods"', content)

    def test_log_backslashes(self):
        MEETMIX.log("Path: C:\\Users\\test\\file.wav")
        with open(self._log_path) as f:
            content = f.read()
        self.assertIn("C:\\Users\\test\\file.wav", content)

    def test_log_unicode(self):
        MEETMIX.log("Mic: Jabra Evolve2 75 (\u00e9\u00e0\u00fc\u00f1)")
        with open(self._log_path) as f:
            content = f.read()
        self.assertIn("\u00e9\u00e0\u00fc\u00f1", content)

    def test_log_format_has_timestamp(self):
        MEETMIX.log("timestamped message")
        with open(self._log_path) as f:
            content = f.read()
        self.assertRegex(content, r"\[\d{2}:\d{2}:\d{2}\] timestamped message")

    def test_warn_format_has_warning_prefix(self):
        MEETMIX.warn("bad thing")
        with open(self._log_path) as f:
            content = f.read()
        self.assertRegex(content, r"\[\d{2}:\d{2}:\d{2}\] WARNING: bad thing")


class MatchesDeviceTests(unittest.TestCase):
    def test_matches_name_case_insensitive(self):
        device = make_source(name="bluez_input.airpods", description="Other")
        self.assertTrue(MEETMIX.matches_device(device, "AirPods"))

    def test_matches_description_case_insensitive(self):
        device = make_source(name="bluez_input.abc", description="AirPods Pro")
        self.assertTrue(MEETMIX.matches_device(device, "airpods"))

    def test_no_match(self):
        device = make_source(name="alsa_input.usb", description="USB Mic")
        self.assertFalse(MEETMIX.matches_device(device, "AirPods"))


class MoveSinkInputsTests(unittest.TestCase):
    def test_moves_inputs_to_target(self):
        target_sink = make_sink("meetmix_combined", "MeetMix Combined")
        target_sink.index = 10
        other_sink = make_sink("bluez_output.abc", "AirPods")
        other_sink.index = 20
        pulse = mock.Mock()
        pulse.sink_list.return_value = [target_sink, other_sink]
        si = mock.Mock()
        si.index = 100
        si.sink = 20
        si.proplist = {"application.name": "Firefox"}
        pulse.sink_input_list.return_value = [si]
        MEETMIX.move_sink_inputs(pulse, "meetmix_combined")
        pulse.sink_input_move.assert_called_once_with(100, 10)

    def test_skips_inputs_already_on_target(self):
        target_sink = make_sink("meetmix_combined", "MeetMix Combined")
        target_sink.index = 10
        pulse = mock.Mock()
        pulse.sink_list.return_value = [target_sink]
        si = mock.Mock()
        si.index = 100
        si.sink = 10
        pulse.sink_input_list.return_value = [si]
        MEETMIX.move_sink_inputs(pulse, "meetmix_combined")
        pulse.sink_input_move.assert_not_called()


class ParseArgsTests(unittest.TestCase):
    def test_no_subcommand_defaults_to_record(self):
        with mock.patch("sys.argv", ["meetmix"]):
            args = MEETMIX.parse_args({})
        self.assertIsNone(args.command)

    def test_subcommand_record(self):
        with mock.patch("sys.argv", ["meetmix", "record"]):
            args = MEETMIX.parse_args({})
        self.assertEqual("record", args.command)

    def test_subcommand_devices(self):
        with mock.patch("sys.argv", ["meetmix", "devices"]):
            args = MEETMIX.parse_args({})
        self.assertEqual("devices", args.command)

    def test_subcommand_cleanup(self):
        with mock.patch("sys.argv", ["meetmix", "cleanup"]):
            args = MEETMIX.parse_args({})
        self.assertEqual("cleanup", args.command)

    def test_device_match_flag(self):
        with mock.patch("sys.argv", ["meetmix", "--device-match", "AirPods"]):
            args = MEETMIX.parse_args({})
        self.assertEqual("AirPods", args.device_match)

    def test_conf_device_match_used_as_default(self):
        with mock.patch("sys.argv", ["meetmix"]):
            args = MEETMIX.parse_args({"device_match": "AirPods"})
        self.assertEqual("AirPods", args.device_match)
        self.assertIsNone(args.cli_device_match)

    def test_cli_device_match_overrides_conf(self):
        with mock.patch("sys.argv", ["meetmix", "--device-match", "Jabra"]):
            args = MEETMIX.parse_args({"device_match": "AirPods"})
        self.assertEqual("Jabra", args.device_match)
        self.assertEqual("Jabra", args.cli_device_match)

    def test_extra_args_passed_through(self):
        with mock.patch("sys.argv", ["meetmix", "record", "--unknown-flag", "foo"]):
            args = MEETMIX.parse_args({})
        self.assertEqual(["--unknown-flag", "foo"], args.extra_args)

    def test_extra_args_with_separator(self):
        with mock.patch("sys.argv", ["meetmix", "record", "--", "--language", "en"]):
            args = MEETMIX.parse_args({})
        self.assertEqual(["--language", "en"], args.extra_args)

    def test_extra_args_empty_by_default(self):
        with mock.patch("sys.argv", ["meetmix", "devices"]):
            args = MEETMIX.parse_args({})
        self.assertEqual([], args.extra_args)

    def test_language_after_subcommand_captured(self):
        with mock.patch("sys.argv", ["meetmix", "record", "--language", "en"]):
            args = MEETMIX.parse_args({})
        self.assertEqual("en", args.language)
        self.assertEqual([], args.extra_args)

    def test_keep_recording_flag(self):
        with mock.patch("sys.argv", ["meetmix", "--keep-recording"]):
            args = MEETMIX.parse_args({})
        self.assertTrue(args.keep_recording)

    def test_keep_recording_after_subcommand(self):
        with mock.patch("sys.argv", ["meetmix", "record", "--keep-recording"]):
            args = MEETMIX.parse_args({})
        self.assertTrue(args.keep_recording)
        self.assertEqual([], args.extra_args)

    def test_keep_recording_defaults_to_false(self):
        with mock.patch("sys.argv", ["meetmix"]):
            args = MEETMIX.parse_args({})
        self.assertFalse(args.keep_recording)

    def test_device_match_after_subcommand(self):
        with mock.patch("sys.argv", ["meetmix", "record", "--device-match", "AirPods"]):
            args = MEETMIX.parse_args({})
        self.assertEqual("AirPods", args.device_match)
        self.assertEqual([], args.extra_args)


class ProcessRecordingTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @mock.patch.object(MEETMIX, "warn")
    def test_exits_when_no_recording(self, _warn):
        session = MEETMIX.Session()
        session.wav_path = os.path.join(self._tmpdir, "nonexistent.wav")
        args = mock.Mock(extra_args=[], language=None)
        with self.assertRaises(SystemExit) as ctx:
            MEETMIX.process_recording(session, args)
        self.assertEqual(1, ctx.exception.code)

    @mock.patch.object(MEETMIX, "run_minutes", return_value=mock.Mock(returncode=0))
    @mock.patch.object(MEETMIX, "log_wav_stats")
    @mock.patch.object(MEETMIX, "fix_wav_header")
    @mock.patch("builtins.print")
    def test_builds_minutes_cmd_with_extra_args(self, _print, _fix, _stats, run_minutes_mock):
        wav_path = os.path.join(self._tmpdir, "test.wav")
        with open(wav_path, "wb") as f:
            f.write(b"\x00" * 2000)
        session = MEETMIX.Session()
        session.wav_path = wav_path
        args = mock.Mock(extra_args=[], language="en")
        with self.assertRaises(SystemExit):
            MEETMIX.process_recording(session, args)
        cmd = run_minutes_mock.call_args.args[0]
        self.assertEqual("minutes", cmd[0])
        self.assertEqual("process", cmd[1])
        self.assertIn("--content-type", cmd)
        self.assertIn("meeting", cmd)
        self.assertIn(wav_path, cmd)
        self.assertIn("--language", cmd)
        self.assertIn("en", cmd)

    @mock.patch.object(MEETMIX, "run_minutes", return_value=mock.Mock(returncode=0))
    @mock.patch.object(MEETMIX, "log_wav_stats")
    @mock.patch.object(MEETMIX, "fix_wav_header")
    @mock.patch("builtins.print")
    def test_deletes_wav_on_success(self, _print, _fix, _stats, _run):
        wav_path = os.path.join(self._tmpdir, "test.wav")
        with open(wav_path, "wb") as f:
            f.write(b"\x00" * 2000)
        session = MEETMIX.Session()
        session.wav_path = wav_path
        args = mock.Mock(extra_args=[], language=None)
        with self.assertRaises(SystemExit) as ctx:
            MEETMIX.process_recording(session, args)
        self.assertEqual(0, ctx.exception.code)
        self.assertFalse(os.path.exists(wav_path))

    @mock.patch.object(MEETMIX, "run_minutes", return_value=mock.Mock(returncode=0))
    @mock.patch.object(MEETMIX, "log_wav_stats")
    @mock.patch.object(MEETMIX, "fix_wav_header")
    @mock.patch("builtins.print")
    def test_keeps_wav_with_keep_recording(self, _print, _fix, _stats, _run):
        wav_path = os.path.join(self._tmpdir, "test.wav")
        with open(wav_path, "wb") as f:
            f.write(b"\x00" * 2000)
        session = MEETMIX.Session()
        session.wav_path = wav_path
        session.keep_recording = True
        args = mock.Mock(extra_args=[], language=None)
        with self.assertRaises(SystemExit) as ctx:
            MEETMIX.process_recording(session, args)
        self.assertEqual(0, ctx.exception.code)
        self.assertTrue(os.path.exists(wav_path))

    @mock.patch.object(MEETMIX, "run_minutes", return_value=mock.Mock(returncode=1))
    @mock.patch.object(MEETMIX, "log_wav_stats")
    @mock.patch.object(MEETMIX, "fix_wav_header")
    @mock.patch("builtins.print")
    def test_keeps_wav_on_failure(self, _print, _fix, _stats, _run):
        wav_path = os.path.join(self._tmpdir, "test.wav")
        with open(wav_path, "wb") as f:
            f.write(b"\x00" * 2000)
        session = MEETMIX.Session()
        session.wav_path = wav_path
        args = mock.Mock(extra_args=[], language=None)
        with self.assertRaises(SystemExit) as ctx:
            MEETMIX.process_recording(session, args)
        self.assertEqual(1, ctx.exception.code)
        self.assertTrue(os.path.exists(wav_path))


class RequestStopTests(unittest.TestCase):
    def setUp(self):
        MEETMIX._stop_requested = False

    def tearDown(self):
        MEETMIX._stop_requested = False

    def test_sets_flag(self):
        MEETMIX._request_stop(signal.SIGINT, None)
        self.assertTrue(MEETMIX._stop_requested)

    def test_sets_flag_for_sigterm(self):
        MEETMIX._request_stop(signal.SIGTERM, None)
        self.assertTrue(MEETMIX._stop_requested)


class RecordingMonitorTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_init_reads_profile_and_default_sink(self, _print, pulse_cls):
        pulse = pulse_cls.return_value
        card = mock.Mock()
        card.profile_active = mock.Mock()
        card.profile_active.name = "headset-head-unit"
        server_info = mock.Mock()
        server_info.default_sink_name = "alsa_output.hdmi"
        pulse.server_info.return_value = server_info
        with mock.patch.object(MEETMIX, "find_bt_card", return_value=card):
            session = MEETMIX.Session()
            session.device_match = "AirPods"
            mon = MEETMIX._RecordingMonitor(session)
        self.assertEqual("headset-head-unit", mon._last_profile)
        self.assertEqual("alsa_output.hdmi", mon._last_default_sink)
        mon.close()

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_init_handles_pulse_failure(self, _print, pulse_cls):
        pulse_cls.side_effect = Exception("no pulse")
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        mon = MEETMIX._RecordingMonitor(session)
        self.assertIsNone(mon._pulse)
        mon.close()

    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_warns_on_profile_change(self, _print, pulse_cls, warn_mock):
        pulse = pulse_cls.return_value
        card_init = mock.Mock()
        card_init.profile_active = mock.Mock()
        card_init.profile_active.name = "headset-head-unit"
        card_changed = mock.Mock()
        card_changed.profile_active = mock.Mock()
        card_changed.profile_active.name = "a2dp-sink"
        server_info = mock.Mock()
        server_info.default_sink_name = "alsa_output.hdmi"
        pulse.server_info.return_value = server_info

        with mock.patch.object(MEETMIX, "find_bt_card", side_effect=[card_init, card_changed]):
            session = MEETMIX.Session()
            session.device_match = "AirPods"
            mon = MEETMIX._RecordingMonitor(session)
            mon.check(session)

        warn_mock.assert_any_call("BT profile changed: headset-head-unit -> a2dp-sink")
        mon.close()

    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_warns_on_default_sink_change(self, _print, pulse_cls, warn_mock):
        pulse = pulse_cls.return_value
        card = mock.Mock()
        card.profile_active = mock.Mock()
        card.profile_active.name = "headset-head-unit"
        info1 = mock.Mock(default_sink_name="alsa_output.hdmi")
        info2 = mock.Mock(default_sink_name="bluez_sink.aa_bb")
        pulse.server_info.side_effect = [info1, info2]

        with mock.patch.object(MEETMIX, "find_bt_card", return_value=card):
            session = MEETMIX.Session()
            session.device_match = "AirPods"
            mon = MEETMIX._RecordingMonitor(session)
            mon.check(session)

        warn_mock.assert_any_call("Default sink changed: alsa_output.hdmi -> bluez_sink.aa_bb")
        mon.close()

    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_warns_on_capture_loopback_exit(self, _print, pulse_cls, warn_mock):
        pulse = pulse_cls.return_value
        server_info = mock.Mock(default_sink_name="alsa_output.hdmi")
        pulse.server_info.return_value = server_info
        card = mock.Mock()
        card.profile_active = mock.Mock()
        card.profile_active.name = "headset-head-unit"

        with mock.patch.object(MEETMIX, "find_bt_card", return_value=card):
            session = MEETMIX.Session()
            session.device_match = "AirPods"
            session.capture_loopback = mock.Mock()
            session.capture_loopback.poll.return_value = 1
            session.capture_loopback.returncode = 1
            mon = MEETMIX._RecordingMonitor(session)
            mon.check(session)

        warn_mock.assert_any_call("pw-loopback (capture) exited unexpectedly (code 1)")
        mon.close()

    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_loopback_warning_only_once(self, _print, pulse_cls, warn_mock):
        pulse = pulse_cls.return_value
        server_info = mock.Mock(default_sink_name="alsa_output.hdmi")
        pulse.server_info.return_value = server_info
        card = mock.Mock()
        card.profile_active = mock.Mock()
        card.profile_active.name = "headset-head-unit"

        with mock.patch.object(MEETMIX, "find_bt_card", return_value=card):
            session = MEETMIX.Session()
            session.device_match = "AirPods"
            session.capture_loopback = mock.Mock()
            session.capture_loopback.poll.return_value = 1
            session.capture_loopback.returncode = 1
            mon = MEETMIX._RecordingMonitor(session)
            mon.check(session)
            mon.check(session)

        loopback_warns = [c for c in warn_mock.call_args_list if "pw-loopback" in str(c)]
        self.assertEqual(1, len(loopback_warns))
        mon.close()

    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_warns_on_forwarding_loopback_exit(self, _print, pulse_cls, warn_mock):
        pulse = pulse_cls.return_value
        server_info = mock.Mock(default_sink_name="alsa_output.hdmi")
        pulse.server_info.return_value = server_info
        card = mock.Mock()
        card.profile_active = mock.Mock()
        card.profile_active.name = "headset-head-unit"

        with mock.patch.object(MEETMIX, "find_bt_card", return_value=card):
            session = MEETMIX.Session()
            session.device_match = "AirPods"
            session.capture_loopback = mock.Mock()
            session.capture_loopback.poll.return_value = None
            session.forwarding_loopback = mock.Mock()
            session.forwarding_loopback.poll.return_value = 1
            session.forwarding_loopback.returncode = 1
            mon = MEETMIX._RecordingMonitor(session)
            mon.check(session)

        warn_mock.assert_any_call("pw-loopback (forwarding) exited unexpectedly (code 1)")
        mon.close()

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_no_warnings_when_stable(self, _print, pulse_cls):
        pulse = pulse_cls.return_value
        card = mock.Mock()
        card.profile_active = mock.Mock()
        card.profile_active.name = "headset-head-unit"
        server_info = mock.Mock(default_sink_name="alsa_output.hdmi")
        pulse.server_info.return_value = server_info

        with mock.patch.object(MEETMIX, "find_bt_card", return_value=card), \
             mock.patch.object(MEETMIX, "warn") as warn_mock:
            session = MEETMIX.Session()
            session.device_match = "AirPods"
            session.capture_loopback = mock.Mock()
            session.capture_loopback.poll.return_value = None
            session.forwarding_loopback = mock.Mock()
            session.forwarding_loopback.poll.return_value = None
            mon = MEETMIX._RecordingMonitor(session)
            mon.check(session)

        warn_mock.assert_not_called()
        mon.close()

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_close_is_idempotent(self, _print, pulse_cls):
        pulse = pulse_cls.return_value
        server_info = mock.Mock(default_sink_name="sink")
        pulse.server_info.return_value = server_info
        with mock.patch.object(MEETMIX, "find_bt_card", return_value=None):
            session = MEETMIX.Session()
            mon = MEETMIX._RecordingMonitor(session)
        mon.close()
        mon.close()
        pulse.close.assert_called_once()


class RestoreDefaultSinkTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_restores_saved_sink(self, pulse_cls):
        pulse = pulse_cls.return_value
        MEETMIX.restore_default_sink("alsa_output.hdmi")
        pulse.sink_default_set.assert_called_once_with("alsa_output.hdmi")
        pulse.close.assert_called_once()

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_noop_when_no_saved_sink(self, pulse_cls):
        MEETMIX.restore_default_sink(None)
        pulse_cls.assert_not_called()

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_handles_pulse_error(self, pulse_cls):
        pulse = pulse_cls.return_value
        pulse.sink_default_set.side_effect = MEETMIX.pulsectl.PulseError("gone")
        MEETMIX.restore_default_sink("alsa_output.hdmi")
        pulse.close.assert_called_once()


class RestoreBtProfileTests(unittest.TestCase):
    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "find_bt_card")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.time, "monotonic", side_effect=[0, 0])
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_restores_profile_and_waits_for_default_change(
        self, _print, pulse_cls, _mono, _sleep, find_card, restore_default
    ):
        profile = mock.Mock()
        profile.name = "headset-head-unit"
        card = mock.Mock(profile_active=profile)
        find_card.return_value = card
        pulse = pulse_cls.return_value
        # First server_info (pre_default), then second (changed)
        pulse.server_info.side_effect = [
            mock.Mock(default_sink_name="alsa_output.hdmi"),
            mock.Mock(default_sink_name="bluez_output.abc"),
        ]
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        session.original_card_profile = "a2dp-sink"
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.restore_bt_profile(session)
        pulse.card_profile_set.assert_called_once_with(card, "a2dp-sink")
        restore_default.assert_called_once_with("alsa_output.hdmi")
        pulse.close.assert_called_once()

    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "find_bt_card")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.time, "monotonic", side_effect=[0, 0, 0])
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_polls_until_default_changes(
        self, _print, pulse_cls, _mono, _sleep, find_card, restore_default
    ):
        profile = mock.Mock()
        profile.name = "headset-head-unit"
        card = mock.Mock(profile_active=profile)
        find_card.return_value = card
        pulse = pulse_cls.return_value
        # pre_default, first poll (same), second poll (changed)
        pulse.server_info.side_effect = [
            mock.Mock(default_sink_name="alsa_output.hdmi"),
            mock.Mock(default_sink_name="alsa_output.hdmi"),
            mock.Mock(default_sink_name="bluez_output.abc"),
        ]
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        session.original_card_profile = "a2dp-sink"
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.restore_bt_profile(session)
        restore_default.assert_called_once_with("alsa_output.hdmi")
        _sleep.assert_called_once_with(0.24)

    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "find_bt_card")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_skips_poll_when_default_already_changed(
        self, _print, pulse_cls, _sleep, find_card, restore_default
    ):
        profile = mock.Mock()
        profile.name = "headset-head-unit"
        card = mock.Mock(profile_active=profile)
        find_card.return_value = card
        pulse = pulse_cls.return_value
        # WirePlumber already changed default before our snapshot
        pulse.server_info.return_value = mock.Mock(
            default_sink_name="bluez_output.abc"
        )
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        session.original_card_profile = "a2dp-sink"
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.restore_bt_profile(session)
        pulse.card_profile_set.assert_called_once()
        restore_default.assert_called_once_with("alsa_output.hdmi")
        _sleep.assert_not_called()

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_noop_when_no_profile_saved(self, pulse_cls):
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        MEETMIX.restore_bt_profile(session)
        pulse_cls.assert_not_called()

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_noop_when_no_device_match(self, pulse_cls):
        session = MEETMIX.Session()
        session.original_card_profile = "a2dp-sink"
        MEETMIX.restore_bt_profile(session)
        pulse_cls.assert_not_called()

    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "find_bt_card")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_restores_default_when_profile_already_correct(
        self, _print, pulse_cls, find_card, restore_default
    ):
        profile = mock.Mock()
        profile.name = "a2dp-sink"
        card = mock.Mock(profile_active=profile)
        find_card.return_value = card
        pulse = pulse_cls.return_value
        # Default already changed to BT (WirePlumber acted) -> skip poll
        pulse.server_info.return_value = mock.Mock(
            default_sink_name="bluez_output.abc"
        )
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        session.original_card_profile = "a2dp-sink"
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.restore_bt_profile(session)
        pulse.card_profile_set.assert_not_called()
        restore_default.assert_called_once_with("alsa_output.hdmi")
        pulse.close.assert_called_once()

    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "find_bt_card")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.time, "monotonic", side_effect=[0, 0])
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_polls_when_profile_correct_but_default_not_settled(
        self, _print, pulse_cls, _mono, _sleep, find_card, restore_default
    ):
        profile = mock.Mock()
        profile.name = "a2dp-sink"
        card = mock.Mock(profile_active=profile)
        find_card.return_value = card
        pulse = pulse_cls.return_value
        # Default still matches original (WirePlumber hasn't acted yet)
        # Then changes on second poll
        pulse.server_info.side_effect = [
            mock.Mock(default_sink_name="alsa_output.hdmi"),
            mock.Mock(default_sink_name="bluez_output.abc"),
        ]
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        session.original_card_profile = "a2dp-sink"
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.restore_bt_profile(session)
        pulse.card_profile_set.assert_not_called()
        restore_default.assert_called_once_with("alsa_output.hdmi")
        pulse.close.assert_called_once()

    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "find_bt_card")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.time, "monotonic", side_effect=[0, 6])
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_timeout_restores_default_anyway(
        self, _print, pulse_cls, _mono, _sleep, find_card, restore_default
    ):
        profile = mock.Mock()
        profile.name = "headset-head-unit"
        card = mock.Mock(profile_active=profile)
        find_card.return_value = card
        pulse = pulse_cls.return_value
        pulse.server_info.return_value = mock.Mock(default_sink_name="alsa_output.hdmi")
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        session.original_card_profile = "a2dp-sink"
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.restore_bt_profile(session)
        pulse.card_profile_set.assert_called_once()
        restore_default.assert_called_once_with("alsa_output.hdmi")
        pulse.close.assert_called_once()

    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX, "find_bt_card")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_restores_default_on_exception(
        self, _print, pulse_cls, find_card, restore_default
    ):
        profile = mock.Mock()
        profile.name = "headset-head-unit"
        card = mock.Mock(profile_active=profile)
        find_card.return_value = card
        pulse = pulse_cls.return_value
        pulse.card_profile_set.side_effect = Exception("pulse error")
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        session.original_card_profile = "a2dp-sink"
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.restore_bt_profile(session)
        restore_default.assert_called_once_with("alsa_output.hdmi")
        pulse.close.assert_called_once()

    @mock.patch.object(MEETMIX, "restore_default_sink")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_restores_default_on_pulse_connect_failure(
        self, pulse_cls, restore_default
    ):
        pulse_cls.side_effect = Exception("connection refused")
        session = MEETMIX.Session()
        session.device_match = "AirPods"
        session.original_card_profile = "a2dp-sink"
        session.original_default_sink = "alsa_output.hdmi"
        MEETMIX.restore_bt_profile(session)
        restore_default.assert_called_once_with("alsa_output.hdmi")


class RunDevicesTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_lists_sources_and_sinks(self, print_mock, pulse_cls):
        pulse = pulse_cls.return_value
        pulse.source_list.return_value = [
            make_source("bluez_input.abc", "AirPods Pro"),
        ]
        pulse.sink_list.return_value = [
            make_sink("bluez_output.abc", "AirPods Pro", "bluez_output.abc.monitor"),
        ]
        args = mock.Mock(device_match=None)
        MEETMIX.run_devices(args)
        output = " ".join(call.args[0] if call.args else "" for call in print_mock.call_args_list)
        self.assertIn("bluez_input.abc", output)
        self.assertIn("bluez_output.abc", output)

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_highlights_matching_devices(self, print_mock, pulse_cls):
        pulse = pulse_cls.return_value
        pulse.source_list.return_value = [
            make_source("bluez_input.abc", "AirPods Pro"),
            make_source("alsa_input.usb", "USB Mic"),
        ]
        pulse.sink_list.return_value = [
            make_sink("bluez_output.abc", "AirPods Pro", "bluez_output.abc.monitor"),
        ]
        args = mock.Mock(device_match="AirPods")
        MEETMIX.run_devices(args)
        lines = [call.args[0] if call.args else "" for call in print_mock.call_args_list]
        airpods_lines = [line for line in lines if "AirPods" in line]
        self.assertTrue(any("*" in line for line in airpods_lines))
        usb_lines = [line for line in lines if "USB Mic" in line]
        self.assertTrue(all("*" not in line for line in usb_lines))


class RunRecordTests(unittest.TestCase):
    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX, "require_commands")
    def test_exits_without_device_match(self, _require, _warn):
        args = mock.Mock(device_match=None)
        with self.assertRaises(SystemExit):
            MEETMIX.run_record(args)

    @mock.patch.object(MEETMIX, "process_recording")
    @mock.patch.object(MEETMIX, "cleanup_session")
    @mock.patch.object(MEETMIX, "run_recording")
    @mock.patch.object(MEETMIX, "setup_pipeline")
    @mock.patch.object(MEETMIX, "setup_logging", return_value="/tmp/test.log")
    @mock.patch.object(MEETMIX, "require_commands")
    def test_calls_pipeline_stages_in_order(
        self, _require, _setup_log, setup_pipe, run_rec, cleanup, process
    ):
        args = mock.Mock(device_match="AirPods", keep_recording=False)
        MEETMIX.run_record(args)
        setup_pipe.assert_called_once()
        run_rec.assert_called_once()
        cleanup.assert_called_once()
        process.assert_called_once()

    @mock.patch.object(MEETMIX, "process_recording")
    @mock.patch.object(MEETMIX, "cleanup_session")
    @mock.patch.object(MEETMIX, "run_recording", side_effect=Exception("boom"))
    @mock.patch.object(MEETMIX, "setup_pipeline")
    @mock.patch.object(MEETMIX, "setup_logging", return_value="/tmp/test.log")
    @mock.patch.object(MEETMIX, "require_commands")
    def test_cleanup_runs_on_recording_error(
        self, _require, _setup_log, _setup_pipe, _run_rec, cleanup, process
    ):
        args = mock.Mock(device_match="AirPods", keep_recording=False)
        with self.assertRaises(Exception):
            MEETMIX.run_record(args)
        cleanup.assert_called_once()
        process.assert_not_called()

    @mock.patch.object(MEETMIX, "process_recording")
    @mock.patch.object(MEETMIX, "cleanup_session")
    @mock.patch.object(MEETMIX, "run_recording")
    @mock.patch.object(MEETMIX, "setup_pipeline", side_effect=Exception("setup failed"))
    @mock.patch.object(MEETMIX, "setup_logging", return_value="/tmp/test.log")
    @mock.patch.object(MEETMIX, "require_commands")
    def test_cleanup_runs_on_setup_error(
        self, _require, _setup_log, _setup_pipe, _run_rec, cleanup, process
    ):
        args = mock.Mock(device_match="AirPods", keep_recording=False)
        with self.assertRaises(Exception):
            MEETMIX.run_record(args)
        cleanup.assert_called_once()
        process.assert_not_called()

    @mock.patch.object(MEETMIX, "process_recording")
    @mock.patch.object(MEETMIX, "cleanup_session")
    @mock.patch.object(MEETMIX, "run_recording")
    @mock.patch.object(MEETMIX, "setup_pipeline")
    @mock.patch.object(MEETMIX, "setup_logging", return_value="/tmp/test.log")
    @mock.patch.object(MEETMIX, "require_commands")
    def test_skips_recording_when_stop_requested_during_setup(
        self, _require, _setup_log, setup_pipe, run_rec, cleanup, process
    ):
        def set_stop_flag(session, device_match):
            MEETMIX._stop_requested = True

        setup_pipe.side_effect = set_stop_flag
        args = mock.Mock(device_match="AirPods", keep_recording=False)
        try:
            MEETMIX.run_record(args)
        finally:
            MEETMIX._stop_requested = False
        setup_pipe.assert_called_once()
        run_rec.assert_not_called()
        cleanup.assert_called_once()
        process.assert_not_called()


_REAL_RECORDING_MONITOR = MEETMIX._RecordingMonitor


def _noop_monitor():
    """Return a mock _RecordingMonitor that does nothing."""
    return mock.Mock(spec=_REAL_RECORDING_MONITOR)


class RunRecordingTests(unittest.TestCase):
    def setUp(self):
        MEETMIX._stop_requested = False

    def tearDown(self):
        MEETMIX._stop_requested = False

    @mock.patch.object(MEETMIX, "_RecordingMonitor", side_effect=lambda s: _noop_monitor())
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    @mock.patch("builtins.print")
    def test_starts_pw_record_with_correct_args(self, _print, popen_mock, _sleep, _mon):
        proc = mock.Mock(pid=1234, returncode=0)
        proc.poll.side_effect = [None, 0]
        popen_mock.return_value = proc
        session = MEETMIX.Session()
        session.wav_path = "/tmp/test.wav"
        MEETMIX.run_recording(session)
        cmd = popen_mock.call_args.args[0]
        self.assertEqual("pw-record", cmd[0])
        self.assertIn("--target=meetmix_capture", cmd)
        self.assertIn("stream.capture.sink=true", cmd)

    @mock.patch.object(MEETMIX, "_RecordingMonitor", side_effect=lambda s: _noop_monitor())
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    @mock.patch("builtins.print")
    def test_stops_on_flag(self, _print, popen_mock, _mon):
        proc = mock.Mock(pid=1234, returncode=0)
        proc.poll.return_value = None
        proc.wait.return_value = None
        popen_mock.return_value = proc
        MEETMIX._stop_requested = True
        session = MEETMIX.Session()
        session.wav_path = "/tmp/test.wav"
        MEETMIX.run_recording(session)
        proc.wait.assert_called_once_with(timeout=3)

    @mock.patch.object(MEETMIX, "_RecordingMonitor", side_effect=lambda s: _noop_monitor())
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    @mock.patch("builtins.print")
    def test_wav_path_with_spaces(self, _print, popen_mock, _sleep, _mon):
        proc = mock.Mock(pid=1234, returncode=0)
        proc.poll.side_effect = [None, 0]
        popen_mock.return_value = proc
        session = MEETMIX.Session()
        session.wav_path = "/tmp/my meetings/session 1.wav"
        MEETMIX.run_recording(session)
        cmd = popen_mock.call_args.args[0]
        self.assertIn("/tmp/my meetings/session 1.wav", cmd)

    @mock.patch.object(MEETMIX, "_RecordingMonitor", side_effect=lambda s: _noop_monitor())
    @mock.patch.object(MEETMIX, "warn")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    @mock.patch("builtins.print")
    def test_warns_on_nonzero_exit(self, _print, popen_mock, _sleep, warn_mock, _mon):
        proc = mock.Mock(pid=1234, returncode=1)
        proc.poll.side_effect = [None, 1]
        popen_mock.return_value = proc
        session = MEETMIX.Session()
        session.wav_path = "/tmp/test.wav"
        MEETMIX.run_recording(session)
        warn_mock.assert_called_once()
        self.assertIn("unexpectedly", warn_mock.call_args.args[0])


class SessionTests(unittest.TestCase):
    def test_defaults(self):
        session = MEETMIX.Session()
        self.assertIsNone(session.bt_sink_name)
        self.assertIsNone(session.capture_loopback)
        self.assertEqual("meetmix_capture", session.capture_target)
        self.assertIsNone(session.device_match)
        self.assertIsNone(session.forwarding_loopback)
        self.assertFalse(session.keep_recording)
        self.assertIsNone(session.log_path)
        self.assertEqual({}, session.modules)
        self.assertIsNone(session.original_bt_sink_volume)
        self.assertIsNone(session.original_card_profile)
        self.assertIsNone(session.original_default_sink)
        self.assertIsNone(session.record_proc)
        self.assertIsNone(session.sco_warmup_proc)
        self.assertIsNone(session.wav_path)


class SetupLoggingTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_log_dir = MEETMIX.LOG_DIR
        self._orig_log_file = MEETMIX._log_file
        MEETMIX.LOG_DIR = self._tmpdir

    def tearDown(self):
        if MEETMIX._log_file and MEETMIX._log_file != self._orig_log_file:
            MEETMIX._log_file.close()
        MEETMIX._log_file = self._orig_log_file
        MEETMIX.LOG_DIR = self._orig_log_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @mock.patch("builtins.print")
    def test_creates_log_file(self, _print):
        path = MEETMIX.setup_logging("20260411-120000")
        self.assertTrue(os.path.exists(path))
        self.assertIn("meetmix-20260411-120000.log", path)

    @mock.patch("builtins.print")
    def test_creates_log_dir_if_missing(self, _print):
        subdir = os.path.join(self._tmpdir, "subdir")
        MEETMIX.LOG_DIR = subdir
        MEETMIX.setup_logging("20260411-120000")
        self.assertTrue(os.path.isdir(subdir))

    @mock.patch("builtins.print")
    def test_log_file_is_writable(self, _print):
        MEETMIX.setup_logging("20260411-120000")
        MEETMIX._log_file.write("test\n")
        MEETMIX._log_file.flush()

    @mock.patch("builtins.print")
    def test_close_log_file(self, _print):
        MEETMIX.setup_logging("20260411-120000")
        self.assertIsNotNone(MEETMIX._log_file)
        MEETMIX._close_log_file()
        self.assertIsNone(MEETMIX._log_file)

    def test_close_log_file_noop_when_none(self):
        orig = MEETMIX._log_file
        MEETMIX._log_file = None
        try:
            MEETMIX._close_log_file()
        finally:
            MEETMIX._log_file = orig


def _fake_create_capture(pulse, mic, bt, modules):
    modules.update({"capture_sink": 101, "mic_loopback": 102})


class SetupPipelineTests(unittest.TestCase):
    @mock.patch.object(MEETMIX, "verify_forwarding_link")
    @mock.patch.object(MEETMIX, "warm_up_sco")
    @mock.patch.object(MEETMIX, "wait_for_forwarding_link")
    @mock.patch.object(MEETMIX, "save_sink_volume", return_value=0.55)
    @mock.patch.object(MEETMIX, "disable_wpctl_autoswitch")
    @mock.patch.object(MEETMIX, "start_forwarding_loopback", return_value=mock.Mock(pid=888))
    @mock.patch.object(MEETMIX, "start_capture_loopback", return_value=mock.Mock(pid=999))
    @mock.patch.object(MEETMIX, "create_capture_devices", side_effect=_fake_create_capture)
    @mock.patch.object(MEETMIX, "prepare_source")
    @mock.patch.object(MEETMIX, "get_node_serial", return_value="99999")
    @mock.patch.object(MEETMIX, "find_bt_sink", return_value="bluez_output.abc")
    @mock.patch.object(MEETMIX, "find_mic_source", return_value="bluez_input.abc")
    @mock.patch.object(MEETMIX, "ensure_headset_profile")
    @mock.patch.object(MEETMIX, "move_sink_inputs")
    @mock.patch.object(MEETMIX, "create_combined_sink", return_value=100)
    @mock.patch.object(MEETMIX, "cleanup_orphans")
    @mock.patch.object(MEETMIX, "find_bt_card")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_sets_up_full_pipeline(
        self,
        _print,
        pulse_cls,
        find_bt_card,
        _cleanup,
        _create_combined,
        move_inputs,
        _ensure,
        _find_mic,
        _find_sink,
        _get_serial,
        _prepare,
        _create_capture,
        _start_lb,
        _start_fwd,
        wpctl_disable,
        save_vol,
        _wait_fwd,
        _warmup,
        verify_fwd,
    ):
        pulse = pulse_cls.return_value
        pulse.server_info.return_value = mock.Mock(default_sink_name="alsa_output.hdmi")
        profile = mock.Mock()
        profile.name = "a2dp-sink"
        find_bt_card.return_value = mock.Mock(profile_active=profile)
        session = MEETMIX.Session()
        MEETMIX.setup_pipeline(session, "AirPods")
        self.assertEqual("AirPods", session.device_match)
        self.assertEqual("alsa_output.hdmi", session.original_default_sink)
        self.assertEqual("a2dp-sink", session.original_card_profile)
        self.assertEqual("bluez_output.abc", session.bt_sink_name)
        self.assertEqual(0.55, session.original_bt_sink_volume)
        self.assertEqual(100, session.modules["combined_sink"])
        self.assertIn("capture_sink", session.modules)
        self.assertIn("mic_loopback", session.modules)
        pulse.sink_default_set.assert_called_once_with("meetmix_combined")
        move_inputs.assert_called_once_with(pulse, "meetmix_combined")
        self.assertIsNotNone(session.capture_loopback)
        self.assertIsNotNone(session.forwarding_loopback)
        wpctl_disable.assert_called_once()
        save_vol.assert_called_once_with(pulse, "alsa_output.hdmi")
        verify_fwd.assert_called_once()
        pulse.close.assert_called_once()

    @mock.patch.object(MEETMIX, "disable_wpctl_autoswitch")
    @mock.patch.object(MEETMIX, "create_capture_devices")
    @mock.patch.object(MEETMIX, "find_bt_sink")
    @mock.patch.object(MEETMIX, "find_mic_source")
    @mock.patch.object(MEETMIX, "ensure_headset_profile")
    @mock.patch.object(MEETMIX, "move_sink_inputs")
    @mock.patch.object(MEETMIX, "create_combined_sink", return_value=100)
    @mock.patch.object(MEETMIX, "cleanup_orphans")
    @mock.patch.object(MEETMIX, "find_bt_card", return_value=None)
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_aborts_after_profile_switch_on_stop(
        self,
        _print,
        pulse_cls,
        _find_bt_card,
        _cleanup,
        _create_combined,
        _move_inputs,
        ensure_profile,
        find_mic,
        find_sink,
        create_capture,
        _wpctl_disable,
    ):
        def set_stop_flag(pulse, device_match):
            MEETMIX._stop_requested = True

        ensure_profile.side_effect = set_stop_flag
        pulse = pulse_cls.return_value
        pulse.server_info.return_value = mock.Mock(default_sink_name="alsa_output.hdmi")
        MEETMIX._stop_requested = False
        try:
            session = MEETMIX.Session()
            MEETMIX.setup_pipeline(session, "AirPods")
        finally:
            MEETMIX._stop_requested = False
        find_mic.assert_not_called()
        find_sink.assert_not_called()
        create_capture.assert_not_called()
        pulse.close.assert_called_once()

    @mock.patch.object(MEETMIX, "verify_forwarding_link")
    @mock.patch.object(MEETMIX, "warm_up_sco")
    @mock.patch.object(MEETMIX, "wait_for_forwarding_link")
    @mock.patch.object(MEETMIX, "save_sink_volume", return_value=None)
    @mock.patch.object(MEETMIX, "disable_wpctl_autoswitch")
    @mock.patch.object(MEETMIX, "start_forwarding_loopback", return_value=mock.Mock(pid=888))
    @mock.patch.object(MEETMIX, "start_capture_loopback", return_value=mock.Mock(pid=999))
    @mock.patch.object(MEETMIX, "create_capture_devices", side_effect=_fake_create_capture)
    @mock.patch.object(MEETMIX, "prepare_source")
    @mock.patch.object(MEETMIX, "get_node_serial", return_value="99999")
    @mock.patch.object(MEETMIX, "find_bt_sink", return_value="bluez_output.abc")
    @mock.patch.object(MEETMIX, "find_mic_source", return_value="bluez_input.abc")
    @mock.patch.object(MEETMIX, "ensure_headset_profile")
    @mock.patch.object(MEETMIX, "move_sink_inputs")
    @mock.patch.object(MEETMIX, "create_combined_sink", return_value=100)
    @mock.patch.object(MEETMIX, "cleanup_orphans")
    @mock.patch.object(MEETMIX, "find_bt_card", return_value=None)
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch("builtins.print")
    def test_no_bt_card_sets_profile_to_none(
        self,
        _print,
        pulse_cls,
        find_bt_card,
        _cleanup,
        _create_combined,
        _move_inputs,
        _ensure,
        _find_mic,
        _find_sink,
        _get_serial,
        _prepare,
        _create_capture,
        _start_lb,
        _start_fwd,
        _wpctl_disable,
        _save_vol,
        _wait_fwd,
        _warmup,
        _verify_fwd,
    ):
        pulse = pulse_cls.return_value
        pulse.server_info.return_value = mock.Mock(default_sink_name="alsa_output.hdmi")
        session = MEETMIX.Session()
        MEETMIX.setup_pipeline(session, "AirPods")
        self.assertIsNone(session.original_card_profile)


class StartCaptureLoopbackTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    @mock.patch("builtins.print")
    def test_spawns_pw_loopback(self, _print, popen_mock):
        proc = mock.Mock(pid=1234)
        popen_mock.return_value = proc
        result = MEETMIX.start_capture_loopback()
        self.assertEqual(proc, result)
        cmd = popen_mock.call_args.args[0]
        self.assertEqual("pw-loopback", cmd[0])
        self.assertIn("-C", cmd)
        self.assertIn("-P", cmd)
        self.assertIn("stream.capture.sink=true", " ".join(cmd))


class WarmUpScoTests(unittest.TestCase):
    @mock.patch.object(MEETMIX, "_stop_requested", False)
    @mock.patch.object(MEETMIX, "log")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    def test_starts_pw_play_and_terminates(self, popen_mock, sleep_mock, _log):
        proc = mock.Mock()
        proc.pid = 999
        popen_mock.return_value = proc
        session = MEETMIX.Session()
        MEETMIX.warm_up_sco(session)
        popen_mock.assert_called_once()
        cmd = popen_mock.call_args[0][0]
        self.assertEqual("pw-play", cmd[0])
        self.assertIn("/dev/zero", cmd)
        self.assertTrue(sleep_mock.call_count > 0)
        proc.terminate.assert_called_once()
        self.assertIsNone(session.sco_warmup_proc)

    @mock.patch.object(MEETMIX, "_stop_requested", False)
    @mock.patch.object(MEETMIX, "log")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    def test_kills_after_terminate_timeout(self, popen_mock, _sleep, _log):
        proc = mock.Mock()
        proc.pid = 999
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 2), None]
        popen_mock.return_value = proc
        session = MEETMIX.Session()
        MEETMIX.warm_up_sco(session)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertIsNone(session.sco_warmup_proc)

    @mock.patch.object(MEETMIX, "_stop_requested", False)
    @mock.patch.object(MEETMIX, "log")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    def test_sets_session_proc_during_warmup(self, popen_mock, sleep_mock, _log):
        proc = mock.Mock()
        proc.pid = 999
        popen_mock.return_value = proc
        session = MEETMIX.Session()
        captured = []
        sleep_mock.side_effect = lambda _: captured.append(session.sco_warmup_proc)
        MEETMIX.warm_up_sco(session)
        self.assertIn(proc, captured)

    @mock.patch.object(MEETMIX, "log")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.subprocess, "Popen")
    def test_skips_when_stop_requested(self, popen_mock, sleep_mock, _log):
        MEETMIX._stop_requested = True
        try:
            session = MEETMIX.Session()
            MEETMIX.warm_up_sco(session)
            popen_mock.assert_not_called()
            sleep_mock.assert_not_called()
        finally:
            MEETMIX._stop_requested = False


class WaitForForwardingLinkTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.subprocess, "run")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.time, "monotonic", side_effect=[0, 0.1, 0.2])
    @mock.patch("builtins.print")
    def test_returns_immediately_when_linked(self, _print, _mono, _sleep, run_mock):
        session = MEETMIX.Session()
        session.bt_sink_name = "bluez_output.abc"
        session.forwarding_loopback = mock.Mock(pid=555, poll=mock.Mock(return_value=None))
        run_mock.return_value = mock.Mock(
            stdout=(
                "pw-loopback-555:output_FL:\n"
                "   |-> bluez_output.abc:playback_FL\n"
            ),
        )
        MEETMIX.wait_for_forwarding_link(session, timeout=5)
        _sleep.assert_not_called()

    @mock.patch.object(MEETMIX.subprocess, "run")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.time, "monotonic", side_effect=[0, 0.2, 0.5, 0.7])
    @mock.patch("builtins.print")
    def test_polls_until_linked(self, _print, _mono, _sleep, run_mock):
        session = MEETMIX.Session()
        session.bt_sink_name = "bluez_output.abc"
        session.forwarding_loopback = mock.Mock(pid=555, poll=mock.Mock(return_value=None))
        run_mock.side_effect = [
            mock.Mock(stdout=""),
            mock.Mock(stdout=(
                "pw-loopback-555:output_FL:\n"
                "   |-> bluez_output.abc:playback_FL\n"
            )),
        ]
        MEETMIX.wait_for_forwarding_link(session, timeout=5)
        self.assertEqual(1, _sleep.call_count)

    @mock.patch.object(MEETMIX.subprocess, "run")
    @mock.patch.object(MEETMIX.time, "sleep")
    @mock.patch.object(MEETMIX.time, "monotonic", side_effect=[0, 6])
    @mock.patch("builtins.print")
    def test_warns_on_timeout(self, _print, _mono, _sleep, run_mock):
        session = MEETMIX.Session()
        session.bt_sink_name = "bluez_output.abc"
        session.forwarding_loopback = mock.Mock(pid=555, poll=mock.Mock(return_value=None))
        run_mock.return_value = mock.Mock(stdout="")
        MEETMIX.wait_for_forwarding_link(session, timeout=5)
        _print.assert_called()
        last_msg = _print.call_args.args[0]
        self.assertIn("not linked after", last_msg)

    def test_noop_when_no_forwarding(self):
        session = MEETMIX.Session()
        session.bt_sink_name = "bluez_output.abc"
        session.forwarding_loopback = None
        MEETMIX.wait_for_forwarding_link(session, timeout=5)

    @mock.patch("builtins.print")
    def test_returns_early_when_process_exited(self, _print):
        session = MEETMIX.Session()
        session.bt_sink_name = "bluez_output.abc"
        session.forwarding_loopback = mock.Mock(
            pid=555, poll=mock.Mock(return_value=1), returncode=1,
        )
        MEETMIX.wait_for_forwarding_link(session, timeout=5)
        last_msg = _print.call_args.args[0]
        self.assertIn("exited", last_msg)


class CheckLoopbackLinkedTests(unittest.TestCase):
    def test_detects_forward_link(self):
        output = (
            "pw-loopback-555:output_FL:\n"
            "   |-> bluez_output.abc:playback_FL\n"
            "pw-loopback-555:output_FR:\n"
            "   |-> bluez_output.abc:playback_FR\n"
        )
        self.assertTrue(
            MEETMIX._check_loopback_linked(output, "pw-loopback-555", "bluez_output.abc")
        )

    def test_detects_reverse_link(self):
        output = (
            "bluez_output.abc:playback_FL:\n"
            "   |<- pw-loopback-555:output_FL\n"
        )
        self.assertTrue(
            MEETMIX._check_loopback_linked(output, "pw-loopback-555", "bluez_output.abc")
        )

    def test_no_match_when_unlinked(self):
        output = (
            "pw-loopback-555:output_FL:\n"
            "pw-loopback-555:output_FR:\n"
        )
        self.assertFalse(
            MEETMIX._check_loopback_linked(output, "pw-loopback-555", "bluez_output.abc")
        )

    def test_no_match_wrong_target(self):
        output = (
            "pw-loopback-555:output_FL:\n"
            "   |-> alsa_output.hdmi:playback_FL\n"
        )
        self.assertFalse(
            MEETMIX._check_loopback_linked(output, "pw-loopback-555", "bluez_output.abc")
        )


class StopProcessTests(unittest.TestCase):
    def test_noop_on_none(self):
        MEETMIX.stop_process(None)

    def test_noop_on_already_exited(self):
        proc = mock.Mock()
        proc.poll.return_value = 0
        MEETMIX.stop_process(proc)
        proc.terminate.assert_not_called()

    def test_terminates_running_process(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        MEETMIX.stop_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    def test_kills_after_terminate_timeout(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = [
            subprocess.TimeoutExpired("cmd", 2),
            None,
        ]
        MEETMIX.stop_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_survives_double_timeout(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = [
            subprocess.TimeoutExpired("cmd", 2),
            subprocess.TimeoutExpired("cmd", 2),
        ]
        MEETMIX.stop_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_handles_terminate_race(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.terminate.side_effect = OSError("No such process")
        MEETMIX.stop_process(proc)
        proc.kill.assert_not_called()

    def test_handles_kill_race(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 2)
        proc.kill.side_effect = OSError("No such process")
        MEETMIX.stop_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


class UnloadModulesTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_unloads_all_indices(self, pulse_cls):
        pulse = pulse_cls.return_value
        modules = {"capture_sink": 100, "mic_loopback": 101, "combined_sink": 102}
        MEETMIX.unload_modules(modules)
        self.assertEqual(3, pulse.module_unload.call_count)
        pulse.module_unload.assert_any_call(100)
        pulse.module_unload.assert_any_call(101)
        pulse.module_unload.assert_any_call(102)

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_handles_pulse_error(self, pulse_cls):
        pulse = pulse_cls.return_value
        pulse.module_unload.side_effect = MEETMIX.pulsectl.PulseError("gone")
        modules = {"capture_sink": 100}
        MEETMIX.unload_modules(modules)

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_clears_modules_after_cleanup(self, pulse_cls):
        modules = {"capture_sink": 100, "mic_loopback": 101}
        MEETMIX.unload_modules(modules)
        self.assertEqual({}, modules)

    def test_noop_on_empty_dict(self):
        MEETMIX.unload_modules({})


class PrepareSourceTests(unittest.TestCase):
    @mock.patch.object(MEETMIX, "warn")
    def test_preserves_mute_warns_and_sets_volume(self, warn_mock):
        source = make_source("bluez_input.abc", "AirPods Pro")
        source.mute = True
        source.index = 42
        source.volume = mock.Mock()
        pulse = mock.Mock()
        pulse.source_list.return_value = [source]
        MEETMIX.prepare_source(pulse, "bluez_input.abc")
        pulse.source_mute.assert_not_called()
        pulse.source_volume_set.assert_called_once()
        warn_mock.assert_called_once()
        self.assertIn("preserving mute state", warn_mock.call_args.args[0])

    @mock.patch.object(MEETMIX, "warn")
    def test_preserves_unmuted_and_sets_volume(self, warn_mock):
        source = make_source("bluez_input.abc", "AirPods Pro")
        source.mute = False
        source.index = 42
        source.volume = mock.Mock()
        pulse = mock.Mock()
        pulse.source_list.return_value = [source]
        MEETMIX.prepare_source(pulse, "bluez_input.abc")
        pulse.source_mute.assert_not_called()
        pulse.source_volume_set.assert_called_once()
        warn_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
