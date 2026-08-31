import ctypes
import ctypes.wintypes as wt
import inspect
import json
import os
import queue
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image

import screen_search


class NewTextHotkeyTests(unittest.TestCase):
    def test_hotkey_is_ctrl_alt_n(self):
        self.assertEqual("ctrl+alt+n", screen_search.NEW_TEXT_HOTKEY)

    def test_new_text_action_opens_and_tracks_builtin_editor(self):
        app = screen_search.ScreenSearchApp.__new__(screen_search.ScreenSearchApp)
        app.root = mock.Mock()
        app._text_editors = set()
        editor = mock.Mock()
        with mock.patch.object(
            screen_search, "TextDocumentWindow", return_value=editor
        ) as editor_class:
            app._open_new_text_document()

        self.assertIn(editor, app._text_editors)
        editor_class.assert_called_once()
        self.assertIs(app.root, editor_class.call_args.args[0])
        editor_class.call_args.kwargs["on_closed"]()
        self.assertNotIn(editor, app._text_editors)

    def test_builtin_editor_defaults_and_character_count(self):
        self.assertEqual(14, screen_search.TEXT_EDITOR_FONT_SIZE)
        self.assertEqual("word", screen_search.TEXT_EDITOR_WRAP)
        self.assertEqual(6, screen_search._count_document_characters("abc \n中文！"))

    def test_builtin_editor_scales_geometry_from_2k_reference(self):
        self.assertEqual(
            {"scale": 0.75, "right": 1875, "x": 1425, "y": 38,
             "width": 450, "height": 450},
            screen_search._scaled_editor_metrics(1080),
        )
        self.assertEqual(
            {"scale": 1.5, "right": 3750, "x": 2850, "y": 75,
             "width": 900, "height": 900},
            screen_search._scaled_editor_metrics(2160),
        )
        self.assertEqual(
            ("600x600+1900+50", 1.0),
            screen_search._scaled_editor_geometry((0, 0, 2560, 1440), 1440),
        )

    def test_builtin_editor_scroll_thumb_uses_70_percent_visual_opacity(self):
        self.assertEqual(0.70, screen_search.TEXT_EDITOR_SCROLL_OPACITY)
        self.assertEqual("#8c8c8c", screen_search._scroll_thumb_color())

    def test_editor_uses_only_the_standard_windows_frame(self):
        source = inspect.getsource(screen_search.TextDocumentWindow)
        self.assertNotIn("overrideredirect", source)
        self.assertNotIn("SetWindowLong", source)
        self.assertNotIn("WM_NCHITTEST", source)
        self.assertNotIn("_native_frame", source)
        self.assertNotIn("_close_button", source)

        editor = screen_search.TextDocumentWindow.__new__(
            screen_search.TextDocumentWindow
        )
        editor.win = mock.Mock()
        editor._focus_and_release_topmost = mock.Mock()
        editor._show_standard_window()
        editor.win.deiconify.assert_called_once_with()
        editor.win.after.assert_called_once_with(
            120, editor._focus_and_release_topmost
        )

    def test_builtin_editor_saves_utf8_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "note.txt")
            screen_search._write_text_document(path, "中文\nhello")
            with open(path, "r", encoding="utf-8", newline="") as handle:
                self.assertEqual("中文\nhello", handle.read())

    def test_tray_menu_exposes_the_same_new_text_action(self):
        app = screen_search.ScreenSearchApp.__new__(screen_search.ScreenSearchApp)
        app.event_queue = queue.Queue()

        menu = app._build_tray_menu_spec(lambda _lang: lambda: None)

        new_text_item = menu[2]
        self.assertIn("Ctrl+Alt+N", new_text_item[0])
        new_text_item[1]()
        self.assertEqual("new_text", app.event_queue.get_nowait())


class KeyLogStoreTests(unittest.TestCase):
    def make_store(self, directory):
        return screen_search.KeyLogStore(
            path=os.path.join(directory, "log.jsonl"),
            config_path=os.path.join(directory, "config.json"),
        )

    def test_privacy_defaults_are_off_and_config_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            self.assertFalse(store.keylog_enabled)
            self.assertFalse(store.clipboard_enabled)
            self.assertFalse(store.uia_enabled)
            store.set_keylog_enabled(True)
            with open(store.config_path, "r", encoding="utf-8") as f:
                self.assertTrue(json.load(f)["keylog_enabled"])
            leftovers = [name for name in os.listdir(directory) if name.endswith(".tmp")]
            self.assertEqual([], leftovers)

    def test_disk_pages_are_bounded_and_walk_backwards(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.set_keylog_enabled(True)
            for index in range(3000):
                store.append("key", str(index % 10))

            seen = 0
            offset = None
            has_older = True
            page_sizes = []
            while has_older:
                page, offset, has_older = store.load_page_before(
                    offset, target_chars=120, max_records=200
                )
                self.assertTrue(page)
                page_sizes.append(len(page))
                seen += len(page)
            self.assertEqual(3000, seen)
            self.assertLessEqual(max(page_sizes), 200)

    def test_copy_builder_is_bounded_and_omits_special_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.set_keylog_enabled(True)
            store.set_uia_enabled(True)
            store.append("key", "a")
            store.append("key", "[*Enter]")
            store.append("ime", "中文")
            self.assertEqual("a中文", store.build_copy_text(100))
            with self.assertRaises(ValueError):
                store.build_copy_text(1)

    def test_expired_tail_stops_without_scanning_for_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            old = time.time() - (30 * 86400)
            with open(store.path, "w", encoding="utf-8") as f:
                for _ in range(100):
                    f.write(json.dumps({"ts": old, "kind": "key", "text": "x"}) + "\n")
            page, offset, has_older = store.load_page_before(None, target_chars=10)
            self.assertEqual([], page)
            self.assertEqual(0, offset)
            self.assertFalse(has_older)

    def test_plaintext_uia_diagnostic_code_is_absent(self):
        with open(screen_search.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("stderr.write", source)
        self.assertIn("not (self.store.keylog_enabled and self.store.uia_enabled)", source)


@unittest.skipUnless(os.name == "nt", "Windows mutex test")
class SingleInstanceTests(unittest.TestCase):
    def test_second_mutex_acquire_is_rejected(self):
        first = screen_search.SingleInstanceGuard()
        second = screen_search.SingleInstanceGuard()
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            second.release()
            first.release()


@unittest.skipUnless(os.name == "nt", "Windows tray icon tests")
class TrayIconTests(unittest.TestCase):
    def test_native_hotkey_message_dispatches_registered_callback(self):
        calls = []
        tray = screen_search.NativeTray.__new__(screen_search.NativeTray)
        tray._registered_hotkeys = {2: lambda: calls.append("keylog")}
        tray._TaskbarCreated = 0

        result = tray._wnd_proc(1, screen_search._WM_HOTKEY, 2, 0)

        self.assertEqual(0, result)
        self.assertEqual(["keylog"], calls)

    def test_native_hotkeys_register_and_unregister_with_windows(self):
        fake_user32 = mock.Mock()
        fake_user32.RegisterHotKey.return_value = True
        tray = screen_search.NativeTray.__new__(screen_search.NativeTray)
        tray.hwnd = 0x1234
        tray._hotkey_specs = {
            1: (
                screen_search._MOD_CONTROL
                | screen_search._MOD_ALT
                | screen_search._MOD_NOREPEAT,
                ord("F"),
                lambda: None,
            )
        }
        tray._registered_hotkeys = {}

        with mock.patch.object(screen_search, "_user32", fake_user32):
            self.assertEqual(1, tray._register_hotkeys())
            tray._unregister_hotkeys()

        fake_user32.RegisterHotKey.assert_called_once_with(
            0x1234,
            1,
            screen_search._MOD_CONTROL
            | screen_search._MOD_ALT
            | screen_search._MOD_NOREPEAT,
            ord("F"),
        )
        fake_user32.UnregisterHotKey.assert_called_once_with(0x1234, 1)

    def test_small_tray_icon_is_blank_ruled_paper(self):
        image = screen_search._build_tray_icon_image(16)
        pixels = list(image.get_flattened_data())

        self.assertIn((250, 250, 247, 255), pixels)
        self.assertIn((75, 145, 194, 255), pixels)
        self.assertNotIn((255, 225, 77, 255), pixels)
        ruled_rows = {
            y
            for y in range(image.height)
            if sum(
                image.getpixel((x, y)) == (75, 145, 194, 255)
                for x in range(image.width)
            ) >= 4
        }
        self.assertEqual(3, len(ruled_rows))

    def test_explicit_icon_size_is_loaded(self):
        class ICONINFO(ctypes.Structure):
            _fields_ = [
                ("fIcon", wt.BOOL),
                ("xHotspot", wt.DWORD),
                ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP),
                ("hbmColor", wt.HBITMAP),
            ]

        class BITMAP(ctypes.Structure):
            _fields_ = [
                ("bmType", wt.LONG),
                ("bmWidth", wt.LONG),
                ("bmHeight", wt.LONG),
                ("bmWidthBytes", wt.LONG),
                ("bmPlanes", wt.WORD),
                ("bmBitsPixel", wt.WORD),
                ("bmBits", wt.LPVOID),
            ]

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetIconInfo.argtypes = [wt.HICON, ctypes.POINTER(ICONINFO)]
        user32.GetIconInfo.restype = wt.BOOL
        gdi32.GetObjectW.argtypes = [wt.HGDIOBJ, ctypes.c_int, wt.LPVOID]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
        gdi32.DeleteObject.restype = wt.BOOL

        hicon, path = screen_search._pil_to_hicon(
            Image.new("RGBA", (64, 64), "red"), 32, 32
        )
        self.assertTrue(hicon)
        info = ICONINFO()
        try:
            with Image.open(path) as ico:
                for size in (16, 20, 24, 32, 40, 48, 64):
                    actual = ico.ico.getimage((size, size)).convert("RGBA")
                    expected = screen_search._build_tray_icon_image(size)
                    self.assertEqual(expected.tobytes(), actual.tobytes())
            self.assertTrue(user32.GetIconInfo(hicon, ctypes.byref(info)))
            bitmap = BITMAP()
            self.assertTrue(
                gdi32.GetObjectW(
                    info.hbmColor, ctypes.sizeof(bitmap), ctypes.byref(bitmap)
                )
            )
            self.assertEqual((32, 32), (bitmap.bmWidth, bitmap.bmHeight))
        finally:
            if info.hbmColor:
                gdi32.DeleteObject(info.hbmColor)
            if info.hbmMask:
                gdi32.DeleteObject(info.hbmMask)
            screen_search._user32.DestroyIcon(hicon)
            if path and os.path.exists(path):
                os.remove(path)

    def test_tooltip_update_does_not_resubmit_icon(self):
        seen_flags = []

        class FakeShell32:
            @staticmethod
            def Shell_NotifyIconW(_operation, pointer):
                update = ctypes.cast(
                    pointer, ctypes.POINTER(screen_search._NOTIFYICONDATAW)
                ).contents
                seen_flags.append(update.uFlags)
                return True

        tray = screen_search.NativeTray.__new__(screen_search.NativeTray)
        tray.tooltip = ""
        tray._nid = screen_search._NOTIFYICONDATAW()
        tray._nid.cbSize = ctypes.sizeof(screen_search._NOTIFYICONDATAW)
        tray._nid.hWnd = 1
        tray._nid.uID = 1
        tray._nid.uFlags = (
            screen_search._NIF_MESSAGE
            | screen_search._NIF_ICON
            | screen_search._NIF_TIP
        )

        original_shell32 = screen_search._shell32
        try:
            screen_search._shell32 = FakeShell32()
            tray.set_tooltip("updated")
        finally:
            screen_search._shell32 = original_shell32

        self.assertEqual([screen_search._NIF_TIP], seen_flags)
        self.assertEqual(
            screen_search._NIF_MESSAGE
            | screen_search._NIF_ICON
            | screen_search._NIF_TIP,
            tray._nid.uFlags,
        )


if __name__ == "__main__":
    unittest.main()
