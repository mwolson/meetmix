# Agent Instructions

## Project overview

meetmix is a Python CLI wrapper around `minutes` that captures both mic input
and speaker output from Bluetooth devices (like AirPods Pro) into a single
PipeWire/PulseAudio source for meeting recording on Linux. It uses `pw-record`
to record from a virtual combined source, then passes the recording to
`minutes process` for transcription.

## Architecture

1. Uses `pulsectl` to create a PulseAudio null sink + two module-loopback
   instances (mic and speaker monitor routed to the null sink)
2. Uses `pw-record` to record from the null sink's monitor source to a WAV file
3. On Ctrl-C, passes the WAV to `minutes process --content-type meeting`
4. Cleans up PipeWire modules on exit (atexit + signal handlers)

Bluetooth profile switching (A2DP to HFP for mic access) is handled
automatically by WirePlumber's `bluetooth.autoswitch-to-headset-profile`
setting. When module-loopback connects to the virtual `bluez_input.*` source
node, WirePlumber detects the link and switches the card to HFP. When meetmix
exits and the loopback is unloaded, WirePlumber restores A2DP after a 2-second
timeout. This avoids conflicts with external profile managers like
`bluetooth-headset-manager` which handle codec selection (e.g., avoiding LC3 on
adapters where it's broken).

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

## Known limitations

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
