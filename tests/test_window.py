"""Smoke tests for the Tk window, driven against the fake backend.

The window is real - every widget is created and every callback runs - but it
is fully transparent, and the worker is replaced by one that runs jobs inline,
so the tests are deterministic and need no monitor.  Skipped where Tk cannot
start (no display).
"""

from __future__ import annotations

import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

from monitorctl.app.settings import AppSettings, RefreshMode, SettingsStore, Theme
from monitorctl.app.window import DOT
from monitorctl.ddc.edid import EdidInfo
from monitorctl.ddc.profiles import ProfileStore
from monitorctl.ddc.service import MonitorService, Pacing
from monitorctl.gpu import GpuController

from .fakes import FakeBackend, FakeMonitor

NO_PACING = Pacing(
    between_io=0.0, after_write=0.0, after_capabilities=0.0, retry_backoff=0.0
)
CAPS = "(prot(monitor)type(LCD)model(TestDisplay)vcp(10 12 14(05 08) 16)mccs_ver(2.1))"
TCL = EdidInfo("TCL", 0x1003, 0xFFFFFFFF, "2E675AA021124", "25G64")


class InlineWorker:
    """Runs every job at once, on the calling thread."""

    def __init__(self, service: MonitorService) -> None:
        self.service = service
        self.labels: list[str] = []

    def submit(self, run, on_done=None, *, label: str = "") -> None:
        self.labels.append(label)
        try:
            result, error = run(self.service), None
        except Exception as exc:
            result, error = None, exc
        if on_done is not None:
            on_done(result, error)

    def reschedule(self) -> None:
        pass


class HoldingWorker(InlineWorker):
    """Queues jobs until :meth:`run_all`: the bus is busy elsewhere."""

    def __init__(self, service: MonitorService) -> None:
        super().__init__(service)
        self.queued: list[tuple] = []

    def submit(self, run, on_done=None, *, label: str = "") -> None:
        self.labels.append(label)
        self.queued.append((run, on_done))

    def run_all(self) -> None:
        while self.queued:
            run, on_done = self.queued.pop(0)
            try:
                result, error = run(self.service), None
            except Exception as exc:
                result, error = None, exc
            if on_done is not None:
                on_done(result, error)


class Bridge:
    def __init__(self, service: MonitorService) -> None:
        self.worker = InlineWorker(service)
        self.settings = SettingsStore(None)
        self.editing: list[bool] = []
        self.hidden = 0
        self.changed = 0

    def settings_changed(self) -> None:
        self.changed += 1

    def set_editing(self, editing: bool) -> None:
        self.editing.append(editing)

    def window_hidden(self) -> None:
        self.hidden += 1

    def tray_status(self) -> str:
        return "Automatic re-apply: every minute (detect)"


class WindowTestCase(unittest.TestCase):
    caps = CAPS

    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - headless machine
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.attributes("-alpha", 0.0)
        self.addCleanup(self.root.destroy)
        # Runs first (cleanups are last in, first out): timers still pending
        # would fire into the destroyed interpreter and print Tcl errors.
        self.addCleanup(self._cancel_timers)

        self.tcl = FakeMonitor(
            device=r"\\.\DISPLAY1",
            capabilities=self.caps,
            values={0x10: 30, 0x12: 50, 0x14: 0x05, 0x16: 70},
            maxima={0x10: 100, 0x12: 100, 0x14: 0x0D, 0x16: 100},
            edid=TCL,
            ignores_same_value=frozenset({0x16}),
        )
        self.service = MonitorService(
            FakeBackend([self.tcl, *self.more_monitors()]),
            pacing=NO_PACING,
            gpu=GpuController([]),
            profiles=ProfileStore(),
        )
        self.addCleanup(self.service.close)
        self.bridge = Bridge(self.service)

        from monitorctl.app.window import MainWindow

        self.window = MainWindow(self.root, self.bridge, icon_path=None)
        self.window.show()
        self.root.update()

    def more_monitors(self) -> list[FakeMonitor]:
        """Monitors connected besides the TCL, after it in the list."""
        return []

    def _cancel_timers(self) -> None:
        for timer in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(timer)

    def texts(self) -> list[str]:
        """Text of every label currently shown in the monitor's tab."""
        found: list[str] = []

        def walk(widget) -> None:
            if widget.winfo_class() == "TLabel" and widget.winfo_ismapped():
                found.append(str(widget.cget("text")))
            for child in widget.winfo_children():
                walk(child)

        self.root.update()
        walk(self.root.nametowidget(self.window.notebook.tabs()[0]))
        return found

    def row(self, key: str):
        monitor_id = self.window.monitors[0].id
        return self.window.rows[(monitor_id, key)]


class TestRendering(WindowTestCase):
    def test_one_tab_per_monitor_plus_settings(self) -> None:
        tabs = [
            self.window.notebook.tab(t, "text").strip()
            for t in self.window.notebook.tabs()
        ]
        self.assertEqual(tabs, ["TestDisplay", "Settings"])

    def test_only_controls_the_monitor_has_get_a_row(self) -> None:
        monitor = self.window.monitors[0]
        supported = {f.key for f in monitor.features if f.supported}
        self.assertEqual({key for _id, key in self.window.rows}, supported)
        self.assertNotIn((monitor.id, "volume"), self.window.rows)

    def test_the_others_are_listed_folded_away(self) -> None:
        self.assertFalse(self.window.show_unavailable)
        labels = self.texts()
        self.assertTrue(
            any(t.startswith("Not available on this monitor") for t in labels)
        )
        self.assertFalse(any("Volume" in t for t in labels))  # folded

    def test_a_per_colour_control_is_drawn_in_its_colour(self) -> None:
        self.assertEqual(
            str(self.row("red_gain").widgets[1].cget("style")),
            "Red.Thin.Horizontal.TScale",
        )

    def test_values_are_shown(self) -> None:
        self.assertEqual(self.row("brightness").text_var.get(), "30")
        self.assertEqual(self.row("color_preset").choice_var.get(), "6500 K")

    def test_nothing_saved_yet(self) -> None:
        self.assertEqual(self.window.snapshot_label.cget("text"), "nothing saved")
        self.assertIn("disabled", self.window.restore_button.state())


class TestMalformedCapabilities(WindowTestCase):
    #: Like the TCL 25G64's: no opening parenthesis.
    caps = CAPS[1:]

    def test_capabilities_notes_are_not_a_banner(self) -> None:
        """Parser diagnostics belong in the log and a tooltip, not on the page."""
        self.assertTrue(self.window.monitors[0].warnings)  # there is one to hide
        self.assertFalse(any("capabilities" in t.lower() for t in self.texts()))


class TestRevert(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.window.save()
        row = self.row("brightness")
        row.text_var.set("61")
        row._on_text_commit()
        self.root.update()

    @staticmethod
    def offered(row) -> bool:
        return row.revert.instate(["!disabled"]) and bool(row.revert.cget("text"))

    def test_a_changed_control_shows_the_way_back(self) -> None:
        self.assertTrue(self.offered(self.row("brightness")))
        self.assertFalse(self.offered(self.row("contrast")))

    def test_the_mark_puts_the_saved_value_back(self) -> None:
        self.row("brightness")._revert()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 30)
        self.assertFalse(self.offered(self.row("brightness")))
        self.assertFalse(self.window.drifted())

    def test_the_keyboard_reaches_it(self) -> None:
        """A button, not a clickable label."""
        revert = self.row("brightness").revert
        self.assertIsInstance(revert, ttk.Button)
        revert.invoke()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 30)

    def test_a_pending_drag_does_not_undo_it(self) -> None:
        row = self.row("brightness")
        row._on_scale("80")  # debounce armed, not yet written
        row._revert()
        self.root.after(400)
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 30)
        self.assertEqual(row.text_var.get(), "30")

    def test_the_slider_keeps_its_width_when_a_mark_appears(self) -> None:
        """The knob must not move under the pointer mid-drag."""
        self.row("brightness")._revert()
        self.root.update()
        scale = self.row("contrast").widgets[1]
        before = scale.winfo_width()
        row = self.row("brightness")
        row.text_var.set("70")
        row._on_text_commit()
        self.root.update()
        self.assertEqual(scale.winfo_width(), before)


class TestRedrawsAndDialogs(WindowTestCase):
    def test_a_write_that_lands_after_a_redraw_updates_the_row_on_screen(self) -> None:
        old = self.row("brightness")
        old._on_scale("77")  # debounce armed
        self.window.render()  # e.g. a keeper reload
        self.root.after(400)
        self.root.update()
        live = self.row("brightness")
        self.assertIsNot(live, old)
        self.assertEqual(self.tcl.values[0x10], 77)
        self.assertEqual(live.text_var.get(), "77")
        self.assertEqual(live.confirmed, 77)

    def test_a_reload_asked_for_while_busy_runs_afterwards(self) -> None:
        self.window._set_busy(True, "something else")
        self.tcl.values[0x10] = 12  # changed behind the window's back
        self.window.load(probe=True)  # refused for now, remembered
        self.window._set_busy(False)
        self.root.update()
        self.assertEqual(self.row("brightness").text_var.get(), "12")

    def test_an_action_confirmed_in_a_dialog_waits_if_work_started_meanwhile(
        self,
    ) -> None:
        self.window.save()

        def busy_meanwhile(*_args, **_kwargs):
            self.window.busy = True  # a keeper reload began during the dialog
            return "Night"

        with mock.patch.object(self.window, "ask_name", side_effect=busy_meanwhile):
            self.window.new_profile(self.window.monitors[0].id)
        self.window.busy = False
        names = self.window.profiles[self.window.monitors[0].id].names
        self.assertEqual(names, ("Default",))


class TestThemeSwitch(WindowTestCase):
    def test_an_open_calibration_window_follows(self) -> None:
        self.window.open_calibration()
        calibration = self.window._calibrations[0]
        calibration.top.attributes("-alpha", 0.0)
        self.window.toggle_theme()
        # The frame follows the theme; the patterns keep their exact colours.
        self.assertEqual(str(calibration.top.cget("background")), self.window.palette.bg)

    def test_drop_down_lists_are_rebuilt_in_the_new_colours(self) -> None:
        combo = self.row("color_preset").widgets[1]
        self.root.tk.call("ttk::combobox::PopdownWindow", combo)  # opened once
        popdown = f"{combo}.popdown"
        self.assertTrue(self.root.tk.call("winfo", "exists", popdown))
        self.window.toggle_theme()
        self.assertFalse(self.root.tk.call("winfo", "exists", popdown))


class TestInput(WindowTestCase):
    def test_the_wheel_does_not_change_a_combobox(self) -> None:
        """Scrolling the page past a control must not move it."""
        combo = self.row("color_preset").widgets[1]
        self.tcl.writes.clear()
        combo.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertEqual(self.row("color_preset").choice_var.get(), "6500 K")
        self.assertEqual(self.tcl.writes, [])

    def test_the_wheel_does_not_change_a_spinbox(self) -> None:
        row = self.row("brightness")
        row.spin.event_generate("<MouseWheel>", delta=120)
        self.root.update()
        self.assertEqual(row.text_var.get(), "30")

    def test_a_click_on_the_slider_jumps_to_that_point(self) -> None:
        row = self.row("brightness")
        scale = row.widgets[1]
        self.root.update()
        x = scale.winfo_width() * 3 // 4
        y = scale.winfo_height() // 2
        expected = round(scale.get(x, y))
        self.assertGreater(expected, 60)  # far from 30: a jump, not a step

        scale.event_generate("<Button-1>", x=x, y=y)
        self.root.after(400)  # past the debounce
        self.root.update()

        self.assertEqual(row.text_var.get(), str(expected))
        self.assertEqual(self.tcl.values[0x10], expected)

    def test_sliders_use_the_thin_style(self) -> None:
        from monitorctl.app.theme import THIN_SCALE

        self.assertEqual(str(self.row("brightness").widgets[1].cget("style")), THIN_SCALE)

    def test_switching_theme_keeps_the_slider_images(self) -> None:
        self.window.toggle_theme()
        self.root.update()
        self.assertEqual(
            str(self.row("brightness").widgets[1].cget("style")), "Thin.Horizontal.TScale"
        )


def _feature(key: str, category: str, supported: bool = True):
    from monitorctl.ddc.service import FeatureState

    return FeatureState(
        key=key,
        code=1,
        label=key,
        kind="continuous",
        category=category,
        tooltip="",
        supported=supported,
        writable=supported,
    )


def _monitor(*features):
    from monitorctl.ddc.service import MonitorState

    return MonitorState("m", "m", "d", True, 0, None, None, None, features, "", ())


class TestPageLayout(unittest.TestCase):
    """The order a person adjusts a display in; see app/presentation.py."""

    def page(self, *features):
        from monitorctl.app.window import _page

        return _page(_monitor(*features))

    def test_sections_follow_how_often_they_matter_not_the_registry(self) -> None:
        """The user's order (D28): the picture first, the PC's adjustments
        after the monitor's, and what is set once - input, language - last."""
        page = self.page(
            _feature("osd_language", "osd"),
            _feature("input_source", "input"),
            _feature("gpu_sw_gamma", "gpu_gamma"),
            _feature("volume", "audio"),
            _feature("picture_mode", "image"),
            _feature("color_preset", "color_temperature"),
            _feature("red_black_level", "black_level"),
            _feature("red_gain", "white_balance"),
            _feature("color_saturation", "image"),
            _feature("brightness", "luminance"),
        )
        self.assertEqual(
            [section.key for section, _members in page.sections],
            [
                "luminance",
                "image",
                "white_balance",
                "black_level",
                "color_temperature",
                "mode",
                "gpu_gamma",
                "audio",
                "input",
                "osd",
            ],
        )

    def test_the_picture_mode_has_a_card_of_its_own(self) -> None:
        page = self.page(
            _feature("sharpness", "image"), _feature("picture_mode", "image")
        )
        keys = [(s.key, [f.key for f in members]) for s, members in page.sections]
        self.assertEqual(keys, [("image", ["sharpness"]), ("mode", ["picture_mode"])])

    def test_only_the_pc_sections_are_tagged_as_software(self) -> None:
        from monitorctl.app.presentation import SECTIONS

        self.assertEqual({s.key for s in SECTIONS if s.on_pc}, {"gpu_gamma", "gpu_color"})

    def test_unsupported_controls_are_listed_apart_and_empty_sections_vanish(
        self,
    ) -> None:
        page = self.page(
            _feature("brightness", "luminance"),
            _feature("volume", "audio", supported=False),
        )
        self.assertEqual([s.key for s, _m in page.sections], ["luminance"])
        self.assertEqual([f.key for f in page.unavailable], ["volume"])

    def test_registry_order_holds_within_a_section(self) -> None:
        page = self.page(
            _feature("red_gain", "white_balance"),
            _feature("green_gain", "white_balance"),
            _feature("blue_gain", "white_balance"),
        )
        _section, members = page.sections[0]
        self.assertEqual(
            [f.key for f in members], ["red_gain", "green_gain", "blue_gain"]
        )

    def test_every_registry_category_has_a_section(self) -> None:
        from monitorctl.app.presentation import SECTIONS
        from monitorctl.ddc.vcp import CATEGORIES

        self.assertLessEqual(set(CATEGORIES), {s.key for s in SECTIONS})

    def test_channel_colours_cover_every_per_colour_control(self) -> None:
        from monitorctl.app.presentation import CHANNEL_OF
        from monitorctl.app.theme import CHANNELS
        from monitorctl.ddc.vcp import FEATURES

        for key, channel in CHANNEL_OF.items():
            self.assertIn(key, FEATURES)
            for palette in CHANNELS.values():
                self.assertIn(channel, palette)


class TestWriting(WindowTestCase):
    def test_a_typed_value_is_written_and_confirmed(self) -> None:
        row = self.row("brightness")
        row.text_var.set("45")
        row._on_text_commit()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 45)
        self.assertEqual(row.confirmed, 45)
        self.assertEqual(row.scale_var.get(), 45)

    def test_the_keeper_waits_while_a_write_is_on_its_way(self) -> None:
        """Until the result is drained the drift it makes is unknown; a check
        run in between would put the saved value back on the monitor."""
        held: list[bool] = []
        submit = self.bridge.worker.submit

        def watching(run, on_done=None, *, label: str = "") -> None:
            held.append(self.bridge.editing[-1])
            submit(run, on_done, label=label)

        self.bridge.worker.submit = watching
        row = self.row("brightness")
        row.text_var.set("45")
        row._on_text_commit()
        self.root.update()
        self.assertEqual(held, [True])
        self.assertFalse(self.bridge.editing[-1])  # nothing saved: no drift

    def test_an_out_of_range_value_is_refused_and_reverted(self) -> None:
        row = self.row("brightness")
        self.tcl.writes.clear()
        row.text_var.set("150")
        row._on_text_commit()
        self.root.update()
        self.assertEqual(self.tcl.writes, [])
        self.assertEqual(row.text_var.get(), "30")
        self.assertEqual(self.window.status_label.cget("style"), "StatusError.TLabel")

    def test_retyping_the_reported_value_of_a_forgetful_control_still_writes(
        self,
    ) -> None:
        """The case the user hit: the screen is wrong, the field says it is right."""
        row = self.row("red_gain")
        self.tcl.writes.clear()
        for char in "70":
            row._on_key(mock.Mock(char=char, keysym=char))
        row.text_var.set("70")
        row._on_text_commit()  # leaving the field after typing
        self.root.update()
        self.assertEqual(self.tcl.writes, [(0x16, 69), (0x16, 70)])

    def test_enter_on_the_reported_value_of_a_forgetful_control_writes(self) -> None:
        row = self.row("red_gain")
        self.tcl.writes.clear()
        row._on_text_commit(force=True)
        self.root.update()
        self.assertEqual(self.tcl.writes, [(0x16, 69), (0x16, 70)])

    def test_merely_leaving_the_field_of_a_forgetful_control_writes_nothing(
        self,
    ) -> None:
        """Every click elsewhere moves the focus; each would cost two writes."""
        row = self.row("red_gain")
        self.tcl.writes.clear()
        row._on_key(mock.Mock(char="\t", keysym="Tab"))
        row._on_text_commit()
        self.root.update()
        self.assertEqual(self.tcl.writes, [])

    def test_a_choice_is_written_at_once(self) -> None:
        row = self.row("color_preset")
        row.choice_var.set("9300 K")
        row._on_choice(None)
        self.assertEqual(self.tcl.values[0x14], 0x08)

    def test_a_write_error_is_reported_and_the_value_reverted(self) -> None:
        row = self.row("brightness")
        self.tcl.gone = True
        row.text_var.set("80")
        row._on_text_commit()
        self.root.update()
        self.assertEqual(row.text_var.get(), "30")
        self.assertEqual(self.window.status_label.cget("style"), "StatusError.TLabel")


class TestSaveAndRestore(WindowTestCase):
    def test_save_records_and_pushes_forgetful_controls_through_50(self) -> None:
        self.tcl.writes.clear()
        self.window.save()
        self.assertTrue(self.window.has_saved())
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 70)])
        self.assertEqual(self.window.snapshot_label.cget("text"), "all saved")

    def test_a_change_after_saving_is_flagged_and_suspends_the_keeper(self) -> None:
        self.window.save()
        row = self.row("brightness")
        row.text_var.set("60")
        row._on_text_commit()
        self.root.update()
        self.assertTrue(self.window.drifted())
        self.assertIn("unsaved", self.window.snapshot_label.cget("text"))
        self.assertTrue(self.bridge.editing[-1])

    def test_the_hint_says_which_values_differ(self) -> None:
        self.window.save()
        self.assertIn("matches", self.window._snapshot_tooltip())
        self.window.record(self.window.monitors[0].id, "brightness", 61)
        tip = self.window._snapshot_tooltip()
        self.assertIn("TestDisplay · Brightness: saved 30, now 61", tip)

    def test_geometry_on_a_digital_input_is_never_unsaved(self) -> None:
        """A profile from before D32 holds geometry noise read on HDMI; the
        noise moving (181 -> 189) is not a change anyone made."""
        self.tcl.values[0x20], self.tcl.maxima[0x20] = 181, 255
        self.window.load(probe=True)
        self.root.update()
        self.window.save()  # input unknown: geometry is saved, as before D32
        monitor_id = self.window.monitors[0].id
        self.window.record(monitor_id, "horizontal_position", 189)
        self.assertTrue(self.window.drifted())

        self.window.record(monitor_id, "input_source", 0x04)  # ASUS's HDMI
        self.assertFalse(self.window.drifted())
        self.assertEqual(self.window.snapshot_label.cget("text"), "all saved")
        self.assertIsNone(self.window.saved_value(monitor_id, "horizontal_position"))

    def test_restore_puts_the_saved_values_back(self) -> None:
        self.window.save()
        self.tcl.values[0x10] = 99
        self.window.restore()
        self.assertEqual(self.tcl.values[0x10], 30)
        self.assertFalse(self.window.drifted())

    def test_closing_with_unsaved_changes_asks_first(self) -> None:
        self.window.save()
        self.window.record(self.window.monitors[0].id, "brightness", 61)
        with mock.patch("tkinter.messagebox.askyesnocancel", return_value=None) as ask:
            self.window.close()
        ask.assert_called_once()
        self.assertNotEqual(self.root.state(), "withdrawn")  # cancelled

    def test_exit_with_unsaved_changes_asks_and_cancel_keeps_running(self) -> None:
        self.window.save()
        self.window.record(self.window.monitors[0].id, "brightness", 61)
        exited = []
        with mock.patch("tkinter.messagebox.askyesnocancel", return_value=None) as ask:
            self.window.confirm_exit(lambda: exited.append(True))
        ask.assert_called_once()
        self.assertEqual(exited, [])

    def test_exit_after_saving_the_changes(self) -> None:
        self.window.save()
        row = self.row("brightness")
        row.text_var.set("61")
        row._on_text_commit()
        self.root.update()
        exited = []
        with mock.patch("tkinter.messagebox.askyesnocancel", return_value=True):
            self.window.confirm_exit(lambda: exited.append(True))
        self.assertEqual(exited, [True])
        self.assertEqual(
            self.service.saved_snapshot().monitors[0].values["brightness"], 61
        )

    def test_exit_without_unsaved_changes_does_not_ask(self) -> None:
        exited = []
        with mock.patch("tkinter.messagebox.askyesnocancel") as ask:
            self.window.confirm_exit(lambda: exited.append(True))
        ask.assert_not_called()
        self.assertEqual(exited, [True])

    def test_closing_cleanly_hides_and_remembers_the_geometry(self) -> None:
        self.window.close()
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertEqual(self.bridge.hidden, 1)
        self.assertRegex(self.bridge.settings.get().window_geometry, r"^\d+x\d+")


class TestProfiles(WindowTestCase):
    def bar(self):
        return self.window.profile_bars[self.window.monitors[0].id]

    def write(self, key: str, value: int) -> None:
        row = self.row(key)
        row.text_var.set(str(value))
        row._on_text_commit()
        self.root.update()

    def name_answer(self, name: str | None):
        return mock.patch.object(self.window, "ask_name", return_value=name)

    def test_nothing_saved_shows_no_profile(self) -> None:
        self.assertEqual(self.bar().choice.get(), self.bar().NO_PROFILE)
        self.assertIn("disabled", self.bar().combo.state())

    def test_the_first_save_creates_default(self) -> None:
        self.window.save()
        self.assertEqual(self.bar().choice.get(), "Default")
        self.assertTrue(self.bar().state_label.cget("text").startswith("saved"))

    def test_new_profile_saves_the_current_values_and_is_used(self) -> None:
        self.window.save()
        self.write("brightness", 20)
        with self.name_answer("Night"):
            self.window.new_profile(self.window.monitors[0].id)
        self.root.update()
        self.assertEqual(self.bar().choice.get(), "Night")
        self.assertEqual(list(self.bar().combo.cget("values")), ["Default", "Night"])
        self.assertFalse(self.window.drifted())

    def test_cancelling_the_name_creates_nothing(self) -> None:
        self.window.save()
        with self.name_answer(None):
            self.window.new_profile(self.window.monitors[0].id)
        self.assertEqual(list(self.bar().combo.cget("values")), ["Default"])

    def test_switching_puts_the_profile_on_the_monitor(self) -> None:
        self.window.save()  # Default: brightness 30
        self.write("brightness", 20)
        with self.name_answer("Night"):
            self.window.new_profile(self.window.monitors[0].id)

        self.window.switch_profile(self.window.monitors[0].id, "Default")
        self.root.update()

        self.assertEqual(self.tcl.values[0x10], 30)
        self.assertEqual(self.row("brightness").text_var.get(), "30")
        self.assertEqual(self.bar().choice.get(), "Default")

    def test_switching_with_unsaved_changes_asks_and_cancel_stays(self) -> None:
        self.window.save()
        with self.name_answer("Night"):
            self.window.new_profile(self.window.monitors[0].id)
        self.write("brightness", 55)
        with mock.patch("tkinter.messagebox.askyesnocancel", return_value=None) as ask:
            self.window.switch_profile(self.window.monitors[0].id, "Default")
        ask.assert_called_once()
        self.assertEqual(self.tcl.values[0x10], 55)
        self.assertEqual(self.bar().choice.get(), "Night")

    def test_switching_after_choosing_to_save_keeps_the_changes_in_the_old_one(
        self,
    ) -> None:
        self.window.save()
        with self.name_answer("Night"):
            self.window.new_profile(self.window.monitors[0].id)
        self.write("brightness", 55)
        with mock.patch("tkinter.messagebox.askyesnocancel", return_value=True):
            self.window.switch_profile(self.window.monitors[0].id, "Default")
        self.root.update()
        night = self.window.profiles[self.window.monitors[0].id].profile("Night")
        self.assertEqual(night.values["brightness"], 55)
        self.assertEqual(self.tcl.values[0x10], 30)

    def test_delete_lists_only_profiles_not_in_use(self) -> None:
        self.window.save()
        with self.name_answer("Night"):
            self.window.new_profile(self.window.monitors[0].id)
        menu = self.bar().delete_menu
        self.assertEqual(menu.index("end"), 0)
        self.assertEqual(menu.entrycget(0, "label"), "Default")

        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            self.window.delete_profile(self.window.monitors[0].id, "Default")
        self.assertEqual(list(self.bar().combo.cget("values")), ["Night"])
        self.assertIn("disabled", self.bar().delete_button.state())

    def test_rename(self) -> None:
        self.window.save()
        with self.name_answer("Calibrated"):
            self.window.rename_profile(self.window.monitors[0].id)
        self.assertEqual(self.bar().choice.get(), "Calibrated")

    def test_a_name_is_checked_while_typing(self) -> None:
        from monitorctl.app.window import _name_problem

        self.window.save()
        entry = self.window.profiles[self.window.monitors[0].id]
        self.assertIsNone(_name_problem("Night", entry, None))
        self.assertIn("already", _name_problem("default", entry, None))
        self.assertIsNone(_name_problem("DEFAULT", entry, "Default"))
        self.assertIsNotNone(_name_problem("   ", entry, None))


class TestApplyOnDemand(WindowTestCase):
    """Auto apply off: changes wait in the window until Apply."""

    def setUp(self) -> None:
        super().setUp()
        self.window.auto_apply_var.set(False)
        self.window._toggle_auto_apply()
        self.tcl.writes.clear()

    def type_value(self, key: str, value: int) -> None:
        row = self.row(key)
        row.text_var.set(str(value))
        row._on_text_commit()
        self.root.update()

    def test_the_setting_is_remembered(self) -> None:
        self.assertFalse(self.bridge.settings.get().auto_apply)

    def test_a_change_waits_and_writes_nothing(self) -> None:
        self.type_value("brightness", 61)
        self.assertEqual(self.tcl.writes, [])
        self.assertEqual(self.row("brightness").text_var.get(), "61")
        self.assertEqual(self.window.apply_button.cget("text"), "Apply (1)")
        self.assertNotIn("disabled", self.window.apply_button.state())

    def test_apply_writes_what_waits(self) -> None:
        self.type_value("brightness", 61)
        self.type_value("contrast", 40)
        self.window.apply_staged()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 61)
        self.assertEqual(self.tcl.values[0x12], 40)
        self.assertEqual(self.window.staged, {})
        self.assertEqual(self.window.apply_button.cget("text"), "Apply")
        self.assertIn("disabled", self.window.discard_button.state())

    def test_changing_back_is_no_change(self) -> None:
        self.type_value("brightness", 61)
        self.type_value("brightness", 30)
        self.assertEqual(self.window.staged, {})

    def test_discard_shows_the_monitors_values_again(self) -> None:
        self.type_value("brightness", 61)
        self.window.discard_staged()
        self.assertEqual(self.row("brightness").text_var.get(), "30")
        self.assertEqual(self.tcl.writes, [])

    def test_a_redraw_keeps_what_waits(self) -> None:
        self.type_value("brightness", 61)
        self.window.render()
        self.assertEqual(self.row("brightness").text_var.get(), "61")

    def test_a_change_made_before_a_redraw_waits_on_the_row_on_screen(self) -> None:
        """The debounce fires after a keeper reload redrew the rows: the
        change must show as waiting, not hide in ``staged`` behind a dead row."""
        old = self.row("brightness")
        old._debounce(40)
        self.window.render()
        self.root.after(400)
        self.root.update()
        live = self.row("brightness")
        self.assertIsNot(live, old)
        self.assertEqual(live.text_var.get(), "40")
        self.assertIn(DOT, live.name_label.cget("text"))
        self.assertEqual(self.window.apply_button.cget("text"), "Apply (1)")
        self.assertEqual(self.tcl.writes, [])

    def test_switching_auto_apply_back_on_applies_what_waits(self) -> None:
        self.type_value("brightness", 61)
        self.window.auto_apply_var.set(True)
        self.window._toggle_auto_apply()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 61)

    def test_save_applies_first_then_saves(self) -> None:
        self.type_value("brightness", 61)
        self.window.save()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 61)
        saved = self.service.saved_snapshot().monitors[0].values["brightness"]
        self.assertEqual(saved, 61)

    def test_switching_profile_writes_nothing_until_apply(self) -> None:
        monitor_id = self.window.monitors[0].id
        self.window.auto_apply_var.set(True)
        self.window._toggle_auto_apply()
        self.window.save()  # Default: brightness 30
        self.type_value("brightness", 20)
        with mock.patch.object(self.window, "ask_name", return_value="Night"):
            self.window.new_profile(monitor_id)
        self.window.auto_apply_var.set(False)
        self.window._toggle_auto_apply()
        self.tcl.writes.clear()

        self.window.switch_profile(monitor_id, "Default")
        self.root.update()

        self.assertEqual(self.tcl.values[0x10], 20)  # untouched
        self.assertEqual(self.row("brightness").text_var.get(), "30")  # shown
        self.assertEqual(self.window.staged_value(monitor_id, "brightness"), 30)
        self.window.apply_staged()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 30)

    def test_a_new_profile_holds_what_is_on_screen(self) -> None:
        self.type_value("brightness", 61)
        with mock.patch.object(self.window, "ask_name", return_value="Night"):
            self.window.new_profile(self.window.monitors[0].id)
        self.root.update()
        night = self.window.profiles[self.window.monitors[0].id].profile("Night")
        self.assertEqual(night.values["brightness"], 61)
        self.assertEqual(self.tcl.values[0x10], 61)

    def test_a_waiting_change_follows_its_monitor_when_windows_renumbers(self) -> None:
        """Filed by EDID: applied to the panel it was made on, not its port."""
        self.type_value("brightness", 10)
        self.tcl.device = r"\\.\DISPLAY7"
        self.service.refresh(probe=False)
        self.window.keeper_changed(True, "display configuration changed", 0)
        self.root.update()
        self.window.apply_staged()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 10)

    def test_auto_apply_cannot_flip_mid_operation(self) -> None:
        self.type_value("brightness", 61)
        self.window._set_busy(True, "something else")
        self.assertIn("disabled", self.window.auto_check.state())
        self.window.auto_apply_var.set(True)
        self.window._toggle_auto_apply()  # e.g. by keyboard
        self.assertFalse(self.bridge.settings.get().auto_apply)
        self.window._set_busy(False)

    def test_the_mark_cancels_one_waiting_change(self) -> None:
        self.type_value("brightness", 61)
        row = self.row("brightness")
        self.assertIn("●", row.name_label.cget("text"))  # not colour alone
        row._revert()
        self.assertEqual(self.window.staged, {})
        self.assertEqual(row.text_var.get(), "30")
        self.assertEqual(self.tcl.writes, [])

    def test_restore_discards_what_waits_on_screen_too(self) -> None:
        self.window.auto_apply_var.set(True)
        self.window._toggle_auto_apply()
        self.window.save()
        self.window.auto_apply_var.set(False)
        self.window._toggle_auto_apply()
        self.type_value("brightness", 61)
        self.window.restore()
        self.root.update()
        self.assertEqual(self.window.staged, {})
        self.assertEqual(self.row("brightness").text_var.get(), "30")

    def test_switching_asks_about_waiting_changes_and_saves_them_on_yes(self) -> None:
        monitor_id = self.window.monitors[0].id
        self.window.auto_apply_var.set(True)
        self.window._toggle_auto_apply()
        self.window.save()
        with mock.patch.object(self.window, "ask_name", return_value="Night"):
            self.window.new_profile(monitor_id)
        self.window.auto_apply_var.set(False)
        self.window._toggle_auto_apply()
        self.type_value("brightness", 44)  # waits, on profile "Night"
        with mock.patch("tkinter.messagebox.askyesnocancel", return_value=True) as ask:
            self.window.switch_profile(monitor_id, "Default")
        self.root.update()
        self.assertIn("not applied", ask.call_args.args[1])
        night = self.window.profiles[monitor_id].profile("Night")
        self.assertEqual(night.values["brightness"], 44)

    def test_closing_with_changes_waiting_asks_first(self) -> None:
        self.type_value("brightness", 61)
        with mock.patch("tkinter.messagebox.askyesnocancel", return_value=None) as ask:
            self.window.close()
        ask.assert_called_once()
        self.assertIn("waiting", ask.call_args.args[1])
        self.assertNotEqual(self.root.state(), "withdrawn")


class TestApplyOnDemandOnADigitalInput(WindowTestCase):
    """D32: geometry is noise on HDMI, so a profile switch never stages it."""

    caps = "(prot(monitor)type(LCD)model(VS248)vcp(10 20 60(01 04))mccs_ver(2.1))"

    def test_switching_profiles_leaves_inert_geometry_out(self) -> None:
        self.tcl.values.update({0x20: 40, 0x60: 0x01})  # VGA: geometry is real
        self.tcl.maxima.update({0x20: 100, 0x60: 0x04})
        self.window.load(probe=True)
        self.window.save()
        monitor_id = self.window.monitors[0].id
        with mock.patch.object(self.window, "ask_name", return_value="Night"):
            self.window.new_profile(monitor_id)
        self.tcl.values.update({0x20: 189, 0x60: 0x04})  # HDMI: noise
        self.window.load(probe=True)
        self.window.auto_apply_var.set(False)
        self.window._toggle_auto_apply()
        self.tcl.writes.clear()

        self.window.switch_profile(monitor_id, "Default")
        self.window.apply_staged()
        self.root.update()

        self.assertNotIn(0x20, [code for code, _value in self.tcl.writes])


class TestChangesNotLandedYet(WindowTestCase):
    """A drag waits 250 ms to settle, then for the bus: Save, Restore and
    closing must not act on the values as they were before it."""

    def drag(self, key: str, value: int) -> None:
        row = self.row(key)
        row.scale_var.set(value)
        row._on_scale(str(value))  # the debounce is now pending

    def test_save_right_after_a_drag_saves_the_drag(self) -> None:
        self.drag("brightness", 64)
        self.window.save()
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 64)
        saved = self.service.saved_snapshot().monitors[0].values["brightness"]
        self.assertEqual(saved, 64)

    def test_save_waits_for_a_write_still_on_its_way(self) -> None:
        worker = HoldingWorker(self.service)
        self.bridge.worker = worker
        self.drag("brightness", 64)
        self.window.save()
        self.assertEqual(worker.labels, ["Writing Brightness"])  # no save yet
        worker.run_all()  # the write lands, then the save it held runs
        self.assertEqual(worker.labels, ["Writing Brightness", "Saving"])
        saved = self.service.saved_snapshot().monitors[0].values["brightness"]
        self.assertEqual(saved, 64)

    def test_a_number_typed_but_not_committed_is_saved(self) -> None:
        row = self.row("brightness")
        for char in "77":
            row._on_key(mock.Mock(char=char, keysym=char))
        row.text_var.set("77")
        self.window.save()  # Ctrl+S with the focus still in the field
        self.root.update()
        saved = self.service.saved_snapshot().monitors[0].values["brightness"]
        self.assertEqual(saved, 77)

    def test_closing_right_after_a_drag_asks_about_it(self) -> None:
        self.window.save()
        self.drag("brightness", 64)
        with mock.patch("tkinter.messagebox.askyesnocancel", return_value=None) as ask:
            self.window.close()
        ask.assert_called_once()
        self.assertNotEqual(self.root.state(), "withdrawn")

    def test_restore_drops_a_drag_still_settling(self) -> None:
        """Landing after the restore, it would undo it."""
        self.window.save()
        self.tcl.writes.clear()
        self.drag("brightness", 80)
        self.window.restore()
        self.root.after(400)
        self.root.update()
        self.assertEqual(self.tcl.values[0x10], 30)
        self.assertNotIn((0x10, 80), self.tcl.writes)
        self.assertEqual(self.row("brightness").text_var.get(), "30")


class TestWhileBusy(WindowTestCase):
    def test_a_save_asked_for_meanwhile_runs_when_it_ends(self) -> None:
        """From the tray or Ctrl+S, while a rescan runs: once ignored silently."""
        self.window._set_busy(True, "Reading every control of every monitor…")
        self.window.save()
        self.assertIsNone(self.service.saved_snapshot())
        self.assertIn("will run", self.window.status_label.cget("text"))
        self.window._set_busy(False)
        self.root.update()
        self.assertIsNotNone(self.service.saved_snapshot())

    def test_a_spinner_says_so_not_the_pointer(self) -> None:
        self.window._set_busy(True, "Reading…")
        self.root.update()
        self.assertEqual(str(self.root.cget("cursor")), "")
        self.assertTrue(self.window.spinner.turning)
        self.assertTrue(self.window.spinner.canvas.winfo_ismapped())
        self.window._set_busy(False)
        self.root.update()
        self.assertFalse(self.window.spinner.turning)
        self.assertFalse(self.window.spinner.canvas.winfo_ismapped())


class TestApplyWithNothingWaiting(WindowTestCase):
    """Apply can always be pressed: it makes the screen show the window."""

    def test_it_can_be_pressed_with_auto_apply_on(self) -> None:
        self.assertNotIn("disabled", self.window.apply_button.state())
        self.assertIn("disabled", self.window.discard_button.state())

    def test_it_pushes_the_controls_a_monitor_forgets(self) -> None:
        self.tcl.writes.clear()
        self.window.apply()
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 70)])

    def test_it_pushes_what_is_shown_not_what_is_saved(self) -> None:
        self.window.save()  # red gain 70
        row = self.row("red_gain")
        row.text_var.set("66")
        row._on_text_commit()
        self.root.update()
        self.tcl.writes.clear()
        self.window.apply()
        self.assertEqual(self.tcl.writes, [(0x16, 50), (0x16, 66)])


ASUS = EdidInfo("ACI", 0x2498, 0, "SERIAL-B", "VS248")


class TestWindowsRenumbersMidSession(WindowTestCase):
    """A job waits in the bus queue; a keeper rescan ahead of it may hand its
    monitor's position to the other one.  It must still reach its own."""

    def more_monitors(self) -> list[FakeMonitor]:
        self.asus = FakeMonitor(
            device=r"\\.\DISPLAY2",
            capabilities=CAPS,
            values={0x10: 80, 0x12: 50, 0x14: 0x05, 0x16: 50},
            maxima={0x10: 100, 0x12: 100, 0x14: 0x0D, 0x16: 100},
            edid=ASUS,
        )
        return [self.asus]

    def renumber(self) -> None:
        self.tcl.device, self.asus.device = self.asus.device, self.tcl.device
        self.service.refresh(probe=False)

    def test_a_slider_write_reaches_its_own_monitor(self) -> None:
        worker = HoldingWorker(self.service)
        self.bridge.worker = worker
        row = self.row("brightness")  # the TCL's
        row.text_var.set("45")
        row._on_text_commit()
        self.root.update()
        self.renumber()
        worker.run_all()
        self.assertEqual(self.tcl.values[0x10], 45)
        self.assertEqual(self.asus.values[0x10], 80)
        self.assertEqual(row.confirmed, 45)

    def test_the_result_lands_on_its_own_row_after_the_redraw(self) -> None:
        worker = HoldingWorker(self.service)
        self.bridge.worker = worker
        row = self.row("brightness")
        row.text_var.set("45")
        row._on_text_commit()
        self.root.update()
        self.renumber()
        self.window.keeper_changed(True, "display configuration changed", 0)
        worker.run_all()  # the write, then the reload behind it
        self.root.update()
        by_edid = {m.hardware_id: m.id for m in self.window.monitors}
        tcl_row = self.window.rows[(by_edid[TCL.hardware_id], "brightness")]
        asus_row = self.window.rows[(by_edid[ASUS.hardware_id], "brightness")]
        self.assertEqual(tcl_row.text_var.get(), "45")
        self.assertEqual(asus_row.text_var.get(), "80")

    def test_apply_reaches_the_monitor_the_change_was_made_on(self) -> None:
        self.window.auto_apply_var.set(False)
        self.window._toggle_auto_apply()
        row = self.row("brightness")
        row.text_var.set("10")
        row._on_text_commit()
        self.root.update()
        worker = HoldingWorker(self.service)
        self.bridge.worker = worker
        self.window.apply()
        self.renumber()
        worker.run_all()
        self.assertEqual(self.tcl.values[0x10], 10)
        self.assertEqual(self.asus.values[0x10], 80)

    def test_a_monitor_gone_meanwhile_is_not_written_through_its_old_position(
        self,
    ) -> None:
        worker = HoldingWorker(self.service)
        self.bridge.worker = worker
        row = self.row("brightness")
        row.text_var.set("45")
        row._on_text_commit()
        self.root.update()
        self.tcl.gone = True
        self.asus.device = self.tcl.device
        self.service._backend._monitors.remove(self.tcl)
        self.service.refresh(probe=False)
        worker.run_all()
        self.assertEqual(self.asus.values[0x10], 80)
        self.assertEqual(self.window.status_label.cget("style"), "StatusError.TLabel")


class TestTabs(WindowTestCase):
    def test_a_selected_tab_draws_no_focus_ring(self) -> None:
        from tkinter import ttk as _ttk

        layout = str(_ttk.Style(self.root).layout("TNotebook.Tab"))
        self.assertNotIn("focus", layout)


class TestSettingsTab(WindowTestCase):
    def test_changes_are_validated_and_stored(self) -> None:
        self.window.interval_var.set("15")
        self.window.mode_var.set(RefreshMode.REWRITE.value)
        self.window._settings_edited()
        settings = self.bridge.settings.get()
        self.assertEqual(settings.interval_minutes, 15)
        self.assertIs(settings.mode, RefreshMode.REWRITE)
        self.assertEqual(self.bridge.changed, 1)

    def test_an_invalid_interval_is_refused(self) -> None:
        self.window.interval_var.set("0")
        self.window._settings_edited()
        self.assertEqual(self.bridge.settings.get(), AppSettings())
        self.assertEqual(self.window.interval_var.get(), "1")

    def test_the_theme_toggles(self) -> None:
        self.window.toggle_theme()
        self.assertIs(self.bridge.settings.get().theme, Theme.LIGHT)
        self.window.toggle_theme()
        self.assertIs(self.bridge.settings.get().theme, Theme.DARK)

    def test_nothing_draws_a_dotted_focus_ring(self) -> None:
        # A click focuses buttons, check boxes and tabs; clam would then ring
        # their label with dots.  Also after a theme switch.
        from monitorctl.app.theme import FOCUS_RING_STYLES

        self.window.toggle_theme()
        style = ttk.Style(self.root)

        def names(layout) -> list[str]:
            return [
                n
                for name, options in layout
                for n in (name, *names(options.get("children", [])))
            ]

        for name in (*FOCUS_RING_STYLES, "Accent.TButton", "Revert.TButton"):
            with self.subTest(style=name):
                elements = names(style.layout(name))
                self.assertTrue(elements)
                self.assertFalse([e for e in elements if e.endswith(".focus")])
        self.window.toggle_theme()


class TestCalibration(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        from monitorctl.app.calibration import CalibrationWindow

        self.cal = CalibrationWindow(self.root, self.window.palette)
        self.cal.top.attributes("-alpha", 0.0)
        self.cal.top.update()
        self.addCleanup(self.cal.top.destroy)

    def test_every_step_draws_on_its_own_field(self) -> None:
        from monitorctl.app.calibration import STEPS

        for index, step in enumerate(STEPS):
            with self.subTest(step=step.title):
                self.cal.show(index)
                self.assertTrue(self.cal.canvas.find_all())
                self.assertEqual(str(self.cal.canvas.cget("background")), step.background)

    def test_steps_stop_at_both_ends(self) -> None:
        from monitorctl.app.calibration import STEPS

        self.cal.go(-1)
        self.assertEqual(self.cal.index, 0)
        self.cal.show(len(STEPS) - 1)
        self.cal.go(1)
        self.assertEqual(self.cal.index, len(STEPS) - 1)

    def test_the_clipping_bars_blink_against_their_field(self) -> None:
        from monitorctl.app.calibration import SHADOW_BARS, STEPS

        self.cal.show(next(i for i, s in enumerate(STEPS) if "Shadows" in s.title))
        self.assertEqual(len(self.cal._blinkers), len(SHADOW_BARS))
        item, colour = self.cal._blinkers[0]
        fills = set()
        for _ in range(2):
            self.cal._phase = not self.cal._phase
            self.cal._paint_blinkers()
            fills.add(self.cal.canvas.itemcget(item, "fill"))
        self.assertEqual(fills, {colour, "#000000"})

    def test_blinking_can_be_turned_off(self) -> None:
        from monitorctl.app.calibration import STEPS

        self.cal.show(next(i for i, s in enumerate(STEPS) if "Shadows" in s.title))
        self.cal.blink_var.set(False)
        self.cal._blink_toggled()
        item, colour = self.cal._blinkers[0]
        self.cal._tick()
        self.assertEqual(self.cal.canvas.itemcget(item, "fill"), colour)

    def test_the_gamma_step_judges_one_square_against_the_stripes(self) -> None:
        from monitorctl.app.calibration import GAMMA_TARGET, STEPS

        self.cal.show(next(i for i, s in enumerate(STEPS) if "Gamma" in s.title))
        canvas = self.cal.canvas
        squares = [
            i
            for i in canvas.find_all()
            if canvas.type(i) == "rectangle"
            and canvas.itemcget(i, "fill") == "#" + f"{GAMMA_TARGET:02x}" * 3
        ]
        self.assertEqual(len(squares), 1)
        texts = [
            canvas.itemcget(i, "text")
            for i in canvas.find_all()
            if canvas.type(i) == "text"
        ]
        self.assertTrue(any("gamma 2.2" in t for t in texts))

    def test_every_step_states_a_goal_and_how_to_fix_it(self) -> None:
        from monitorctl.app.calibration import STEPS

        for index, step in enumerate(STEPS):
            with self.subTest(step=step.title):
                self.cal.show(index)
                self.assertTrue(self.cal.goal_label.cget("text").startswith("Goal: "))
                for fix in step.fixes:
                    self.assertIn(fix, self.cal.fixes_label.cget("text"))
        # Every step after the preparation says what to move.
        self.assertTrue(all(step.fixes for step in STEPS[1:]))

    def test_full_screen_keeps_the_goal_in_view(self) -> None:
        self.cal.show(1)
        self.cal.toggle_fullscreen()
        self.cal.top.update()
        self.cal.draw()
        texts = [
            self.cal.canvas.itemcget(i, "text")
            for i in self.cal.canvas.find_all()
            if self.cal.canvas.type(i) == "text"
        ]
        self.assertTrue(any(self.cal.step.goal in t for t in texts))
        self.cal._escape()

    def test_escape_only_leaves_full_screen(self) -> None:
        self.cal._escape()
        self.assertTrue(self.cal.top.winfo_exists())

    def test_opening_again_brings_the_same_guide_forward(self) -> None:
        self.window._calibrations = [self.cal]
        self.window.open_calibration()
        self.assertEqual(self.window._calibrations, [self.cal])

    def test_full_screen_hides_the_step_bar_and_comes_back(self) -> None:
        self.cal.toggle_fullscreen()
        self.cal.top.update()
        self.assertFalse(self.cal.chrome.winfo_ismapped())
        self.cal._escape()
        self.cal.top.update()
        self.assertTrue(self.cal.chrome.winfo_ismapped())


if __name__ == "__main__":
    unittest.main()
