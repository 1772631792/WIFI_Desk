"""屏幕采集与分块差异检测。

优先使用 DXGI Desktop Duplication（dxcam），它能正确采集浏览器、Electron、
视频播放器等硬件加速窗口；不可用时回退 PIL/GDI。分块比较仍用 NumPy，
不逐像素 Python 循环。
"""
import time
import zlib

import cv2
import numpy as np
from PIL import ImageGrab

from .wd_cursor import draw as _draw_cursor

# 参考 RustDesk 的预设分层：清晰档优先保文字边缘，低延迟档优先保帧率。
# dynamic_ratio 是切换到整帧 JPEG 的阈值，越高越偏向无损瓦片。
# crf 为 H.264 恒定质量：ABR 在画面突然变化时会先糊一段，CRF 立刻按内容给足码率。
PRESETS = {
    "auto": {"tile": 96, "fps": 30, "quality": 93, "dynamic_ratio": 0.55,
             "full_chroma": True, "zlib_level": 2, "bitrate": 30_000_000, "crf": 19},
    "office": {"tile": 64, "fps": 30, "quality": 96, "dynamic_ratio": 0.80,
               "full_chroma": True, "zlib_level": 3, "bitrate": 20_000_000, "crf": 19},
    "sharp": {"tile": 48, "fps": 30, "quality": 98, "dynamic_ratio": 0.92,
              "full_chroma": True, "zlib_level": 2, "bitrate": 60_000_000, "crf": 16},
    "latency": {"tile": 160, "fps": 60, "quality": 82, "dynamic_ratio": 0.35,
                "full_chroma": False, "zlib_level": 1, "bitrate": 40_000_000, "crf": 22},
    "power": {"tile": 192, "fps": 15, "quality": 75, "dynamic_ratio": 0.55,
              "full_chroma": False, "zlib_level": 1, "bitrate": 6_000_000, "crf": 26},
}


class AdaptiveQuality:
    """参考 RustDesk video_qos 的迟滞状态机：先降帧率，再降压缩质量。

    只在指标持续偏离时调整，且恢复速度慢于降级速度，避免画质来回跳动。
    """

    def __init__(self, preset: str = "auto"):
        self.preset = preset if preset in PRESETS else "auto"
        self.base = dict(PRESETS[self.preset])
        self.params = dict(self.base)
        self.level = 1.0
        self.rtt_ema = 0.0
        self.last_change = time.monotonic()
        self._set_floors()

    def _set_floors(self):
        if self.preset in ("office", "sharp"):
            self.floor_quality = 85
        elif self.preset in ("latency", "power"):
            self.floor_quality = 55
        else:
            self.floor_quality = 75
        # 远程桌面宁可掉画质也别掉帧：帧率下限保持在流畅区间。
        self.floor_fps = 15 if self.preset == "power" else 24

    def set_preset(self, preset: str):
        self.preset = preset if preset in PRESETS else "auto"
        self.base = dict(PRESETS[self.preset])
        self.params = dict(self.base)
        self.level = 1.0
        self.rtt_ema = 0.0
        self.last_change = time.monotonic()
        self._set_floors()

    def observe(self, rtt_ms: float = 0.0, pending: int = 0, dropped: int = 0,
                loss=None) -> bool:
        rtt = max(0.0, float(rtt_ms or 0.0))
        self.rtt_ema = 0.85 * self.rtt_ema + 0.15 * rtt
        now = time.monotonic()
        if now - self.last_change < 2.0:
            return False
        if loss is None:
            bad = self.rtt_ema > 70 or pending >= 2 or dropped > 0
            good = self.rtt_ema < 25 and pending == 0 and dropped == 0
        else:
            # 媒体通道按丢失率判定；WiFi 上偶发丢一两帧不该把整条通道降级。
            bad = self.rtt_ema > 120 or pending >= 2 or (dropped >= 2 and loss > 0.05)
            good = self.rtt_ema < 55 and pending == 0 and loss < 0.01
        if bad:
            target = max(0.55, self.level - 0.15)
        elif good:
            target = min(1.0, self.level + 0.05)
        else:
            return False
        if abs(target - self.level) < 0.01:
            return False
        self.level = target
        self._apply()
        self.last_change = now
        return True

    def _apply(self):
        base = self.base
        fps = int(round(base["fps"] * (0.7 + 0.3 * self.level)))
        fps = max(self.floor_fps, min(base["fps"], fps))
        quality = int(round(base["quality"] - (1.0 - self.level) * 22))
        quality = max(self.floor_quality, min(base["quality"], quality))
        self.params.update(base)
        self.params["fps"] = fps
        self.params["quality"] = quality
        self.params["dynamic_ratio"] = min(0.95, base["dynamic_ratio"] + (1.0 - self.level) * 0.25)
        self.params["zlib_level"] = 1 if self.level < 0.8 else base["zlib_level"]
        self.params["bitrate"] = max(1_500_000, int(base["bitrate"] * (0.55 + 0.45 * self.level)))
        # 链路变差时放宽 CRF（牺牲细节换码率），而不是一刀切降分辨率。
        self.params["crf"] = int(round(min(34, base.get("crf", 20) + (1.0 - self.level) * 10)))


class ScreenCapture:
    def __init__(self, preset: str = "auto"):
        self.params = dict(PRESETS.get(preset, PRESETS["auto"]))
        self.prev = None
        self.black = False
        self.backend = "PIL"
        self.backend_error = ""
        self._camera = None
        self._camera_retry_at = 0.0
        self._last = None
        self._closed = False
        self._first = True

    def set_preset(self, preset: str):
        self.params = dict(PRESETS.get(preset, PRESETS["auto"]))
        self.prev = None
        self.black = False

    def frame_size(self):
        """不启动 DXGI 的尺寸探测，供握手阶段使用。"""
        try:
            image = ImageGrab.grab()
            return image.height, image.width
        except Exception:
            return 1080, 1920

    def _ensure_camera(self):
        if self._camera is not None or self._closed:
            return self._camera
        if time.monotonic() < self._camera_retry_at:
            return None
        try:
            import dxcam
            self._camera = dxcam.create(output_color="RGB")
            self.backend = "DXGI"
            self.backend_error = ""
        except Exception as exc:
            self.backend = "PIL"
            self.backend_error = f"{type(exc).__name__}: {exc}"
            self._camera_retry_at = time.monotonic() + 10.0
        return self._camera

    def _drop_camera(self, reason: str):
        self.backend = "PIL"
        self.backend_error = reason
        camera, self._camera = self._camera, None
        if camera is not None:
            try:
                camera.release()
            except Exception:
                pass
        self._last = None
        self._first = True
        self._camera_retry_at = time.monotonic() + 10.0

    def grab(self) -> np.ndarray:
        """取一帧；桌面没有变化时返回 None，调用方据此跳过编码与发送。"""
        camera = self._ensure_camera()
        if camera is not None:
            try:
                frame = camera.grab(new_frame_only=not self._first)
            except Exception as exc:
                self._drop_camera(f"{type(exc).__name__}: {exc}")
                frame = None
            if frame is not None:
                cur = np.asarray(frame)
                if cur.ndim == 3 and cur.shape[2] == 3:
                    self._first = False
                    self._last = cur
                    _draw_cursor(cur)
                    self.black = self._is_black(cur)
                    return cur
            if not self._first:
                return None
        if self._last is not None:
            # DXGI 在画面静止时可能没有新帧，复用上一帧交给差异检测即可。
            return self._last
        cur = np.asarray(ImageGrab.grab())
        self._first = False
        self._last = cur
        _draw_cursor(cur)
        self.black = self._is_black(cur)
        return cur

    @staticmethod
    def _is_black(cur: np.ndarray) -> bool:
        # 锁屏/切换用户/安全桌面时可能抓到全黑，先标记避免把黑帧发出去。
        return bool(cur.size and cur[::16, ::16].mean() < 2.0)

    def close(self):
        # dxcam.create 是进程内单例；重连时旧会话若 release，会让新会话
        # 拿到已失效的相机。这里只断开引用，让下一次连接复用同一个相机。
        self._closed = True
        self._camera = None
        self._last = None
        self._first = True

    def diff(self, cur: np.ndarray):
        h, w = cur.shape[:2]
        tile = self.params["tile"]
        if self.prev is None or self.prev.shape != cur.shape:
            return self.snapshot(cur), 1.0
        count = 0
        total = 0
        changed = []
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                tw, th = min(tile, w - x), min(tile, h - y)
                total += 1
                if not np.array_equal(self.prev[y:y + th, x:x + tw], cur[y:y + th, x:x + tw]):
                    changed.append((x, y, tw, th))
                    count += 1
        self.prev = cur.copy()
        return changed, (count / total if total else 0.0)

    def snapshot(self, cur: np.ndarray):
        h, w = cur.shape[:2]
        tile = self.params["tile"]
        tiles = [(x, y, min(tile, w - x), min(tile, h - y))
                 for y in range(0, h, tile) for x in range(0, w, tile)]
        self.prev = cur.copy()
        return tiles

    def pack_tiles(self, cur: np.ndarray, tiles, level: int = None):
        parts = []
        meta = []
        if level is None:
            level = int(self.params.get("zlib_level", 1))
        for x, y, tw, th in tiles:
            raw = cur[y:y + th, x:x + tw].tobytes()
            comp = zlib.compress(raw, level)
            meta.append([x, y, tw, th, len(comp), len(raw)])
            parts.append(comp)
        return meta, b"".join(parts)

    @staticmethod
    def encode_jpeg(cur: np.ndarray, quality: int, full_chroma: bool = False) -> bytes:
        bgr = cv2.cvtColor(cur, cv2.COLOR_RGB2BGR)
        options = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
        if full_chroma:
            options.extend([int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR), int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444)])
        ok, buf = cv2.imencode(".jpg", bgr, options)
        if not ok:
            raise RuntimeError("JPEG 编码失败")
        return buf.tobytes()
