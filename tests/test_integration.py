import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
import wave


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

HAS_ESPEAK = shutil.which("espeak-ng") is not None
HAS_PW_PLAY = shutil.which("pw-play") is not None
HAS_PW_RECORD = shutil.which("pw-record") is not None

try:
    import pulsectl

    _pulse = pulsectl.Pulse("meetmix-test-probe")
    _pulse.close()
    HAS_PULSE = True
except Exception:
    HAS_PULSE = False


def generate_fixture_wav(path):
    subprocess.run(
        ["espeak-ng", "-w", path, "one two three four five"],
        check=True,
        capture_output=True,
    )


@unittest.skipUnless(HAS_ESPEAK, "espeak-ng not available")
class EspeakFixtureTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.wav_path = os.path.join(self._tmpdir.name, "test_speech.wav")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_espeak_generates_valid_wav(self):
        generate_fixture_wav(self.wav_path)
        self.assertTrue(os.path.exists(self.wav_path))
        with open(self.wav_path, "rb") as f:
            header = f.read(4)
        self.assertEqual(b"RIFF", header)
        self.assertGreater(os.path.getsize(self.wav_path), 1024)

    def test_espeak_generates_readable_wav(self):
        generate_fixture_wav(self.wav_path)
        with wave.open(self.wav_path, "rb") as w:
            self.assertGreater(w.getnframes(), 0)
            self.assertGreater(w.getsampwidth(), 0)


@unittest.skipUnless(HAS_PULSE, "PulseAudio/PipeWire not available")
class VirtualDeviceTests(unittest.TestCase):
    def setUp(self):
        self.pulse = pulsectl.Pulse("meetmix-test")
        self.modules = []

    def tearDown(self):
        for idx in reversed(self.modules):
            try:
                self.pulse.module_unload(idx)
            except Exception:
                pass
        self.pulse.close()

    def test_create_and_teardown_null_sink(self):
        idx = self.pulse.module_load(
            "module-null-sink",
            "sink_name=meetmix_test_sink"
            " sink_properties=device.description=MeetMixTest",
        )
        self.modules.append(idx)
        sinks = [s for s in self.pulse.sink_list() if s.name == "meetmix_test_sink"]
        self.assertEqual(1, len(sinks))
        sources = [s for s in self.pulse.source_list() if s.name == "meetmix_test_sink.monitor"]
        self.assertEqual(1, len(sources))

    def test_create_virtual_devices_function(self):
        fake_mic_idx = self.pulse.module_load(
            "module-null-sink",
            "sink_name=meetmix_test_mic"
            " sink_properties=device.description=FakeMic",
        )
        self.modules.append(fake_mic_idx)

        fake_spk_idx = self.pulse.module_load(
            "module-null-sink",
            "sink_name=meetmix_test_spk"
            " sink_properties=device.description=FakeSpeaker",
        )
        self.modules.append(fake_spk_idx)

        indices = MEETMIX.create_virtual_devices(
            self.pulse,
            "meetmix_test_mic.monitor",
            "meetmix_test_spk.monitor",
        )
        self.modules.extend(indices)
        self.assertEqual(3, len(indices))

        sinks = [s for s in self.pulse.sink_list() if s.name == "meetmix_combined"]
        self.assertEqual(1, len(sinks))

    def test_cleanup_orphans_removes_virtual_devices(self):
        self.pulse.module_load(
            "module-null-sink",
            "sink_name=meetmix_orphan_test"
            " sink_properties=device.description=MeetMixOrphan",
        )
        count = MEETMIX.cleanup_orphans(self.pulse)
        self.assertGreaterEqual(count, 1)
        sinks = [s for s in self.pulse.sink_list() if s.name == "meetmix_orphan_test"]
        self.assertEqual(0, len(sinks))


@unittest.skipUnless(
    HAS_PULSE and HAS_ESPEAK and HAS_PW_PLAY and HAS_PW_RECORD,
    "requires PulseAudio/PipeWire, espeak-ng, pw-play, and pw-record",
)
class AudioCaptureTests(unittest.TestCase):
    def setUp(self):
        self.pulse = pulsectl.Pulse("meetmix-test")
        self.modules = []
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        for idx in reversed(self.modules):
            try:
                self.pulse.module_unload(idx)
            except Exception:
                pass
        self.pulse.close()
        self._tmpdir.cleanup()

    def test_combined_source_captures_audio(self):
        fixture_path = os.path.join(self._tmpdir.name, "fixture.wav")
        generate_fixture_wav(fixture_path)

        fake_mic_idx = self.pulse.module_load(
            "module-null-sink",
            "sink_name=meetmix_test_mic2"
            " sink_properties=device.description=FakeMic2",
        )
        self.modules.append(fake_mic_idx)

        fake_spk_idx = self.pulse.module_load(
            "module-null-sink",
            "sink_name=meetmix_test_spk2"
            " sink_properties=device.description=FakeSpeaker2",
        )
        self.modules.append(fake_spk_idx)

        indices = MEETMIX.create_virtual_devices(
            self.pulse,
            "meetmix_test_mic2.monitor",
            "meetmix_test_spk2.monitor",
        )
        self.modules.extend(indices)

        out_path = os.path.join(self._tmpdir.name, "captured.wav")
        rec = subprocess.Popen(
            ["pw-record", "--target", "meetmix_combined.monitor", out_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        time.sleep(0.5)

        p1 = subprocess.Popen(
            ["pw-play", "--target", "meetmix_test_mic2", fixture_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        p2 = subprocess.Popen(
            ["pw-play", "--target", "meetmix_test_spk2", fixture_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        p1.wait(timeout=10)
        p2.wait(timeout=10)

        time.sleep(0.5)
        rec.terminate()
        rec.wait(timeout=5)

        self.assertTrue(os.path.exists(out_path))
        self.assertGreater(os.path.getsize(out_path), 1000)

        with wave.open(out_path, "rb") as w:
            self.assertGreater(w.getnframes(), 0)


if __name__ == "__main__":
    unittest.main()
