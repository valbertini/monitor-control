"""Orchestration layer: enumeration, probing, pacing, retry and safe writes.

This is where the protocol-level concerns from the research are enforced:

* **One lock for everything.**  The display bus is shared and not reentrant, so
  every DDC operation in the process is serialised, whichever thread asks -
  and a cross-process lock (``ddc/buslock.py``) makes the tray app and the CLI
  take turns too.
* **Pacing.**  Minimum delays between operations, taken from ddcutil's
  empirically tuned constants.  ddcutil's own release notes record that
  aggressive timing optimisation broke ``setvcp`` in the field.
* **One retry, only for transient failures.**  Mangled bytes on the wire are
  worth retrying; "feature not supported" is not.
* **Read-back verification.**  The high-level Windows API is documented to
  report success on writes that did not happen, so a write is confirmed by
  reading the value back.

Save, Restore and the profile operations also live here rather than in the
app, because putting saved values back is a batch of ordinary writes and has to
obey every rule above.  See ``ddc/profiles.py`` for how they are stored.

Probing reads *every* registry feature rather than only the ones the monitor
advertises.  Both monitors surveyed for ``docs/monitors/`` implement codes they
do not list, so trusting the capabilities string hides working controls.
Unsupported features are still returned, flagged, so the UI can show them
disabled instead of silently dropping them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeVar

from ..gpu import GpuAdjustment, GpuController, GpuError
from .backend import DdcBackend, PhysicalMonitor
from .capabilities import Capabilities, parse_capabilities
from .errors import (
    DdcBackendError,
    DdcError,
    DdcTransientError,
    DdcUnsupportedFeature,
    FeatureNotFound,
    MonitorNotFound,
    RateLimited,
    SnapshotMissing,
)
from .policy import WriteBudget, check_write
from .profiles import MonitorProfiles, ProfileStore
from .quirks import quirks_for
from .snapshot import (
    NON_RESTORABLE_KEYS,
    MonitorSnapshot,
    Snapshot,
    pair_monitors,
)
from .vcp import FEATURES, FeatureKind, VcpFeature

__all__ = [
    "PIVOT_VALUE",
    "FeatureState",
    "FeatureValueOption",
    "MonitorService",
    "MonitorState",
    "Pacing",
    "Reassert",
    "RestoreIssue",
    "RestoreReport",
    "WriteOutcome",
]

_log = logging.getLogger(__name__)

T = TypeVar("T")

#: The value a volatile control passes through on its way to the target when
#: it is reasserted with :attr:`Reassert.PIVOT`: mid-scale on the usual 0-100.
PIVOT_VALUE = 50


class Reassert(Enum):
    """How a volatile control is pushed so that the firmware acts on it.

    A volatile control (``ddc/quirks.py``) is one the firmware forgets on
    power-off while still reporting the old value, and it ignores a write of the
    value it reports.  Reasserting it means writing through an intermediate
    value first, so the final write is a real change.  The two strategies trade
    certainty against visibility.
    """

    PIVOT = "pivot"
    """Through :data:`PIVOT_VALUE` (or a neighbour when the target *is* the
    pivot).  A large, unmistakable change: used when the user asks for it -
    Save, Restore - and when a monitor has just come back on."""

    NEIGHBOUR = "neighbour"
    """Through ``value - 1`` (``+ 1`` at zero).  Invisible on screen, so it is
    the one used for periodic upkeep."""


@dataclass(frozen=True, slots=True)
class Pacing:
    """Minimum delays between DDC operations, in seconds.

    Defaults mirror ddcutil's ``src/base/parms.h``.  Raising them is always
    safe; lowering them is how monitors start returning corrupted replies.
    """

    between_io: float = 0.040
    """Gap after a read before the next operation."""

    after_write: float = 0.050
    """Gap after a write before the next operation."""

    after_capabilities: float = 0.100
    """Gap after a capabilities read.

    Longer than a plain read because the capabilities string is a multi-part
    transaction: the driver exchanges several fragments, so the bus has been
    busy for far longer than a single get by the time it returns.
    """

    retry_backoff: float = 0.100
    """Extra pause before retrying a transient failure."""

    def __post_init__(self) -> None:
        for name in ("between_io", "after_write", "after_capabilities", "retry_backoff"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class FeatureValueOption:
    """One selectable value of a non-continuous feature."""

    value: int
    label: str


@dataclass(frozen=True, slots=True)
class FeatureState:
    """Current state of one feature on one monitor.

    A feature the monitor does not implement is still present, with
    ``supported`` false and no value, so the UI can render it disabled.
    """

    key: str
    code: int | None
    label: str
    kind: str
    category: str
    tooltip: str
    supported: bool
    writable: bool
    #: Which mechanism applies this control: the monitor over DDC/CI, or the
    #: GPU.  The UI labels them differently because the trade-offs differ.
    source: str = "DDC/CI"
    value: int | None = None
    maximum: int | None = None
    percent: int | None = None
    options: tuple[FeatureValueOption, ...] = ()
    error: str | None = None
    #: For a GPU adjustment, the value meaning "no adjustment".
    neutral: int | None = None
    #: The read failed for a reason other than "not implemented" - a garbled
    #: reply, a monitor mid power-up.  Shown unavailable for now, but its saved
    #: value is not dropped from a profile on that evidence.
    read_failed: bool = False


@dataclass(frozen=True, slots=True)
class MonitorState:
    """A monitor and everything we know about it."""

    id: str
    name: str
    device: str
    primary: bool
    index: int
    model: str | None
    display_type: str | None
    mccs_version: str | None
    features: tuple[FeatureState, ...]
    capabilities_raw: str
    warnings: tuple[str, ...]
    #: EDID identity (``"TCL-1003-2E675AA021124"``), or ``""`` if unreadable.
    #: The only identifier here that follows the panel instead of the port.
    hardware_id: str = ""
    #: Controls this model forgets on power-off while still reporting them.
    #: See ``ddc/quirks.py``.
    volatile: frozenset[str] = frozenset()

    def feature(self, key: str) -> FeatureState | None:
        """Look up a feature state by key."""
        return next((f for f in self.features if f.key == key), None)

    @property
    def supported_features(self) -> tuple[FeatureState, ...]:
        """Only the features this monitor actually implements."""
        return tuple(f for f in self.features if f.supported)

    @property
    def probed(self) -> bool:
        """Whether the DDC/CI values were read, as opposed to only enumerated.

        An enumeration without probing leaves every value ``None``.  Anything
        that compares against current values - saving them, deciding what a
        restore has to write - must not run on such a state.
        """
        return not any(
            f.code is not None and f.supported and f.value is None for f in self.features
        )


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """Result of a write, including what the monitor actually ended up at."""

    monitor_id: str
    key: str
    requested: int
    applied: int
    """The value sent after policy clamping."""
    confirmed: int | None
    """Value read back, or ``None`` if verification could not be performed."""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RestoreIssue:
    """One saved value that a restore could not put back.

    Attributes:
        monitor_id: Monitor the value belongs to.
        monitor_name: Its name, so a message can be written without a second
            lookup.
        key: Feature key, or ``""`` when the whole monitor is the problem.
        reason: What stopped it, phrased for a person.
    """

    monitor_id: str
    monitor_name: str
    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """What a restore actually did.

    A restore is a bulk operation over hardware that can refuse any individual
    write, so it reports per-value rather than succeeding or failing as a whole:
    a monitor that was unplugged since the snapshot must not prevent the others
    from being put back.

    Attributes:
        saved_at: When the snapshot being restored was taken.
        applied: Writes that were performed, in the order they were sent.
        unchanged: Values that already matched and were therefore not written.
        skipped: Values that no longer apply, e.g. a monitor that is gone.
        failed: Values that were attempted and refused by policy or hardware.
    """

    saved_at: float
    applied: tuple[WriteOutcome, ...]
    unchanged: int
    skipped: tuple[RestoreIssue, ...]
    failed: tuple[RestoreIssue, ...]


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or fallback


def _unique_by_hardware_id(states: Iterable[MonitorState]) -> dict[str, MonitorState]:
    """States by EDID identity, leaving out any identity shared by several."""
    states = list(states)
    counts = Counter(s.hardware_id for s in states if s.hardware_id)
    return {
        s.hardware_id: s for s in states if s.hardware_id and counts[s.hardware_id] == 1
    }


def _restorable_values(state: MonitorState) -> dict[str, int]:
    """The values of one monitor that are worth capturing in a snapshot.

    Anything the monitor does not implement has no value to save, and a
    momentary command has no state to put back.
    """
    return {
        feature.key: feature.value
        for feature in state.features
        if feature.supported
        and feature.writable
        and feature.value is not None
        and feature.key not in NON_RESTORABLE_KEYS
    }


class MonitorService:
    """Thread-safe facade over a :class:`~monitorctl.ddc.backend.DdcBackend`."""

    def __init__(
        self,
        backend: DdcBackend,
        *,
        pacing: Pacing | None = None,
        budget: WriteBudget | None = None,
        verify_writes: bool = True,
        gpu: GpuController | None = None,
        profiles: ProfileStore | None = None,
        bus_lock: AbstractContextManager[object] | None = None,
    ) -> None:
        """
        Args:
            backend: Platform transport to drive.
            pacing: Inter-operation delays.  Defaults to ddcutil's values.
            budget: Write rate limiter.  Defaults to 40 burst / 30 per minute.
            verify_writes: Read each written value back to confirm it took.
            gpu: GPU-side fallback controls.  ``None`` discovers whatever the
                machine offers; pass an empty controller to disable them.
            profiles: Where Save/Restore and the profiles keep their state.
                Defaults to an in-memory store: unlike hardware discovery,
                writing to the user's filesystem is not something a caller
                should get by omission, so persistence is opted into by passing
                a store with a path
                (:func:`~monitorctl.ddc.profiles.default_profiles_path`).
            bus_lock: Held around every DDC/CI operation, so that *other
                processes* - the tray app and the CLI - take turns on the bus
                too (``ddc/buslock.py``).  ``None`` means this process is the
                only one that matters, which is true in tests.
        """
        self._backend = backend
        self._gpu = gpu if gpu is not None else GpuController()
        self._pacing = pacing or Pacing()
        self._budget = budget or WriteBudget()
        self._verify_writes = verify_writes
        self._profiles = profiles if profiles is not None else ProfileStore()
        self._bus = bus_lock if bus_lock is not None else contextlib.nullcontext()

        self._lock = threading.RLock()
        self._next_ready_at = 0.0
        self._monitors: dict[str, PhysicalMonitor] = {}
        self._states: dict[str, MonitorState] = {}
        self._closed = False

    # ------------------------------------------------------------------ #
    # Timing and retry
    # ------------------------------------------------------------------ #

    def _wait_turn(self) -> None:
        """Block until the bus has been idle long enough for the next op."""
        wait = self._next_ready_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _mark_done(self, delay: float) -> None:
        self._next_ready_at = time.monotonic() + delay

    def _on_bus(self, op: Callable[[], T], delay: float) -> T:
        """Run ``op`` holding the cross-process bus lock, pacing included.

        The pause after an operation belongs to the bus, not to this process:
        it is held inside the lock so another process cannot start its own
        transaction before the monitor is ready for one.
        """
        with self._bus:
            try:
                return op()
            finally:
                if delay > 0:
                    time.sleep(delay)

    def _run(self, op: Callable[[], T], *, delay: float, what: str) -> T:
        """Execute one paced DDC operation, retrying once on a transient fault."""
        self._wait_turn()
        try:
            result = self._on_bus(op, delay)
            self._mark_done(0.0)
            return result
        except DdcTransientError as exc:
            # The operation's own pause was already taken on the bus; the
            # retry additionally backs off.
            _log.info("Transient failure in %s (%s); retrying once", what, exc)
            self._mark_done(self._pacing.retry_backoff)
            self._wait_turn()
            try:
                result = self._on_bus(op, delay)
                self._mark_done(0.0)
                return result
            except DdcTransientError:
                self._mark_done(0.0)
                raise
        except Exception:
            self._mark_done(0.0)
            raise

    # ------------------------------------------------------------------ #
    # Enumeration
    # ------------------------------------------------------------------ #

    def refresh(self, *, probe: bool = True) -> tuple[MonitorState, ...]:
        """Re-enumerate displays and rebuild their state.

        Without probing, a monitor that was already known *by its EDID*
        keeps the values read for it before: re-enumerating only renews the
        OS handles, and the monitor itself has not changed.  This is what lets
        the tray app re-enumerate on every display change without throwing away
        a probe that took seconds.  A monitor without an EDID identity is never
        assumed to be the same one.

        Args:
            probe: Read the current value of every registry feature.  Set to
                ``False`` when only the monitor list is needed: probing costs
                one DDC round trip per feature per monitor, and issuing that
                many transactions just to resolve a monitor name is the kind of
                traffic that congests the display bus.

        Returns:
            The freshly built states, in enumeration order.
        """
        with self._lock:
            self._ensure_open()
            # Only an EDID identity that names one monitor on both sides: two
            # identical panels without a serial share one, and carrying values
            # by it would hand one unit's readings to the other.
            previous = _unique_by_hardware_id(self._states.values())
            physical = self._backend.enumerate()
            fresh_ids = Counter(m.edid.hardware_id for m in physical if m.edid)
            # The GPU side holds per-output handles too, and they go stale
            # for the same reasons the monitor handles just did.
            self._gpu.refresh()
            self._monitors.clear()
            self._states.clear()

            used_ids: set[str] = set()
            states: list[MonitorState] = []
            for monitor in physical:
                state = self._build_state(monitor, used_ids, probe=probe)
                if (
                    not probe
                    and state.hardware_id in previous
                    and fresh_ids[state.hardware_id] == 1
                ):
                    state = self._carry_over(previous[state.hardware_id], state)
                used_ids.add(state.id)
                self._monitors[state.id] = monitor
                self._states[state.id] = state
                states.append(state)
            return tuple(states)

    @staticmethod
    def _carry_over(known: MonitorState, fresh: MonitorState) -> MonitorState:
        """``fresh``, with the DDC/CI values already read for ``known``.

        GPU values are not carried: they were just re-read, for free, and are
        the newer of the two.  Nothing is carried from a state that was never
        probed, or whose feature list differs (a different build's registry).
        """
        if not known.probed:
            return fresh
        if [f.key for f in known.features] != [f.key for f in fresh.features]:
            return fresh
        features = tuple(
            old if old.code is not None else new
            for old, new in zip(known.features, fresh.features, strict=True)
        )
        return dataclasses.replace(fresh, features=features)

    def _build_state(
        self, monitor: PhysicalMonitor, used_ids: set[str], *, probe: bool
    ) -> MonitorState:
        warnings: list[str] = []

        try:
            raw_caps = self._run(
                lambda: self._backend.read_capabilities(monitor),
                delay=self._pacing.after_capabilities,
                what="read_capabilities",
            )
        except DdcBackendError as exc:
            raw_caps = ""
            warnings.append(f"could not read the capabilities string: {exc}")

        caps = parse_capabilities(raw_caps) if raw_caps else parse_capabilities("")
        warnings.extend(caps.warnings)

        base_id = _slugify(
            monitor.device or monitor.description, f"monitor-{monitor.index}"
        )
        monitor_id = base_id
        suffix = 2
        while monitor_id in used_ids:
            monitor_id = f"{base_id}-{suffix}"
            suffix += 1

        edid = monitor.edid
        # The EDID name is the fallback rather than the first choice so that
        # names already in saved snapshots stay stable for monitors whose
        # capabilities string does carry a model.
        name = (
            caps.model
            or (edid.name if edid else "")
            or monitor.description
            or monitor.device
            or monitor_id
        )
        quirks = quirks_for(edid)

        features = self._probe_features(monitor, caps, warnings, probe=probe)
        for warning in warnings:
            # Diagnostics, not news: the same ones come back on every
            # enumeration, so they stay out of the default log level.
            _log.debug("%s: %s", name, warning)

        return MonitorState(
            id=monitor_id,
            name=name,
            device=monitor.device,
            primary=monitor.primary,
            index=monitor.index,
            model=caps.model,
            display_type=caps.display_type,
            mccs_version=caps.mccs_version,
            features=features,
            capabilities_raw=raw_caps,
            warnings=tuple(warnings),
            hardware_id=edid.hardware_id if edid else "",
            volatile=quirks.volatile if quirks else frozenset(),
        )

    def _probe_features(
        self,
        monitor: PhysicalMonitor,
        caps: Capabilities,
        warnings: list[str],
        *,
        probe: bool,
    ) -> tuple[FeatureState, ...]:
        """Read every registry feature, keeping the unsupported ones flagged.

        The capabilities string is used for value labels and permitted-value
        lists, never to decide whether to probe: monitors under-report.
        """
        if caps.is_empty:
            warnings.append(
                "monitor published no usable capabilities string; "
                "features were discovered by direct probing"
            )

        if not probe:
            states = [self._unprobed_state(f, caps) for f in FEATURES.values()]
        else:
            states = [
                self._probe_one(monitor, feature, caps) for feature in FEATURES.values()
            ]
        states.extend(self._gpu_states(monitor.device))
        return tuple(states)

    def _gpu_states(self, device: str) -> list[FeatureState]:
        """Build states for the GPU-side controls available on this output.

        These cost no DDC traffic, so they are read even in the lightweight
        path where VCP features are left unprobed.
        """
        return [
            self._gpu_state(device, adjustment)
            for adjustment in self._gpu.adjustments_for(device)
        ]

    def _gpu_state(self, device: str, adjustment: GpuAdjustment) -> FeatureState:
        base = {
            "key": adjustment.key,
            "code": None,
            "label": adjustment.label,
            "kind": FeatureKind.CONTINUOUS.value,
            "category": adjustment.category,
            "tooltip": adjustment.tooltip,
            "source": adjustment.source,
            "neutral": adjustment.neutral,
        }
        try:
            reading = self._gpu.get(device, adjustment.key)
        except GpuError as exc:
            return FeatureState(**base, supported=False, writable=False, error=str(exc))
        span = reading.maximum - reading.minimum
        percent = (
            round((reading.current - reading.minimum) * 100 / span) if span > 0 else None
        )
        return FeatureState(
            **base,
            supported=True,
            writable=adjustment.writable,
            value=reading.current,
            maximum=reading.maximum,
            percent=percent,
        )

    def _unprobed_state(self, feature: VcpFeature, caps: Capabilities) -> FeatureState:
        """Metadata-only state, with no value read from the hardware.

        ``supported`` is optimistic here; the hardware call will say otherwise
        if the feature is absent.
        """
        return FeatureState(
            key=feature.key,
            code=feature.code,
            label=feature.label,
            kind=feature.kind.value,
            category=feature.category.value,
            tooltip=feature.tooltip,
            supported=True,
            writable=True,
            options=self._options_for(feature, caps),
        )

    def _base_state(
        self, feature: VcpFeature, caps: Capabilities, **overrides: object
    ) -> FeatureState:
        fields: dict[str, object] = {
            "key": feature.key,
            "code": feature.code,
            "label": feature.label,
            "kind": feature.kind.value,
            "category": feature.category.value,
            "tooltip": feature.tooltip,
            "options": self._options_for(feature, caps),
            "supported": False,
            "writable": False,
        }
        fields.update(overrides)
        return FeatureState(**fields)  # type: ignore[arg-type]

    def _probe_one(
        self,
        monitor: PhysicalMonitor,
        feature: VcpFeature,
        caps: Capabilities,
        *,
        strict: bool = False,
    ) -> FeatureState:
        """Read one feature into a state.

        Args:
            strict: Raise a failure other than "unsupported" instead of turning
                it into an unavailable state.  A full probe wants the state -
                one control must not stop the others - but re-reading a single
                control that was working must not have one garbled reply mark
                it unsupported for the rest of the session.
        """
        try:
            reading = self._run(
                lambda: self._backend.get_vcp(monitor, feature.code),
                delay=self._pacing.between_io,
                what=f"get_vcp(0x{feature.code:02X})",
            )
        except DdcUnsupportedFeature:
            return self._base_state(
                feature,
                caps,
                error="This monitor does not implement this feature.",
            )
        except DdcBackendError as exc:
            if strict:
                raise
            return self._base_state(feature, caps, error=str(exc), read_failed=True)

        percent = None
        if feature.kind is FeatureKind.CONTINUOUS and reading.maximum > 0:
            percent = round(reading.current * 100 / reading.maximum)

        return self._base_state(
            feature,
            caps,
            supported=True,
            writable=True,
            value=reading.current,
            maximum=reading.maximum,
            percent=percent,
        )

    @staticmethod
    def _options_for(
        feature: VcpFeature, caps: Capabilities
    ) -> tuple[FeatureValueOption, ...]:
        """Build the selectable options for a non-continuous feature."""
        if feature.kind is not FeatureKind.NON_CONTINUOUS:
            return ()
        advertised = caps.permitted_values(feature.code)
        values = sorted(advertised) if advertised else sorted(feature.values)
        return tuple(
            FeatureValueOption(
                value=v,
                label=feature.values.get(v, f"Value 0x{v:02X}"),
            )
            for v in values
        )

    # ------------------------------------------------------------------ #
    # Public queries
    # ------------------------------------------------------------------ #

    def list_monitors(self, *, probe: bool = True) -> tuple[MonitorState, ...]:
        """Return the cached monitor states, enumerating first if needed.

        Args:
            probe: Passed to :meth:`refresh` when an enumeration is needed.
        """
        with self._lock:
            self._ensure_open()
            if not self._states:
                return self.refresh(probe=probe)
            return tuple(self._states.values())

    def get_monitor(self, monitor_id: str) -> MonitorState:
        """Return one cached monitor state.

        Raises:
            MonitorNotFound: If no monitor has that id.
        """
        with self._lock:
            self.list_monitors()
            state = self._states.get(monitor_id)
            if state is None:
                raise MonitorNotFound(f"unknown monitor: {monitor_id!r}")
            return state

    def read_feature(self, monitor_id: str, key: str) -> FeatureState:
        """Re-read one feature from the hardware and update the cache.

        Raises:
            DdcUnsupportedFeature: The monitor says it does not implement it;
                the cache now says so too.
            DdcBackendError: The read failed some other way (after the one
                retry for a transient fault); the cache is left as it was.
        """
        with self._lock:
            state = self.get_monitor(monitor_id)
            if key.startswith("gpu_"):
                adjustment = next(
                    (a for a in self._gpu.adjustments_for(state.device) if a.key == key),
                    None,
                )
                if adjustment is None:
                    raise FeatureNotFound(f"unknown GPU adjustment {key!r}")
                fresh = self._gpu_state(state.device, adjustment)
                self._replace_feature(monitor_id, fresh)
                if not fresh.supported:
                    raise DdcUnsupportedFeature(fresh.error or "unavailable")
                return fresh
            feature = self._require_feature(key)
            monitor = self._monitors[monitor_id]
            caps = parse_capabilities(state.capabilities_raw)
            # Strict: a failed read raises and leaves the cached state alone.
            fresh = self._probe_one(monitor, feature, caps, strict=True)
            self._replace_feature(monitor_id, fresh)
            if not fresh.supported:
                raise DdcUnsupportedFeature(
                    f"{state.name} does not support {feature.label} "
                    f"(0x{feature.code:02X})"
                )
            return fresh

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def write_feature(
        self,
        monitor_id: str,
        key: str,
        value: int,
        *,
        reassert: Reassert | None = None,
    ) -> WriteOutcome:
        """Validate, meter and apply a write, then confirm it by reading back.

        Args:
            monitor_id: Target monitor.
            key: Feature key.
            value: Raw value to write.
            reassert: For a volatile control, always write through an
                intermediate value using this strategy, whatever the monitor
                reports.  ``None`` steps through a neighbour only when the
                monitor already reports ``value``, which is the minimum needed
                for the write not to be dropped.  Ignored for every other
                control: their read-back can be trusted.

        Raises:
            MonitorNotFound: Unknown monitor id.
            FeatureNotFound: Unknown feature key.
            PolicyViolation: The write was refused by the safety policy.
            RateLimited: The write budget is exhausted.
            DdcUnsupportedFeature: The monitor does not have this feature.
            DdcBackendError: The hardware call failed.
        """
        with self._lock:
            state = self.get_monitor(monitor_id)
            if key.startswith("gpu_"):
                return self._write_gpu(state, key, value)
            feature = self._require_feature(key)
            current = state.feature(key)
            if current is None:
                raise FeatureNotFound(f"{state.name} has no entry for {key!r}")
            if not current.supported:
                raise DdcUnsupportedFeature(
                    f"{state.name} does not support {feature.label} "
                    f"(0x{feature.code:02X})"
                )
            if not current.writable:
                raise DdcUnsupportedFeature(
                    f"{feature.label} cannot be written on this monitor: "
                    f"{current.error or 'reason unknown'}"
                )
            if current.value is None:
                # State was built without probing; the maximum is needed before
                # a continuous value can be validated, so read it now.
                current = self.read_feature(monitor_id, key)

            caps = parse_capabilities(state.capabilities_raw)
            allowed = caps.permitted_values(feature.code)
            applied = check_write(
                feature,
                value,
                maximum=current.maximum or 0,
                allowed_values=allowed,
            )

            warnings: list[str] = []
            if applied != value:
                warnings.append(
                    f"value {value} was clamped to {applied} "
                    f"(monitor maximum: {current.maximum})"
                )

            steps = self._write_steps(state, feature, current, applied, reassert)
            if len(steps) > 1:
                warnings.append(
                    f"stepped through {steps[0]} so the monitor acts on the write "
                    "(it forgets this control at power-off and ignores repeats)"
                )

            # Metered only once the request is known to be valid, so a rejected
            # request never costs budget.  All steps are paid for at once: a
            # budget that ran out between them would leave the control one step
            # off, which is worse than not starting.
            self._budget.consume(len(steps))

            monitor = self._monitors[monitor_id]
            for step in steps:
                self._run(
                    lambda step=step: self._backend.set_vcp(monitor, feature.code, step),
                    delay=self._pacing.after_write,
                    what=f"set_vcp(0x{feature.code:02X})",
                )

            confirmed = self._confirm(monitor, feature, applied, warnings)
            self._replace_feature(
                monitor_id,
                self._feature_state_after_write(current, feature, confirmed, applied),
            )

            return WriteOutcome(
                monitor_id=monitor_id,
                key=key,
                requested=value,
                applied=applied,
                confirmed=confirmed,
                warnings=tuple(warnings),
            )

    @staticmethod
    def _write_steps(
        state: MonitorState,
        feature: VcpFeature,
        current: FeatureState,
        value: int,
        reassert: Reassert | None,
    ) -> list[int]:
        """The values to send so that ``value`` actually takes effect.

        Normally just ``value``.  A volatile control (``ddc/quirks.py``) whose
        firmware already reports ``value`` would drop the write as a no-op -
        and after a power cycle that report is a lie, so the no-op leaves the
        panel on its factory setting.  Writing an intermediate value first turns
        the final write into a real change.  See :class:`Reassert`.
        """
        if (
            feature.key not in state.volatile
            or feature.kind is not FeatureKind.CONTINUOUS
        ):
            return [value]
        maximum = current.maximum or 0
        if reassert is None:
            if current.value != value:
                return [value]
            reassert = Reassert.NEIGHBOUR
        if reassert is Reassert.PIVOT:
            pivot = min(PIVOT_VALUE, maximum)
            if pivot != value:
                return [pivot, value]
        neighbour = value - 1 if value > 0 else value + 1
        if neighbour > maximum:
            return [value]
        return [neighbour, value]

    def _write_gpu(self, state: MonitorState, key: str, value: int) -> WriteOutcome:
        """Apply a GPU-side adjustment.

        No pacing, no write budget: this never reaches the display bus and
        cannot wear an EEPROM.
        """
        current = state.feature(key)
        if current is None:
            raise FeatureNotFound(f"{state.name} has no entry for {key!r}")
        if not current.supported or not current.writable:
            raise DdcUnsupportedFeature(
                f"{current.label} cannot be changed on {state.name}: "
                f"{current.error or 'the driver offers it read-only'}"
            )

        applied = self._gpu.set(state.device, key, value)
        warnings: list[str] = []
        if applied != value:
            warnings.append(f"value {value} was clamped to {applied}")

        maximum = current.maximum or 0
        percent = round(applied * 100 / maximum) if maximum > 0 else None
        self._replace_feature(
            state.id, dataclasses.replace(current, value=applied, percent=percent)
        )
        return WriteOutcome(
            monitor_id=state.id,
            key=key,
            requested=value,
            applied=applied,
            confirmed=applied,
            warnings=tuple(warnings),
        )

    def _confirm(
        self,
        monitor: PhysicalMonitor,
        feature: VcpFeature,
        applied: int,
        warnings: list[str],
    ) -> int | None:
        if not self._verify_writes:
            return None
        try:
            reading = self._run(
                lambda: self._backend.get_vcp(monitor, feature.code),
                delay=self._pacing.between_io,
                what=f"verify 0x{feature.code:02X}",
            )
        except DdcBackendError as exc:
            warnings.append(f"could not confirm the write: {exc}")
            return None
        if reading.current != applied:
            warnings.append(
                f"monitor applied {reading.current} instead of {applied} "
                "(some firmwares round or ignore values)"
            )
        return reading.current

    @staticmethod
    def _feature_state_after_write(
        previous: FeatureState,
        feature: VcpFeature,
        confirmed: int | None,
        applied: int,
    ) -> FeatureState:
        value = confirmed if confirmed is not None else applied
        percent = None
        if feature.kind is FeatureKind.CONTINUOUS and (previous.maximum or 0) > 0:
            percent = round(value * 100 / previous.maximum)  # type: ignore[operator]
        return FeatureState(
            key=previous.key,
            code=previous.code,
            label=previous.label,
            kind=previous.kind,
            category=previous.category,
            tooltip=previous.tooltip,
            supported=previous.supported,
            writable=previous.writable,
            value=value,
            maximum=previous.maximum,
            percent=percent,
            options=previous.options,
            error=None,
        )

    # ------------------------------------------------------------------ #
    # Saved settings and profiles
    # ------------------------------------------------------------------ #

    @property
    def profiles_path(self) -> Path | None:
        """File the profiles are persisted to, or ``None`` when memory-only."""
        return self._profiles.path

    def saved_snapshot(self) -> Snapshot | None:
        """The active profiles - what a restore would apply - or ``None``."""
        with self._lock:
            return self._profiles.load()

    def profiles_by_monitor(self) -> dict[str, MonitorProfiles | None]:
        """Every connected monitor's profiles, by monitor id.

        Matched against all connected monitors at once, the same way a restore
        matches them, so what the window shows is what Restore will use.
        """
        with self._lock:
            present = self.list_monitors(probe=False)
            book = self._profiles.book()
            found: dict[str, MonitorProfiles | None] = {s.id: None for s in present}
            for entry, state in pair_monitors(book.monitors, present):
                if state is not None:
                    found[state.id] = entry
            return found

    def save_snapshot(
        self, *, reread: bool = False, monitor_ids: Iterable[str] | None = None
    ) -> Snapshot:
        """Store the current value of every restorable control in each
        monitor's active profile, creating "Default" for a monitor without one.

        Args:
            reread: Read every value from the monitors first.  Without it the
                values are the cached ones, which every write through this
                service keeps current - the window saves what it shows.  The
                tray, which shows nothing, rereads: a change made with the
                monitor's own buttons is invisible to the cache.
            monitor_ids: Only these monitors; ``None`` means all connected.
                The profiles of the others, connected or not, are untouched.

        Raises:
            SnapshotError: If the profiles could not be persisted.
        """
        with self._lock:
            present = self._probed_monitors()
            wanted = None if monitor_ids is None else frozenset(monitor_ids)
            states = [s for s in present if wanted is None or s.id in wanted]
            if reread:
                states = [self._reread(state) for state in states]
            snapshot = Snapshot(
                saved_at=time.time(),
                monitors=tuple(
                    MonitorSnapshot(
                        id=state.id,
                        name=state.name,
                        device=state.device,
                        values=_restorable_values(state),
                        hardware_id=state.hardware_id,
                    )
                    for state in states
                ),
            )
            # Controls whose read failed have no value here; their saved one
            # stays in the profile rather than being dropped on that evidence.
            unread = {
                state.id: frozenset(f.key for f in state.features if f.read_failed)
                for state in states
            }
            self._profiles.save(snapshot, present, keep=unread)
            _log.info(
                "saved %d settings across %d monitor(s)%s",
                snapshot.value_count,
                len(snapshot.monitors),
                " (read from the monitors first)" if reread else "",
            )
            return snapshot

    def reread(self, monitor_id: str) -> MonitorState:
        """Read every restorable value of one monitor again; returns its state.

        For comparing against the monitor rather than the cache - which the
        monitor's own buttons leave behind - without writing anything.
        """
        with self._lock:
            return self._reread(self.get_monitor(monitor_id))

    def _reread(self, state: MonitorState) -> MonitorState:
        """``state`` with every restorable value read from the hardware again.

        A control that fails to answer keeps its cached value: one flaky read
        is no reason to drop it from the profile.  One that now reports itself
        unsupported is dropped, as a probe would.  One whose earlier read
        failed is tried again, so a probe that hit a bad moment heals here.
        """
        for feature in state.features:
            if feature.key in NON_RESTORABLE_KEYS:
                continue
            if (feature.supported and feature.writable) or feature.read_failed:
                try:
                    self.read_feature(state.id, feature.key)
                except DdcUnsupportedFeature:
                    pass  # read_feature has already marked it unsupported
                except DdcError as exc:  # GpuError is one too
                    _log.info(
                        "%s :: %s kept its cached value: %s", state.name, feature.key, exc
                    )
        return self._states[state.id]

    def create_profile(self, monitor_id: str, name: str) -> None:
        """Save one monitor's current values as a new profile, now active.

        The values are the cached ones, as for the window's Save.

        Raises:
            ProfileError: Invalid name, or one the monitor already uses.
            SnapshotError: If the profiles could not be persisted.
        """
        with self._lock:
            present = self._probed_monitors()
            state = self.get_monitor(monitor_id)
            values = _restorable_values(state)
            now = time.time()
            self._profiles.update(
                lambda book: book.creating(state, present, name, values, now)
            )
            _log.info("%s: new profile %r", state.name, name.strip())

    def activate_profile(
        self, monitor_id: str, name: str, *, reassert: Reassert = Reassert.PIVOT
    ) -> RestoreReport:
        """Make ``name`` the monitor's active profile and put its values back.

        Only that monitor is written, with restore semantics: what drifted,
        plus its volatile controls through ``reassert``.

        Raises:
            ProfileError: The monitor has no such profile.
        """
        with self._lock:
            self.select_profile(monitor_id, name)
            return self._put_back(
                reassert=reassert, monitor_ids=frozenset({monitor_id}), upkeep=False
            )

    def select_profile(self, monitor_id: str, name: str) -> None:
        """Make ``name`` the monitor's active profile.  Writes nothing to it.

        For a window that applies changes only when asked: the profile's
        values are then shown as pending, and written by Apply.

        Raises:
            ProfileError: The monitor has no such profile.
        """
        with self._lock:
            present = self.list_monitors(probe=False)
            state = self.get_monitor(monitor_id)
            self._profiles.update(lambda book: book.activating(state, present, name))
            _log.info("%s: switched to profile %r", state.name, name)

    def rename_profile(self, monitor_id: str, old: str, new: str) -> None:
        """Rename one of a monitor's profiles.  Writes nothing to the monitor.

        Raises:
            ProfileError: Unknown profile, or an invalid or taken new name.
        """
        with self._lock:
            present = self.list_monitors(probe=False)
            state = self.get_monitor(monitor_id)
            self._profiles.update(lambda book: book.renaming(state, present, old, new))

    def delete_profile(self, monitor_id: str, name: str) -> None:
        """Delete one of a monitor's profiles; never the active one.

        Raises:
            ProfileError: Unknown profile, or the active one.
        """
        with self._lock:
            present = self.list_monitors(probe=False)
            state = self.get_monitor(monitor_id)
            self._profiles.update(lambda book: book.deleting(state, present, name))
            _log.info("%s: deleted profile %r", state.name, name)

    def restore_snapshot(
        self,
        *,
        reassert: Reassert = Reassert.PIVOT,
        monitor_ids: Iterable[str] | None = None,
    ) -> RestoreReport:
        """Put the active profiles back, writing only the values that drifted.

        "Drifted" is decided against a fresh read of each saved control, not
        against the cache: a change made with the monitor's own buttons is
        invisible to the cache, and would otherwise never be put back.

        Every value goes through :meth:`write_feature`, so the safety policy,
        the write budget and the read-back verification all apply exactly as
        they do to a slider drag.  A file edited by hand can therefore ask for
        nothing that the UI could not.

        The exception to "only what drifted" is a control the model's quirks
        mark as volatile (``ddc/quirks.py``).  Its read-back cannot be believed,
        so it is always rewritten, through an intermediate value chosen by
        ``reassert`` - see :class:`Reassert`.

        Individual values are allowed to fail: a monitor unplugged since the
        save, or a control the firmware has stopped answering, is reported in
        the result rather than aborting the rest.  Exhausting the write budget
        is the one exception - it stops the remaining writes, because every one
        of them would fail the same way and hammering the bus to collect
        identical errors helps nobody.

        Args:
            reassert: Strategy for the volatile controls.
            monitor_ids: Only these monitors; ``None`` means every saved one.

        Raises:
            SnapshotMissing: If no monitor has an active profile.
        """
        wanted = None if monitor_ids is None else frozenset(monitor_ids)
        return self._put_back(reassert=reassert, monitor_ids=wanted, upkeep=False)

    def reassert_saved(
        self, *, reassert: Reassert, monitor_ids: Iterable[str] | None = None
    ) -> RestoreReport:
        """Push the saved values of the controls a monitor cannot be trusted with.

        This is upkeep, not a restore: it touches only what can be lost without
        anyone noticing from the read-back -

        * volatile controls (``ddc/quirks.py``), always rewritten through
          ``reassert``;
        * GPU adjustments, always rewritten, because a driver reload or a
          display hot-plug resets the gamma ramp.  They cost no bus traffic and
          no write budget.

        Everything else is left alone, so calling this on a timer costs no
        EEPROM wear on a monitor that keeps its settings.

        Args:
            reassert: Strategy for the volatile controls.
            monitor_ids: Only these monitors; ``None`` means every connected
                one.  Unknown ids are ignored.

        Raises:
            SnapshotMissing: If no monitor has an active profile.
        """
        wanted = None if monitor_ids is None else frozenset(monitor_ids)
        return self._put_back(reassert=reassert, monitor_ids=wanted, upkeep=True)

    def _put_back(
        self,
        *,
        reassert: Reassert,
        monitor_ids: frozenset[str] | None,
        upkeep: bool,
    ) -> RestoreReport:
        with self._lock:
            book = self._profiles.book()
            snapshot = book.active_snapshot()
            if snapshot is None:
                raise SnapshotMissing("nothing has been saved yet")

            applied: list[WriteOutcome] = []
            skipped: list[RestoreIssue] = []
            failed: list[RestoreIssue] = []
            unchanged = 0
            halted: str | None = None

            # A cheap enumeration is enough here: upkeep writes whatever the
            # monitor reports, and a restore re-reads each control it compares
            # (below) rather than trusting the cache.
            present = self.list_monitors(probe=False)
            # Matched over the whole book, not only the active entries: the
            # same pairing the window and the profile operations use.
            for entry, state in pair_monitors(book.monitors, present):
                saved = entry.as_snapshot()
                if saved is None:
                    continue  # profiles, but none in use: nothing to apply
                if state is None:
                    if not upkeep and monitor_ids is None:
                        skipped.append(
                            RestoreIssue(
                                saved.id, saved.name, "", "this monitor is not connected"
                            )
                        )
                    continue
                if monitor_ids is not None and state.id not in monitor_ids:
                    continue

                pending = dict(saved.values)
                # Registry order, so a coarse control lands before the fine ones
                # it resets: writing a colour preset after its own gains would
                # silently undo them.
                for feature in state.features:
                    if feature.key not in pending:
                        continue
                    value = pending.pop(feature.key)
                    volatile = feature.key in state.volatile
                    gpu = feature.code is None
                    if upkeep and not (volatile or gpu):
                        continue
                    if upkeep and gpu and value == feature.neutral:
                        # Neutral means monitorctl does not own the setting:
                        # re-applying it on every event would overwrite what
                        # the NVIDIA panel, Night Light or a game set (D25).
                        continue
                    if feature.supported and not volatile and not upkeep:
                        # The cache can be days old in the tray app, and the
                        # monitor's own buttons change values behind it.  A
                        # read costs no EEPROM wear; a missed restore costs
                        # the user's calibration.
                        try:
                            feature = self.read_feature(state.id, feature.key)
                        except DdcUnsupportedFeature as exc:
                            skipped.append(
                                RestoreIssue(state.id, state.name, feature.key, str(exc))
                            )
                            continue
                        except DdcError as exc:
                            failed.append(
                                RestoreIssue(state.id, state.name, feature.key, str(exc))
                            )
                            continue
                    if not feature.supported or not feature.writable:
                        skipped.append(
                            RestoreIssue(
                                state.id,
                                state.name,
                                feature.key,
                                feature.error or "not available on this monitor",
                            )
                        )
                    elif feature.value == value and not volatile and not upkeep:
                        unchanged += 1
                    elif halted is not None:
                        failed.append(
                            RestoreIssue(state.id, state.name, feature.key, halted)
                        )
                    else:
                        try:
                            applied.append(
                                self.write_feature(
                                    state.id,
                                    feature.key,
                                    value,
                                    reassert=reassert if volatile else None,
                                )
                            )
                        except RateLimited as exc:
                            halted = str(exc)
                            failed.append(
                                RestoreIssue(state.id, state.name, feature.key, halted)
                            )
                        except DdcError as exc:  # GpuError is one too
                            failed.append(
                                RestoreIssue(state.id, state.name, feature.key, str(exc))
                            )

                if not upkeep:
                    for key in pending:
                        skipped.append(
                            RestoreIssue(
                                state.id,
                                state.name,
                                key,
                                "this build has no such control",
                            )
                        )

            _log.info(
                "%s: %d written, %d already correct, %d skipped, %d failed",
                "upkeep" if upkeep else "restore",
                len(applied),
                unchanged,
                len(skipped),
                len(failed),
            )
            return RestoreReport(
                saved_at=snapshot.saved_at,
                applied=tuple(applied),
                unchanged=unchanged,
                skipped=tuple(skipped),
                failed=tuple(failed),
            )

    def _probed_monitors(self) -> tuple[MonitorState, ...]:
        """The monitors, with every value read.

        Probes again when the cache came from an enumeration without probing -
        which is what the tray app does on every display change.  Without this,
        Save would write a snapshot with every DDC/CI value missing, and Restore
        would see every control as drifted and write all of them.
        """
        states = self.list_monitors()
        if all(state.probed for state in states):
            return states
        _log.info("values not read since the last enumeration; probing first")
        return self.refresh(probe=True)

    def ping(self, monitor_id: str) -> bool:
        """Whether the monitor answers DDC/CI right now: one read, no write.

        Used to notice a monitor that was switched off and back on, which is
        when a volatile control is lost.  The value read is discarded - on such
        a monitor it is exactly the thing that cannot be trusted - only whether
        an answer came back matters.
        """
        with self._lock:
            state = self.get_monitor(monitor_id)
            monitor = self._monitors[monitor_id]
            code = next(
                (f.code for f in state.features if f.code is not None and f.supported),
                FEATURES["brightness"].code,
            )
            try:
                self._run(
                    lambda: self._backend.get_vcp(monitor, code),
                    delay=self._pacing.between_io,
                    what=f"ping 0x{code:02X}",
                )
            except DdcError:
                return False
            return True

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_feature(key: str) -> VcpFeature:
        feature = FEATURES.get(key)
        if feature is None:
            known = ", ".join(sorted(FEATURES))
            raise FeatureNotFound(f"unknown feature {key!r}; known: {known}")
        return feature

    def _replace_feature(self, monitor_id: str, fresh: FeatureState) -> None:
        state = self._states[monitor_id]
        features = tuple(fresh if f.key == fresh.key else f for f in state.features)
        self._states[monitor_id] = dataclasses.replace(state, features=features)

    def _ensure_open(self) -> None:
        if self._closed:
            raise DdcBackendError("MonitorService has already been closed")

    @property
    def write_budget_available(self) -> float:
        """Tokens left in the write budget, for diagnostics."""
        return self._budget.available()

    def close(self) -> None:
        """Release all hardware handles.  Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._monitors.clear()
            self._states.clear()
            try:
                self._backend.close()
            except Exception:  # pragma: no cover - defensive
                _log.exception("failed to close the backend")
            try:
                self._gpu.close()
            except Exception:  # pragma: no cover - defensive
                _log.exception("failed to close the GPU controller")

    def __enter__(self) -> MonitorService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
