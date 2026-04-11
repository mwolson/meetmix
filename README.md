# meetmix

Combine mic + speaker monitor into a single PipeWire source for
[minutes](https://github.com/silverstein/minutes) record.

## The Problem

When recording meetings with `minutes record` on Linux, it captures from a
single audio device. If you're on a call with Bluetooth headphones (like AirPods
Pro), `minutes` records your mic but not the other participants' audio coming
through your speakers. There's no way to get both streams into a single
recording.

## How It Works

`meetmix` creates a PipeWire virtual audio pipeline:

1. Disables WirePlumber's automatic HFP/A2DP switching so it won't interfere
2. Switches the Bluetooth card from A2DP to HFP (headset) profile for mic access
3. Creates a null sink (`meetmix_combined`) and sets it as the default audio
   output so application audio flows through the pipeline
4. Starts a forwarding loopback from `meetmix_combined` to the Bluetooth HFP
   sink, polls `pw-link` to confirm the link is established, then sends a few
   seconds of silence to warm up the Bluetooth SCO transport
5. Creates a capture sink with loopbacks that mix your mic and the combined
   audio into a single stream
6. Records from the capture sink via `pw-record`
7. On Ctrl-C, tears down the pipeline and restores the original audio profile,
   default sink, and WirePlumber settings
8. Processes the recording with `minutes process`

Logs are written to `~/.minutes/logs/` with timestamps for each session.

## Requirements

- Linux with PipeWire and PulseAudio compatibility (`pipewire-pulse`)
- WirePlumber (`wpctl`)
- PipeWire tools: `pw-link`, `pw-loopback`, `pw-play`, `pw-record`
- Python 3.9+
- [minutes](https://github.com/silverstein/minutes)
- A Bluetooth audio device (e.g. AirPods Pro)

The PipeWire tools and WirePlumber are included in standard PipeWire
installations on most distributions.

## Installation

Install minutes first (pick one):

```bash
# NVidia GPU:
cargo install minutes-cli --features cuda
# AMD GPU:
cargo install minutes-cli --features hipblas
# or
cargo install minutes-cli --features vulkan

# If you have very tiny amounts of RAM/VRAM, use "small" model instead
minutes setup --model large-v3
minutes setup --model large-v3 --diarization
```

Then install meetmix:

```bash
uv tool install git+https://github.com/mwolson/meetmix.git
```

For development:

```bash
git clone https://github.com/mwolson/meetmix.git
cd meetmix
uv tool install --editable .
```

## Configuration

List available audio devices to find your Bluetooth device:

```bash
meetmix devices
```

Create `~/.config/meetmix.conf` with your device match pattern:

```text
--device-match=AirPods
```

Or pass it on the command line:

```bash
meetmix --device-match AirPods record
```

The match is a case-insensitive substring checked against both the PulseAudio
device name and description.

## Usage

### Record a meeting

```bash
meetmix record
```

Since `record` is the default command, you can omit it:

```bash
meetmix
```

Extra arguments are passed through to `minutes record`:

```bash
meetmix record -- --language en
```

### List audio devices

```bash
meetmix devices
```

Matching devices (based on `--device-match`) are marked with `*`.

### Clean up orphaned modules

If meetmix exits uncleanly (e.g. SIGKILL), virtual devices may remain. Clean
them up manually:

```bash
meetmix cleanup
```

Orphaned modules are also cleaned up automatically on the next `meetmix record`.

## Commands

```text
meetmix                     Record with combined audio (default)
meetmix record [ARGS...]    Record, passing extra args to minutes record
meetmix devices             List audio sources and sinks
meetmix cleanup             Remove orphaned meetmix PipeWire modules
```

### Options

```text
--device-match PATTERN  Substring to match Bluetooth device name/description
--keep-recording        Preserve the WAV file after processing (for debugging)
--version               Show version and exit
```

### Configuration file

`meetmix` reads defaults from `~/.config/meetmix.conf` (or
`$XDG_CONFIG_HOME/meetmix.conf`). The file uses one flag per line:

```text
--device-match=AirPods
```

Only `--device-match` is supported. Unrecognized flags cause an error at
startup. Command-line arguments always take precedence over the config file.

## Testing

```bash
bun run test       # unit tests
```

Integration tests require a running PipeWire session, `espeak-ng`, `pw-play`,
and `pw-record`:

```bash
python3 -m unittest tests/test_integration.py -v
```

## Hooks

```bash
bun run hooks:check    # run checks against working tree
lefthook install       # install git hooks
```

The pre-commit hook runs `uvx ruff check`, `uvx ty check`, and the unit test
suite.

## Troubleshooting

### Check the logs

Each recording session writes a timestamped log to `~/.minutes/logs/`. Start
here when something goes wrong:

```bash
ls -lt ~/.minutes/logs/meetmix-*.log | head -5
```

### No audio through headset during recording

If you can't hear application audio (e.g. Teams) through your Bluetooth headset
while meetmix is recording, run the interactive diagnostic:

```bash
uv run python3 tests/one-off/test-hfp-audio-diagnostic.py
```

The diagnostic walks through four steps, playing a test sound at each one and
asking whether you heard it. Each step isolates a different part of the audio
path:

1. **A2DP baseline**: Confirms your Bluetooth connection works for normal audio
   playback. If this fails, reconnect your headset.
2. **HFP direct output**: Plays audio directly to the HFP Bluetooth sink. If
   this fails, the Bluetooth SCO transport is not activating. Try reconnecting
   your headset or restarting PipeWire (`systemctl --user restart pipewire`).
3. Forwarding loopback: Plays audio through a `pw-loopback` forwarding path to
   the HFP sink. If this fails, the loopback is not linking to the Bluetooth
   sink. Check `pw-link -l` output for the expected connections.
4. Full pipeline: Runs the complete meetmix audio path including SCO warmup. If
   this fails but step 3 passed, the issue is likely with the pipeline ordering
   or an external service (such as a Bluetooth profile manager) interfering with
   the HFP profile during setup.

### Orphaned modules after a crash

If meetmix is killed with SIGKILL or crashes, virtual PipeWire modules may be
left behind. Clean them up with:

```bash
meetmix cleanup
```

Orphaned modules are also removed automatically at the start of the next
`meetmix record`.

### Recording is silent or truncated

meetmix automatically repairs WAV headers when `pw-record` exits uncleanly (e.g.
from a crash or forced stop). If the recording is still silent, check the
session log for warnings about the Bluetooth profile reverting during recording,
or the forwarding loopback process exiting unexpectedly.

Use `--keep-recording` to preserve the raw WAV file for manual inspection:

```bash
meetmix record --keep-recording
```

Recordings are saved to `~/meetings/recordings/`.

## License

MIT
