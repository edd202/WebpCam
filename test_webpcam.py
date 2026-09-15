import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from PIL import Image
from webpcam import capture_region, export_webp, frame_durations, ratio_size, constrain_rect, AspectResize, WebPCam


class WebPCamTests(unittest.TestCase):
    def test_exact_capture_ratios_with_minimum_and_rounding(self):
        for ratio in ((4, 3), (16, 9), (16, 10)):
            for width, height in ((1, 1), (331, 271), (1921, 1081)):
                for drive in ("width", "height"):
                    w, h = ratio_size(width, height, ratio, (148, 202), drive)
                    self.assertEqual(w * ratio[1], h * ratio[0])
                    self.assertGreaterEqual(w, 148)
                    self.assertGreaterEqual(h, 202)

    def test_all_drag_edges_exclude_chrome_and_keep_opposite_anchor(self):
        rect = (-800, -100, -150, 450)
        chrome = (108, 47)
        for ratio in ((4, 3), (16, 9), (16, 10)):
            for edge in range(1, 9):
                left, top, right, bottom = constrain_rect(rect, edge, chrome, ratio, (148, 202))
                self.assertEqual((right - left - chrome[0]) * ratio[1],
                                 (bottom - top - chrome[1]) * ratio[0])
                if edge in (1, 4, 7):
                    self.assertEqual(right, rect[2])
                else:
                    self.assertEqual(left, rect[0])
                if edge in (3, 4, 5):
                    self.assertEqual(bottom, rect[3])
                else:
                    self.assertEqual(top, rect[1])

    def test_native_chrome_ignores_stale_tk_child_dimensions(self):
        resize = AspectResize.__new__(AspectResize)
        resize.old_proc, resize.hwnd = 123, 456
        resize.client_insets = (92, 8)
        resize.api = Mock()
        resize.area = Mock()
        resize.area.winfo_width.side_effect = AssertionError("Stale Tk measurement")
        for width, height in ((784, 213), (1136, 782), (784, 213)):
            def outer(hwnd, ptr):
                r = ptr._obj
                r.left, r.top, r.right, r.bottom = 10, 20, 10 + width + 16, 20 + height + 39
                return True
            def client(hwnd, ptr):
                r = ptr._obj
                r.left, r.top, r.right, r.bottom = 0, 0, width, height
                return True
            resize.api.GetWindowRect.side_effect = outer
            resize.api.GetClientRect.side_effect = client
            resize.refresh()
            self.assertEqual(resize.chrome, (108, 47))

    def test_move_only_never_requests_ratio_correction(self):
        resize = AspectResize.__new__(AspectResize)
        resize.old_proc, resize.ratio = 123, (16, 9)
        resize.refresh, resize.api = Mock(), Mock()
        resize.settle_pending = False
        for _ in range(50):
            resize.window_proc(456, 0x0231, 0, 0)
            resize.window_proc(456, 0x0216, 0, 0)  # WM_MOVING
            resize.window_proc(456, 0x0232, 0, 0)
            self.assertFalse(resize.settle_pending)

    def test_reported_692_by_205_corrects_once_without_growth(self):
        app = WebPCam.__new__(WebPCam)
        app.state, app.root, app.area_changed = "idle", Mock(), Mock()
        app.aspect_resize = Mock(ratio=(16, 9), client_insets=(92, 8), minimum=(148, 202))
        dimensions = [784, 213]
        app.root.winfo_width.side_effect = lambda: dimensions[0]
        app.root.winfo_height.side_effect = lambda: dimensions[1]
        def geometry(value):
            dimensions[:] = map(int, value.split("x"))
        app.root.geometry.side_effect = geometry
        for _ in range(50):
            app.enforce_aspect()
        self.assertEqual(dimensions, [780, 395])  # capture = 688 x 387, exact 16:9
        self.assertEqual(app.root.geometry.call_count, 1)

    def test_timing_without_rounding_drift(self):
        stamps = [10 + i / 30 for i in range(300)]
        durations = frame_durations(stamps, 20)
        self.assertEqual(sum(durations), 10000)
        self.assertTrue(all(d in (33, 34) for d in durations))

    def test_webp_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "테스트.webp"
            colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
            frames = [Image.new("RGB", (64, 48), c) for c in colors]
            export_webp(target, frames, [40, 90, 120], lossless=True)
            with Image.open(target) as decoded:
                self.assertEqual(decoded.format, "WEBP")
                self.assertEqual(decoded.n_frames, 3)
                self.assertEqual(decoded.info["loop"], 0)
                self.assertEqual(decoded.size, (64, 48))
                durations = []
                for i, color in enumerate(colors):
                    decoded.seek(i)
                    decoded.load()
                    self.assertEqual(decoded.convert("RGB").getpixel((10, 10)), color)
                    durations.append(decoded.info["duration"])
                self.assertEqual(durations, [40, 90, 120])

    def test_lossy_and_single_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            for count in (1, 2):
                target = Path(directory) / f"{count}.webp"
                frames = [Image.new("RGB", (32, 32), (i * 200, 80, 40)) for i in range(count)]
                export_webp(target, frames, [100] * count, quality=70)
                with Image.open(target) as decoded:
                    self.assertEqual(decoded.n_frames, count)
                    decoded.load()

    def test_failure_preserves_existing_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.webp"
            target.write_bytes(b"existing contents")
            with patch.object(Image.Image, "save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    export_webp(target, [Image.new("RGB", (10, 10))], [100])
            self.assertEqual(target.read_bytes(), b"existing contents")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_capture_memory_cap_and_scale(self):
        events = queue.Queue()
        capture_region((0, 0, 20, 20), 1000, .5, threading.Event(), events,
                       grab=lambda: Image.new("RGB", (20, 20), "blue"),
                       memory_limit=800, seconds_limit=5)
        result = None
        while not events.empty():
            kind, data = events.get_nowait()
            if kind == "captured":
                result = data
        frames, durations, reason = result
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].size, (10, 10))
        self.assertEqual(len(durations), 2)
        self.assertIn("메모리", reason)

    def test_capture_error_keeps_first_frame(self):
        calls = 0
        def grab():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("display disconnected")
            return Image.new("RGB", (10, 10))
        events = queue.Queue()
        capture_region((0, 0, 10, 10), 1000, 1, threading.Event(), events, grab=grab)
        while not events.empty():
            kind, data = events.get_nowait()
            if kind == "captured":
                self.assertEqual(len(data[0]), 1)
                self.assertIn("display disconnected", data[2])

    def test_stop_before_start(self):
        events, stop = queue.Queue(), threading.Event()
        stop.set()
        capture_region((0, 0, 10, 10), 15, 1, stop, events,
                       grab=lambda: self.fail("Should not capture after stop"))
        kind, (frames, durations, reason) = events.get_nowait()
        self.assertEqual(kind, "captured")
        self.assertEqual(frames, [])
        self.assertEqual(durations, [])


if __name__ == "__main__":
    unittest.main()
