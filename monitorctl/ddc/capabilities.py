"""Parser for the DDC/CI capabilities string.

A monitor describes itself with a nested, parenthesised ASCII blob::

    (prot(monitor)type(LCD)model(VS248)cmds(01 02 03 07 0C E3 F3)
     vcp(02 04 10 12 14(05 08 0B) 16 18 1A 60(01 03 11) AC AE DF)
     mswhql(1)asset_eep(40)mccs_ver(2.1))

Inside ``vcp(...)``, a bare hex byte is a continuous feature and a byte followed
by a parenthesised list is a non-continuous feature enumerating its permitted
values.

The string arrives in fragments over I2C with no error correction, so it can be
truncated, have dropped characters or carry bit errors.  Every function here is
therefore total: it never raises on malformed input, it records what went wrong
in :attr:`Capabilities.warnings` and returns whatever could be recovered.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

__all__ = ["Capabilities", "parse_capabilities"]

_HEX_BYTE = re.compile(r"^[0-9A-Fa-f]{1,2}$")

#: A run of concatenated hex byte pairs, e.g. ``11120F``.  See _parse_hex_bytes.
_HEX_RUN = re.compile(r"^(?:[0-9A-Fa-f]{2})+$")

#: Hard ceiling on input length.  Real capabilities strings are well under 1 KiB;
#: anything larger is a malfunctioning monitor or a corrupted read, and parsing
#: it would only waste time.
_MAX_LENGTH = 8192

#: The segment name right before a ``(``: whatever junk a dropped character
#: left in front of it is not part of the name.
_SEGMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Structured view of a monitor's capabilities string.

    Attributes:
        raw: The original string, as received.
        model: Value of the ``model(...)`` segment, if present.
        display_type: Value of the ``type(...)`` segment, e.g. ``LCD``.
        protocol: Value of the ``prot(...)`` segment, e.g. ``monitor``.
        mccs_version: Value of the ``mccs_ver(...)`` segment, e.g. ``2.1``.
        vcp: Map of VCP code to the tuple of permitted values.  An empty tuple
            means the monitor listed the code without enumerating values, which
            is how continuous features are declared.
        commands: Set of DDC command opcodes from ``cmds(...)``.
        warnings: Human-readable notes about anything malformed.
    """

    raw: str
    model: str | None = None
    display_type: str | None = None
    protocol: str | None = None
    mccs_version: str | None = None
    vcp: Mapping[int, tuple[int, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    commands: frozenset[int] = frozenset()
    warnings: tuple[str, ...] = ()

    def permitted_values(self, code: int) -> frozenset[int] | None:
        """Values the monitor accepts for a non-continuous feature.

        Returns:
            The advertised set, or ``None`` if the code is absent or was
            declared without an explicit value list.
        """
        values = self.vcp.get(code)
        return frozenset(values) if values else None

    @property
    def is_empty(self) -> bool:
        """Whether nothing useful could be extracted."""
        return not self.vcp and not self.commands


def _sanitize(raw: str) -> tuple[str, list[str]]:
    """Drop non-ASCII and control characters, which are always transport noise."""
    warnings: list[str] = []
    if len(raw) > _MAX_LENGTH:
        warnings.append(
            f"capabilities string truncated at {_MAX_LENGTH} characters "
            f"(received {len(raw)})"
        )
        raw = raw[:_MAX_LENGTH]
    cleaned = "".join(c for c in raw if 0x20 <= ord(c) <= 0x7E)
    dropped = len(raw) - len(cleaned)
    if dropped:
        warnings.append(f"{dropped} non-ASCII character(s) discarded")
    return cleaned, warnings


def _iter_segments(body: str, warnings: list[str]) -> Iterator[tuple[str, str]]:
    """Yield ``(name, inner)`` for each top-level ``name(inner)``.

    Text between segments that is not part of a ``name(...)`` construct is
    ignored, which is what keeps the parser working on strings with dropped
    characters.
    """
    i = 0
    n = len(body)
    while i < n:
        open_at = body.find("(", i)
        if open_at < 0:
            return
        found = _SEGMENT_NAME.search(body[i:open_at].strip())
        name = found.group(0) if found else ""
        close_at = _match_paren(body, open_at)
        if close_at < 0:
            warnings.append(
                f"unclosed parenthesis in segment {name!r}; "
                "the remaining content is assumed truncated"
            )
            yield name, body[open_at + 1 :]
            return
        yield name, body[open_at + 1 : close_at]
        i = close_at + 1


def _match_paren(text: str, open_at: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``open_at``, or -1 if unbalanced."""
    depth = 0
    for i in range(open_at, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _parse_hex_bytes(text: str, warnings: list[str], context: str) -> tuple[int, ...]:
    """Parse a list of hex bytes, skipping unparsable tokens.

    Bytes are normally whitespace-separated, but some firmwares run them
    together: a TCL 25G64 on hand emits ``60(11120F)`` for what the standard
    would write as ``60(11 12 0F)``.  An all-hex token of even length is
    therefore split into pairs rather than discarded.
    """
    out: list[int] = []
    for token in text.split():
        if _HEX_BYTE.match(token):
            out.append(int(token, 16))
        elif _HEX_RUN.match(token):
            out.extend(int(token[i : i + 2], 16) for i in range(0, len(token), 2))
        else:
            warnings.append(f"invalid token {token!r} in {context}")
    return tuple(out)


def _parse_vcp_section(body: str, warnings: list[str]) -> dict[int, tuple[int, ...]]:
    """Parse the contents of ``vcp(...)``.

    Tokens are hex bytes; a token immediately followed by a parenthesised group
    takes that group as its permitted value list.
    """
    result: dict[int, tuple[int, ...]] = {}
    i = 0
    n = len(body)
    pending: int | None = None

    while i < n:
        ch = body[i]

        if ch.isspace():
            i += 1
            continue

        if ch == "(":
            close_at = _match_paren(body, i)
            if close_at < 0:
                warnings.append("unclosed VCP value list")
                close_at = n
            inner = body[i + 1 : close_at]
            if pending is None:
                warnings.append(f"value list {inner!r} has no VCP code attached")
            else:
                values = _parse_hex_bytes(inner, warnings, f"vcp 0x{pending:02X}")
                result[pending] = values
                pending = None
            i = close_at + 1
            continue

        if ch == ")":
            # Stray closing paren from a corrupted read; ignore it.
            i += 1
            continue

        # Accumulate a bare token.
        start = i
        while i < n and not body[i].isspace() and body[i] not in "()":
            i += 1
        token = body[start:i]

        if pending is not None:
            result.setdefault(pending, ())
            pending = None

        if _HEX_BYTE.match(token):
            pending = int(token, 16)
        elif _HEX_RUN.match(token):
            # Concatenated codes, e.g. "10121A".  All but the last are complete;
            # the last stays pending so a following "(...)" can attach to it.
            codes = [int(token[i : i + 2], 16) for i in range(0, len(token), 2)]
            for code in codes[:-1]:
                result.setdefault(code, ())
            pending = codes[-1]
        else:
            warnings.append(f"invalid token {token!r} in the vcp section")

    if pending is not None:
        result.setdefault(pending, ())

    return result


def parse_capabilities(raw: str) -> Capabilities:
    """Parse a capabilities string into structured form.

    Never raises.  Malformed input yields a partially populated
    :class:`Capabilities` with the problems listed in ``warnings``.

    Args:
        raw: The string as returned by the monitor.

    Returns:
        The parsed capabilities.
    """
    if not raw or not raw.strip():
        return Capabilities(raw=raw or "", warnings=("empty capabilities string",))

    cleaned, warnings = _sanitize(raw)
    cleaned = cleaned.strip()

    # The whole payload is normally wrapped in one outer pair of parens.
    if cleaned.startswith("("):
        close_at = _match_paren(cleaned, 0)
        if close_at < 0:
            body = cleaned[1:]
            warnings.append("unclosed outer parenthesis")
        else:
            body = cleaned[1:close_at]
            rest = cleaned[close_at + 1 :].strip()
            if rest.strip(")"):
                # An inner "(" was lost, so the outer pair closed early.  What
                # follows is still the monitor's data; dropping it silently
                # would lose the vcp list without a trace.
                warnings.append(
                    "text after the outer parenthesis (a '(' was probably "
                    "lost); parsed as well"
                )
                body = f"{body} {rest}"
    else:
        body = cleaned
        warnings.append("capabilities string does not start with '('")

    model: str | None = None
    display_type: str | None = None
    protocol: str | None = None
    mccs_version: str | None = None
    vcp: dict[int, tuple[int, ...]] = {}
    commands: set[int] = set()

    for name, inner in _iter_segments(body, warnings):
        key = name.lower()
        if key == "vcp":
            vcp.update(_parse_vcp_section(inner, warnings))
        elif key == "cmds":
            commands.update(_parse_hex_bytes(inner, warnings, "cmds"))
        elif key == "model":
            model = inner.strip() or None
        elif key == "type":
            display_type = inner.strip() or None
        elif key == "prot":
            protocol = inner.strip() or None
        elif key == "mccs_ver":
            mccs_version = inner.strip() or None
        # Other segments (mswhql, asset_eep, vcpname, ...) are not needed here.

    return Capabilities(
        raw=raw,
        model=model,
        display_type=display_type,
        protocol=protocol,
        mccs_version=mccs_version,
        vcp=MappingProxyType(dict(sorted(vcp.items()))),
        commands=frozenset(commands),
        warnings=tuple(warnings),
    )
