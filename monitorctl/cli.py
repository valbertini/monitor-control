"""Command-line entry point.

The CLI exists for two reasons beyond convenience: it is the diagnostic tool for
when a monitor misbehaves (``caps`` dumps exactly what the display claims to
support), and it exercises the whole stack without a window.  The tray app is
``monitorctl app`` (``monitorctl.exe`` in the packaged build).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import TextIO

from . import __version__, autostart
from .ddc.backend import DdcBackend
from .ddc.capabilities import parse_capabilities
from .ddc.errors import DdcError
from .ddc.profiles import ProfileStore, default_profiles_path
from .ddc.service import MonitorService, MonitorState, Reassert
from .ddc.snapshot import legacy_snapshot_path
from .ddc.vcp import CATEGORIES, FEATURES, feature_by_code
from .fileio import state_directory
from .gpu.gamma import SOFTWARE_LUT_KEYS

__all__ = ["main"]

_log = logging.getLogger("monitorctl")


def _build_backend() -> DdcBackend:
    """Select the backend for this platform.

    Raises:
        SystemExit: On an unsupported platform, with an actionable message.
    """
    if sys.platform == "win32":
        from .ddc.windows import WindowsDdcBackend

        return WindowsDdcBackend()
    raise SystemExit(
        f"Platform {sys.platform!r} is not supported yet. "
        "Only Windows (Monitor Configuration API) is implemented."
    )


#: A log file past this size is rotated to ``<name>.1`` before the next run.
#: ``--log-file`` exists for the logon task of v0.1 (``monitorctl-restore``),
#: which runs ``restore --log-file ...`` and may still be installed: enabling
#: Start with Windows removes it (``autostart.remove_legacy_task``).  Drop the
#: option once no installation can still have that task.
_LOG_ROTATE_BYTES = 256 * 1024


def _configure_logging(verbose: bool, stream: TextIO | None = None) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
            if stream is not None
            else "%(levelname)-7s %(name)s: %(message)s"
        ),
        stream=stream or sys.stderr,
    )


def _open_log_file(path: Path) -> TextIO:
    """Open the log for appending, rotating it first once it has grown.

    Rotation keeps exactly one old generation.  A failure to rotate is not a
    reason to skip the restore, so it only costs the rotation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > _LOG_ROTATE_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    return path.open("a", encoding="utf-8", buffering=1)


def _resolve(service: MonitorService, selector: str) -> MonitorState:
    """Find a monitor by id, by case-insensitive name, or by 1-based position.

    Enumerates without probing: resolving a name should not cost a DDC round
    trip per feature.
    """
    monitors = service.list_monitors(probe=False)
    if not monitors:
        raise SystemExit("No DDC/CI-capable monitor detected.")

    for monitor in monitors:
        if monitor.id == selector:
            return monitor
    lowered = selector.lower()
    for monitor in monitors:
        if monitor.name.lower() == lowered:
            return monitor
    if selector.isdigit():
        position = int(selector)
        if 1 <= position <= len(monitors):
            return monitors[position - 1]

    known = ", ".join(f"{i + 1}={m.id}" for i, m in enumerate(monitors))
    raise SystemExit(f"Monitor {selector!r} not found. Available: {known}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _cmd_list(service: MonitorService, args: argparse.Namespace) -> int:
    monitors = service.list_monitors()
    if not monitors:
        print("No DDC/CI-capable monitor detected.")
        print("Check that DDC/CI is enabled in the monitor's own OSD menu.")
        return 1

    for position, monitor in enumerate(monitors, start=1):
        tags = []
        if monitor.primary:
            tags.append("primary")
        if monitor.display_type:
            tags.append(monitor.display_type)
        if monitor.mccs_version:
            tags.append(f"MCCS {monitor.mccs_version}")
        suffix = f"  [{', '.join(tags)}]" if tags else ""

        supported = monitor.supported_features
        print(f"\n[{position}] {monitor.name}{suffix}")
        print(f"    id      : {monitor.id}")
        print(f"    device  : {monitor.device or '(unknown)'}")
        print(f"    hardware: {monitor.hardware_id or '(EDID unavailable)'}")
        print(f"    features: {len(supported)} of {len(monitor.features)} supported")
        if monitor.volatile:
            print(
                "    volatile: "
                + ", ".join(sorted(monitor.volatile))
                + "  (forgotten at power-off; restore always rewrites them)"
            )

        for warning in monitor.warnings:
            print(f"    warning : {warning}")

        if not supported:
            print("    (no controllable feature detected)")
            continue

        current_category = None
        for feature in supported:
            if feature.category != current_category:
                current_category = feature.category
                print(f"    -- {CATEGORIES[current_category].label}")
            # GPU-side controls have no VCP code; show the mechanism instead.
            code = f"0x{feature.code:02X}" if feature.code is not None else feature.source
            if feature.kind == "continuous":
                print(
                    f"       {feature.key:<22} {code:<14} "
                    f"{feature.value}/{feature.maximum}  ({feature.percent}%)"
                )
            else:
                label = next(
                    (o.label for o in feature.options if o.value == feature.value),
                    f"0x{feature.value:02X}" if feature.value is not None else "?",
                )
                print(f"       {feature.key:<22} {code:<14} {label}")

        if not args.all:
            continue
        missing = [f for f in monitor.features if not f.supported]
        if missing:
            print(f"    -- not supported ({len(missing)})")
            print("       " + ", ".join(f.key for f in missing))
    print()
    return 0


def _cmd_features(args: argparse.Namespace) -> int:
    """Print the whole registry, grouped, without touching any hardware."""
    del args
    for category in CATEGORIES.values():
        members = [f for f in FEATURES.values() if f.category.value == category.key]
        if not members:
            continue
        print(f"\n{category.label}")
        print(f"  {category.description}")
        for feature in members:
            kind = "range" if feature.kind.value == "continuous" else "choice"
            print(
                f"    {feature.key:<22} 0x{feature.code:02X}  {kind:<6} {feature.label}"
            )
    print()
    return 0


def _cmd_caps(service: MonitorService, args: argparse.Namespace) -> int:
    monitor = _resolve(service, args.monitor)
    print(f"Monitor : {monitor.name}  ({monitor.id})")
    print(f"Device  : {monitor.device or '(unknown)'}")
    print()

    if not monitor.capabilities_raw:
        print("The monitor returned no capabilities string.")
        print("That is common and does not mean DDC/CI is absent.")
        return 0

    print("--- capabilities (raw) ---")
    print(monitor.capabilities_raw)
    print()

    caps = parse_capabilities(monitor.capabilities_raw)
    print("--- parsed ---")
    print(f"model      : {caps.model or '-'}")
    print(f"type       : {caps.display_type or '-'}")
    print(f"mccs_ver   : {caps.mccs_version or '-'}")
    print(f"cmds       : {' '.join(f'{c:02X}' for c in sorted(caps.commands)) or '-'}")
    print("vcp        :")
    for code, values in caps.vcp.items():
        known = feature_by_code(code)
        name = f"  ({known.label})" if known else ""
        rendered = " ".join(f"{v:02X}" for v in values) if values else "continuous"
        print(f"    0x{code:02X}  {rendered}{name}")
    for warning in caps.warnings:
        print(f"warning    : {warning}")
    return 0


def _cmd_get(service: MonitorService, args: argparse.Namespace) -> int:
    monitor = _resolve(service, args.monitor)
    state = service.read_feature(monitor.id, args.feature)
    if state.kind == "continuous":
        print(f"{state.value}  (max {state.maximum}, {state.percent}%)")
    else:
        label = next(
            (o.label for o in state.options if o.value == state.value), "unknown"
        )
        print(f"{state.value}  ({label})")
    return 0


def _cmd_set(service: MonitorService, args: argparse.Namespace) -> int:
    if args.feature in SOFTWARE_LUT_KEYS:
        # It would be undone as this process exits (``GammaRampBackend.close``).
        raise SystemExit(
            f"{args.feature} lasts only while the process that set it runs; "
            "set it in the tray app's window."
        )
    monitor = _resolve(service, args.monitor)
    outcome = service.write_feature(monitor.id, args.feature, args.value)
    confirmed = outcome.confirmed if outcome.confirmed is not None else outcome.applied
    print(f"{monitor.name} :: {args.feature} = {confirmed}")
    if outcome.applied != outcome.requested:
        print(f"  (requested {outcome.requested}, applied {outcome.applied})")
    for warning in outcome.warnings:
        print(f"  warning: {warning}")
    return 0


def _cmd_save(service: MonitorService, args: argparse.Namespace) -> int:
    del args
    # This process cannot see the software LUT values the tray app applies;
    # it would read them as neutral and erase them from every profile.
    snapshot = service.save_snapshot(keep_saved=SOFTWARE_LUT_KEYS)
    print(
        f"Saved {snapshot.value_count} settings "
        f"across {len(snapshot.monitors)} monitor(s), "
        "each into the profile it is using."
    )
    for monitor in snapshot.monitors:
        print(f"    {monitor.name:<24} {len(monitor.values)} settings")
    if service.profiles_path is not None:
        print(f"\nProfiles file: {service.profiles_path}")

    # A monitor that forgets controls also reports values it is not using, so
    # what was just saved is pushed to make it what the screen shows.
    report = service.reassert_saved(reassert=Reassert.PIVOT)
    pushed = [o for o in report.applied if not o.key.startswith("gpu_")]
    if pushed:
        print(f"\nPushed {len(pushed)} control(s) that a monitor forgets at power-off.")
    for issue in report.failed:
        print(f"  failed : {issue.monitor_name} :: {issue.key} - {issue.reason}")
    return 1 if report.failed else 0


def _cmd_restore(service: MonitorService, args: argparse.Namespace) -> int:
    del args
    report = service.restore_snapshot()
    saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.saved_at))
    print(f"Restored the profiles in use (last saved {saved_at}).\n")

    names = {m.id: m.name for m in service.list_monitors(probe=False)}
    for outcome in report.applied:
        confirmed = (
            outcome.confirmed if outcome.confirmed is not None else outcome.applied
        )
        name = names.get(outcome.monitor_id, outcome.monitor_id)
        print(f"    {name} :: {outcome.key} = {confirmed}")
    print(
        f"\n{len(report.applied)} written, {report.unchanged} already correct, "
        f"{len(report.skipped)} skipped, {len(report.failed)} failed."
    )
    for issue in report.skipped:
        target = (
            f"{issue.monitor_name} :: {issue.key}" if issue.key else issue.monitor_name
        )
        print(f"  skipped: {target} - {issue.reason}")
    for issue in report.failed:
        print(f"  failed : {issue.monitor_name} :: {issue.key} - {issue.reason}")
    return 1 if report.failed else 0


def _cmd_autostart(args: argparse.Namespace) -> int:
    """Manage starting the tray app with Windows.  Touches no monitor."""
    if args.action == "enable":
        command = autostart.enable()
        print("monitorctl will start with Windows, in the tray. The Run key holds:\n")
        print(f"    {command}\n")
        print("It also appears in Task Manager > Startup apps.")
        return 0

    if args.action == "disable":
        if autostart.disable():
            print("monitorctl will no longer start with Windows.")
        else:
            print("monitorctl was not set to start with Windows; nothing to change.")
        return 0

    key = autostart.RegistryRunKey()
    current = key.get(autostart.VALUE_NAME)
    if current is None:
        print("Start with Windows: off")
    elif key.switched_off(autostart.VALUE_NAME):
        print("Start with Windows: off (switched off in Task Manager > Startup apps)")
        print(f"    {current}")
        print("    'autostart enable' switches it back on.")
    else:
        here = current == autostart.launch_command()
        print(f"Start with Windows: on{'' if here else ' (another copy of monitorctl)'}")
        print(f"    {current}")
    log = state_directory() / "monitorctl.log"
    if log.is_file():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"\nLast lines of {log}:\n")
        for line in lines[len(lines) - args.lines :]:
            print(f"    {line}")
    return 0


def _cmd_app(args: argparse.Namespace) -> int:
    """Run the tray app in this process (what ``monitorctl.exe`` does)."""
    from .app.main import main as app_main

    return app_main(["--startup"] if args.startup else [])


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _count(text: str) -> int:
    """argparse type: a whole number, zero or more."""
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or more")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monitorctl",
        description=(
            "DDC/CI monitor control with an explicit write-safety policy. "
            "Manufacturer-reserved codes and destructive commands are blocked."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"monitorctl {__version__}"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument(
        "--log-file",
        type=Path,
        metavar="PATH",
        help=(
            "append all output to PATH instead of the console; used by the "
            "logon task of v0.1, which has no console to print to"
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list monitors and the state of each feature")
    p_list.add_argument(
        "--all",
        action="store_true",
        help="also list the features the monitor does not support",
    )
    p_list.set_defaults(handler=_cmd_list)

    p_features = sub.add_parser(
        "features", help="list every controllable feature, grouped by category"
    )
    p_features.set_defaults(command_handler=_cmd_features)

    p_caps = sub.add_parser("caps", help="show a monitor's capabilities string")
    p_caps.add_argument("monitor", help="id, name or position (1, 2, ...)")
    p_caps.set_defaults(handler=_cmd_caps)

    feature_help = f"one of: {', '.join(sorted(FEATURES))}"

    p_get = sub.add_parser("get", help="read one feature")
    p_get.add_argument("monitor", help="id, name or position")
    p_get.add_argument(
        "feature", choices=sorted(FEATURES), metavar="feature", help=feature_help
    )
    p_get.set_defaults(handler=_cmd_get)

    p_set = sub.add_parser("set", help="write one feature")
    p_set.add_argument("monitor", help="id, name or position")
    p_set.add_argument(
        "feature", choices=sorted(FEATURES), metavar="feature", help=feature_help
    )
    p_set.add_argument("value", type=int, help="raw VCP value")
    p_set.set_defaults(handler=_cmd_set)

    p_save = sub.add_parser(
        "save",
        help=(
            "save the current settings of every monitor into the profile it is "
            "using, and push the controls a monitor forgets (through 50) so the "
            "screen matches"
        ),
    )
    p_save.set_defaults(handler=_cmd_save)

    p_restore = sub.add_parser(
        "restore",
        help=(
            "put the profile each monitor is using back: what drifted, plus "
            "every control a monitor forgets (through 50)"
        ),
    )
    p_restore.set_defaults(handler=_cmd_restore)

    p_autostart = sub.add_parser(
        "autostart", help="start the tray app with Windows (per-user Run key)"
    )
    autostart_actions = p_autostart.add_subparsers(dest="action", required=True)
    autostart_actions.add_parser(
        "enable", help="start with Windows (no administrator rights needed)"
    )
    autostart_actions.add_parser("disable", help="stop starting with Windows")
    p_status = autostart_actions.add_parser(
        "status", help="show whether it starts with Windows, and the app's recent log"
    )
    p_status.add_argument(
        "--lines", type=_count, default=20, help="log lines to show (default 20)"
    )
    p_autostart.set_defaults(command_handler=_cmd_autostart)

    p_app = sub.add_parser("app", help="run the tray app")
    p_app.add_argument(
        "--startup", action="store_true", help="stay in the tray instead of opening"
    )
    p_app.set_defaults(command_handler=_cmd_app)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = _build_parser().parse_args(argv)

    with contextlib.ExitStack() as stack:
        if args.log_file is not None:
            # Under pythonw there is no console: stdout and stderr are None and
            # everything printed would vanish.  Sending both to the file keeps
            # the restore report, not just the log records.
            log = stack.enter_context(_open_log_file(args.log_file))
            stack.enter_context(contextlib.redirect_stdout(log))
            stack.enter_context(contextlib.redirect_stderr(log))
            _configure_logging(args.verbose, stream=log)
            _log.info("monitorctl %s: %s", __version__, " ".join(argv or sys.argv[1:]))
        else:
            _configure_logging(args.verbose)
        try:
            return _dispatch(args)
        except Exception:
            if args.log_file is None:
                raise
            # Nobody is watching a logon run; the traceback has to be in the log.
            _log.exception("unexpected failure")
            return 1
        finally:
            # basicConfig handlers hold the stream; detach them before the file
            # closes so nothing logs into a closed handle at shutdown.
            for handler in logging.root.handlers[:]:
                logging.root.removeHandler(handler)
                handler.close()


def _dispatch(args: argparse.Namespace) -> int:
    command_handler = getattr(args, "command_handler", None)
    if command_handler is not None:
        try:
            return command_handler(args)
        except autostart.AutostartError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        backend = _build_backend()
    except DdcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # The CLI and the tray app share one profiles file, so `monitorctl save`
    # and the window's Save button are the same button.
    store = ProfileStore(default_profiles_path(), legacy_path=legacy_snapshot_path())

    # Shared with the tray app, which may be running: the two take turns on
    # the display bus one transaction at a time.
    from .ddc.buslock import BusLock

    with MonitorService(backend, profiles=store, bus_lock=BusLock()) as service:
        try:
            return args.handler(service, args)
        except DdcError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
