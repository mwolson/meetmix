# Agent Instructions

## Project Overview

meetmix is a Rust CLI wrapper around `minutes` that captures Bluetooth mic input
and speaker output into one PipeWire recording source on Linux. It uses
PulseAudio compatibility commands through `pactl` and PipeWire tools for
loopbacks. By default it records the virtual capture sink with `pw-record` and
runs `minutes live` in parallel against the cpal-visible `MeetMixCapture`
device, echoing finalized live transcript lines while preserving the reliable
PipeWire recording path. `\-\-no-live` disables the live sidecar. The
experimental `minutes record` cpal backend remains available with
`\-\-record-backend minutes`.

## Architecture

The startup sequence is split into two phases to protect browser streams from
the A2DP to HFP profile switch.

Phase 1 prepares for the profile switch:

1. Save the original default sink name.
2. Disable WirePlumber Bluetooth autoswitching.
3. Switch the matching Bluetooth card to an HFP headset profile.

Phase 2 builds the recording graph:

1. Create `meetmix_combined` and set it as the default sink.
2. Move existing sink inputs to `meetmix_combined` after the HFP switch.
3. Create `meetmix_capture`.
4. Add a PulseAudio module loopback from the Bluetooth mic source to
   `meetmix_capture`.
5. Start the forwarding `pw-loopback` from `meetmix_combined` to the HFP sink.
6. Wait for the forwarding link, then run the SCO warmup.
7. Start the capture `pw-loopback` from `meetmix_combined` to `meetmix_capture`.
8. Record `meetmix_capture` with `pw-record`. If live transcription is enabled,
   also run `minutes live \-\-device MeetMixCapture` and echo finalized sidecar
   JSONL entries as `[live] ...`.
9. On interrupt, stop child processes, restore the default sink, unload modules,
   restore the Bluetooth profile, and restore WirePlumber autoswitching. With
   the default `pw-record` backend, meetmix processes the WAV with
   `minutes process` after cleanup. With the Minutes backend, `minutes stop`
   queues processing.

## Conventions

- Rust 2021 CLI crate with a binary at `src/main.rs`.
- Keep command parsing in `src/cli.rs`.
- Keep config file parsing in `src/config.rs`.
- Keep PulseAudio and PipeWire helper parsing in `src/audio.rs`.
- Keep orchestration in `src/pipeline.rs`.
- Prefer small parser helpers with focused unit tests.
- Keep comments minimal unless they preserve audio routing knowledge that is not
  obvious from the code.

## Key Files

- `src/main.rs`: CLI dispatch.
- `src/pipeline.rs`: recording lifecycle and cleanup.
- `src/audio.rs`: `pactl`, `pw-dump`, `pw-link`, and audio object parsing.
- `src/wav.rs`: WAV repair and analysis.
- `tests/`: unit tests for parsing and WAV handling.

## Dev Loop

Run tests:

```sh
cargo test
```

Run formatting:

```sh
cargo fmt
```

Run the full hook set against the working tree:

```sh
bun run hooks:check
```

## Releasing

### Pre-Release Steps

1. Check for uncommitted changes:

   ```sh
   git status
   ```

   If there are uncommitted changes, offer to commit them before proceeding.

2. Fetch latest tags:

   ```sh
   git fetch \-\-tags
   ```

3. Run `bun run hooks:check` and confirm everything passes. CI runs the Rust
   check workflow on push to `main`, so this is the last local opportunity to
   catch format, lint, and unit-test failures before a release. The pre-commit
   hook alone is not enough: its `glob` gate filters on staged files, which can
   silently skip entire check groups when the staged set does not match.

4. Run a crates.io dry run:

   ```sh
   cargo publish \-\-dry-run \-\-locked
   ```

   If the tree is intentionally dirty while preparing a split commit, use
   `\-\-allow-dirty` only for local verification. Do not publish from a dirty
   tree.

5. Update the version in `Cargo.toml` and `package.json`. Run
   `cargo update -p meetmix` to refresh `Cargo.lock`. Commit all three files
   with message `chore: bump version to <version>`. This must be its own commit,
   not combined with other changes, unless the user explicitly agrees to that.

6. Push the version-bump commit:

   ```sh
   git push
   ```

   CI runs on push to `main` via `check.yml`.

### Creating a Release

When the user provides a version or indicates major, minor, or bugfix:

1. Create and push the tag:

   ```sh
   git tag v<version>
   git push origin v<version>
   ```

2. Wait for `publish.yml` to pass before drafting release notes. This run
   publishes to crates.io after trusted publishing has been configured, so if it
   fails the release is incomplete:

   ```sh
   gh run list
   gh run watch <run-id>
   ```

   If CI fails, fix the issue on `main`, delete the tag locally and remotely,
   re-tag, and push again.

3. Examine each commit since the last tag:

   ```sh
   git log <previous-tag>..HEAD \-\-oneline
   ```

   For each commit, run `git show <commit>` to see the full commit message and
   diff. Commit messages may be terse, so examining the full commit is essential
   for accurate release notes.

4. Create a draft GitHub release:

   ```sh
   gh release create v<version> \-\-draft \-\-title "v<version>" \-\-generate-notes
   ```

5. Enhance the draft release notes with more context:
   - Use insights from examining each commit in step 3.
   - Group related changes under descriptive headings, such as `### Fixed X`.
   - Use bullet lists within each section to describe the changes.
   - Include a brief summary of what changed and why it matters.
   - Keep the "Full Changelog" link at the bottom.
   - Update the release with `gh release edit v<version> \-\-notes "..."`.

   Ordering guidelines:
   - Put user-visible changes first.
   - Put internal improvements, refactoring, and docs later.
   - Within each section, order by user impact.

6. Publish the release after review:

   ```sh
   gh release edit v<version> \-\-draft=false
   ```

7. Tell the user to review the published release and provide a link:

   ```text
   https://github.com/mwolson/meetmix/releases
   ```

## Audio Routing Requirements

These constraints are hard won. Violating them causes subtle failures.

- Mic audio must never be forwarded to the speaker output. The two sink design
  exists to avoid echo.
- Existing sink inputs must be moved after the HFP profile switch. Moving them
  before the switch can cause Chromium based browsers to pause or silently stop
  producing audio.
- The forwarding loopback must start before the capture loopback.
- Wait for the forwarding link before the SCO warmup.
- Use `object.serial` from `pw-dump` when targeting Bluetooth nodes when
  available.
- Preserve the mic mute state, but set the source volume to 100 percent.
- Restore the original default sink before unloading modules.
- Restore the Bluetooth profile and then set the default sink again, because
  WirePlumber may override it when the A2DP sink reappears.
