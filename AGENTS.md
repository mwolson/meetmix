# Agent Instructions

## Project overview

meetmix is a Python CLI wrapper around `minutes` that captures both mic input
and speaker output from Bluetooth devices (like AirPods Pro) into a single
PipeWire/PulseAudio source for meeting recording on Linux. It uses `pw-record`
to record from a virtual combined source, then passes the recording to
`minutes process` for transcription.

## Architecture

The startup sequence is split into two phases to protect browser streams from
the A2DP-to-HFP profile switch (which destroys the A2DP sink).

Phase 1 (prepare for profile switch):

1. Saves the original default sink name (before any profile switch).
2. Creates `meetmix_combined` null sink via `create_combined_sink()`.
3. Sets `meetmix_combined` as default sink (but does NOT move existing sink
   inputs yet). Setting the default ensures new streams go to meetmix_combined.

Phase 2 (BT profile switch and stream capture):

4. Switches the BT card from A2DP to HFP via `ensure_headset_profile()`, then
   waits for both the HFP sink and source to appear. The A2DP sink destruction
   causes PipeWire to reassign browser streams to a fallback sink (often HDMI).
5. Runs `move_sink_inputs` to move all sink inputs (browser streams, etc.) from
   the fallback to `meetmix_combined`. This must happen after the HFP switch
   (not before) because Chromium-based browsers internally mute their audio
   renderer when device-change events fire while they're on a null sink. Moving
   after the switch avoids this. It must also happen before creating loopback
   modules, otherwise their sink inputs would be incorrectly moved.
6. Creates remaining devices via `create_capture_devices()`:
   - `meetmix_capture` (null sink): merges app audio + mic for recording
   - Mic loopback (module-loopback): BT mic source into `meetmix_capture`
     (provides a hardware clock driver for `meetmix_capture`; uses
     `media.role=Communication` to satisfy intended-roles policy)
7. Starts two `pw-loopback` subprocesses in a specific order:
   - Forwarding loopback FIRST: `meetmix_combined` out to BT HFP speaker sink
     using `--playback-props "target.object=<serial> media.role=Communication"`
     (so the user hears app audio through the headset). Must start first and be
     confirmed linked (via `wait_for_forwarding_link` polling `pw-link -l`)
     before the capture loopback starts. Starting capture first causes the
     forwarding loopback to intermittently fail to produce audio through the BT
     sink (PipeWire graph routing race).
   - SCO warmup: sends 3 seconds of silence through `meetmix_combined` after the
     forwarding link is confirmed. This activates the Bluetooth SCO transport,
     which may not produce audible output until it has been active for a brief
     period.
   - Capture loopback SECOND: `meetmix_combined` into `meetmix_capture` using
     `stream.capture.sink=true` (records app audio alongside mic). Both must be
     PipeWire-native `pw-loopback`, not PulseAudio `module-loopback`, because
     module-loopback between two null sinks produces silence (the target null
     sink has no hardware driver to pull data through the graph).
8. Uses `pw-record --target=meetmix_capture -P stream.capture.sink=true` to
   record from `meetmix_capture` to a WAV file (captures both app audio and mic
   input, without echoing mic back to speaker). The `stream.capture.sink=true`
   property is required because PipeWire monitor source names (like
   `meetmix_capture.monitor`) don't exist as PipeWire nodes, so `--target` must
   reference the sink name directly.
9. On Ctrl-C, passes the WAV to `minutes process --content-type meeting`.
10. Cleans up via flag-based cleanup (`_stop_requested` + `finally`): stops
    subprocesses, restores the original default sink, unloads PipeWire modules,
    then restores the BT card profile to A2DP and re-sets the default sink
    (since WirePlumber overrides the default when it sees the new A2DP sink).
    Signal handlers (SIGINT/SIGTERM/SIGHUP) set the stop flag; real cleanup
    happens in the `finally` block of `run_record()`.

Bluetooth profile switching (A2DP to HFP) is handled explicitly by meetmix via
`ensure_headset_profile()`, which uses pulsectl's `card_profile_set` to switch
before creating any loopbacks. Relying on WirePlumber's automatic
`bluetooth.autoswitch-to-headset-profile` does not work because:

- In A2DP mode, `bluez_input.*` exists as a stub source that produces silence. A
  module-loopback connected to this stub will never carry real mic audio.
- WirePlumber's async profile switch destroys and recreates the source node, and
  PulseAudio module-loopback does not reliably reconnect to the new node.
- The profile switch also resets mic volume to 100% and may leave the source
  muted. meetmix preserves the mute state so push-to-talk setups keep working,
  but still sets source volume to 100% (50% was too quiet for whisper to detect
  speech in the recording). A muted source is expected to record silence until
  it is unmuted.

After the switch, meetmix waits (up to 5 seconds) for the HFP mic source to
appear before proceeding. On cleanup, meetmix explicitly restores the card to
the original A2DP profile, waits for the A2DP sink to reappear, then re-sets the
default sink. This two-pass default restore is necessary because WirePlumber
overrides the default sink whenever a new A2DP sink appears after a profile
switch.

`pw-record` was chosen over `parec` because PulseAudio's client library cannot
stream from Bluetooth HFP sources through PipeWire's compatibility layer (parec
produces empty recordings). PipeWire's native `pw-record` works correctly.

The virtual-device approach was chosen over `minutes record --device pulse`
because minutes' cpal/ALSA backend enters a 2-second reconnect loop when
module-loopback modules are active (WirePlumber triggers "default audio device
changed" notifications that cpal re-enumerates as device changes).

## Conventions

- Single-module Python 3 package (`meetmix/`).
- Keep code comments minimal.
- When making changes to data in existing code, try to keep things in
  alphabetical order when it's reasonable to do so.
- Prefer top-down control flow: caller first, then callee.
- When writing bash scripts: `#!/bin/bash`, 4-space indentation, fail-fast
  dependency checks.

## Key files

- `meetmix/meetmix.py` -- main script (CLI + recording + PulseAudio plumbing)
- `meetmix/__init__.py` -- re-exports `main`
- `tests/test_meetmix.py` -- unit tests
- `tests/test_integration.py` -- integration tests (requires running PipeWire)
- `tests/one-off/` -- hardware-dependent exploratory tests (see below)

## Dev loop tools

### Running tests

Run unit tests with:

```sh
bun run test
```

This executes `python3 -m unittest discover -s tests -v`.

### Pre-commit hooks

Lefthook runs the following checks on commit (see `lefthook.yaml`):

- `md-format` -- Prettier formatting for Markdown files
- `ruff-check` -- linting via `uvx ruff check`
- `ty-check` -- type checking via `uvx ty check`
- `unit-tests` -- full unit test suite

Run checks against the working tree (no staging required):

```sh
bun run hooks:check
```

This runs the `pre-commit` hooks against all working tree files with
`--all-files --no-stage-fixed`, so there is no stashing and no auto-staging.
Prefer this for iterating on changes before committing.

### One-off hardware tests

Scripts in `tests/one-off/` require a connected Bluetooth device (e.g. AirPods)
and a running PipeWire session. They are not part of the automated test suite.
Use them when:

- Investigating PipeWire/WirePlumber behavior that cannot be mocked (profile
  switching timing, default sink reassignment, source node reuse).
- Validating assumptions from code review before implementing fixes (e.g.
  confirming that a property difference exists before adding a filter).
- Testing cleanup/restore paths with real hardware.

Run with `uv run python3 tests/one-off/<script>.py` from the project root.

Available tests:

- `test-a2dp-restore.py`: Verifies that WirePlumber restores A2DP after dropping
  HFP, and whether the default sink returns to its original value. Finding:
  WirePlumber auto-restores A2DP within ~1s and overrides the default sink to
  the BT A2DP sink, so meetmix must re-set the default after the A2DP switch
  settles.
- `test-forwarding-loopback.py`: Verifies that `pw-loopback` with
  `--playback-props "target.object=<serial> media.role=Communication"` links to
  an HFP BT sink and forwards audio. Finding: `-P` and `--playback-props`
  conflict (playback-props overrides the -P target), so both target.object and
  media.role must go in `--playback-props`. WirePlumber's intended-roles policy
  blocks streams without `media.role=Communication` from HFP sinks.
- `test-stub-vs-hfp-source.py`: Compares the A2DP stub `bluez_input.*` source
  against the real HFP source. Finding: PipeWire reuses the same node (same
  name, index, and proplist) for both modes. There is no PulseAudio-visible way
  to distinguish them. This is safe because `wait_for_sink()` blocks until the
  HFP transport is up, and by that time the reused source node is delivering
  real audio.
- `test-incremental-pipeline.py`: Adds pipeline components one at a time with a
  bell between each step (HFP switch, null sinks, forwarding, capture, mic
  loopback, default sink change) to isolate which component breaks BT output.
  Used to discover the forwarding-first ordering requirement.
- `test-pw-record-isolate.py`: Tests whether `pw-record` from the capture sink
  disrupts BT HFP audio output. Sets up the full pipeline (with correct ordering
  and warmup), then plays bells before/during/after pw-record. Finding:
  pw-record does NOT disrupt forwarding when the pipeline is set up correctly
  (forwarding first + SCO warmup + bluetooth-headset-manager backing off).
- `test-hfp-audio-diagnostic.py`: Interactive step-by-step diagnostic for when
  BT HFP audio output fails. Runs four checks (A2DP baseline, HFP direct,
  forwarding loopback, full pipeline) and reports which step failed with
  suggestions for each failure mode. Run this first when HFP output breaks.
- `test-mic-loopback.py`: Compares PulseAudio `module-loopback` vs PipeWire
  `pw-loopback` for carrying BT mic audio to a null sink. Records 5 seconds from
  each approach and reports peak/RMS amplitude. Finding: module-loopback works
  reliably for mic capture (provides a hardware clock driver), while pw-loopback
  for mic-to-sink requires different property configuration.
- `test-mic-single.py`: Tests the actual meetmix mic capture architecture
  (module-loopback from BT mic to null sink, pw-record capturing from the null
  sink) in a single continuous recording session. Validates that mic audio
  reaches the recording without graph disruption.
- `test-mic-volume.py`: Records at different mic volume levels (25%, 50%, 75%,
  100%) and reports peak/RMS for each. Finding: 100% source volume is needed for
  whisper to reliably detect speech in BT HFP recordings.

## Releasing

### Pre-release steps

1. Check for uncommitted changes:

   ```sh
   git status
   ```

   If there are uncommitted changes, offer to commit them before proceeding.

2. Fetch latest tags to ensure we have the complete history:

   ```sh
   git fetch --tags
   ```

3. Update the version in `pyproject.toml`, `package.json`, and the `VERSION`
   constant in `meetmix/meetmix.py`. Then run `uv lock` to sync `uv.lock` with
   the new version. Commit the version bump (including `uv.lock`) separately
   from other changes with message `chore: bump version to <version>`.

4. Push the version-bump commit and verify CI passes before tagging:

   ```sh
   git push
   gh run watch          # wait for the check job to go green
   ```

   If CI fails, fix the issue and push again before proceeding.

5. Ask the user what tag name they want. Provide examples based on the current
   version:
   - If current version is `0.1.0`:
     - Minor update (new features): `0.2.0`
     - Bugfix update (patches): `0.1.1`

### Creating the release

When the user provides a version (or indicates major/minor/bugfix):

1. Create and push the tag:

   ```sh
   git tag v<version>
   git push origin v<version>
   ```

2. Examine each commit since the last tag to understand the full context:

   ```sh
   git log <previous-tag>..HEAD --oneline
   ```

   For each commit, run `git show <commit>` to see the full commit message and
   diff. Commit messages may be terse or only show the first line in `--oneline`
   output, so examining the full commit is essential for accurate release notes.

3. Create a draft GitHub release:

   ```sh
   gh release create v<version> --draft --title "v<version>" --generate-notes
   ```

4. Enhance the release notes with more context:
   - Use insights from examining each commit in step 2
   - Group related changes under descriptive headings (e.g., "### Refactored X",
     "### Fixed Y")
   - Use bullet lists within each section to describe the changes
   - Include a brief summary of what changed and why it matters
   - Keep the "Full Changelog" link at the bottom
   - Update the release with `gh release edit v<version> --notes "..."`

   Ordering guidelines:
   - Put user-visible changes first (new features, bug fixes, breaking changes)
   - Put under-the-hood changes later (refactoring, internal improvements, docs)
   - Within each section, order by user impact (most impactful first)

5. Tell the user to review the draft release and provide a link:

   ```
   https://github.com/mwolson/meetmix/releases
   ```

## Audio routing requirements

These are hard-won constraints discovered during development. Violating them
causes subtle failures that are difficult to diagnose.

- Mic audio must never be forwarded to the speaker output. This causes echo (the
  user hears their own voice played back). The two-sink architecture
  (`meetmix_combined` for apps, `meetmix_capture` for recording) exists
  specifically to prevent this.
- WirePlumber does not route general media streams (role "Music" or unset) to
  HFP sinks because they have `device.intended-roles = "Communication"`. The
  only reliable workaround is to make a meetmix null sink the default sink so
  apps route to it, then forward to the BT speaker via module-loopback.
- `pw-play` with `--target` silently succeeds (exit 0) even when the BT
  transport is stuck or the node never reaches RUNNING. Do not trust its exit
  code as proof that audio was delivered. Use `paplay` for quick audio tests.
- Very short sounds (under ~0.2s) may be swallowed by Bluetooth codec buffering
  before the AirPods start playing. Use sounds of at least 1 second for testing.
- PipeWire's `target.object` property (used by `pw-play --target`,
  `pw-record --target`, and `pw-loopback -C`/`-P`) accepts either
  `object.serial` or `node.name`. It does NOT accept `object.id`. Using
  `node.name` silently fails for Bluetooth sinks (exit 0, no audio). Always use
  `object.serial` from `pw-dump` for reliable BT node targeting.
- `pw-loopback` supports two ways to specify targets: the `-C`/`-P` shorthand
  flags (which set `target.object` directly), or `node.target` inside
  `--capture-props`/`--playback-props`. The `-C`/`-P` flags are more reliable
  and should be preferred. For capturing from a sink's monitor, combine
  `-C <sink_name>` with `--capture-props "stream.capture.sink=true"`.
- `pw-loopback -P <serial>` links to a BT A2DP sink but fails to link to an HFP
  sink unless the playback stream has `media.role=Communication`. WirePlumber's
  intended-roles policy blocks streams without a matching role from connecting
  to HFP sinks (which have `device.intended-roles = "Communication"`). Always
  set `--playback-props "media.role=Communication"` when targeting an HFP sink.
- Null sinks have no hardware clock (QUANT=0). PulseAudio `module-loopback`
  between two null sinks produces silence because neither end has a driver to
  pull data through the graph. Each null sink needs at least one connection to
  real hardware (via module-loopback) to provide a clock driver. For the
  combined-to-capture path specifically, PipeWire-native `pw-loopback` with
  `stream.capture.sink=true` is required instead of module-loopback.
- In A2DP mode, `bluez_input.*` exists as a stub source that produces silence.
  Creating a module-loopback from it does NOT trigger a reliable profile switch.
  The BT card must be explicitly switched to an HFP profile before creating
  loopbacks. PipeWire reuses the same source node for both A2DP (stub) and HFP
  (real): same name, index, and proplist through the PulseAudio API. There is no
  PulseAudio-visible way to distinguish them. This is safe because
  `wait_for_sink()` blocks until the HFP sink appears (proving the transport is
  up), and by that time the reused source node is delivering real audio.
- The HFP profile switch resets mic source volume to 100% and may mute the
  source. Preserve the mute state so push-to-talk keeps working, but set the
  source volume to 100% and log clearly when the source is still muted.
- Switching from A2DP to HFP destroys the A2DP sink, which disconnects any
  browser streams attached to it. PipeWire reassigns them to a fallback sink
  (often HDMI). Sink inputs must be moved to `meetmix_combined` AFTER the HFP
  switch, not before. Moving before causes Chromium-based browsers (Vivaldi,
  Chrome) to internally mute their audio renderer when subsequent device-change
  events fire during the profile switch (the PulseAudio stream stays uncorked
  but stops producing data). The brief appearance on HDMI is harmless since
  `move_sink_inputs` runs immediately after the switch.
- Switching from A2DP to HFP also causes PipeWire to reassign the default to a
  fallback (often an HDMI/TV output). Save the original default sink before the
  profile switch, not after.
- After the HFP switch, wait for both the HFP sink and source to appear before
  proceeding. The source is needed for mic capture, and the sink is needed for
  the forwarding loopback.
- The forwarding loopback must start BEFORE the capture loopback. Both use
  `stream.capture.sink=true` on `meetmix_combined`, and starting them in the
  wrong order causes the forwarding loopback to intermittently fail to link to
  the BT HFP sink. Use `wait_for_forwarding_link` (polls `pw-link -l`) to
  confirm the forwarding loopback is connected before starting the capture.
- BT HFP SCO output requires a warmup period. After the forwarding loopback is
  linked, send 3 seconds of silence through `meetmix_combined` to activate the
  SCO transport. Without this, the BT sink may show as "running" in PipeWire but
  produce no audible output.
- External services that manage BT profiles (like bluetooth-headset-manager) can
  disrupt the HFP SCO transport by issuing `pactl` commands during recording.
  meetmix disables WirePlumber's `bluetooth.autoswitch-to-headset- profile`
  setting during its lifecycle; any external manager should detect this as a
  signal to skip reconciliation.
- Setting the default sink only affects future PulseAudio streams. Existing sink
  inputs (browser audio, media players) remain connected to their original sink.
  Use `sink_input_move` to migrate them to `meetmix_combined` after changing the
  default.
- On exit, the original default sink must be restored before unloading modules.
  If modules are unloaded first, PipeWire may assign an unexpected default.
- After unloading modules, WirePlumber auto-switches the BT card from HFP back
  to A2DP and overrides the default sink with the new A2DP sink. To preserve the
  user's original default, meetmix explicitly restores the card profile to A2DP,
  waits for the sink to appear (up to 5s), lets WirePlumber settle (1s), then
  re-sets the default sink.

## Upstream minutes issues to track

These affect meetmix and may eventually let us simplify or remove it.

- **#62** (open): PipeWire auto-detection for `--call` on Linux.
  `minutes record --call` detects the loopback device but does not auto-route to
  it. Detection works after PRs #75/#85/#87 but `call = "auto"` is not wired
  into the runtime path. Once this ships, meetmix may be able to use
  `minutes record --call` directly.

- **#69** (open): Linux capture audio from PipeWire monitor sources. After the
  native PipeWire backend (PR #75) and categorizer fix (PR #85), cpal's PipeWire
  host can expose `Audio/Sink` nodes as `Duplex` devices. End-to-end capture
  test on real PipeWire hardware has not been confirmed. Pure-ALSA setups are
  still an open question.

- **#36** (open): AirPods/Bluetooth codec switching causes audio stream
  degradation and whisper hallucinations. Mitigated by noise marker filter and
  silence detection, but worth being aware of for Bluetooth recording quality.

- Speaker mapping emits
  `WARN Level 1: speaker mapping failed error=Unknown engine: auto`. This is a
  minutes-side configuration issue where the speaker mapping engine is set to
  "auto" but no engine is available. Does not affect transcription, only speaker
  label assignment.

## Known limitations

- Chromium-based browsers (Vivaldi, Chrome) pause media playback when the A2DP
  Bluetooth sink is destroyed during the HFP profile switch. The user must press
  play to resume after meetmix starts recording. This cannot be prevented from
  the PipeWire side: moving the stream to a null sink before the switch causes
  Chromium to silently mute its audio renderer (worse, since pressing play
  doesn't recover it). The HFP-first approach (destroy A2DP, then create null
  sinks and move streams) is preferable because pressing play cleanly restarts
  the audio pipeline.

- The installed `minutes` binary uses cpal's ALSA backend, not the native
  PipeWire backend from PR #75. `minutes sources` shows "System Audio: (none
  detected)" and only ALSA devices. Once a PipeWire-enabled build is available,
  multi-source capture (`--source`) may work without meetmix.

- `minutes record --device pulse` enters a 2-second reconnect loop caused by
  WirePlumber reacting to module-loopback creation. Setting `node.passive=true`
  and `priority.session=0` on the null sink did not prevent this. The
  `pw-record` approach avoids the issue entirely.

- PulseAudio's `parec` cannot stream from Bluetooth HFP sources through
  PipeWire's compatibility layer (produces empty WAV files). PipeWire's native
  `pw-record` works correctly for this use case.

- Using `pw-record` means we lose `minutes record`'s live features (in-flight
  notes via `minutes note`, auto-stop timers, real-time processing). The
  recording is processed after capture ends.

- The `large-v3` whisper model sometimes reports wildly incorrect durations
  (e.g., 60m for a 4m recording) and collapses most of the transcript into
  "[repeated audio removed]" sections. The `small` model processes full meetings
  but with lower transcription quality. This appears to be a minutes/whisper
  issue, not a meetmix audio capture issue.
