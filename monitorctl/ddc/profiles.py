"""Named profiles: several sets of saved values per monitor, one of them in use.

Each monitor has its own list of profiles - "Calibrated", "Night", "Games" -
and its own *active* profile.  The active profiles of all monitors together are
what the rest of the program used to call the snapshot: what Restore puts back
and what the keeper maintains (:meth:`ProfileBook.active_snapshot`).  Switching
the profile of one monitor never touches another.

A profile holds every restorable control of its monitor, like Save always has.
The monitor's identity is stored once, beside its profiles, and is matched to a
connected monitor exactly as a snapshot entry is (``snapshot.pair_monitors``):
by EDID first, never by position alone.

Everything here is immutable: a :class:`ProfileBook` method returns a new book,
and :meth:`ProfileStore.update` swaps it in and writes it out atomically.  A
change that is refused raises :class:`~monitorctl.ddc.errors.ProfileError` and
changes nothing, on disk or in memory.

The file is untrusted on the way in, like every state file here: a structurally
invalid one is ignored as a whole (and kept aside before it is overwritten); an
active profile that names no profile just leaves that monitor without one.
Values still go through the write policy when they are applied.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ..fileio import UNREADABLE, atomic_write_text, decode_state, state_directory
from .errors import ProfileError, SnapshotError
from .snapshot import (
    Identity,
    MonitorSnapshot,
    Snapshot,
    pair_monitors,
    parse_values,
)

__all__ = [
    "DEFAULT_PROFILE_NAME",
    "MAX_NAME_LENGTH",
    "MonitorProfiles",
    "Profile",
    "ProfileBook",
    "ProfileStore",
    "check_profile_name",
    "default_profiles_path",
]

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Name of the profile that Save creates for a monitor that has none.
DEFAULT_PROFILE_NAME = "Default"

#: Long enough for a description, short enough for a drop-down.
MAX_NAME_LENGTH = 40


def default_profiles_path() -> Path:
    """Where the profiles live, in the program's state directory."""
    return state_directory() / "profiles.json"


def check_profile_name(name: str) -> str:
    """Return ``name`` trimmed, or refuse it with a message for a person.

    Raises:
        ProfileError: Empty, too long, or containing control characters.
    """
    if not isinstance(name, str):
        raise ProfileError("A profile name must be text.")
    trimmed = name.strip()
    if not trimmed:
        raise ProfileError("A profile needs a name.")
    if len(trimmed) > MAX_NAME_LENGTH:
        raise ProfileError(
            f"A profile name can have at most {MAX_NAME_LENGTH} characters."
        )
    if any(unicodedata.category(char).startswith("C") for char in trimmed):
        raise ProfileError("A profile name cannot contain control characters.")
    return trimmed


def _same_name(a: str, b: str) -> bool:
    # "Night" and "night" side by side in a list would only confuse.
    return a.casefold() == b.casefold()


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Profile:
    """One named set of values for one monitor.

    Attributes:
        name: Unique within its monitor, ignoring case.
        saved_at: Unix time of the last save into it.
        values: Feature key -> raw value.
    """

    name: str
    saved_at: float
    values: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class MonitorProfiles:
    """A monitor, as last seen, and its profiles.

    The identifiers are those of :class:`~monitorctl.ddc.snapshot.MonitorSnapshot`
    and are refreshed on every save, so the device name follows Windows'
    renumbering while the EDID identity keeps the match honest.

    Attributes:
        active: Name of the profile in use, or ``""`` for none.
    """

    id: str
    name: str
    device: str
    hardware_id: str
    profiles: tuple[Profile, ...] = ()
    active: str = ""

    def profile(self, name: str) -> Profile | None:
        return next((p for p in self.profiles if _same_name(p.name, name)), None)

    @property
    def active_profile(self) -> Profile | None:
        return self.profile(self.active) if self.active else None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.profiles)

    def as_snapshot(self) -> MonitorSnapshot | None:
        """The active profile as a snapshot entry, or ``None`` without one."""
        active = self.active_profile
        if active is None:
            return None
        return MonitorSnapshot(
            id=self.id,
            name=self.name,
            device=self.device,
            values=active.values,
            hardware_id=self.hardware_id,
        )

    def _identified_as(self, monitor: Identity) -> MonitorProfiles:
        return dataclasses.replace(
            self,
            id=monitor.id,
            name=monitor.name,
            device=monitor.device,
            # An EDID that could not be read this time is no reason to forget
            # the one read before.
            hardware_id=monitor.hardware_id or self.hardware_id,
        )

    def _unused_name(self, base: str) -> str:
        if self.profile(base) is None:
            return base
        n = 2
        while self.profile(f"{base} {n}") is not None:
            n += 1
        return f"{base} {n}"


# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProfileBook:
    """Every monitor's profiles: the whole content of the profiles file."""

    monitors: tuple[MonitorProfiles, ...] = ()

    # Queries ----------------------------------------------------------------- #

    def active_snapshot(self) -> Snapshot | None:
        """The active profile of every monitor that has one, or ``None``.

        ``saved_at`` is that of the most recently saved of them.
        """
        in_use = [(m, m.active_profile) for m in self.monitors]
        in_use = [(m, p) for m, p in in_use if p is not None]
        if not in_use:
            return None
        return Snapshot(
            saved_at=max(p.saved_at for _m, p in in_use),
            monitors=tuple(m.as_snapshot() for m, _p in in_use),
        )

    def index_for(self, monitor: Identity, present: Sequence[Identity]) -> int | None:
        """Position of ``monitor``'s entry, matched against all of ``present``.

        Matching against every connected monitor, not ``monitor`` alone, is
        what makes the answer the same one a restore reaches.
        """
        for position, (_entry, state) in enumerate(pair_monitors(self.monitors, present)):
            if state is not None and state.id == monitor.id:
                return position
        return None

    # Changes ----------------------------------------------------------------- #

    def saving(
        self,
        captured: Sequence[tuple[Identity, Mapping[str, int]]],
        present: Sequence[Identity],
        saved_at: float,
        keep: Mapping[str, frozenset[str]] | None = None,
    ) -> ProfileBook:
        """Store each monitor's values into its active profile.

        A monitor without an active profile gets a new one,
        :data:`DEFAULT_PROFILE_NAME`; a monitor never seen before gets an
        entry.  Monitors not in ``captured`` - unplugged ones included - keep
        everything they had.

        Args:
            keep: By monitor id, keys whose value could not be read this time:
                the active profile's value for them is carried over instead of
                the key disappearing.
        """
        book = self
        for monitor, captured_values in captured:
            values = dict(captured_values)
            position = book.index_for(monitor, present)
            if position is None:
                profile = Profile(DEFAULT_PROFILE_NAME, saved_at, values)
                entry = MonitorProfiles(
                    id=monitor.id,
                    name=monitor.name,
                    device=monitor.device,
                    hardware_id=monitor.hardware_id,
                    profiles=(profile,),
                    active=profile.name,
                )
                book = ProfileBook((*book.monitors, entry))
                continue
            entry = book.monitors[position]._identified_as(monitor)
            active = entry.active_profile
            if active is None:
                name = entry._unused_name(DEFAULT_PROFILE_NAME)
                entry = dataclasses.replace(
                    entry,
                    profiles=(*entry.profiles, Profile(name, saved_at, values)),
                    active=name,
                )
            else:
                kept = (keep or {}).get(monitor.id, frozenset())
                for key in kept:
                    if key in active.values and key not in values:
                        values[key] = active.values[key]
                entry = dataclasses.replace(
                    entry,
                    profiles=tuple(
                        Profile(p.name, saved_at, values) if p is active else p
                        for p in entry.profiles
                    ),
                )
            book = book._with(position, entry)
        return book

    def creating(
        self,
        monitor: Identity,
        present: Sequence[Identity],
        name: str,
        values: Mapping[str, int],
        saved_at: float,
    ) -> ProfileBook:
        """Add a profile holding ``values`` and make it the active one.

        Raises:
            ProfileError: Invalid name, or one this monitor already uses.
        """
        name = check_profile_name(name)
        position = self.index_for(monitor, present)
        profile = Profile(name, saved_at, values)
        if position is None:
            entry = MonitorProfiles(
                id=monitor.id,
                name=monitor.name,
                device=monitor.device,
                hardware_id=monitor.hardware_id,
                profiles=(profile,),
                active=name,
            )
            return ProfileBook((*self.monitors, entry))
        entry = self.monitors[position]
        if entry.profile(name) is not None:
            raise ProfileError(f"{monitor.name} already has a profile named {name!r}.")
        entry = dataclasses.replace(
            entry._identified_as(monitor),
            profiles=(*entry.profiles, profile),
            active=name,
        )
        return self._with(position, entry)

    def activating(
        self, monitor: Identity, present: Sequence[Identity], name: str
    ) -> ProfileBook:
        """Make ``name`` the active profile.  Writes nothing to the monitor.

        Raises:
            ProfileError: The monitor has no such profile.
        """
        position, entry = self._require(monitor, present)
        profile = entry.profile(name)
        if profile is None:
            raise ProfileError(f"{monitor.name} has no profile named {name!r}.")
        return self._with(position, dataclasses.replace(entry, active=profile.name))

    def renaming(
        self, monitor: Identity, present: Sequence[Identity], old: str, new: str
    ) -> ProfileBook:
        """Rename a profile; the active one stays active under its new name.

        Raises:
            ProfileError: Unknown profile, or an invalid or taken new name.
        """
        new = check_profile_name(new)
        position, entry = self._require(monitor, present)
        profile = entry.profile(old)
        if profile is None:
            raise ProfileError(f"{monitor.name} has no profile named {old!r}.")
        clash = entry.profile(new)
        if clash is not None and clash is not profile:
            raise ProfileError(f"{monitor.name} already has a profile named {new!r}.")
        renamed = Profile(new, profile.saved_at, profile.values)
        entry = dataclasses.replace(
            entry,
            profiles=tuple(renamed if p is profile else p for p in entry.profiles),
            active=new if entry.active_profile is profile else entry.active,
        )
        return self._with(position, entry)

    def deleting(
        self, monitor: Identity, present: Sequence[Identity], name: str
    ) -> ProfileBook:
        """Remove a profile that is not in use.

        The active profile cannot be deleted: the keeper and Restore would be
        left applying nothing, while the screen still shows its values.
        Switch to another profile first.

        Raises:
            ProfileError: Unknown profile, or the active one.
        """
        position, entry = self._require(monitor, present)
        profile = entry.profile(name)
        if profile is None:
            raise ProfileError(f"{monitor.name} has no profile named {name!r}.")
        if profile is entry.active_profile:
            raise ProfileError(
                f"{profile.name!r} is the profile in use; switch to another one first."
            )
        entry = dataclasses.replace(
            entry, profiles=tuple(p for p in entry.profiles if p is not profile)
        )
        return self._with(position, entry)

    def _require(
        self, monitor: Identity, present: Sequence[Identity]
    ) -> tuple[int, MonitorProfiles]:
        position = self.index_for(monitor, present)
        if position is None:
            raise ProfileError(f"{monitor.name} has no profiles yet.")
        return position, self.monitors[position]

    def _with(self, position: int, entry: MonitorProfiles) -> ProfileBook:
        monitors = list(self.monitors)
        monitors[position] = entry
        return ProfileBook(tuple(monitors))

    # Files ------------------------------------------------------------------- #

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot) -> ProfileBook:
        """Carry a pre-profiles snapshot over: one active "Default" each."""
        return cls(
            tuple(
                MonitorProfiles(
                    id=m.id,
                    name=m.name,
                    device=m.device,
                    hardware_id=m.hardware_id,
                    profiles=(
                        Profile(DEFAULT_PROFILE_NAME, snapshot.saved_at, m.values),
                    ),
                    active=DEFAULT_PROFILE_NAME,
                )
                for m in snapshot.monitors
            )
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "version": SCHEMA_VERSION,
            "monitors": [
                {
                    "id": m.id,
                    "name": m.name,
                    "device": m.device,
                    "hardwareId": m.hardware_id,
                    "active": m.active,
                    "profiles": [
                        {"name": p.name, "savedAt": p.saved_at, "values": dict(p.values)}
                        for p in m.profiles
                    ],
                }
                for m in self.monitors
            ],
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProfileBook:
        """Rebuild a book from parsed JSON.

        Raises:
            ValueError: If the payload is not a book this version can read.
        """
        if not isinstance(payload, dict):
            raise ValueError("profiles must be a JSON object")
        if payload.get("version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported profiles version {payload.get('version')!r}")
        raw_monitors = payload.get("monitors")
        if not isinstance(raw_monitors, list):
            raise ValueError("monitors must be a list")
        return cls(tuple(_parse_monitor(raw) for raw in raw_monitors))


def _text(raw: dict[str, object], field: str) -> str:
    value = raw.get(field, "")
    if not isinstance(value, str):
        raise ValueError(f"{field!r} must be a string")
    return value


def _parse_monitor(raw: object) -> MonitorProfiles:
    if not isinstance(raw, dict):
        raise ValueError("each monitor entry must be a JSON object")
    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ValueError("profiles must be a list")
    profiles: list[Profile] = []
    for item in raw_profiles:
        if not isinstance(item, dict):
            raise ValueError("each profile must be a JSON object")
        try:
            name = check_profile_name(_text(item, "name"))
        except ProfileError as exc:
            raise ValueError(str(exc)) from exc
        if any(_same_name(name, p.name) for p in profiles):
            raise ValueError(f"duplicate profile name {name!r}")
        saved_at = item.get("savedAt")
        if isinstance(saved_at, bool) or not isinstance(saved_at, int | float):
            raise ValueError("savedAt must be a number")
        profiles.append(Profile(name, float(saved_at), parse_values(item.get("values"))))
    entry = MonitorProfiles(
        id=_text(raw, "id"),
        name=_text(raw, "name"),
        device=_text(raw, "device"),
        hardware_id=_text(raw, "hardwareId"),
        profiles=tuple(profiles),
    )
    active = _text(raw, "active")
    if active:
        profile = entry.profile(active)
        if profile is None:
            _log.warning(
                "%s: active profile %r does not exist; none is active", entry.name, active
            )
        else:
            entry = dataclasses.replace(entry, active=profile.name)
    return entry


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #


class ProfileStore:
    """Holds the profile book, optionally backed by a file.

    Without a path it lives in memory only, which is what tests get.  With one,
    every change is written atomically, and a change made by another process
    - ``monitorctl save`` beside the tray app - is noticed on the next read by
    the file's identity, time and size, at the cost of one ``stat``.
    """

    def __init__(
        self, path: Path | str | None = None, *, legacy_path: Path | str | None = None
    ) -> None:
        """
        Args:
            path: The profiles file, or ``None`` for memory only.
            legacy_path: A ``snapshot.json`` from before profiles.  Read only
                while the profiles file does not exist, so that its values
                become every monitor's "Default" profile instead of vanishing
                on upgrade.  Never written or deleted.
        """
        self._path = Path(path) if path is not None else None
        self._legacy = Path(legacy_path) if legacy_path is not None else None
        self._lock = threading.Lock()
        self._book = ProfileBook()
        self._stamp: tuple[int, int, int] | None = None
        self._loaded = self._path is None
        #: The file on disk could not be parsed; it is kept aside, not
        #: silently overwritten, on the next write.
        self._unreadable = False
        #: The last attempt to read the file failed outright (not a parse
        #: error): what is held may be stale, so no change is written over it.
        self._read_failed = False

    @property
    def path(self) -> Path | None:
        return self._path

    def book(self) -> ProfileBook:
        """The current book, read again if the file changed."""
        with self._lock:
            return self._current_locked()

    def load(self) -> Snapshot | None:
        """The active profiles as a snapshot, or ``None`` if none is active."""
        return self.book().active_snapshot()

    def save(
        self,
        snapshot: Snapshot,
        present: Sequence[Identity] | None = None,
        *,
        keep: Mapping[str, frozenset[str]] | None = None,
    ) -> ProfileBook:
        """Store ``snapshot``'s values into the active profiles it names.

        Args:
            snapshot: The values to store, one entry per monitor.
            present: Every connected monitor, for matching; defaults to the
                snapshot's own entries.
            keep: See :meth:`ProfileBook.saving`.

        Raises:
            SnapshotError: The file could not be written.  The change is kept
                in memory, so a read-only disk costs persistence, not the save.
        """
        captured = [(m, m.values) for m in snapshot.monitors]
        matched_against = present or snapshot.monitors
        return self.update(
            lambda book: book.saving(captured, matched_against, snapshot.saved_at, keep)
        )

    def update(self, change: Callable[[ProfileBook], ProfileBook]) -> ProfileBook:
        """Apply ``change`` to the current book and persist the result.

        Raises:
            ProfileError: ``change`` refused; nothing was changed.
            SnapshotError: The file could not be read just now, so nothing was
                changed - writing a book built without its content would erase
                it; or the file could not be written, and the change is kept in
                memory.
        """
        with self._lock:
            current = self._current_locked()
            if self._read_failed:
                raise SnapshotError(
                    f"could not read {self._path} just now; nothing was changed, "
                    "try again"
                )
            updated = change(current)
            self._book = updated
            self._loaded = True
            if self._path is not None:
                self._write_locked(updated)
            return updated

    # Internals --------------------------------------------------------------- #

    def _file_stamp(self) -> tuple[int, int, int] | None:
        try:
            info = self._path.stat()  # type: ignore[union-attr]
        except OSError:
            return None
        # The file id changes on every save - it is replaced, not rewritten -
        # so two saves within one tick of the file clock still differ.
        return info.st_ino, info.st_mtime_ns, info.st_size

    def _current_locked(self) -> ProfileBook:
        if self._path is None:
            return self._book
        stamp = self._file_stamp()
        if self._loaded and stamp == self._stamp and not self._read_failed:
            return self._book
        try:
            book = self._read_locked(stamp is not None)
        except OSError as exc:
            # Locked by an antivirus or a writer mid-save: a moment, not a
            # verdict.  Keep serving what was read before, and try again next
            # time instead of caching an empty book as if it were the file.
            _log.warning("could not read %s: %s", self._path, exc)
            self._read_failed = True
            return self._book
        self._read_failed = False
        self._loaded = True
        self._stamp = stamp
        self._book = book
        return book

    def _read_locked(self, exists: bool) -> ProfileBook:
        """Parse the file (or the legacy one); ``OSError`` propagates."""
        self._unreadable = False
        if not exists:
            return self._from_legacy()
        raw = self._path.read_bytes()  # type: ignore[union-attr]
        try:
            return ProfileBook.from_payload(json.loads(decode_state(raw)))
        except UNREADABLE as exc:
            _log.warning("ignoring unreadable profiles %s: %s", self._path, exc)
            self._unreadable = True
            return ProfileBook()

    def _from_legacy(self) -> ProfileBook:
        if self._legacy is None:
            return ProfileBook()
        try:
            raw = self._legacy.read_bytes()
        except FileNotFoundError:
            return ProfileBook()
        # Any other OSError propagates, like the main file's: losing the
        # migration to a momentary lock would lose what was saved before.
        try:
            snapshot = Snapshot.from_payload(json.loads(decode_state(raw)))
        except UNREADABLE as exc:
            _log.warning("ignoring unreadable snapshot %s: %s", self._legacy, exc)
            return ProfileBook()
        _log.info(
            "carrying %d monitor(s) over from %s as profile %r",
            len(snapshot.monitors),
            self._legacy,
            DEFAULT_PROFILE_NAME,
        )
        return ProfileBook.from_snapshot(snapshot)

    def _write_locked(self, book: ProfileBook) -> None:
        target: Path = self._path  # type: ignore[assignment]
        if self._unreadable:
            aside = target.with_name(target.stem + ".unreadable.json")
            try:
                os.replace(target, aside)
            except OSError as exc:
                # Writing anyway would destroy the only copy of what the user
                # saved; leave it for inspection and refuse.
                raise SnapshotError(
                    f"could not keep the unreadable {target} aside ({exc}); "
                    "not overwriting it"
                ) from exc
            _log.warning("kept the unreadable %s as %s", target.name, aside.name)
            self._unreadable = False
        text = json.dumps(book.to_payload(), ensure_ascii=False, indent=2) + "\n"
        try:
            atomic_write_text(target, text)
        except OSError as exc:
            raise SnapshotError(f"could not write {target}: {exc}") from exc
        self._stamp = self._file_stamp()
