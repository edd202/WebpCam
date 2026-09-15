"""Real Tk widget regression tests. Run on Windows, or under an X display."""
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock
from hotkeys import DEFAULT_HOTKEY, load_hotkey
from webpcam import WebPCam, CLIENT_INSETS


@unittest.skipUnless(os.name == 'nt' or os.environ.get('DISPLAY'), 'Needs a graphical display')
class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self.app = WebPCam(self.root)
        self.root.update()

    def tearDown(self):
        if self.app.key_dialog is not None:
            self.app.key_dialog.close()
        self.app.hide_saved_message()
        self.app.hotkey.close()
        self.app.aspect_resize.close()
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.assertEqual(self.callback_errors, [], "Unexpected Tk callback exception")

    def size(self):
        return self.app.area.winfo_width(), self.app.area.winfo_height()

    def test_three_ratios_actual_widgets_and_displayed_dimensions(self):
        for initial_h in (577, 611, 679):
            for ratio in ('4:3', '16:9', '16:10'):
                self.root.geometry(f'{831 + CLIENT_INSETS[0]}x{initial_h + CLIENT_INSETS[1]}')
                self.root.update()
                self.app.frame_menu.invoke({'4:3': 2, '16:9': 3, '16:10': 4}[ratio])
                self.root.update()
                width, height = self.size()
                a, b = map(int, ratio.split(':'))
                self.assertEqual(width * b, height * a, (ratio, width, height))
                self.assertEqual(self.app.size_label.cget('text'), f'{width} × {height}\n{ratio}')
                self.assertEqual((self.root.winfo_width() - width,
                                  self.root.winfo_height() - height), CLIENT_INSETS)
                print(f'Actual Tk: {ratio} -> {width} x {height}')
                # Switch from each lock to free sizing without changing the size.
                self.app.frame_menu.invoke(0)
                self.assertEqual(self.app.aspect.get(), 'free')
                self.root.update()
                self.assertIsNone(self.app.aspect_resize.ratio)
                self.assertEqual(self.size(), (width, height))
                for free_w, free_h in ((831, 577), (700, 577), (700, 611)):
                    self.root.geometry(f'{free_w + CLIENT_INSETS[0]}x{free_h + CLIENT_INSETS[1]}')
                    self.root.update()
                    self.app.enforce_aspect()
                    self.assertEqual(self.size(), (free_w, free_h))
                    self.assertEqual(self.app.size_label.cget('text'),
                                     f'{free_w} × {free_h}\n자유 비율')

    def test_move_keeps_actual_capture_size(self):
        self.app.aspect.set('16:9')
        self.app.select_aspect()
        self.root.update()
        original = self.size()
        for x, y in ((10, 10), (300, 120), (80, 200)):
            self.root.geometry(f'+{x}+{y}')
            self.root.update()
            self.assertEqual(self.size(), original)

    def test_key_dialog_input_confirm_cancel_and_saved_setting(self):
        self.app.hotkey.close()
        self.app.hotkey.api = Mock()
        self.app.hotkey.api.RegisterHotKey.return_value = True
        self.app.hotkey.bind(DEFAULT_HOTKEY)
        with tempfile.TemporaryDirectory() as directory:
            self.app.settings_path = Path(directory) / 'settings.json'
            self.app.key_button.invoke()
            self.root.update()
            dialog = self.app.key_dialog
            dialog.window.event_generate('<KeyPress-F6>')
            self.root.update()
            self.assertEqual(dialog.selection.get(), '입력된 키: F6')
            self.assertEqual(self.app.hotkey.current.label, 'F5')
            dialog.confirm.invoke()
            self.root.update()
            self.assertIsNone(self.app.key_dialog)
            self.assertEqual(self.app.hotkey.current.label, 'F6')
            self.assertEqual(load_hotkey(self.app.settings_path).label, 'F6')
            self.app.key_button.invoke()
            self.root.update()
            dialog = self.app.key_dialog
            dialog.window.event_generate('<KeyPress-F8>')
            self.root.update()
            self.assertEqual(dialog.selection.get(), '입력된 키: F8')
            dialog.close()
            self.assertEqual(self.app.hotkey.current.label, 'F6')

    def test_saved_overlay_path_timeout_and_geometry(self):
        self.app.aspect.set('16:9')
        self.app.select_aspect()
        self.root.update()
        original = self.size()
        path = Path(tempfile.gettempdir()) / 'WebPCam-test.webp'
        self.app.show_saved_message(path)
        self.root.update()
        self.assertEqual(self.app.toast.cget('text'), f'저장이 완료되었습니다.\n{path.resolve()}')
        self.assertEqual(self.size(), original)
        self.root.after(1150, self.root.quit)
        self.root.mainloop()
        self.assertIsNone(self.app.toast)
        self.assertEqual(self.size(), original)


if __name__ == '__main__':
    unittest.main()
