"""Entry point of the tray app (``monitorctl.exe`` in the packaged build).

Started without arguments - a double-click - it opens its window.  Started
with ``--startup`` - by the Run key - it stays in the tray.  A second launch
never runs a second copy: it asks the first one to show its window and exits.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import threading

from .. import __version__, autostart
from ..fileio import state_directory

__all__ = ["main"]

_log = logging.getLogger("monitorctl")

#: Instance mutex name; scoped to the logon session by ``SingleInstance``.
MUTEX_NAME = "monitorctl.app"

LOG_BYTES = 512 * 1024


def _configure_logging(verbose: bool) -> None:
    """Log to a rotating file in the state directory, and to stderr if any.

    A windowless process has no stderr, so the file is the only record of what
    the app did while nobody was looking - which is all the time.
    """
    directory = state_directory()
    handlers: list[logging.Handler] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                directory / "monitorctl.log",
                maxBytes=LOG_BYTES,
                backupCount=1,
                encoding="utf-8",
            )
        )
    except OSError:
        pass  # a read-only profile costs the log, not the app
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

    def log_unhandled(exc_type, exc, tb) -> None:
        _log.critical("unhandled error", exc_info=(exc_type, exc, tb))

    def log_thread(args: threading.ExceptHookArgs) -> None:
        _log.critical(
            "unhandled error in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_unhandled
    threading.excepthook = log_thread


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="monitorctl", description="monitorctl tray app")
    parser.add_argument(
        autostart.STARTUP_FLAG,
        dest="startup",
        action="store_true",
        help="started with Windows: stay in the tray and wait before the first apply",
    )
    parser.add_argument(
        "--quit",
        action="store_true",
        help="ask a running instance to exit (for update scripts), then exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument(
        "--version", action="version", version=f"monitorctl {__version__}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    _configure_logging(args.verbose)
    if sys.platform != "win32":
        _log.error("the tray app needs Windows")
        return 2

    from . import win32
    from .tray import CLASS_NAME, WM_APP_SHOW, TrayApp

    if args.quit:
        hwnd = win32.find_window(CLASS_NAME)
        if hwnd is None:
            _log.info("--quit: no instance running")
            return 0
        win32.post_message(hwnd, win32.WM_CLOSE)
        _log.info("--quit: asked the running instance to exit")
        return 0

    win32.make_dpi_aware()
    instance = win32.SingleInstance(MUTEX_NAME)
    if instance.already_running:
        hwnd = win32.find_window(CLASS_NAME)
        if hwnd is not None and not args.startup:
            win32.post_message(hwnd, WM_APP_SHOW)
        shown = hwnd is not None and not args.startup
        _log.info("already running; %s", "showing it" if shown else "nothing to do")
        return 0

    _log.info("monitorctl %s starting", __version__)
    try:
        autostart.repair()
    except autostart.AutostartError as exc:
        _log.warning("%s", exc)

    try:
        return TrayApp.create(started_with_windows=args.startup).run()
    finally:
        instance.release()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
