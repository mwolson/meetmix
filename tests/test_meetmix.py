import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
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
            f.write("--language=en\n")
        with self.assertRaises(SystemExit):
            MEETMIX.load_conf()

    @mock.patch.object(MEETMIX, "warn")
    def test_load_conf_rejects_malformed_lines(self, _warn):
        with open(MEETMIX.CONF_PATH, "w") as f:
            f.write("garbage\n")
        with self.assertRaises(SystemExit):
            MEETMIX.load_conf()


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
        with mock.patch("sys.argv", ["meetmix", "record", "--language", "en"]):
            args = MEETMIX.parse_args({})
        self.assertEqual(["--language", "en"], args.extra_args)

    def test_extra_args_with_separator(self):
        with mock.patch("sys.argv", ["meetmix", "record", "--", "--language", "en"]):
            args = MEETMIX.parse_args({})
        self.assertEqual(["--language", "en"], args.extra_args)

    def test_extra_args_empty_by_default(self):
        with mock.patch("sys.argv", ["meetmix", "devices"]):
            args = MEETMIX.parse_args({})
        self.assertEqual([], args.extra_args)


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


class FindSpeakerMonitorTests(unittest.TestCase):
    def test_finds_matching_sink_monitor(self):
        pulse = mock.Mock()
        pulse.sink_list.return_value = [
            make_sink("alsa_output.usb", "USB Speakers", "alsa_output.usb.monitor"),
            make_sink("bluez_output.abc", "AirPods Pro", "bluez_output.abc.monitor"),
        ]
        result = MEETMIX.find_speaker_monitor(pulse, "AirPods")
        self.assertEqual("bluez_output.abc.monitor", result)

    @mock.patch.object(MEETMIX, "warn")
    def test_exits_on_no_match(self, _warn):
        pulse = mock.Mock()
        pulse.sink_list.return_value = [
            make_sink("alsa_output.usb", "USB Speakers"),
        ]
        with self.assertRaises(SystemExit):
            MEETMIX.find_speaker_monitor(pulse, "AirPods")

    @mock.patch.object(MEETMIX, "warn")
    def test_exits_on_multiple_matches(self, _warn):
        pulse = mock.Mock()
        pulse.sink_list.return_value = [
            make_sink("bluez_output.abc", "AirPods Pro"),
            make_sink("bluez_output.def", "AirPods Max"),
        ]
        with self.assertRaises(SystemExit):
            MEETMIX.find_speaker_monitor(pulse, "AirPods")


class CreateVirtualDevicesTests(unittest.TestCase):
    def setUp(self):
        self.pulse = mock.Mock()
        self.pulse.module_load.side_effect = [100, 101, 102]

    @mock.patch("builtins.print")
    def test_loads_three_modules(self, _print):
        indices = MEETMIX.create_virtual_devices(self.pulse, "mic_src", "spk.monitor")
        self.assertEqual(3, self.pulse.module_load.call_count)
        self.assertEqual([100, 101, 102], indices)

    @mock.patch("builtins.print")
    def test_null_sink_args(self, _print):
        MEETMIX.create_virtual_devices(self.pulse, "mic_src", "spk.monitor")
        call = self.pulse.module_load.call_args_list[0]
        self.assertEqual("module-null-sink", call.args[0])
        self.assertIn("sink_name=meetmix_combined", call.args[1])

    @mock.patch("builtins.print")
    def test_loopback_mic_args(self, _print):
        MEETMIX.create_virtual_devices(self.pulse, "mic_src", "spk.monitor")
        call = self.pulse.module_load.call_args_list[1]
        self.assertEqual("module-loopback", call.args[0])
        self.assertIn("source=mic_src", call.args[1])
        self.assertIn("sink=meetmix_combined", call.args[1])

    @mock.patch("builtins.print")
    def test_loopback_speaker_args(self, _print):
        MEETMIX.create_virtual_devices(self.pulse, "mic_src", "spk.monitor")
        call = self.pulse.module_load.call_args_list[2]
        self.assertEqual("module-loopback", call.args[0])
        self.assertIn("source=spk.monitor", call.args[1])
        self.assertIn("sink=meetmix_combined", call.args[1])


class CleanupModulesTests(unittest.TestCase):
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_unloads_all_indices(self, pulse_cls):
        pulse = pulse_cls.return_value
        indices = [100, 101, 102]
        MEETMIX.cleanup_modules(indices)
        self.assertEqual(3, pulse.module_unload.call_count)
        pulse.module_unload.assert_any_call(102)
        pulse.module_unload.assert_any_call(101)
        pulse.module_unload.assert_any_call(100)

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_handles_pulse_error(self, pulse_cls):
        pulse = pulse_cls.return_value
        pulse.module_unload.side_effect = MEETMIX.pulsectl.PulseError("gone")
        indices = [100]
        MEETMIX.cleanup_modules(indices)

    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    def test_clears_indices_after_cleanup(self, pulse_cls):
        indices = [100, 101]
        MEETMIX.cleanup_modules(indices)
        self.assertEqual([], indices)

    def test_noop_on_empty_list(self):
        MEETMIX.cleanup_modules([])


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


class RunRecordTests(unittest.TestCase):
    @mock.patch.object(MEETMIX, "warn")
    def test_exits_without_device_match(self, _warn):
        args = mock.Mock(device_match=None, extra_args=[])
        with self.assertRaises(SystemExit):
            MEETMIX.run_record(args)

    @mock.patch.object(MEETMIX.os, "unlink")
    @mock.patch.object(MEETMIX.os.path, "getsize", return_value=50000)
    @mock.patch.object(MEETMIX.os.path, "exists", return_value=True)
    @mock.patch.object(MEETMIX, "setup_cleanup")
    @mock.patch.object(MEETMIX, "create_virtual_devices", return_value=[100, 101, 102])
    @mock.patch.object(MEETMIX, "find_speaker_monitor", return_value="bluez_output.abc.monitor")
    @mock.patch.object(MEETMIX, "find_mic_source", return_value="bluez_input.abc")
    @mock.patch.object(MEETMIX, "cleanup_orphans")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch.object(MEETMIX.subprocess, "run", return_value=mock.Mock(returncode=0))
    @mock.patch.object(MEETMIX.shutil, "which", return_value="/usr/bin/minutes")
    @mock.patch("builtins.print")
    def test_runs_parec_then_minutes_process(
        self,
        _print,
        _which,
        run_mock,
        _pulse_cls,
        _cleanup,
        _find_mic,
        _find_spk,
        _create,
        _setup,
        _exists,
        _getsize,
        _unlink,
    ):
        args = mock.Mock(device_match="AirPods", extra_args=[])
        with self.assertRaises(SystemExit) as ctx:
            MEETMIX.run_record(args)
        self.assertEqual(0, ctx.exception.code)
        self.assertEqual(2, run_mock.call_count)
        parec_cmd = run_mock.call_args_list[0].args[0]
        self.assertEqual("parec", parec_cmd[0])
        self.assertIn("--device=meetmix_combined.monitor", parec_cmd)
        self.assertIn("--file-format=wav", parec_cmd)
        minutes_cmd = run_mock.call_args_list[1].args[0]
        self.assertEqual("minutes", minutes_cmd[0])
        self.assertEqual("process", minutes_cmd[1])
        self.assertIn("--content-type", minutes_cmd)
        self.assertIn("meeting", minutes_cmd)

    @mock.patch.object(MEETMIX.os, "unlink")
    @mock.patch.object(MEETMIX.os.path, "getsize", return_value=50000)
    @mock.patch.object(MEETMIX.os.path, "exists", return_value=True)
    @mock.patch.object(MEETMIX, "setup_cleanup")
    @mock.patch.object(MEETMIX, "create_virtual_devices", return_value=[100, 101, 102])
    @mock.patch.object(MEETMIX, "find_speaker_monitor", return_value="bluez_output.abc.monitor")
    @mock.patch.object(MEETMIX, "find_mic_source", return_value="bluez_input.abc")
    @mock.patch.object(MEETMIX, "cleanup_orphans")
    @mock.patch.object(MEETMIX.pulsectl, "Pulse")
    @mock.patch.object(MEETMIX.subprocess, "run", return_value=mock.Mock(returncode=0))
    @mock.patch.object(MEETMIX.shutil, "which", return_value="/usr/bin/minutes")
    @mock.patch("builtins.print")
    def test_passes_extra_args_to_minutes_process(
        self,
        _print,
        _which,
        run_mock,
        _pulse_cls,
        _cleanup,
        _find_mic,
        _find_spk,
        _create,
        _setup,
        _exists,
        _getsize,
        _unlink,
    ):
        args = mock.Mock(device_match="AirPods", extra_args=["--language", "en"])
        with self.assertRaises(SystemExit):
            MEETMIX.run_record(args)
        minutes_cmd = run_mock.call_args_list[1].args[0]
        self.assertIn("--language", minutes_cmd)
        self.assertIn("en", minutes_cmd)


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


if __name__ == "__main__":
    unittest.main()
