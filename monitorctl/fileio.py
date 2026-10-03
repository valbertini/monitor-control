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

__all__ = ["atomic_write_text", "state_directory"]


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
