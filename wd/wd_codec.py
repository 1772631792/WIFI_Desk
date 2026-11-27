"""H.264/H.265 编解码：按实测吞吐选择编码器，同画质下 H.265 省约 40% 带宽。"""
from dataclasses import dataclass
from fractions import Fraction
import threading
import time

import av
import numpy as np
from av.codec import CodecContext

try:
    import cv2
except Exception:  # pragma: no cover - 缺少 OpenCV 时退回 swscale
    cv2 = None

CANDIDATES = {
    "h264": (
        ("h264_nvenc", True, {"preset": "p1", "tune": "ull", "zerolatency": "1", "delay": "0"}),
        ("h264_qsv", True, {"preset": "veryfast", "async_depth": "1", "low_delay_brc": "1"}),
        ("h264_amf", True, {"usage": "ultralowlatency", "quality": "speed"}),
        ("libx264", False, {"preset": "veryfast", "tune": "zerolatency"}),
    ),
    "hevc": (
        ("hevc_qsv", True, {"preset": "veryfast", "async_depth": "1", "low_delay_brc": "1"}),
        ("hevc_mf", True, {}),
        ("libx265", False, {"preset": "superfast", "tune": "zerolatency"}),
    ),
}

# 这些编码器只吃 nv12；libx264/libx265 可以直接吃 yuv420p，省掉 UV 交织。
_NV12_CODECS = {"h264_qsv", "h264_mf", "h264_nvenc", "h264_amf",
                "hevc_qsv", "hevc_mf", "hevc_nvenc", "hevc_amf"}

# CRF 在不同编码器里刻度不同：x265 通常比 x264 高 4 档，主观画质才接近。
_CRF_OFFSET = {"libx264": 0, "libx265": 4, "libsvtav1": 12}

@dataclass
class EncodedFrame:
    data: bytes
    keyframe: bool
    pts: int


def family_of(name: str) -> str:
    return "hevc" if name.startswith("hevc") or name == "libx265" else "h264"


def _open_encoder(name: str, options: dict, width: int, height: int,
                  fps: int, bitrate: int, gop: int, crf: int = 0):
    ctx = CodecContext.create(name, "w")
    ctx.width = int(width)
    ctx.height = int(height)
    ctx.pix_fmt = "nv12" if name in _NV12_CODECS else "yuv420p"
    ctx.time_base = Fraction(1, max(1, int(fps)))
    ctx.framerate = Fraction(max(1, int(fps)), 1)
    ctx.bit_rate = int(bitrate)
    ctx.gop_size = max(1, int(gop))
    ctx.max_b_frames = 0
    if options:
        opts = dict(options)
        if crf:
            # 恒定质量 + 1 秒 VBV 上限：画面一动立刻给足码率，同时不突破链路预算。
            # PyAV 没有暴露 rc_max_rate，只能走编码器自己的参数串（单位 kbps）。
            kbps = max(500, int(bitrate // 1000))
            value = int(crf) + _CRF_OFFSET.get(name, 0)
            if name == "libx264":
                opts["crf"] = str(value)
                opts["x264-params"] = f"vbv-maxrate={kbps}:vbv-bufsize={kbps}:vbv-init=0"
            elif name == "libx265":
                opts["crf"] = str(value)
                opts["x265-params"] = f"vbv-maxrate={kbps}:vbv-bufsize={kbps}:log-level=error"
        ctx.options = opts
    return ctx


def _to_frame(rgb: np.ndarray, width: int, height: int, pix_fmt: str):
    """RGB 画面 → 编码器输入帧。

    1080p 下 OpenCV 的 SIMD 转换约 2ms，swscale 要 12ms；缩放也交给 OpenCV
    （INTER_AREA），比 swscale 缩放再转换快得多。缺少 OpenCV 时退回 swscale。
    """
    scaled = width and height and (width != rgb.shape[1] or height != rgb.shape[0])
    if cv2 is not None:
        source = rgb
        if scaled:
            source = cv2.resize(rgb, (int(width), int(height)), interpolation=cv2.INTER_AREA)
        source = np.ascontiguousarray(source)
        if pix_fmt == "yuv420p":
            return av.VideoFrame.from_ndarray(
                cv2.cvtColor(source, cv2.COLOR_RGB2YUV_I420), format="yuv420p")
        height_px, width_px = source.shape[0], source.shape[1]
        if pix_fmt == "nv12" and height_px % 2 == 0 and width_px % 2 == 0:
            yuv = cv2.cvtColor(source, cv2.COLOR_RGB2YUV_I420)
            buf = np.empty((height_px * 3 // 2, width_px), dtype=np.uint8)
            buf[:height_px] = yuv[:height_px]
            uv = buf[height_px:].reshape(height_px // 2, width_px // 2, 2)
            chroma = yuv[height_px:].reshape(2, height_px // 2, width_px // 2)
            uv[..., 0] = chroma[0]
            uv[..., 1] = chroma[1]
            return av.VideoFrame.from_ndarray(buf, format="nv12")
        if not source.flags.writeable:
            source = source.copy()
        frame = av.VideoFrame.from_ndarray(source, format="rgb24")
        if scaled:
            return frame.reformat(width=int(width), height=int(height), format=pix_fmt)
        return frame.reformat(format=pix_fmt)
    frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
    if scaled:
        return frame.reformat(width=int(width), height=int(height), format=pix_fmt)
    return frame.reformat(format=pix_fmt)


def _benchmark(name: str, options: dict, width: int, height: int,
               fps: int, bitrate: int, crf: int = 0, sample=None) -> float:
    ctx = _open_encoder(name, options, width, height, fps, bitrate, max(1, fps), crf)
    if sample is not None and sample.shape[0] and sample.shape[1]:
        frame = sample
        if (frame.shape[1], frame.shape[0]) != (width, height):
            frame = cv2.resize(frame, (int(width), int(height)),
                               interpolation=cv2.INTER_AREA) if cv2 is not None else frame
    else:
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[:: max(16, height // 12), :, :] = 220
    warmup = _to_frame(frame, width, height, ctx.pix_fmt)
    warmup.pts = 0
    _ = ctx.encode(warmup)      # 预热：硬件编码器首帧要建会话，不能算进吞吐
    started = time.perf_counter()
    for index in range(3):
        video = _to_frame(frame, width, height, ctx.pix_fmt)
        video.pts = index + 1
        _ = ctx.encode(video)
    _ = ctx.encode(None)
    return (time.perf_counter() - started) / 3.0


def select_encoder(width: int, height: int, fps: int, bitrate: int,
                   crf: int = 0, family: str = "auto", sample=None):
    """实测选编码器：能跟上帧率的前提下优先 H.265（同画质更省带宽）。

    sample 传入真实抓屏帧时按内容实测；否则用空白图，容易低估复杂画面的耗时。
    """
    family = family if family in CANDIDATES else "auto"
    families = ("hevc", "h264") if family == "auto" else (family,)
    results = []
    for fam in families:
        for name, hardware, options in CANDIDATES.get(fam, ()):
            try:
                elapsed = _benchmark(name, options, width, height, fps, bitrate,
                                     crf, sample)
                results.append((elapsed, name, fam, hardware, options))
            except Exception:
                continue
    if not results:
        raise RuntimeError("没有可用的视频编码器")
    budget = (1000.0 / max(1, int(fps))) * 0.55      # 给采集/发送留出余量
    eligible = [item for item in results if item[0] * 1000.0 <= budget]
    if eligible:
        # 排序依据（实测）：H.265 比 H.264 省 30%~50% 码率；
        # 同族里软件编码器用 CRF，码率控制比硬件 ABR 准，也更省带宽。
        pool = sorted(eligible, key=lambda item: (
            0 if item[2] == "hevc" else 1,
            0 if not item[3] else 1,
            item[0],
        ))
        choice = pool[0]
    else:
        # 都跟不上帧率时只能选最快的，让自适应降分辨率去补。
        choice = min(results, key=lambda item: item[0])
    selected = (choice[1], choice[3], dict(choice[4]), choice[2])
    return selected


class H264Encoder:
    def __init__(self, width: int, height: int, fps: int = 30, bitrate: int = 8_000_000,
                 codec: str = "", crf: int = 0, family: str = "auto", sample=None):
        self.width = int(width)
        self.height = int(height)
        self.fps = max(1, int(fps))
        self.bitrate = max(500_000, int(bitrate))
        self.crf = max(0, int(crf))
        if codec:
            options = next((dict(opt) for items in CANDIDATES.values()
                            for name, _hw, opt in items if name == codec), {})
            hardware = any(name == codec and is_hw for items in CANDIDATES.values()
                           for name, is_hw, _opt in items)
            self.name, self.hardware, self.options, self.family = (
                codec, hardware, options, family_of(codec))
        else:
            self.name, self.hardware, self.options, self.family = select_encoder(
                self.width, self.height, self.fps, self.bitrate, self.crf, family, sample)
        self.ctx = _open_encoder(self.name, self.options, self.width, self.height,
                                 self.fps, self.bitrate, max(1, self.fps), self.crf)
        self.pix_fmt = self.ctx.pix_fmt
        self.pts = 0
        self.force_keyframe = True
        self.lock = threading.Lock()

    def request_keyframe(self):
        self.force_keyframe = True

    def set_bitrate(self, bitrate: int):
        self.set_quality(self.crf, bitrate)

    def set_quality(self, crf: int, bitrate: int):
        """CRF 只能在开编码器时设定，变化超过 2 档就重建（重建后首帧为关键帧）。"""
        crf = max(10, min(40, int(crf)))
        bitrate = max(500_000, min(120_000_000, int(bitrate)))
        with self.lock:
            self.bitrate = bitrate
            if abs(crf - self.crf) < 2:
                if self.ctx is not None:
                    try:
                        self.ctx.bit_rate = bitrate
                    except Exception:
                        pass
                return
            self.crf = crf
            if self.name not in ("libx264", "libx265") or self.ctx is None:
                return
            try:
                self.ctx = _open_encoder(self.name, self.options, self.width, self.height,
                                         self.fps, bitrate, max(1, self.fps), crf)
                self.pix_fmt = self.ctx.pix_fmt
                self.force_keyframe = True
            except Exception:
                pass

    def encode(self, rgb: np.ndarray) -> EncodedFrame:
        with self.lock:
            video = _to_frame(rgb, self.width, self.height, self.pix_fmt)
            if self.force_keyframe:
                video.pict_type = 1
                self.force_keyframe = False
            video.pts = self.pts
            self.pts += 1
            packets = self.ctx.encode(video)
            if not packets:
                return None
            data = b"".join(bytes(packet) for packet in packets)
            return EncodedFrame(data=data,
                                keyframe=any(packet.is_keyframe for packet in packets),
                                pts=self.pts - 1)

    def close(self):
        with self.lock:
            self.ctx = None


def _frame_to_rgb(frame):
    """解码帧 → RGB。OpenCV 的 YUV 转换比 swscale 快约 2 倍。"""
    if cv2 is not None and frame.format.name == "yuv420p":
        return cv2.cvtColor(frame.to_ndarray(format="yuv420p"), cv2.COLOR_YUV2RGB_I420)
    return frame.to_ndarray(format="rgb24")


class H264Decoder:
    def __init__(self, codec: str = "h264"):
        self.codec = codec if codec in ("h264", "hevc") else "h264"
        self.ctx = CodecContext.create(self.codec, "r")
        self.lock = threading.Lock()

    def _reset_locked(self):
        try:
            self.ctx.flush_buffers()
        except Exception:
            pass
        self.ctx = CodecContext.create(self.codec, "r")

    def reset(self):
        with self.lock:
            self._reset_locked()

    def decode(self, data: bytes):
        with self.lock:
            try:
                frames = self.ctx.decode(av.Packet(data))
            except Exception:
                self._reset_locked()
                return []
            return [_frame_to_rgb(frame) for frame in frames]

    def close(self):
        with self.lock:
            self.ctx = None


def probe(width: int = 640, height: int = 360, fps: int = 30, sample=None) -> str:
    name, hardware, _options, family = select_encoder(width, height, fps, 2_000_000, 20,
                                                      sample=sample)
    return f"{family.upper()} {name}{'(硬件)' if hardware else '(软件)'}"
