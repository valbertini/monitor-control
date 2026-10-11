"""Small file helpers shared by every piece of state this program persists.

There are two such pieces - the profiles and the app settings - and both must
survive the same failures: a crash or power cut in the middle of a write,
and a disk that is full or read-only.  Keeping the mechanism in one place keeps
the guarantee identical for both.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

__all__ = ["UNREADABLE", "atomic_write_text", "decode_state", "state_directory"]

#: What reading a damaged state file can raise past ``json.loads`` and the
#: payload parsers: bad bytes and bad JSON (``ValueError``), a value of the
#: wrong type, a number too large for a float, nesting too deep to parse.
#: Each means "unreadable", never "crash": the settings are read when the
#: app starts, so an escaping error would stop it from starting at all.
UNREADABLE = (ValueError, TypeError, OverflowError, RecursionError)


def state_directory() -> Path:
    """Where this program keeps its machine-local state.

    Windows gets ``%LOCALAPPDATA%\\monitorctl``; anything else follows the XDG
    state directory.  Everything here is state the program rewrites, not
    configuration a user maintains by hand, which is exactly the distinction
    between the state directory and the config one.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "monitorctl"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return base / "monitorctl"


def decode_state(raw: bytes) -> str:
    """The text of a state file, as written by this program or edited by hand.

    ``utf-8-sig`` also accepts a byte-order mark, which Notepad and Windows
    PowerShell 5.1 put in front of UTF-8 and plain ``json.loads`` rejects.

    Raises:
        UnicodeDecodeError: Not UTF-8 - one of :data:`UNREADABLE`.
    """
    return raw.decode("utf-8-sig")


def atomic_write_text(target: Path, text: str) -> None:
    """Replace ``target`` with ``text`` so that readers see old or new, never half.

    The text goes to a temporary file in the *same directory*, is flushed to
    disk, and is then renamed over the target.  Same directory because
    ``os.replace`` is only atomic within one filesystem: a temporary file under
    ``%TEMP%`` could land on another volume and silently degrade into a copy.

    Raises:
        OSError: If the directory cannot be created or the file written.  The
            temporary file is removed; the target is untouched.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
