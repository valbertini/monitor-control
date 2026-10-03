# AGENTS.md

monitorctl: Windows DDC/CI monitor control, shipped as a tray app (`monitorctl.exe`)
plus a diagnostic CLI. Pure stdlib Python + ctypes. `README.md` is the product doc
(Portuguese); read it before changing behaviour. Per-monitor survey data lives in
`docs/monitors/`, protocol research in `docs/PESQUISA-DDC-CI.md`, UI research in
`docs/PESQUISA-UI.md` (applied in `app/presentation.py`), calibration research in
`docs/PESQUISA-CALIBRACAO.md` (applied in `app/calibration.py`).

`docs/PROJETO.md` is the **decision record** (D1–D29, history, measurements, bugs).
Read the relevant decision before changing a design invariant; record every new
decision there in the same context → decision → alternatives → consequences form.

## Working with this user

- Answer in **Portuguese**. Code, UI text and code comments stay in **English**;
  docs in `docs/` and `README.md` are Portuguese.
- **Industry rigor** is non-negotiable even for small features: tests, failure
  handling, a written *why* for every non-obvious decision.
- **Ask before expanding scope** (new features, profiles, Linux backend, signing,
  auto-update). Propose with a recommendation; build only what was agreed.
- Research findings go into a `.md` in the repo, not only into chat.
- Anything that writes to the real monitors, the registry or Task Scheduler is a
  **hardware/system change**: say what will happen first. Never decide a
  Save/Restore/"unsaved changes" prompt on the user's behalf.

## Hardware facts (learned on this machine, not discoverable from code)

- Monitors: **TCL 25G64** (EDID `TCL-1003`, no `model(...)` in caps, malformed caps
  string) and **ASUS VS248** (`ACI-2498`); GPU RTX 4070 (NVAPI works).
- **TCL forgets RGB gains/offsets (0x16 0x18 0x1A 0x6C 0x6E 0x70) on power-button
  off, yet keeps reporting the forgotten values, and ignores a write of the value
  it reports.** Read-back can never reveal the loss. `0xB0`/SaveCurrentSettings and
  OSD open/close were tried: neither persists. Registered in `ddc/quirks.py`.
  Brightness/contrast/preset were never tested for this.
- Windows renumbers `\\.\DISPLAYn` between boots (TCL has been DISPLAY1, 2, 1).
  Identity is the EDID `hardware_id`; device/id are positions.
- The ASUS keeps its settings: automatic code must never write to it.

## Design invariants (break one only with the user's agreement)

- **Volatile** controls are always written through an intermediate value:
  **50 (PIVOT)** on Save, Restore and when a monitor comes back;
  **value−1 (NEIGHBOUR)** for periodic upkeep and for re-typing the reported value.
  Both steps are paid from the write budget together.
- **Upkeep** (`reassert_saved`) touches only volatile controls and GPU adjustments.
  A timer must never become an EEPROM-wear loop.  The software gamma ramp at
  neutral (50/50/50) is **not ours**: it writes nothing, so ICC curves and Night
  Light survive; leaving neutral saves the device's ramp, returning (or exiting)
  restores it, and only if the ramp there is still ours. Upkeep skips any GPU
  adjustment saved at its neutral value (D27).
- **Restore** re-reads each saved control before comparing; the cache goes stale
  in a long-running app (OSD buttons). **Save** writes the cache from the window
  (what it shows) and re-reads first from the tray (`reread=True`).
- **Profiles are per monitor** (`ddc/profiles.py`); the "snapshot" is only the
  view of every monitor's active profile. Switching one monitor's profile never
  writes another. Pair saved entries with `pair_monitors` over the *whole* book
  against *all* connected monitors, everywhere. The active profile cannot be
  deleted. Book changes are pure `book -> book` functions via `ProfileStore.update`.
- Save/Restore on an **unprobed** cache probe first; `refresh(probe=False)` carries
  values over only for a monitor matched by EDID.
- Every DDC op runs inside the cross-process **bus lock** (`ddc/buslock.py`),
  pacing included: app and CLI may run together.
- Only the bus thread (`app/worker.py`) touches the service; results reach the UI
  via `PostMessageW` → `Worker.drain()` on the main thread. Tk runs only on the main
  thread and is imported only when the window first opens.
- Policy blocks (`ddc/policy.py`): `0xE0–0xFF`, factory resets, `0xB0`, `0xCA`, `0xD6`.

## Commands

```bash
python -m unittest discover -s tests -t .              # all tests, no hardware
python -m ruff check . && python -m ruff format --check .
python tools/build.py                                  # -> dist/monitorctl/, zip
python launch_app.pyw                                  # tray app from source
python launch_app.pyw --quit                           # stop a running instance
python -m monitorctl list                              # CLI against real monitors
```

Run ruff format **then** ruff check. Tests use `tests/fakes.py` (`FakeMonitor` can
simulate the TCL: `ignores_same_value`, `edid`, read counting). Window tests build a
real, transparent Tk window with an inline worker.

## Gotchas

- **Not a git repo.** Before large changes, tar a backup into the scratchpad.
- Editing through a bash heredoc mangles backslashes (`\\.\DISPLAY1`, `\n`, `\d`).
  Use the Edit/Write tools for any text containing a backslash. Python
  `write_text` on Windows writes CRLF; the repo already mixes LF and CRLF per file.
- `RegisterPowerSettingNotification` delivers the current display state at once;
  only an off/dim → on transition is an event.
- In a Tk grid, empty columns in a `uniform` group take full width.
- Rebuilding deletes `dist/monitorctl`; `tools/build.py` sends `--quit` first.
  Start-with-Windows (HKCU Run key) must point at a copy outside `dist/`.
- Memory numbers: measure the packaged exe with private working set
  (`Win32_PerfFormattedData_PerfProc_Process.WorkingSetPrivate`), not private bytes.

## Open items (as of 2026-10-02, v0.2.0)

- Validate TCL power-cycle **detection** with the user (power button with app in
  tray, then read `%LOCALAPPDATA%\monitorctl\monitorctl.log` for
  `Windows reported`, `stopped answering`, `came back on`). Fallback: `rewrite` mode.
- Start-with-Windows not enabled yet; the legacy scheduled task
  `monitorctl-restore` (v0.1) is still installed and is removed when it is enabled.
- The saved snapshot dates from 2026-09-30 and differs from current values; the
  user decides whether to re-save. On first run of the profiles build it becomes
  each monitor's "Default" profile (`snapshot.json` is kept, no longer read).
- The running tray exe (`dist\monitorctl`) predates the 2026-10-02 review fixes,
  the UI input changes and profiles; rebuilding stops it (`--quit`) - ask first.
