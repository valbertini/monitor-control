"""Build the portable Windows package: ``dist/monitorctl-<version>-win64.zip``.

    python tools/build.py

Steps, each repeatable:

1. A dedicated virtual environment, ``.venv-build``, with PyInstaller pinned.
   The build never installs anything into the interpreter you develop with,
   and the same pin produces the same bootloader every time.
2. The Windows version resource, from ``monitorctl.__version__``, so Explorer's
   Properties > Details shows what build this is.
3. PyInstaller, with ``packaging/monitorctl.spec``.
4. A zip of the resulting folder.

The app must not be running from ``dist`` while it is rebuilt: Windows locks a
running executable.  The script asks *that* copy to exit and waits for it; a
copy running from anywhere else - the one that starts with Windows - is left
alone.
"""

from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv-build"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
PYINSTALLER = "pyinstaller==6.22.3"

VERSION_TEMPLATE = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'monitorctl'),
        StringStruct('FileDescription', 'monitorctl - DDC/CI monitor control'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'monitorctl'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', 'monitorctl.exe'),
        StringStruct('ProductName', 'monitorctl'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def run(*args: str | Path) -> None:
    print("+", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, cwd=ROOT)


def version() -> str:
    text = (ROOT / "monitorctl" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text)
    if match is None:
        raise SystemExit("could not read __version__ from monitorctl/__init__.py")
    return ".".join(match.groups())


def stop_copy_running_from(folder: Path) -> None:
    """Close the tray app if, and only if, its executable is under ``folder``.

    ``monitorctl.exe --quit`` would close whichever copy is running, including
    one installed elsewhere; this looks at the process behind the window first.
    """
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.LPDWORD,
    ]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    hwnd = user32.FindWindowW("monitorctl.tray", None)  # app/tray.py CLASS_NAME
    if not hwnd:
        return
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    synchronize, query_limited = 0x00100000, 0x1000
    process = kernel32.OpenProcess(synchronize | query_limited, False, pid.value)
    if not process:
        print("a monitorctl copy is running and could not be inspected; left alone")
        return
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(size)
        ):
            print("could not tell where the running monitorctl lives; left alone")
            return
        image = Path(buffer.value).resolve()
        if not image.is_relative_to(folder.resolve()):
            print(f"monitorctl running from {image.parent} is not touched")
            return
        print(f"closing the copy running from {image.parent}")
        wm_close = 0x0010
        user32.PostMessageW(hwnd, wm_close, 0, 0)
        if kernel32.WaitForSingleObject(process, 15_000) != 0:
            raise SystemExit("the running copy in dist did not exit; close it and retry")
    finally:
        kernel32.CloseHandle(process)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("the package is Windows-only")

    python = VENV / "Scripts" / "python.exe"
    if not python.is_file():
        print(f"creating {VENV}")
        venv.create(VENV, with_pip=True)
    pip = (python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check")
    run(*pip, PYINSTALLER)

    release = version()
    major, minor, patch = (int(part) for part in release.split("."))
    BUILD.mkdir(exist_ok=True)
    (BUILD / "version_info.txt").write_text(
        VERSION_TEMPLATE.format(major=major, minor=minor, patch=patch, version=release),
        encoding="utf-8",
    )

    stop_copy_running_from(DIST)

    run(
        python,
        "-m",
        "PyInstaller",
        ROOT / "packaging" / "monitorctl.spec",
        "--noconfirm",
        "--clean",
        "--distpath",
        DIST,
        "--workpath",
        BUILD / "pyinstaller",
    )

    folder = DIST / "monitorctl"
    archive = DIST / f"monitorctl-{release}-win64.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(folder.rglob("*")):
            zf.write(path, Path("monitorctl") / path.relative_to(folder))

    size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    print(f"\nfolder : {folder}  ({size / 2**20:.1f} MiB)")
    print(f"archive: {archive}  ({archive.stat().st_size / 2**20:.1f} MiB)")
    shutil.rmtree(BUILD / "pyinstaller", ignore_errors=True)


if __name__ == "__main__":
    main()
