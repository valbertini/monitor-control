"""Run the tray app from a source checkout, without a console window.

Double-click it, or let the Run key start it.  The packaged build does not use
this file: there, ``monitorctl.exe`` is the app.

It exists because a ``.pyw`` started by Windows has no working directory to
find the package from, so it puts its own folder on ``sys.path`` first.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitorctl.app.main import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
