"""PyInstaller entry point for ``monitorctl.exe``: the tray app, no console."""

from monitorctl.app.main import main

raise SystemExit(main())
