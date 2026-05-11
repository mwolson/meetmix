# meetmix

Combine Bluetooth mic input and speaker output into one PipeWire recording for
[minutes](https://github.com/silverstein/minutes).

## What it does

`meetmix` builds a temporary PipeWire and PulseAudio compatibility pipeline for
meeting recording on Linux:

1. Saves the current default sink and Bluetooth profile.
2. Disables WirePlumber Bluetooth profile autoswitching.
3. Switches the matching Bluetooth card to an HFP headset profile.
4. Creates the `meetmix_combined` null sink for application audio.
5. Moves active sink inputs to the combined sink after the HFP switch.
6. Creates the `meetmix_capture` sink and mixes in Bluetooth mic audio.
7. Starts the PipeWire loopbacks in the order needed for reliable HFP output.
8. Records `meetmix_capture` with `pw-record`.
9. Runs `minutes live` against `MeetMixCapture` in parallel and echoes finalized
   utterances as `[live] ...`.
10. On interrupt, stops live transcription, processes the WAV with Minutes, and
    restores audio state.

Logs are written under `~/.minutes/logs/`. Recordings are written under
`~/meetings/recordings/` while they are being processed.

## Requirements

- Linux with PipeWire and PulseAudio compatibility
- WirePlumber and `wpctl`
- PipeWire tools: `pw-dump`, `pw-link`, `pw-loopback`, `pw-play`, `pw-record`
- PulseAudio compatibility tooling: `pactl`
- [minutes](https://github.com/silverstein/minutes)
- A Bluetooth audio device with an HFP profile

## Install

Install `minutes` first. Then install `meetmix` from this repository:

```bash
cargo install \-\-git https://github.com/mwolson/meetmix.git
```

For development:

```bash
git clone https://github.com/mwolson/meetmix.git
cd meetmix
cargo build
```

## Configure

List available audio devices:

```bash
meetmix devices
```

Create `~/.config/meetmix.conf` with a match pattern for your headset:

```text
\-\-device-match=AirPods
```

The match is case insensitive and checks the PulseAudio device name and
description. The same option can also be passed on the command line.

## Use

Record a meeting:

```bash
meetmix record
```

`record` is the default command, so this is equivalent:

```bash
meetmix
```

The default backend records the combined `meetmix_capture` sink with
`pw-record`, while `minutes live` listens to the same device for real-time
transcription. Finalized live utterances are echoed as `[live] ...` while the
recording is running.

To disable the live transcript and only process after recording:

```bash
meetmix \-\-no-live
```

An experimental Minutes/cpal recording backend is also available:

```bash
meetmix \-\-record-backend minutes
```

That backend uses Minutes' recording sidecar for live transcription. In short
test recordings, the sidecar can emit no live lines even when the final batch
transcript succeeds, so it is not the default.

With the default backend, `\-\-keep-recording` keeps the intermediate
`~/meetings/recordings/meetmix-*.wav` file after Minutes processing succeeds.

Clean up orphaned virtual modules after an unclean exit:

```bash
meetmix cleanup
```

## Development

```bash
cargo test
cargo fmt
cargo clippy \-\-all-targets \-\-all-features \-\- -D warnings
```

The npm scripts wrap the same commands for consistency with related projects:

```bash
bun run test
bun run hooks:check
```

## Troubleshooting

If you cannot hear application audio through the headset while recording, check
the latest session log:

```bash
ls -lt ~/.minutes/logs/meetmix-*.log | head -5
```

Common failure points are the Bluetooth profile reverting during recording, the
selected mic source starting muted, or a PipeWire loopback process exiting. The
raw WAV is preserved automatically when `minutes` fails, and can also be kept on
successful runs with the keep recording option.
