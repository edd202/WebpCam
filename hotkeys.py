"""Global recording hotkeys and local settings (Windows RegisterHotKey)."""
from dataclasses import dataclass, asdict
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile


@dataclass(frozen=True)
class Hotkey:
    modifiers: int = 0
    vk: int = 0x74
    name: str = 'F5'

    @property
    def label(self):
        parts = [name for bit, name in ((2, 'Ctrl'), (1, 'Alt'), (4, 'Shift'))
                 if self.modifiers & bit]
        return '+'.join(parts + [self.name])


DEFAULT_HOTKEY = Hotkey()
MODIFIER_KEYS = {'Shift_L', 'Shift_R', 'Control_L', 'Control_R', 'Alt_L', 'Alt_R',
                 'Meta_L', 'Meta_R', 'Super_L', 'Super_R', 'ISO_Level3_Shift'}
KEYS = {
    'space': (32, 'Space'), 'Return': (13, 'Enter'), 'Escape': (27, 'Esc'),
    'Tab': (9, 'Tab'), 'BackSpace': (8, 'Backspace'), 'Delete': (46, 'Delete'),
    'Insert': (45, 'Insert'), 'Home': (36, 'Home'), 'End': (35, 'End'),
    'Prior': (33, 'Page Up'), 'Next': (34, 'Page Down'),
    'Left': (37, 'Left'), 'Up': (38, 'Up'), 'Right': (39, 'Right'), 'Down': (40, 'Down'),
    'minus': (189, '-'), 'equal': (187, '='), 'comma': (188, ','), 'period': (190, '.'),
    'slash': (191, '/'), 'semicolon': (186, ';'), 'apostrophe': (222, "'"),
    'bracketleft': (219, '['), 'bracketright': (221, ']'), 'backslash': (220, '\\'),
    'grave': (192, '`'),
}


def modifier_bits(state, platform=None, get_key_state=None):
    platform = platform or sys.platform
    if platform == 'win32':
        # Tk's Mod1 bit is not a portable Alt flag (Windows also uses lock bits).
        # Read Ctrl/Alt/Shift from the Windows input queue, and only use bit 15.
        if get_key_state is None:
            get_key_state = ctypes.WinDLL('user32').GetKeyState
            get_key_state.argtypes = [ctypes.c_int]
            get_key_state.restype = ctypes.c_short
        return sum(flag for vk, flag in ((0x11, 2), (0x12, 1), (0x10, 4))
                   if get_key_state(vk) & 0x8000)
    return (2 if state & 4 else 0) | (4 if state & 1 else 0) | (1 if state & 8 else 0)


def from_event(event, *, platform=None, get_key_state=None):
    platform = platform or sys.platform
    key = event.keysym
    if key in MODIFIER_KEYS:
        return None
    if key.startswith('F') and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk, name = 111 + int(key[1:]), key
    elif len(key) == 1 and key.isascii() and key.isalnum():
        vk, name = ord(key.upper()), key.upper()
    elif key in KEYS:
        vk, name = KEYS[key]
    elif platform == 'win32' and 0 < event.keycode < 256:
        vk, name = event.keycode, key
    else:
        raise ValueError('이 키는 지원하지 않습니다. F1~F11 또는 다른 일반 키를 눌러 주세요.')
    if vk == 0x7B:
        raise ValueError('F12는 Windows 예약 키입니다. 다른 키를 선택해 주세요.')
    if vk in (16, 17, 18, 91, 92):
        return None
    modifiers = modifier_bits(event.state, platform, get_key_state)
    return Hotkey(modifiers, vk, name)


def default_settings_path():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'WebPCam' / 'settings.json'


def load_hotkey(path):
    if path is None:
        return DEFAULT_HOTKEY
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))['hotkey']
        key = Hotkey(**value)
        if (type(key.modifiers) is not int or key.modifiers & ~7 or
                type(key.vk) is not int or not 1 <= key.vk <= 254 or
                key.vk in (16, 17, 18, 91, 92, 123) or
                not isinstance(key.name, str) or not 1 <= len(key.name) <= 40):
            return DEFAULT_HOTKEY
        return key
    except (OSError, ValueError, TypeError, KeyError):
        return DEFAULT_HOTKEY


def save_hotkey(path, key):
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='settings-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'hotkey': asdict(key)}, stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class GlobalHotkey:
    def __init__(self, hwnd=None, api=None):
        self.hwnd = hwnd
        self.api = api
        self.current = DEFAULT_HOTKEY
        self.active_id = None
        self.pending = False
        if api is None and sys.platform == 'win32':
            self.api = ctypes.WinDLL('user32', use_last_error=True)
            self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self.api.RegisterHotKey.restype = wintypes.BOOL
            self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self.api.UnregisterHotKey.restype = wintypes.BOOL

    def bind(self, key):
        if self.active_id is not None and key == self.current:
            return
        if self.api is None:
            raise RuntimeError('전역 단축키는 Windows에서 사용할 수 있습니다.')
        new_id = 0x5142 if self.active_id == 0x5141 else 0x5141
        # Reserve the replacement first: conflicts must not disable the old key.
        if not self.api.RegisterHotKey(self.hwnd, new_id, key.modifiers | 0x4000, key.vk):
            raise ValueError(f'{key.label} 키를 등록하지 못했습니다. 다른 프로그램에서 사용 중이거나 예약된 키입니다.')
        if self.active_id is not None:
            self.api.UnregisterHotKey(self.hwnd, self.active_id)
        self.active_id, self.current, self.pending = new_id, key, False

    def receive(self, identifier):
        if identifier == self.active_id:
            self.pending = True

    def close(self):
        if self.api is not None and self.active_id is not None:
            self.api.UnregisterHotKey(self.hwnd, self.active_id)
        self.active_id, self.pending = None, False
