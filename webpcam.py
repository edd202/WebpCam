"""webpCam 1.0 — Windows region recorder, animated WebP export."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import math
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageGrab, ImageTk, features
from hotkeys import GlobalHotkey, from_event, default_settings_path, load_hotkey, save_hotkey

MEMORY_LIMIT = 256 * 1024 * 1024
SECONDS_LIMIT = 60
TRANSPARENT = "#ff00fe"
VERSION = "1.0"
FREE_ASPECT = "free"
# These same physical-pixel constants drive both placement and ratio maths.
CAPTURE_LEFT = 4
CAPTURE_TOP = 4
SIDEBAR_WIDTH = 80
CAPTURE_RIGHT = SIDEBAR_WIDTH + 8
CAPTURE_BOTTOM = 4
CLIENT_INSETS = (CAPTURE_LEFT + CAPTURE_RIGHT, CAPTURE_TOP + CAPTURE_BOTTOM)



def ratio_size(width, height, ratio, minimum=(1, 1), drive="width"):
    """Return exact integer-pixel aspect ratio, excluding window chrome."""
    a, b = ratio
    divisor = math.gcd(a, b)
    a, b = a // divisor, b // divisor
    units = round(width / a if drive == "width" else height / b)
    units = max(1, units, math.ceil(minimum[0] / a), math.ceil(minimum[1] / b))
    return a * units, b * units


def constrain_rect(rect, edge, chrome, ratio, minimum):
    """WM_SIZING edges 1..8; preserve the opposite dragged edge/corner."""
    left, top, right, bottom = rect
    cw, ch = chrome
    width, height = right - left - cw, bottom - top - ch
    drive = "height" if edge in (3, 6) else "width"
    if edge in (4, 5, 7, 8):
        by_width = ratio_size(width, height, ratio, minimum, "width")
        by_height = ratio_size(width, height, ratio, minimum, "height")
        if abs(by_height[0] - width) < abs(by_width[1] - height):
            drive = "height"
    width, height = ratio_size(width, height, ratio, minimum, drive)
    width, height = width + cw, height + ch
    if edge in (1, 4, 7):
        left = right - width
    else:
        right = left + width
    if edge in (3, 4, 5):
        top = bottom - height
    else:
        bottom = top + height
    return left, top, right, bottom


class AspectResize:
    """Constrain the Windows sizing rectangle before the window is redrawn."""
    def __init__(self, root, area):
        self.root, self.area = root, area
        self.ratio = None
        self.chrome = (0, 0)
        # Explicit place() layout and ratio code share one source of truth.
        self.client_insets = CLIENT_INSETS
        mw, mh = root.minsize()
        self.minimum = (max(1, mw - self.client_insets[0]),
                        max(1, mh - self.client_insets[1]))
        self.did_size = False
        self.settle_pending = False
        self.old_proc = None
        self.hotkey_receiver = None
        if sys.platform != "win32":
            return
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.GetParent.argtypes = [wintypes.HWND]
        self.api.GetParent.restype = wintypes.HWND
        self.api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.api.GetWindowRect.restype = wintypes.BOOL
        self.api.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.api.GetClientRect.restype = wintypes.BOOL
        self.api.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                          ctypes.c_void_p, wintypes.UINT]
        self.api.RedrawWindow.restype = wintypes.BOOL
        self.hwnd = self.api.GetParent(root.winfo_id()) or root.winfo_id()
        self.setter = (self.api.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8
                       else self.api.SetWindowLongW)
        self.setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        self.setter.restype = ctypes.c_void_p
        self.api.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                             wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.CallWindowProcW.restype = ctypes.c_ssize_t
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                      wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.callback = proc_type(self.window_proc)  # Keep alive until unhooked.
        ctypes.set_last_error(0)
        self.old_proc = self.setter(self.hwnd, -4, ctypes.cast(self.callback, ctypes.c_void_p))
        if not self.old_proc:
            raise ctypes.WinError(ctypes.get_last_error())
        self.refresh()

    def refresh(self):
        if not self.old_proc:
            return
        bounds, client = wintypes.RECT(), wintypes.RECT()
        if (self.api.GetWindowRect(self.hwnd, ctypes.byref(bounds)) and
                self.api.GetClientRect(self.hwnd, ctypes.byref(client))):
            self.chrome = (bounds.right - bounds.left - (client.right - client.left)
                           + self.client_insets[0],
                           bounds.bottom - bounds.top - (client.bottom - client.top)
                           + self.client_insets[1])

    def redraw(self):
        if self.old_proc:
            # Invalidate/erase all children after layout settles, including the
            # color-keyed capture surface, to clear stale exposed resize pixels.
            self.api.RedrawWindow(self.hwnd, None, None, 0x0001 | 0x0004 | 0x0080 | 0x0100)

    def window_proc(self, hwnd, message, wparam, lparam):
        # No Tk calls here: queue hotkeys for the main Tk loop.
        if message == 0x0312 and self.hotkey_receiver is not None:  # WM_HOTKEY
            self.hotkey_receiver(wparam)
            return 0
        if message == 0x0231:  # WM_ENTERSIZEMOVE: freeze current non-client metrics.
            self.did_size = False
            self.refresh()
        elif message == 0x0232:  # WM_EXITSIZEMOVE: moving alone must never resize.
            self.settle_pending = self.did_size
        if message == 0x0214 and self.ratio:  # WM_SIZING
            self.did_size = True
            rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = constrain_rect(
                (rect.left, rect.top, rect.right, rect.bottom), wparam,
                self.chrome, self.ratio, self.minimum)
            return 1
        if message == 0x0112 and (wparam & 0xFFF0) == 0xF030 and self.ratio:
            return 0  # A maximized desktop rectangle cannot retain this aspect ratio.
        return self.api.CallWindowProcW(self.old_proc, hwnd, message, wparam, lparam)

    def close(self):
        if self.old_proc:
            self.setter(self.hwnd, -4, self.old_proc)
            self.old_proc = None


def frame_durations(timestamps, end):
    """Round absolute boundaries so rounding error cannot accumulate."""
    if not timestamps:
        return []
    origin = timestamps[0]
    bounds = [round((t - origin) * 1000) for t in timestamps]
    bounds.append(round((end - origin) * 1000))
    return [max(1, b - a) for a, b in zip(bounds, bounds[1:])]


def export_webp(path, frames, durations, quality=80, lossless=False):
    if not frames or len(frames) != len(durations):
        raise ValueError("저장할 프레임과 재생 시간이 올바르지 않습니다.")
    if any(f.size != frames[0].size for f in frames):
        raise ValueError("프레임 크기가 서로 다릅니다.")
    if not 0 <= quality <= 100 or any(d <= 0 for d in durations):
        raise ValueError("품질 또는 재생 시간이 올바르지 않습니다.")
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".webpcam-", suffix=".webp", dir=path.parent)
    os.close(fd)
    try:
        frames[0].save(temporary, format="WEBP", save_all=True,
                       append_images=frames[1:], duration=durations, loop=0,
                       quality=quality, lossless=lossless, method=4)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def capture_region(bbox, fps, scale, stop, events, grab=None,
                   memory_limit=MEMORY_LIMIT, seconds_limit=SECONDS_LIMIT):
    """Worker owns all images until done; never calls Tk from a thread."""
    frames, timestamps = [], []
    reason = ""
    started = time.perf_counter()
    interval = 1 / fps
    output_size = (max(1, round((bbox[2] - bbox[0]) * scale)),
                   max(1, round((bbox[3] - bbox[1]) * scale)))
    max_frames = max(1, memory_limit // (output_size[0] * output_size[1] * 4))
    if grab is None:
        grab = lambda: ImageGrab.grab(bbox=bbox, all_screens=True)
    try:
        while not stop.is_set():
            now = time.perf_counter()
            if now - started >= seconds_limit:
                reason = "최대 녹화 시간에 도달했습니다."
                break
            if len(frames) >= max_frames:
                reason = "프레임 메모리 한도에 도달했습니다."
                break
            frame = grab().convert("RGB")
            if frame.size != output_size:
                frame = frame.resize(output_size, Image.Resampling.LANCZOS)
            frames.append(frame)
            timestamps.append(now)
            events.put(("progress", (len(frames), now - started)))
            # Never burst to make up missed frames; timestamps retain real speed.
            stop.wait(max(0, interval - (time.perf_counter() - now)))
    except Exception as exc:
        reason = f"화면 캡처 중 오류: {exc}"
    finally:
        end = time.perf_counter()
        events.put(("captured", (frames, frame_durations(timestamps, end), reason)))


class Preview:
    def __init__(self, parent, frames, durations):
        self.frames, self.durations = frames, durations
        self.index, self.timer, self.playing = 0, None, True
        self.window = tk.Toplevel(parent)
        self.window.title("webpCam · 미리보기")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.label = ttk.Label(self.window, anchor="center")
        self.label.pack(padx=12, pady=12)
        bar = ttk.Frame(self.window)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        self.button = ttk.Button(bar, text="일시정지", command=self.toggle)
        self.button.pack(side="left")
        self.counter = ttk.Label(bar)
        self.counter.pack(side="right")
        self.draw()

    def draw(self):
        frame = self.frames[self.index].copy()
        frame.thumbnail((min(960, self.window.winfo_screenwidth() - 100),
                         min(640, self.window.winfo_screenheight() - 180)))
        self.photo = ImageTk.PhotoImage(frame)
        self.label.configure(image=self.photo)
        self.counter.configure(text=f"{self.index + 1} / {len(self.frames)} 프레임")
        if self.playing:
            self.timer = self.window.after(self.durations[self.index], self.advance)

    def advance(self):
        self.timer = None
        self.index = (self.index + 1) % len(self.frames)
        self.draw()

    def toggle(self):
        self.playing = not self.playing
        self.button.configure(text="일시정지" if self.playing else "재생")
        if self.timer:
            self.window.after_cancel(self.timer)
            self.timer = None
        if self.playing:
            self.draw()

    def close(self):
        if self.timer:
            self.window.after_cancel(self.timer)
        self.window.destroy()


class KeyDialog:
    def __init__(self, app):
        self.app, self.candidate = app, None
        self.window = tk.Toplevel(app.root)
        self.window.title("webpCam · 단축키 설정")
        self.window.resizable(False, False)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        content = ttk.Frame(self.window, padding=18)
        content.pack(fill="both", expand=True)
        suffix = "" if app.hotkey.active_id is not None else " (등록 안 됨)"
        ttk.Label(content, text=f"현재 키: {app.hotkey.current.label}{suffix}").pack(anchor="w")
        ttk.Label(content, text="원하는 키를 누르세요. Ctrl·Alt·Shift 조합도 가능합니다.").pack(anchor="w", pady=(10, 6))
        self.selection = tk.StringVar(value="입력된 키: —")
        ttk.Label(content, textvariable=self.selection, font=("Malgun Gothic", 12, "bold")).pack(anchor="w", pady=6)
        self.error = tk.StringVar()
        ttk.Label(content, textvariable=self.error, foreground="#b42318", wraplength=400).pack(anchor="w")
        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="취소", command=self.close).pack(side="right")
        self.confirm = ttk.Button(buttons, text="확인", command=self.apply, state="disabled")
        self.confirm.pack(side="right", padx=6)
        self.window.bind("<KeyPress>", self.key_pressed)
        self.window.grab_set()
        self.window.focus_force()

    def choose(self, key):
        if key is not None:
            self.candidate = key
            self.selection.set(f"입력된 키: {key.label}")
            self.error.set("")
            self.confirm.configure(state="normal")

    def key_pressed(self, event):
        try:
            self.choose(from_event(event))
        except ValueError as exc:
            self.error.set(str(exc))
            self.candidate = None
            self.confirm.configure(state="disabled")
        return "break"

    def apply(self):
        if self.candidate is None:
            return
        try:
            self.app.hotkey.bind(self.candidate)
        except (ValueError, RuntimeError) as exc:
            self.error.set(str(exc))
            return
        try:
            save_hotkey(self.app.settings_path, self.candidate)
            self.app.status.set(f"녹화 시작 / 정지: {self.candidate.label}")
        except OSError as exc:
            messagebox.showwarning("설정 저장 실패", f"키는 적용됐지만 다음 실행을 위한 저장에 실패했습니다.\n{exc}", parent=self.window)
        self.close()

    def close(self):
        self.app.hotkey.pending = False
        self.window.grab_release()
        self.window.destroy()
        self.app.key_dialog = None


class WebPCam:
    def __init__(self, root):
        self.root = root
        self.frames, self.durations = [], []
        self.dirty = False
        self.state = "idle"
        self.dialog_open = False
        self.key_dialog = None
        self.settings_path = default_settings_path() if sys.platform == "win32" else None
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.controller = None
        self.preview_window = None
        root.title(f"webpCam {VERSION}")
        self.toast = None
        self.toast_timer = None
        self.icon_path = Path(__file__).resolve().with_name("webpcam.ico")
        if sys.platform == "win32" and self.icon_path.exists():
            root.iconbitmap(default=str(self.icon_path))
        root.geometry("420x280+120+120")
        root.minsize(240, 210)
        root.attributes("-topmost", True)
        if sys.platform == "win32":
            root.attributes("-transparentcolor", TRANSPARENT)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        else:
            style.theme_use("clam")
        style.configure("Cam.TButton", padding=(2, 3), font=("Segoe UI", 9))
        self.fps = tk.StringVar(value="15")
        self.scale = tk.StringVar(value="100%")
        self.quality = tk.StringVar(value="80")
        self.lossless = tk.BooleanVar(value=False)
        self.aspect = tk.StringVar(value=FREE_ASPECT)
        self.aspect_resize = None
        self.status = tk.StringVar(value="Ready")
        self.status.trace_add("write", lambda *_: root.title(f"webpCam {VERSION} — " + self.status.get()))

        # Use direct physical-pixel placement instead of pack propagation:
        # capture width = client width - 92; height = client height - 8.
        sidebar = ttk.Frame(root)
        sidebar.place(relx=1.0, x=-SIDEBAR_WIDTH - 3, y=3, width=SIDEBAR_WIDTH,
                      relheight=1.0, height=-6)
        self.menu_buttons = []

        def split_button(label, command, menu):
            row = ttk.Frame(sidebar)
            row.pack(fill="x", pady=(0, 4))
            button = ttk.Button(row, text=label, command=command, width=5, style="Cam.TButton")
            button.pack(side="left", fill="x", expand=True)
            arrow = ttk.Button(row, text="▾", width=1, style="Cam.TButton")
            arrow.configure(command=lambda: self.popup(menu, arrow))
            arrow.pack(side="right")
            self.menu_buttons.append(arrow)
            return button

        rec_menu = tk.Menu(root, tearoff=False)
        for fps in (5, 10, 15, 20, 30):
            rec_menu.add_radiobutton(label=f"{fps} FPS", variable=self.fps, value=str(fps))
        size_menu = tk.Menu(rec_menu, tearoff=False)
        for size in ("100%", "75%", "50%"):
            size_menu.add_radiobutton(label=size, variable=self.scale, value=size)
        rec_menu.add_separator()
        rec_menu.add_cascade(label="녹화 크기", menu=size_menu)
        self.record_button = split_button("Rec", self.record, rec_menu)
        self.new_button = ttk.Button(sidebar, text="New", width=7,
                                      command=self.new, style="Cam.TButton")
        self.new_button.pack(fill="x", pady=(0, 4))
        self.frame_menu = tk.Menu(root, tearoff=False)
        self.frame_menu.add_radiobutton(label="자유 비율", variable=self.aspect, value=FREE_ASPECT,
                                        command=self.select_aspect)
        self.frame_menu.add_separator()
        for ratio in ("4:3", "16:9", "16:10"):
            self.frame_menu.add_radiobutton(label=ratio, variable=self.aspect, value=ratio,
                                            command=self.select_aspect)
        self.frame_button = ttk.Button(sidebar, text="Frame ▾", width=7,
                                        command=lambda: self.popup(self.frame_menu, self.frame_button),
                                        style="Cam.TButton")
        self.frame_button.pack(fill="x", pady=(0, 4))
        self.preview_button = ttk.Button(sidebar, text="View", width=7,
                                          command=self.preview, style="Cam.TButton")
        self.preview_button.pack(fill="x", pady=(0, 4))
        save_menu = tk.Menu(root, tearoff=False)
        save_menu.add_checkbutton(label="무손실 WebP", variable=self.lossless)
        save_menu.add_separator()
        for quality in (50, 60, 70, 80, 90, 100):
            save_menu.add_radiobutton(label=f"품질 {quality}", variable=self.quality, value=str(quality))
        self.save_button = split_button("Save", self.save, save_menu)
        self.key_button = ttk.Button(sidebar, text="Key", width=7, command=self.open_keys,
                                     style="Cam.TButton")
        self.key_button.pack(fill="x", pady=(0, 4))
        self.size_label = ttk.Label(sidebar, text="", anchor="center", font=("Segoe UI", 8))
        self.size_label.pack(fill="x", pady=(5, 0))
        rim = tk.Frame(root, relief="sunken", borderwidth=1)
        rim.place(x=CAPTURE_LEFT - 1, y=CAPTURE_TOP - 1, relwidth=1.0,
                  width=-CLIENT_INSETS[0] + 2, relheight=1.0,
                  height=-CLIENT_INSETS[1] + 2)
        self.area = tk.Frame(root, bg=TRANSPARENT, borderwidth=0, highlightthickness=0)
        self.area.place(x=CAPTURE_LEFT, y=CAPTURE_TOP, relwidth=1.0,
                        width=-CLIENT_INSETS[0], relheight=1.0, height=-CLIENT_INSETS[1])
        self.area.bind("<Configure>", self.area_changed)
        root.update_idletasks()
        root.minsize(240, max(210, sum(child.winfo_reqheight() + 4
                                     for child in sidebar.winfo_children()) + 16))
        root.update_idletasks()
        self.aspect_resize = AspectResize(root, self.area)
        self.hotkey = GlobalHotkey(getattr(self.aspect_resize, "hwnd", None))
        self.aspect_resize.hotkey_receiver = self.hotkey.receive
        self.hotkey.current = load_hotkey(self.settings_path)
        if sys.platform == "win32":
            try:
                self.hotkey.bind(self.hotkey.current)
            except ValueError:
                self.status.set(f"{self.hotkey.current.label} 등록 실패 · Key에서 다른 키를 선택하세요.")
        self.set_buttons()
        root.after(80, self.poll)

    def open_keys(self):
        if self.state != "idle":
            return
        if self.key_dialog is not None:
            self.key_dialog.window.lift()
            return
        self.hotkey.pending = False
        self.key_dialog = KeyDialog(self)

    def process_hotkey(self):
        if not self.hotkey.pending:
            return
        self.hotkey.pending = False
        if self.key_dialog is not None:
            self.key_dialog.choose(self.hotkey.current)
            return
        if self.dialog_open:
            return
        if self.state == "recording":
            self.stop_recording()
        elif self.state == "idle":
            self.record()

    def select_aspect(self):
        if self.state != "idle":
            return
        if self.aspect.get() == FREE_ASPECT:
            self.aspect_resize.ratio = None
            self.aspect_resize.settle_pending = False
            self.aspect_resize.did_size = False
            self.area_changed()
            return
        if self.root.state() == "zoomed":
            self.root.state("normal")
            self.root.update_idletasks()
        self.aspect_resize.ratio = tuple(map(int, self.aspect.get().split(":")))
        self.enforce_aspect()

    def enforce_aspect(self):
        if self.state != "idle" or not self.root.winfo_viewable():
            return
        if not self.aspect_resize.ratio:
            return
        # Explicit actions / sizing completion only. No Configure -> geometry
        # feedback loop, and no measurement of asynchronously laid-out children.
        cw, ch = self.aspect_resize.client_insets
        width, height = self.root.winfo_width() - cw, self.root.winfo_height() - ch
        w, h = ratio_size(width, height, self.aspect_resize.ratio,
                          self.aspect_resize.minimum)
        if (w, h) != (width, height):
            self.root.geometry(f"{w + cw}x{h + ch}")
        self.root.update_idletasks()
        self.aspect_resize.refresh()
        self.aspect_resize.redraw()
        self.area_changed()

    def popup(self, menu, button):
        if self.state != "idle":
            return
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def new(self):
        if self.state != "idle" or not self.discard_ok():
            return
        if self.preview_window and self.preview_window.window.winfo_exists():
            self.preview_window.close()
        self.preview_window = None
        self.frames, self.durations, self.dirty = [], [], False
        self.status.set("Ready")
        self.set_buttons()

    def area_changed(self, _event=None):
        if self.toast is not None:
            self.toast.configure(wraplength=max(60, self.area.winfo_width() - 32))
        self.size_label.configure(text=f"{self.area.winfo_width()} × {self.area.winfo_height()}\n{'자유 비율' if self.aspect.get() == FREE_ASPECT else self.aspect.get()}")

    def hide_saved_message(self):
        if self.toast_timer is not None:
            self.root.after_cancel(self.toast_timer)
            self.toast_timer = None
        if self.toast is not None:
            self.toast.destroy()
            self.toast = None

    def show_saved_message(self, path):
        self.hide_saved_message()
        location = str(Path(path).resolve())
        self.toast = tk.Label(self.area, text=f"저장이 완료되었습니다.\n{location}",
                              bg="#143250", fg="white", font=("Malgun Gothic", 10),
                              justify="center", padx=10, pady=12,
                              wraplength=max(60, self.area.winfo_width() - 32))
        self.toast.place(relx=.5, rely=.5, anchor="center", relwidth=.95)
        self.toast.lift()
        self.toast_timer = self.root.after(1000, self.hide_saved_message)

    def set_buttons(self):
        idle = self.state == "idle"
        for button in (self.record_button, self.new_button, self.frame_button, self.key_button, *self.menu_buttons):
            button.configure(state="normal" if idle else "disabled")
        for button in (self.preview_button, self.save_button):
            button.configure(state="normal" if idle and self.frames else "disabled")

    def discard_ok(self):
        if not self.dirty:
            return True
        self.dialog_open = True
        try:
            return messagebox.askyesno("저장하지 않은 녹화", "저장하지 않은 녹화를 버릴까요?", parent=self.root)
        finally:
            self.dialog_open = False
            self.hotkey.pending = False

    def record(self):
        if self.state != "idle" or not self.discard_ok():
            return
        self.hide_saved_message()
        if self.preview_window and self.preview_window.window.winfo_exists():
            self.preview_window.close()
        self.preview_window = None
        self.root.update_idletasks()
        self.enforce_aspect()
        self.root.update_idletasks()
        x, y = self.area.winfo_rootx(), self.area.winfo_rooty()
        w, h = self.area.winfo_width(), self.area.winfo_height()
        # Validate against the complete virtual desktop, including negative origins.
        if sys.platform == "win32":
            u = ctypes.windll.user32
            vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
            vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
            if x < vx or y < vy or x + w > vx + vw or y + h > vy + vh:
                messagebox.showerror("녹화 영역", "녹화 영역을 화면 안에 배치해 주세요.", parent=self.root)
                return
        fps, scale = int(self.fps.get()), int(self.scale.get().rstrip("%")) / 100
        self.frames, self.durations, self.dirty = [], [], False
        self.state = "recording"
        self.set_buttons()
        self.stop.clear()
        # Compact native control stays immediately to the right of the capture region.
        self.controller = tk.Toplevel(self.root)
        self.controller.overrideredirect(True)
        self.controller.attributes("-topmost", True)
        self.controller.geometry("80x100+0+0")
        self.record_status = tk.StringVar(value="Ready…")
        ttk.Button(self.controller, text="Stop", command=self.stop_recording,
                   style="Cam.TButton").pack(fill="x", padx=2, pady=3)
        ttk.Label(self.controller, textvariable=self.record_status, anchor="center",
                  font=("Segoe UI", 8)).pack(fill="x", padx=2, pady=2)
        self.controller.bind("<Escape>", lambda _: self.stop_recording())
        self.controller.update_idletasks()
        if sys.platform == "win32":
            # Tk interprets negative geometry offsets relative to the right/bottom
            # edge. Win32 placement instead accepts actual virtual-screen pixels.
            u = ctypes.windll.user32
            u.GetParent.argtypes = [wintypes.HWND]
            u.GetParent.restype = wintypes.HWND
            u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            u.SetWindowPos.restype = wintypes.BOOL
            hwnd = u.GetParent(self.controller.winfo_id()) or self.controller.winfo_id()
            u.SetWindowPos(hwnd, None, x + w + 5, y, 80, 100, 0x0004 | 0x0010)
        self.root.withdraw()
        self.controller.lift()
        self.controller.focus_force()
        fps, scale = int(self.fps.get()), int(self.scale.get().rstrip("%")) / 100
        # Let the desktop repaint after hiding the selection window.
        self.root.after(500, lambda: threading.Thread(target=capture_region,
            args=((x, y, x + w, y + h), fps, scale, self.stop, self.events), daemon=True).start())

    def stop_recording(self):
        self.stop.set()
        self.record_status.set("정리 중…")

    def poll(self):
        self.process_hotkey()
        if self.aspect_resize.settle_pending and self.state == "idle":
            self.aspect_resize.settle_pending = False
            self.root.update_idletasks()
            self.enforce_aspect()
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "progress":
                    count, seconds = data
                    if self.controller and self.controller.winfo_exists():
                        self.record_status.set(f"{seconds:.1f}s\n{count} frames")
                elif kind == "captured":
                    self.frames, self.durations, reason = data
                    if self.controller:
                        self.controller.destroy()
                        self.controller = None
                    self.root.deiconify()
                    self.state, self.dirty = "idle", bool(self.frames)
                    details = (f"{len(self.frames)}프레임 · {sum(self.durations) / 1000:.2f}초 · "
                               f"{self.frames[0].width} × {self.frames[0].height}") if self.frames else "녹화된 프레임이 없습니다."
                    self.status.set(f"{details}  {reason}")
                    if reason:
                        messagebox.showinfo("녹화 종료", reason + "\n캡처된 프레임은 저장할 수 있습니다." if self.frames else reason, parent=self.root)
                    self.set_buttons()
                elif kind == "saved":
                    self.state, self.dirty = "idle", False
                    self.status.set(f"저장 완료 · {Path(data).name} · {Path(data).stat().st_size / 1024:.1f} KB")
                    self.set_buttons()
                    self.show_saved_message(data)
                elif kind == "save_error":
                    self.state = "idle"
                    self.status.set("저장 실패 · 녹화는 유지됩니다. 다른 위치로 다시 저장해 주세요.")
                    self.set_buttons()
                    messagebox.showerror("저장 실패", data, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def preview(self):
        if self.state != "idle" or not self.frames:
            return
        if self.preview_window and self.preview_window.window.winfo_exists():
            self.preview_window.window.lift()
            return
        self.preview_window = Preview(self.root, self.frames, self.durations)

    def save(self):
        if self.state != "idle" or not self.frames:
            return
        self.dialog_open = True
        try:
            path = filedialog.asksaveasfilename(parent=self.root, title="움직이는 WebP 저장",
                        defaultextension=".webp", filetypes=[("WebP 이미지", "*.webp")],
                        initialfile=time.strftime("webpCam_%Y%m%d_%H%M%S.webp"))
        finally:
            self.dialog_open = False
            self.hotkey.pending = False
        if not path:
            return
        self.state = "saving"
        self.set_buttons()
        self.status.set("WebP를 압축하고 있습니다… 큰 녹화는 시간이 걸릴 수 있습니다.")
        frames, durations = self.frames, self.durations
        quality, lossless = int(self.quality.get()), self.lossless.get()
        def worker():
            try:
                export_webp(path, frames, durations, quality, lossless)
                self.events.put(("saved", path))
            except Exception as exc:
                self.events.put(("save_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def close(self):
        if self.state == "saving":
            messagebox.showinfo("저장 중", "저장이 끝난 뒤 창을 닫아 주세요.", parent=self.root)
        elif self.state == "recording":
            self.stop_recording()
        elif self.discard_ok():
            self.hide_saved_message()
            self.hotkey.close()
            self.aspect_resize.close()
            self.root.destroy()


def main():
    if sys.platform != "win32":
        print("webpCam GUI는 Windows 10/11용입니다.")
        return 1
    # Enable physical-pixel coordinates before creating any Tk window.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("webpCam.Desktop")
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    if not features.check("webp"):
        root.withdraw()
        messagebox.showerror("WebP 지원 없음", "WebP를 지원하는 Pillow를 설치해 주세요.")
        root.destroy()
        return 1
    WebPCam(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
