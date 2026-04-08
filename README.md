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

1. Creates a null sink (`meetmix_combined`) with a monitor source
2. Routes your Bluetooth mic into the null sink via a loopback
3. Routes the speaker monitor (other participants' audio) into the null sink via
   a second loopback
4. Records from `meetmix_combined.monitor` via `parec`
5. On Ctrl-C, processes the recording with `minutes process`
6. Tears down all virtual devices on exit

## Requirements

- Linux with PipeWire and PulseAudio compatibility (`pipewire-pulse`)
- Python 3.9+
- [minutes](https://github.com/silverstein/minutes)
- A Bluetooth audio device (e.g. AirPods Pro)

## Installation

Install minutes first (pick one):

```bash
# NVidia GPU:
cargo install minutes-cli --features cuda
# AMD GPU:
cargo install minutes-cli

minutes setup --model small
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

## License

MIT
