"""User preferences for the tray application.

The settings file is treated exactly like the profiles (``ddc/profiles.py``):
written atomically, and untrusted on the way back in.  A value outside its range
or of the wrong type is replaced by the default *for that field only*, and the
rest of the file is still honoured - one bad field must not throw away every
other choice the user made.  A file that cannot be parsed at all is left on
disk for inspection and the defaults are used.

Whether the app starts with Windows is deliberately *not* stored here.  The Run
key in the registry is the source of truth (``autostart.py``); the user can
change it from Task Manager without this program running, and a copy here would
go stale.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path

from ..fileio import UNREADABLE, atomic_write_text, decode_state, state_directory

__all__ = [
    "MAX_INTERVAL_MINUTES",
    "MIN_INTERVAL_MINUTES",
    "AppSettings",
    "RefreshMode",
    "SettingsStore",
    "Theme",
    "default_settings_path",
]

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

MIN_INTERVAL_MINUTES = 1
#: A day.  Longer than that and the setting stops meaning "keep it applied".
MAX_INTERVAL_MINUTES = 24 * 60

#: ``1180x760+-1920+40``: size, then a position that may be negative on a
#: monitor left of or above the primary one.
_GEOMETRY_RE = re.compile(r"^\d{2,5}x\d{2,5}[+-]-?\d{1,5}[+-]-?\d{1,5}$")


class RefreshMode(Enum):
    """What the periodic check does.

    Both modes only ever write the controls a monitor is known to forget
    (``ddc/quirks.py``), plus the GPU adjustments.  A monitor that keeps its
    settings is never written to on a timer.
    """

    DETECT = "detect"
    """Read only.  Each check asks every forgetful monitor whether it answers;
    when one comes back after not answering - or Windows reports a monitor
    arriving, a display change or a resume - its settings are pushed again,
    through the 50 pivot.  No writes while nothing happens."""

    REWRITE = "rewrite"
    """Push the forgetful controls on every check, through a neighbouring
    value, which is invisible on screen.  For a monitor whose power cycle the
    detection misses."""


class Theme(Enum):
    DARK = "dark"
    LIGHT = "light"


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Everything the user can change in the Settings tab.

    Attributes:
        interval_minutes: Time between periodic checks.
        mode: What a periodic check does.
        paused: Periodic checks and event-driven re-applies are suspended.
        apply_on_start: Push the saved settings once when the app starts,
            which after a reboot is the moment they are most likely lost.
        theme: Window colour scheme.
        auto_apply: Write a change to the monitor as soon as it is made.  Off,
            changes wait in the window until Apply.
        window_geometry: Tk geometry of the window when it was last closed,
            ``WxH+X+Y``, or ``""`` to let Windows place it.
    """

    interval_minutes: int = 1
    mode: RefreshMode = RefreshMode.DETECT
    paused: bool = False
    apply_on_start: bool = True
    theme: Theme = Theme.DARK
    window_geometry: str = ""
    auto_apply: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.interval_minutes, bool) or not isinstance(
            self.interval_minutes, int
        ):
            raise TypeError("interval_minutes must be an integer")
        if not MIN_INTERVAL_MINUTES <= self.interval_minutes <= MAX_INTERVAL_MINUTES:
            raise ValueError(
                f"interval_minutes must be between {MIN_INTERVAL_MINUTES} "
                f"and {MAX_INTERVAL_MINUTES}"
            )
        if not isinstance(self.window_geometry, str):
            raise TypeError("window_geometry must be a string")
        if self.window_geometry and not _GEOMETRY_RE.match(self.window_geometry):
            raise ValueError(f"not a window geometry: {self.window_geometry!r}")

    @property
    def interval_seconds(self) -> float:
        return self.interval_minutes * 60.0

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["mode"] = self.mode.value
        payload["theme"] = self.theme.value
        return {"version": SCHEMA_VERSION, **payload}

    @classmethod
    def from_payload(cls, payload: object) -> AppSettings:
        """Rebuild settings, falling back to the default field by field.

        Raises:
            ValueError: Only if ``payload`` is not a settings object at all.
        """
        if not isinstance(payload, dict):
            raise ValueError("settings must be a JSON object")
        if payload.get("version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported settings version {payload.get('version')!r}")

        result = cls()
        for name, parse in (
            ("interval_minutes", _parse_interval),
            ("mode", RefreshMode),
            ("paused", _parse_bool),
            ("apply_on_start", _parse_bool),
            ("theme", Theme),
            ("window_geometry", _parse_str),
            ("auto_apply", _parse_bool),
        ):
            if name not in payload:
                continue
            try:
                result = replace(result, **{name: parse(payload[name])})
            except (TypeError, ValueError):
                _log.warning(
                    "ignoring invalid setting %s=%r; using the default",
                    name,
                    payload[name],
                )
        return result


def _parse_interval(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError("not an integer")
    return raw  # range is checked by AppSettings itself


def _parse_str(raw: object) -> str:
    if not isinstance(raw, str):
        raise TypeError("not a string")
    return raw


def _parse_bool(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise TypeError("not a boolean")
    return raw


def default_settings_path() -> Path:
    return state_directory() / "settings.json"


class SettingsStore:
    """Holds the current settings and persists every change.

    Thread-safe: the window changes settings on the main thread while the
    worker reads them before every check.
    """

    def __init__(self, path: Path | None = None) -> None:
        """
        Args:
            path: Backing file, or ``None`` to keep settings in memory only.
        """
        self._path = path
        self._lock = threading.Lock()
        self._current = self._load()

    def get(self) -> AppSettings:
        with self._lock:
            return self._current

    def update(self, **changes: object) -> AppSettings:
        """Apply ``changes`` and persist them.

        The in-memory value changes even if the disk write fails, so a
        read-only profile costs persistence across restarts, not the setting.

        Raises:
            TypeError, ValueError: If a change is invalid; nothing is changed.
        """
        with self._lock:
            updated = replace(self._current, **changes)
            self._current = updated
            if self._path is not None:
                try:
                    atomic_write_text(
                        self._path, json.dumps(updated.to_payload(), indent=2) + "\n"
                    )
                except OSError as exc:
                    _log.warning("could not save settings to %s: %s", self._path, exc)
            return updated

    def _load(self) -> AppSettings:
        if self._path is None:
            return AppSettings()
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return AppSettings()
        except OSError as exc:
            _log.warning("could not read %s: %s", self._path, exc)
            return AppSettings()
        try:
            return AppSettings.from_payload(json.loads(decode_state(raw)))
        except UNREADABLE as exc:
            _log.warning("ignoring unreadable settings %s: %s", self._path, exc)
            return AppSettings()
