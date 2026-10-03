"""PyInstaller entry point for ``monitorctl-cli.exe``: the diagnostic CLI."""

from monitorctl.cli import main

raise SystemExit(main())
