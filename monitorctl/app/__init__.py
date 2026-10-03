"""The tray application: a resident process that keeps saved settings applied.

Layout, from the bottom up:

* ``settings``  - the user's preferences, persisted like the profiles are.
* ``keeper``    - decides *when* the saved settings must be pushed again.
                  Pure logic over :class:`~monitorctl.ddc.service.MonitorService`,
                  so it is tested without a message loop, a thread or a monitor.
* ``worker``    - the one thread that talks to the display bus.  Everything
                  slow (probing, writing) runs there, so the UI never blocks.
* ``win32``     - ctypes bindings: tray icon, hidden window, OS notifications.
* ``tray``      - wires the above together on the main thread.
* ``window``    - the Tk window, imported only when it is first opened.

Only ``window`` and ``calibration`` import :mod:`tkinter`.  A session that never
opens the window never pays for Tcl/Tk.
"""
