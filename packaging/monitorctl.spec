# PyInstaller build definition.  Run it through ``python tools/build.py``,
# which prepares the build environment and the version resource first.
#
# Output: ``dist/monitorctl/`` - a portable folder holding
#   monitorctl.exe       the tray app (windowed: no console ever appears)
#   monitorctl-cli.exe   the command-line tool (console)
#   _internal/           the interpreter and libraries both share
#
# One folder rather than one file: a one-file build unpacks itself to %TEMP%
# on every start (slower, and a second process stays alive as its parent),
# and antivirus products flag that pattern far more often.
#
# optimize=2 drops docstrings and asserts from the bundled bytecode - less to
# load and keep resident.  Nothing in monitorctl reads __doc__ or relies on an
# assert for behaviour.

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ICON = str(ROOT / "monitorctl" / "app" / "assets" / "monitorctl.ico")
VERSION_FILE = str(ROOT / "build" / "version_info.txt")

# Standard-library modules nothing here imports, which PyInstaller's own hooks
# would otherwise drag in through optional imports.
EXCLUDES = [
    "unittest",
    "pydoc",
    "doctest",
    "pdb",
    "lib2to3",
    "http",
    "urllib",
    "email",
    "xmlrpc",
    "sqlite3",
    "asyncio",
    "multiprocessing",
    "concurrent",
    "test",
    # Nothing here hashes or encrypts; these alone pull in libcrypto (5 MB).
    "ssl",
    "_ssl",
    "hashlib",
    "_hashlib",
]


def analysis(entry, datas):
    return Analysis(
        [str(ROOT / "packaging" / entry)],
        pathex=[str(ROOT)],
        datas=datas,
        excludes=EXCLUDES,
        optimize=2,
    )


app = analysis(
    "app_entry.py", datas=[(ICON, "monitorctl/app/assets")]
)
cli = analysis("cli_entry.py", datas=[])

app_exe = EXE(
    PYZ(app.pure),
    app.scripts,
    [],
    exclude_binaries=True,
    name="monitorctl",
    console=False,
    icon=ICON,
    version=VERSION_FILE,
    upx=False,
)
cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name="monitorctl-cli",
    console=True,
    icon=ICON,
    version=VERSION_FILE,
    upx=False,
)

COLLECT(
    app_exe,
    app.binaries,
    app.datas,
    cli_exe,
    cli.binaries,
    cli.datas,
    name="monitorctl",
    upx=False,
)
