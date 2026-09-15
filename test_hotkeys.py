from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from hotkeys import Hotkey, GlobalHotkey, from_event, save_hotkey, load_hotkey, DEFAULT_HOTKEY
from webpcam import WebPCam


class HotkeyTests(TestCase):
    def test_default_and_pressed_keys(self):
        self.assertEqual(DEFAULT_HOTKEY.label, 'F5')
        self.assertEqual(from_event(SimpleNamespace(keysym='F6', state=0)).vk, 0x75)
        self.assertEqual(from_event(SimpleNamespace(keysym='F7', state=5), platform='linux').label, 'Ctrl+Shift+F7')
        self.assertIsNone(from_event(SimpleNamespace(keysym='Control_L', state=0)))
        with self.assertRaises(ValueError):
            from_event(SimpleNamespace(keysym='F12', state=0))

    def test_windows_lock_bits_do_not_add_alt(self):
        for state in (0, 8, 10, 0x20000, 0x20008):
            event = SimpleNamespace(keysym='a', keycode=65, state=state)
            for native_state in (0, 1):  # Released, with or without the toggle bit.
                key = from_event(event, platform='win32', get_key_state=lambda vk: native_state)
                self.assertEqual(key.label, 'A')
                self.assertEqual(key.modifiers, 0)

    def test_windows_actual_modifier_combinations(self):
        event = SimpleNamespace(keysym='a', keycode=65, state=8)
        for mask in range(8):
            down = {vk for vk, flag in ((0x11, 2), (0x12, 1), (0x10, 4)) if mask & flag}
            key = from_event(event, platform='win32',
                             get_key_state=lambda vk: -32768 if vk in down else 0)
            self.assertEqual(key.modifiers, mask)
        key = from_event(event, platform='win32',
                         get_key_state=lambda vk: -32768 if vk == 0x12 else 0)
        self.assertEqual(key.label, 'Alt+A')

    def test_conflict_preserves_original_registration(self):
        api = Mock()
        api.RegisterHotKey.return_value = True
        manager = GlobalHotkey(123, api)
        manager.bind(DEFAULT_HOTKEY)
        previous_id = manager.active_id
        api.RegisterHotKey.return_value = False
        with self.assertRaises(ValueError):
            manager.bind(Hotkey(0, 0x75, 'F6'))
        self.assertEqual(manager.active_id, previous_id)
        self.assertEqual(manager.current, DEFAULT_HOTKEY)
        api.UnregisterHotKey.assert_not_called()
        api.RegisterHotKey.return_value = True
        manager.bind(Hotkey(0, 0x75, 'F6'))
        api.UnregisterHotKey.assert_called_once_with(123, previous_id)
        self.assertNotEqual(manager.active_id, previous_id)
        self.assertTrue(api.RegisterHotKey.call_args.args[2] & 0x4000)
        manager.close()

    def test_settings_round_trip_and_invalid_fallback(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            self.assertEqual(load_hotkey(path), DEFAULT_HOTKEY)
            selected = Hotkey(2, 0x76, 'F7')
            save_hotkey(path, selected)
            self.assertEqual(load_hotkey(path), selected)
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(load_hotkey(path), DEFAULT_HOTKEY)

    def test_dispatch_start_stop_and_suppressed_contexts(self):
        app = WebPCam.__new__(WebPCam)
        app.hotkey = GlobalHotkey(api=Mock())
        app.hotkey.bind(DEFAULT_HOTKEY)
        app.record, app.stop_recording = Mock(), Mock()
        app.dialog_open, app.key_dialog = False, None
        for state in ('idle', 'recording', 'saving'):
            app.state = state
            app.hotkey.receive(app.hotkey.active_id)
            app.process_hotkey()
        app.record.assert_called_once()
        app.stop_recording.assert_called_once()
        app.state, app.dialog_open = 'idle', True
        app.hotkey.receive(app.hotkey.active_id)
        app.process_hotkey()
        self.assertEqual(app.record.call_count, 1)
        app.dialog_open, app.key_dialog = False, Mock()
        app.hotkey.receive(app.hotkey.active_id)
        app.process_hotkey()
        app.key_dialog.choose.assert_called_once_with(DEFAULT_HOTKEY)
        self.assertEqual(app.record.call_count, 1)
