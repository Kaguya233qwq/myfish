import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from myfish.adapters.xhs.device import (
    browser_memory,
    cpu_cores,
    local_timezone,
    screen_size,
)


class EnvironmentTests(unittest.TestCase):
    def test_memory_uses_nearest_browser_bucket(self):
        for gib, expected in ((0.1, 0.25), (0.5, 0.5), (1.9, 2), (7.7, 8), (64, 8)):
            with (
                self.subTest(gib=gib),
                patch(
                    "myfish.adapters.xhs.device.psutil.virtual_memory",
                    return_value=SimpleNamespace(total=int(gib * 1024**3)),
                ),
            ):
                self.assertEqual(browser_memory(), expected)
        with patch(
            "myfish.adapters.xhs.device.psutil.virtual_memory", side_effect=OSError
        ):
            self.assertEqual(browser_memory(), 8)

    def test_timezone_name_and_offset_are_consistent(self):
        for name, expected in (
            ("Asia/Shanghai", -480),
            ("Asia/Kolkata", -330),
            ("UTC", 0),
        ):
            with (
                self.subTest(name=name),
                patch(
                    "myfish.adapters.xhs.device.get_localzone",
                    return_value=ZoneInfo(name),
                ),
            ):
                self.assertEqual(local_timezone(), (name, expected))
        with patch(
            "myfish.adapters.xhs.device.get_localzone", side_effect=RuntimeError
        ):
            self.assertEqual(local_timezone(), ("UTC", 0))

    def test_headless_screen_and_unavailable_cpu_use_defaults(self):
        with patch("myfish.adapters.xhs.device.Path.glob", return_value=[]):
            self.assertEqual(screen_size("Linux"), (1920, 1080))
        self.assertEqual(screen_size("Unknown"), (1920, 1080))
        with patch("myfish.adapters.xhs.device.os.cpu_count", return_value=None):
            self.assertEqual(cpu_cores(), 8)

    def test_native_desktop_probe_closes_window(self):
        root = MagicMock()
        root.winfo_screenwidth.return_value = 2560
        root.winfo_screenheight.return_value = 1440
        tkinter = MagicMock()
        tkinter.Tk.return_value = root
        tkinter.TclError = RuntimeError
        with patch.dict("sys.modules", {"tkinter": tkinter}):
            self.assertEqual(screen_size("Darwin"), (2560, 1440))
        root.withdraw.assert_called_once()
        root.destroy.assert_called_once()
