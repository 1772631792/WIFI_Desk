"""被控端监听与主控端会话：认证、混合传输、输入与遥测。"""
import json
import os
import secrets
import socket
import threading
import time
import zlib

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QImage, QPainter

from . import wd_clipboard, wd_config, wd_crypto, wd_input
from .wd_protocol import MessageSender, recv_msg, send_msg

AUTH_WAIT_SECONDS = 60
HEARTBEAT_INTERVAL = 1.0
RESUME_TTL = 120.0
TILES_PER_MESSAGE = 128  # 留在现有协议的 8 KiB 消息头限制内
MEDIA_GRACE = 5.0        # 首帧送达前允许的协商时间
MEDIA_ACK_TIMEOUT = 3.0  # 超过该时长没有媒体确认就回退图块
FILE_CHUNK_SIZE = 128 * 1024
FILE_WINDOW = 2 * 1024 * 1024    # 发送队列最多积压 2MB，避免大文件把内存顶爆
FILE_MAX_BYTES = 8 * 1024 ** 3
_FINE_TIMER = False


def enable_fine_timer() -> bool:
    """Windows 默认 15.6ms 定时器粒度会让 30/60 FPS 的睡眠节拍失真。"""
    global _FINE_TIMER
    if _FINE_TIMER or os.name != "nt":
        return _FINE_TIMER
    try:
        import ctypes
        _FINE_TIMER = ctypes.windll.winmm.timeBeginPeriod(1) == 0
    except Exception:
        _FINE_TIMER = False
    return _FINE_TIMER


def _capture():
    """按需加载采集模块，避免启动时加载 cv2/numpy/PIL。"""
    from . import wd_capture
    return wd_capture


def _tune_tcp(sock):
    """关掉 Nagle：键鼠是高频小包，攒包会让远端操作明显发涩。"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


class ServerSession(QObject):
    auth_required = Signal(dict)
    session_started = Signal(dict)
    session_ended = Signal(str)
    stats = Signal(dict)
    log = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.running = False
        self.listener = None
        self.active = None
        self.pending = {}
        self.lock = threading.Lock()
        self.resume_tokens = {}

    def start(self):
        if self.running:
            return
        enable_fine_timer()
        self.running = True
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", int(self.config.get("service_port", 47382))))
            sock.listen(4)
        except OSError as exc:
            self.running = False
            sock.close()
            self.log.emit(f"远控监听端口不可用：{exc}")
            return
        sock.settimeout(0.4)
        self.listener = sock
        threading.Thread(target=self._accept_loop, daemon=True, name="wd-accept").start()
        self.log.emit("被控监听已启动")

    def stop(self):
        self.running = False
        if self.listener:
            try:
                self.listener.close()
            except OSError:
                pass
        if self.active:
            self.active.stop()
        self.listener = None

    def _accept_loop(self):
        while self.running and self.listener:
            try:
                conn, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.active and self.active.alive:
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            handler = ClientHandler(self, conn, addr)
            with self.lock:
                self.active = handler
            threading.Thread(target=handler.run, daemon=True, name="wd-client-handler").start()

    def resolve_auth(self, token: str, approve: bool, auto: bool = False):
        pending = self.pending.get(token)
        if pending:
            pending["approved"] = bool(approve)
            pending["auto"] = bool(auto)
            pending["event"].set()

    def issue_resume(self, device_id: str, fingerprint: str) -> str:
        """签发短时重连凭据，避免 Wi-Fi 抖动后每次都要人工授权。"""
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self.lock:
            for key, entry in list(self.resume_tokens.items()):
                if entry["expires"] <= now:
                    self.resume_tokens.pop(key, None)
            self.resume_tokens[token] = {
                "id": device_id, "fp": fingerprint, "expires": now + RESUME_TTL,
            }
        return token

    def check_resume(self, token: str, device_id: str, fingerprint: str) -> bool:
        if not token:
            return False
        with self.lock:
            entry = self.resume_tokens.get(token)
            if not entry:
                return False
            now = time.monotonic()
            if entry["expires"] <= now or entry["id"] != device_id or entry["fp"] != fingerprint:
                self.resume_tokens.pop(token, None)
                return False
            entry["expires"] = now + RESUME_TTL
            return True

    def revoke_resume(self, token: str):
        if token:
            with self.lock:
                self.resume_tokens.pop(token, None)

    def unregister(self, handler):
        with self.lock:
            if self.active is handler:
                self.active = None


class ClientHandler:
    def __init__(self, server: ServerSession, conn, addr):
        self.server = server
        self.raw_conn = conn
        self.addr = addr
        self.sock = None
        self.alive = True
        self.send_lock = threading.Lock()
        self.sender = None
        self.heartbeat_thread = None
        quality = server.config.get("quality", "auto")
        if quality not in _capture().PRESETS:
            quality = "auto"
        self.quality = quality
        self.capture = _capture().ScreenCapture(quality)
        self.qos = _capture().AdaptiveQuality(quality)
        self.capture.params = self.qos.params
        self.capture_lock = threading.Lock()
        self._black_notified = False
        self.resume_token = ""
        self.peer_rtt = 0.0
        self.epoch = 1
        self.seq = 0
        self.frames = 0
        self.bytes_frame = 0
        self.last_dropped = 0
        self.last_stat = time.monotonic()
        self.mode = "TILE"
        self._backend_logged = False
        self.media_tx = None
        self.media_ok = False
        self.media_epoch = 1
        self.media_started = 0.0
        self.media_acked = 0.0
        self.media_acked_once = False
        self.media_frame_id = 0
        self.encoder = None
        self.codec_name = ""
        self.force_codec = ""
        self.family = "h264"
        self._uploads = {}
        self.transfer_active = False
        self._last_tiles = 0
        self.media_lost = 0
        self.media_err = 0
        self.media_frames = 0
        self._media_bad_last = 0
        self._media_frames_last = 0
        # 编码吃力时优先降帧率而不是降分辨率：糊了比掉几帧更难受。
        self.fps_cap = 0        # 0 = 跟随预设，运行时按实测负载下调
        self.encode_scale = 1.0
        self.encode_fast = False
        self.enc_ema = 0.0
        self.grab_ema = 0.0
        self._slow_streak = 0
        self._fast_streak = 0

    def stop(self):
        self.alive = False
        if self.sender:
            self.sender.stop()
        self._release_media()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _on_send_error(self, exc):
        self.server.log.emit(f"被控发送失败：{type(exc).__name__}: {exc}")
        self.alive = False

    def _send(self, typ, payload=b"", video=False, priority=False, **meta):
        if not self.alive or not self.sock:
            return False
        if self.sender:
            return self.sender.send(typ, payload, video=video, priority=priority, **meta)
        with self.send_lock:
            try:
                send_msg(self.sock, typ, payload, **meta)
                return True
            except OSError as exc:
                self._on_send_error(exc)
                return False

    def _heartbeat_loop(self):
        while self.alive:
            time.sleep(HEARTBEAT_INTERVAL)
            if self.alive:
                self._send("PING", t=time.monotonic(), priority=True)

    def _apply_preset(self, preset: str) -> bool:
        if preset not in _capture().PRESETS:
            return False
        self.quality = preset
        self.capture.set_preset(preset)
        self.qos.set_preset(preset)
        self.capture.params = self.qos.params
        return True

    def _release_media(self):
        tx, self.media_tx = self.media_tx, None
        if tx:
            try:
                tx.close()
            except OSError:
                pass
        encoder, self.encoder = self.encoder, None
        if encoder:
            try:
                encoder.close()
            except Exception:
                pass
        self.media_ok = False
        self.codec_name = ""
        self.enc_ema = 0.0
        self._slow_streak = self._fast_streak = 0
        self._media_frames_last = self.media_frames
        self._media_bad_last = self.media_lost + self.media_err

    def _media_off(self, reason: str):
        was_on = self.media_ok or self.media_tx is not None
        self._release_media()
        if not was_on:
            return
        if self.alive:
            self._send("MEDIA_OFF", reason=reason, priority=True)
            self.server.log.emit(f"已回退图块通道：{reason}")

    def _media_alive(self) -> bool:
        if not self.media_ok or not self.media_tx:
            return False
        now = time.monotonic()
        if not self.media_acked_once:
            return now - self.media_started <= MEDIA_GRACE
        if now - self.media_acked > MEDIA_ACK_TIMEOUT:
            self._media_off("视频通道超时无确认")
            return False
        return True

    def _start_media(self, peer_port: int, size) -> dict:
        """在 TLS 已认证的信道上分发一次性媒体密钥，之后视频走加密 UDP。"""
        if int(peer_port) <= 0:
            return {}
        try:
            from . import wd_codec, wd_media
            key = secrets.token_bytes(16)
            prefix = secrets.token_bytes(4)
            self.media_epoch = secrets.randbelow(0x7FFFFFFF) + 1
            tx = wd_media.MediaTx(self.addr[0], int(peer_port), key, prefix,
                                  self.media_epoch,
                                  bitrate=int(self.qos.params.get("bitrate", 8_000_000)))
            tx.start_control(self._on_key_request)
            # 编码器在握手阶段就建好：既能把编码格式告诉主控端，也省掉首帧的选型延迟。
            fps = max(1, int(self.qos.params["fps"]))
            bitrate = int(self.qos.params["bitrate"])
            crf = int(self.qos.params.get("crf", 20))
            ew = max(2, int(size[1]) // 2 * 2)
            eh = max(2, int(size[0]) // 2 * 2)
            family = str(self.server.config.get("codec", "auto") or "auto").lower()
            sample = None
            try:
                sample = self.capture.grab()      # 用真实画面做选型基准，避免低估复杂内容
            except Exception:
                sample = None
            encoder = wd_codec.H264Encoder(ew, eh, fps, bitrate, crf=crf,
                                           family=family, sample=sample)
            self.encoder = encoder
            self.family = encoder.family
            self.codec_name = f"{encoder.name}{'(硬件)' if encoder.hardware else '(软件)'}"
            self.server.log.emit(f"视频编码：{self.codec_name} {ew}x{eh}@{fps}（{encoder.family}）")
        except Exception as exc:
            self.server.log.emit(f"视频通道启动失败：{type(exc).__name__}: {exc}")
            return {}
        self.media_tx = tx
        self.media_ok = True
        self.media_started = time.monotonic()
        self.media_acked = 0.0
        self.media_acked_once = False
        return {"port": tx.local_port, "key": key.hex(), "nonce": prefix.hex(),
                "epoch": self.media_epoch, "codec": self.family}

    def _on_key_request(self):
        encoder = self.encoder
        if encoder:
            encoder.request_keyframe()

    def _reply_clipboard(self):
        # 对端按下 Ctrl+C 后，被控端的应用程序写入剪贴板是异步的：分几次重读。
        text = ""
        for delay in (0.06, 0.08, 0.12, 0.2):
            time.sleep(delay)
            try:
                text = wd_clipboard.get_text()
            except Exception:
                text = ""
            if text:
                break
        if text:
            self._send("CLIP_TEXT", text.encode("utf-8"), priority=True)

    def _send_video(self, screen, cur) -> bool:
        with self.capture_lock:
            fps = max(1, int(self.qos.params["fps"]))
            bitrate = int(self.qos.params["bitrate"])
            crf = int(self.qos.params.get("crf", 20))
        # 偶数宽高：H.264 4:2:0 编码要求，缩放后也要满足。
        ew = max(2, int(cur.shape[1] * self.encode_scale) // 2 * 2)
        eh = max(2, int(cur.shape[0] * self.encode_scale) // 2 * 2)
        try:
            stale = (self.encoder is None
                     or self.encoder.width != ew
                     or self.encoder.height != eh
                     or abs(self.encoder.fps - fps) >= 5
                     or self.encoder.fast != self.encode_fast)
            if stale:
                old, self.encoder = self.encoder, None
                previous = self.family
                if old:
                    old.close()
                from . import wd_codec
                # 重建时沿用已选定的编码器，不再重新跑基准（那会卡住画面几百毫秒）。
                encoder = wd_codec.H264Encoder(
                    ew, eh, fps, bitrate, codec=self.force_codec or (old.name if old else ""),
                    crf=crf, family=self.family, fast=self.encode_fast)
                self.encoder = encoder
                self.family = encoder.family
                if encoder.family != previous:
                    # 中途换了编码格式，主控端要重建对应解码器。
                    self._send("MEDIA_CODEC", codec=encoder.family, priority=True)
                self.codec_name = (f"{encoder.name}"
                                   f"{'(硬件)' if encoder.hardware else '(软件)'}")
                self.server.log.emit(
                    f"视频编码：{self.codec_name} {ew}x{eh}@{self._effective_fps()}"
                    + ("" if self.encode_scale >= 1.0 else
                       f"（原画面 {cur.shape[1]}x{cur.shape[0]}，编码降分辨率）"))
            encoder = self.encoder
            if encoder is None:
                return True
            if abs(bitrate - encoder.bitrate) > max(300_000, encoder.bitrate * 0.12):
                encoder.set_quality(crf, bitrate)
            elif abs(crf - encoder.crf) >= 2:
                encoder.set_quality(crf, bitrate)
            started = time.perf_counter()
            encoded = encoder.encode(cur)
            spent = (time.perf_counter() - started) * 1000.0
            self.enc_ema = spent if self.enc_ema <= 0 else self.enc_ema * 0.8 + spent * 0.2
        except Exception as exc:
            encoder = self.encoder
            if encoder is not None and encoder.hardware and self.force_codec != "libx264":
                # 硬件编码在驱动/会话层面出错时自动退回软件编码，不必整条通道降级。
                self.force_codec = "libx264"
                self.encoder = None
                try:
                    encoder.close()
                except Exception:
                    pass
                self.server.log.emit(f"硬件编码异常，改用软件编码：{exc}")
                return True
            self._media_off(f"编码失败：{type(exc).__name__}: {exc}")
            return True
        if encoded is None:
            return True
        self.media_frame_id = (self.media_frame_id + 1) & 0xFFFFFFFF
        tx = self.media_tx
        if tx is None:
            return True
        try:
            tx.send(encoded.data, keyframe=encoded.keyframe,
                    frame_id=self.media_frame_id)
        except OSError as exc:
            self._media_off(f"视频发送失败：{exc}")
            return True
        encoder = self.encoder
        if encoder is not None:
            self.mode = f"H.{265 if encoder.family == 'hevc' else 264}·{encoder.name}"
        self.frames += 1
        self.bytes_frame += len(encoded.data)
        return True

    def _send_tiles(self, screen, cur, jpeg_active: bool):
        force_full = False
        with self.capture_lock:
            params = screen.params
            full_frame = screen.prev is None or screen.prev.shape != cur.shape
            tiles, ratio = screen.diff(cur)
            use_jpeg = not full_frame and ratio >= params["dynamic_ratio"]
            if jpeg_active and not use_jpeg and not full_frame:
                # 还原 JPEG 压过、但最近没有变化的像素。
                tiles = screen.snapshot(cur)
                force_full = True
        self._last_tiles = len(tiles)
        if not tiles:
            return True, jpeg_active
        messages = []
        if use_jpeg:
            jpeg = screen.encode_jpeg(cur, params["quality"], params["full_chroma"])
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            messages.append(("FRAME_JPEG", jpeg, {
                "w": cur.shape[1], "h": cur.shape[0], "epoch": self.epoch,
                "seq": self.seq, "mode": "JPEG兼容",
                "qos": round(self.qos.level, 2), "qf": int(self.qos.params["quality"]),
            }))
            self.mode = "JPEG兼容"
            size = len(jpeg)
        else:
            size = 0
            for start in range(0, len(tiles), TILES_PER_MESSAGE):
                meta, blob = screen.pack_tiles(cur, tiles[start:start + TILES_PER_MESSAGE])
                self.seq = (self.seq + 1) & 0xFFFFFFFF
                messages.append(("FRAME_TILES", blob, {
                    "tiles": meta, "w": cur.shape[1], "h": cur.shape[0],
                    "epoch": self.epoch, "seq": self.seq, "mode": "TILE无损",
                    "full": bool(full_frame or force_full),
                    "qos": round(self.qos.level, 2), "qf": int(self.qos.params["quality"]),
                }))
                size += len(blob)
            self.mode = "TILE无损"
        # 整帧作为一个批次入队：队里已有未发完的旧帧时直接丢弃旧帧。
        if not self.sender or not self.sender.send_batch(messages, video=True):
            return False, jpeg_active
        self.frames += 1
        self.bytes_frame += size
        return True, use_jpeg

    def _publish_stats(self, now: float):
        elapsed = now - self.last_stat
        if elapsed > 2.0:
            # 启动阶段要探测编码器、初始化采集，这一窗口的耗时/队列不能代表稳态。
            self.frames = 0
            self.bytes_frame = 0
            self.last_stat = now
            return
        dropped_total = self.sender.dropped_frames if self.sender else 0
        dropped_delta = max(0, dropped_total - self.last_dropped)
        self.last_dropped = dropped_total
        pending = self.sender.pending_frames() if self.sender else 0
        loss = None
        if self.media_ok:
            bad_total = self.media_lost + self.media_err
            lost_delta = max(0, bad_total - self._media_bad_last)
            self._media_bad_last = bad_total
            frame_delta = max(0, self.media_frames - self._media_frames_last)
            self._media_frames_last = self.media_frames
            seen = frame_delta + lost_delta
            # 媒体模式用丢失率而不是绝对丢包数：WiFi 偶发丢一帧不该把整条通道降级。
            loss = (lost_delta / seen) if seen else 0.0
            dropped_delta = lost_delta
            pending = 0
        with self.capture_lock:
            self.qos.observe(self.peer_rtt, pending=pending, dropped=dropped_delta, loss=loss)
            target = int(self.qos.params["bitrate"])
        if self.media_ok and self.enc_ema > 0:
            base_fps = max(15, int(self.qos.params.get("fps", 30)))
            budget = 1000.0 / max(1, self._effective_fps())
            load = self.enc_ema + self.grab_ema
            if load > budget * 0.9:
                self._slow_streak += 1
                self._fast_streak = 0
            elif load < budget * 0.45:
                self._fast_streak += 1
                self._slow_streak = 0
            else:
                self._slow_streak = self._fast_streak = 0
            if self._slow_streak >= 2:
                if self.family == "hevc" and not self.force_codec:
                    # 先退回更快的 H.264 保住分辨率；不行再考虑降分辨率。
                    self.force_codec = "libx264"
                    old, self.encoder = self.encoder, None
                    if old:
                        try:
                            old.close()
                        except Exception:
                            pass
                    self._slow_streak = 0
                    self.server.log.emit("H.265 编码跟不上帧率，改用 H.264（保持分辨率）")
                elif self._effective_fps() > 30:
                    # 降帧率比降分辨率划算得多：1080p@30 比放大后的 1440×810 清楚。
                    current = self._effective_fps()
                    self.fps_cap = max(30, current - 10)
                    self._slow_streak = 0
                    self.server.log.emit(
                        f"编码跟不上，帧率上限 {current} → {self.fps_cap} FPS（保持分辨率）")
                elif not self.encode_fast:
                    # 换更快的编码预设，画质略降但分辨率不变，比放大 540p 清楚得多。
                    self.encode_fast = True
                    old, self.encoder = self.encoder, None
                    if old:
                        try:
                            old.close()
                        except Exception:
                            pass
                    self._slow_streak = 0
                    self.server.log.emit("编码跟不上帧率，改用更快预设（保持分辨率）")
                elif self.encode_scale > 0.5:
                    self.encode_scale = max(0.5, self.encode_scale - 0.25)
                    self._slow_streak = 0
                    self.server.log.emit(
                        f"采集+编码 {load:.0f}ms 偏高，编码分辨率降到 {int(self.encode_scale * 100)}%（画面由主控端放大）")
            elif self._fast_streak >= 8:
                if self.fps_cap and self.fps_cap < base_fps:
                    self.fps_cap = min(base_fps, self.fps_cap + 5)
                    self._fast_streak = 0
                    self.server.log.emit(f"余量充足，帧率上限回到 {self.fps_cap} FPS")
                elif self.encode_scale < 1.0:
                    self.encode_scale = min(1.0, self.encode_scale + 0.25)
                    self._fast_streak = 0
                    self.server.log.emit(
                        f"采集+编码余量充足，编码分辨率恢复到 {int(self.encode_scale * 100)}%")
        if self.media_tx:
            self.media_tx.set_bitrate(target)
        self.server.stats.emit({
            "mode": self.mode,
            "fps": round(self.frames / elapsed, 1),
            "kbps": round(self.bytes_frame * 8 / elapsed / 1000, 1),
            "tiles": self._last_tiles,
            "preset": self.quality,
            "rtt": round(self.peer_rtt, 1),
            "dropped": dropped_total,
            "qos": round(self.qos.level, 2),
            "qf": int(self.qos.params["quality"]),
            "fps_target": self._effective_fps(),
            "codec": self.codec_name,
            "bitrate": int(target),
            "enc": round(self.enc_ema, 1),
            "cap": round(self.grab_ema, 1),
            "scale": int(self.encode_scale * 100),
            "loss": round((loss or 0.0) * 100, 1),
        })
        self.frames = 0
        self.bytes_frame = 0
        self.last_stat = now

    def run(self):
        try:
            self.sock = self.raw_conn
            self.sock.settimeout(5.0)
            typ, meta, _ = recv_msg(self.sock)
            if typ != "HELLO_PLAIN":
                raise ValueError("握手消息无效")
            declared = str(meta.get("fp", ""))
            client_pem = str(meta.get("cert", ""))
            if not client_pem or not declared:
                raise ValueError("客户端证书缺失")
            actual = wd_crypto.fingerprint(client_pem)
            if declared != actual:
                raise ValueError("客户端证书指纹不匹配")
            client_id = str(meta.get("id", ""))
            name = str(meta.get("name", "未知设备"))
            if not client_id:
                raise ValueError("客户端身份缺失")

            resume_token = str(meta.get("resume", ""))
            resumed = self.server.check_resume(resume_token, client_id, actual)
            approved = False
            mode = self.server.config.get("auth_mode")
            auto = mode == "auto" or self.server.config.is_auto_trusted(client_id, actual)
            if auto or resumed:
                approved = True
                auto = auto or resumed
            else:
                token = secrets.token_hex(8)
                pending = {"event": threading.Event(), "approved": False, "auto": False}
                self.server.pending[token] = pending
                self.server.auth_required.emit({
                    "token": token, "id": client_id, "name": name,
                    "fp": actual, "code": wd_crypto.short_code(actual),
                    "ip": self.addr[0], "auto_available": self.server.config.get("auth_mode") == "trusted",
                })
                pending["event"].wait(AUTH_WAIT_SECONDS)
                approved = bool(pending.get("approved"))
                auto = bool(pending.get("auto"))
                self.server.pending.pop(token, None)

            if not approved:
                send_msg(self.sock, "AUTH_DENIED", reason="用户拒绝授权")
                return

            ctx = wd_crypto.tls_context(wd_config.APP_DIR, True, peer_pem=client_pem)
            send_msg(self.sock, "TLS_READY", cert=self.server.config.cert_pem)
            self.sock = ctx.wrap_socket(self.sock, server_side=True)
            self.sock.setblocking(True)
            _tune_tcp(self.sock)
            peer_der = self.sock.getpeercert(binary_form=True)
            if wd_crypto.fingerprint(peer_der or b"") != actual:
                raise ValueError("TLS 客户端证书与声明不一致")

            typ, meta, _ = recv_msg(self.sock)
            if typ != "HELLO" or str(meta.get("fp", "")) != actual:
                raise ValueError("加密握手身份不一致")
            preset = str(meta.get("quality", "auto"))
            self._apply_preset(preset)
            try:
                media_port = int(meta.get("media_port", 0) or 0)
            except (TypeError, ValueError):
                media_port = 0
            self.server.config.remember(client_id, name, actual, auto=auto)
            # 复用同一个采集对象，RESYNC / QUALITY 才能作用到实际发送循环
            screen = self.capture
            size = screen.frame_size()
            self.resume_token = self.server.issue_resume(client_id, actual)
            self.sender = MessageSender(self.sock, on_error=self._on_send_error)
            self.sender.start()
            media = {}
            if str(self.server.config.get("media", "auto")).lower() != "off":
                media = self._start_media(media_port, size)
            self._send("HELLO_OK", session_id=secrets.token_hex(8), epoch=self.epoch,
                       w=int(size[1]), h=int(size[0]), fps=screen.params["fps"],
                       name=self.server.config.device_name, resume=self.resume_token,
                       media=media)
            self.server.session_started.emit({
                "id": client_id, "name": name, "ip": self.addr[0],
                "code": wd_crypto.short_code(actual), "auto": auto, "resumed": resumed,
            })
            threading.Thread(target=self._recv_loop, daemon=True, name="wd-ctrl-recv").start()
            self.heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="wd-ctrl-hb")
            self.heartbeat_thread.start()
            self._send_loop(screen)
        except Exception as exc:
            try:
                self.server.log.emit(f"被控会话异常：{exc}")
            except RuntimeError:
                pass
        finally:
            self.alive = False
            if self.sender:
                self.sender.stop()
            for upload_id in list(self._uploads):
                self._drop_upload(upload_id)
            self._release_media()
            try:
                self.capture.close()
            except Exception:
                pass
            try:
                if self.sock:
                    self.sock.close()
                else:
                    self.raw_conn.close()
            except OSError:
                pass
            try:
                self.server.session_ended.emit("会话已结束")
            except RuntimeError:
                pass
            self.server.unregister(self)

    def _effective_fps(self) -> int:
        """实际发送帧率：预设帧率与运行时下调值取小。"""
        base = max(15, int(self.qos.params.get("fps", 30)))
        if self.fps_cap:
            return max(15, min(base, int(self.fps_cap)))
        return base

    def _send_loop(self, screen):
        # 节拍用高精度时钟：Windows 上 time.monotonic() 只有约 15.6ms 粒度，
        # 用它算帧间隔会让每帧多睡一次，30 FPS 被压到 20 FPS 左右。
        next_at = 0.0
        idle_since = 0.0
        jpeg_active = False
        while self.alive:
            media = self._media_alive()
            target_fps = self._effective_fps()
            if self.transfer_active:
                # 传文件时把画面降到 8 FPS，把带宽和 CPU 让给文件通道。
                target_fps = min(target_fps, 8)
            now = time.perf_counter()
            if now < next_at:
                time.sleep(min(next_at - now, 0.004))
                continue
            try:
                grab_started = time.perf_counter()
                cur = screen.grab()
                spent = (time.perf_counter() - grab_started) * 1000.0
                self.grab_ema = spent if self.grab_ema <= 0 else self.grab_ema * 0.8 + spent * 0.2
            except Exception as exc:
                self.server.log.emit(f"采集失败：{exc}")
                time.sleep(0.5)
                continue
            if cur is None:
                # 桌面没有变化：不编码也不发送。短间隔重试保证动起来的第一帧不迟到，
                # 长时间静止后退避到 8ms，避免空转吃 CPU。
                if not idle_since:
                    idle_since = now
                time.sleep(0.001 if now - idle_since < 0.5 else 0.008)
                continue
            idle_since = 0.0
            next_at = now + 1.0 / max(1, target_fps)
            if not self._backend_logged:
                detail = f"（{screen.backend_error}）" if screen.backend_error else ""
                self.server.log.emit(f"采集后端：{screen.backend}{detail}")
                self._backend_logged = True
            if screen.black:
                if not self._black_notified:
                    self.server.log.emit("采集到黑屏：被控端可能锁屏、切换用户或运行在非交互桌面")
                    self._black_notified = True
                time.sleep(0.5)
                continue
            self._black_notified = False
            if media:
                if not self._send_video(screen, cur):
                    break
            else:
                ok, jpeg_active = self._send_tiles(screen, cur, jpeg_active)
                if not ok:
                    break
            now = time.monotonic()
            if now - self.last_stat >= 1.0:
                self._publish_stats(now)

    def _upload_dir(self) -> str:
        base = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(base):
            base = os.path.expanduser("~")
        folder = os.path.join(base, "WiFiDesk")
        os.makedirs(folder, exist_ok=True)
        return folder

    def _file_begin(self, meta):
        name = os.path.basename(str(meta.get("name", "")).replace("\\", "/")).strip()
        file_id = str(meta.get("id", ""))
        try:
            size = int(meta.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        if not file_id or not name or name in (".", "..") or size <= 0 or size > FILE_MAX_BYTES:
            self.server.log.emit("拒绝接收文件：参数异常")
            return
        if len(self._uploads) >= 3:
            self.server.log.emit("同时接收的文件过多，已拒绝")
            return
        try:
            folder = self._upload_dir()
            target = os.path.join(folder, name)
            stem, ext = os.path.splitext(name)
            index = 1
            while os.path.exists(target):
                target = os.path.join(folder, f"{stem} ({index}){ext}")
                index += 1
            handle = open(target, "wb")
        except OSError as exc:
            self.server.log.emit(f"无法创建接收文件：{exc}")
            return
        self._uploads[file_id] = {"file": handle, "path": target, "size": size,
                                  "got": 0, "mark": 0}
        self.transfer_active = True
        self.server.log.emit(f"开始接收 {name}（{size / 1048576:.1f} MB）→ {target}")

    def _file_chunk(self, meta, payload):
        file_id = str(meta.get("id", ""))
        entry = self._uploads.get(file_id)
        if entry is None or not payload:
            return
        try:
            entry["file"].write(payload)
        except OSError as exc:
            self.server.log.emit(f"写入失败：{exc}")
            self._drop_upload(file_id)
            return
        entry["got"] += len(payload)
        percent = min(100, int(entry["got"] * 100 / max(1, entry["size"])))
        if percent >= entry["mark"] + 25:
            entry["mark"] = percent
            self.server.log.emit(f"接收 {os.path.basename(entry['path'])}：{percent}%")

    def _file_end(self, meta):
        file_id = str(meta.get("id", ""))
        entry = self._uploads.pop(file_id, None)
        if entry is None:
            return
        self.transfer_active = bool(self._uploads)
        try:
            entry["file"].close()
        except OSError:
            pass
        name = os.path.basename(entry["path"])
        if not bool(meta.get("ok", True)) or entry["got"] != entry["size"]:
            self.server.log.emit(
                f"文件未接收完成：{name}（{entry['got']}/{entry['size']} 字节）")
            try:
                os.remove(entry["path"])
            except OSError:
                pass
        else:
            self.server.log.emit(f"文件已保存：{entry['path']}")

    def _drop_upload(self, file_id):
        entry = self._uploads.pop(file_id, None)
        self.transfer_active = bool(self._uploads)
        if entry is not None:
            try:
                entry["file"].close()
            except OSError:
                pass

    def _recv_loop(self):
        while self.alive:
            try:
                typ, meta, payload = recv_msg(self.sock)
            except (OSError, ValueError) as exc:
                self.server.log.emit(f"被控接收结束：{type(exc).__name__}: {exc}")
                break
            if typ in ("INPUT", "INPUT2"):
                try:
                    if typ == "INPUT2":
                        event = wd_input.decode_event(payload)
                    else:
                        event = json.loads(payload.decode("utf-8"))
                    if event:
                        wd_input.inject(event)
                except Exception:
                    pass
            elif typ == "PING":
                try:
                    self.peer_rtt = float(meta.get("rtt", 0) or 0)
                except (TypeError, ValueError):
                    pass
                self._send("PONG", t=meta.get("t", 0), priority=True)
            elif typ == "QUALITY":
                preset = str(meta.get("preset", "auto"))
                with self.capture_lock:
                    if preset != self.quality:
                        self._apply_preset(preset)
            elif typ == "CLIP_SET":
                text = payload.decode("utf-8", "ignore")
                if text:
                    threading.Thread(target=wd_clipboard.set_text, args=(text,),
                                     daemon=True, name="wd-clip").start()
                    self.server.log.emit(f"已同步剪贴板文本（{len(text)} 字）")
            elif typ == "CLIP_GET":
                threading.Thread(target=self._reply_clipboard, daemon=True,
                                 name="wd-clip-get").start()
            elif typ == "FILE_BEGIN":
                self._file_begin(meta)
            elif typ == "FILE_CHUNK":
                self._file_chunk(meta, payload)
            elif typ == "FILE_END":
                self._file_end(meta)
            elif typ == "RESYNC":
                with self.capture_lock:
                    self.capture.prev = None
                self._on_key_request()
            elif typ == "MEDIA_ACK":
                self.media_acked = time.monotonic()
                self.media_acked_once = True
                try:
                    self.media_lost = max(0, int(meta.get("lost", 0) or 0))
                    self.media_err = max(0, int(meta.get("err", 0) or 0))
                    self.media_frames = max(0, int(meta.get("frames", 0) or 0))
                except (TypeError, ValueError):
                    pass
            elif typ == "MEDIA_KEY":
                self._on_key_request()
            elif typ == "MEDIA_OFF":
                self._media_off("主控端停用视频通道")
            elif typ == "DISCONNECT":
                self.server.revoke_resume(str(meta.get("resume", "")))
                break
        self.alive = False


class ClientSession(QObject):
    connected = Signal(dict)
    disconnected = Signal(str)
    frame_ready = Signal()
    stats = Signal(dict)
    state = Signal(str)
    log = Signal(str)
    file_progress = Signal(dict)
    clipboard_text = Signal(str)

    def __init__(self, config, device: dict, quality: str = "auto", parent=None,
                 resume_token: str = ""):
        super().__init__(parent)
        self.config = config
        self.device = dict(device)
        self.quality = quality if quality in _capture().PRESETS else "auto"
        self.resume_token = str(resume_token or "")
        self.sock = None
        self.alive = True
        self.manual = False
        self.connected_flag = False
        self.send_lock = threading.Lock()
        self.sender = None
        self.canvas = None
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.failure_reason = "连接已断开"
        self.frames = 0
        self.bytes_frame = 0
        self.rtt_ms = 0.0
        self.mode = "-"
        self.last_seq = -1
        self.await_full = False
        self.qos_level = 1.0
        self.qf = 0
        self.last_stat = time.monotonic()
        self.media = None
        self.media_sock = None
        self.media_epoch = 0
        self.codec_name = ""
        self.remote_size = (0, 0)
        self.stream_size = (0, 0)
        self._media_bytes_last = 0
        self._clip_last = ""
        # 剪贴板只能在 GUI 线程改，接收线程通过信号转回来。
        self.clipboard_text.connect(self._apply_clipboard)

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="wd-controller").start()

    def stop(self):
        self.manual = True
        self.alive = False
        if self.sender:
            self.sender.stop()
        self._stop_media()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _on_send_error(self, exc):
        self.log.emit(f"发送失败：{type(exc).__name__}: {exc}")
        self.failure_reason = str(exc)
        self.alive = False

    def _send(self, typ, payload=b"", video=False, priority=False, **meta):
        if not self.alive or not self.sock:
            return False
        if self.sender:
            return self.sender.send(typ, payload, video=video, priority=priority, **meta)
        with self.send_lock:
            try:
                send_msg(self.sock, typ, payload, **meta)
                return True
            except OSError as exc:
                self._on_send_error(exc)
                return False

    def send_input(self, event: dict):
        blob = wd_input.encode_event(event)
        if blob:
            self._send("INPUT2", blob, priority=True)

    def request_clipboard(self) -> bool:
        """Ctrl+C/X 时向被控端要一次剪贴板文本，让本地也能粘贴远端复制的内容。"""
        return self._send("CLIP_GET", priority=True)

    def _apply_clipboard(self, text: str):
        from PySide6.QtGui import QGuiApplication
        if not text or text == self._clip_last:
            return
        self._clip_last = text
        QGuiApplication.clipboard().setText(text)

    def push_clipboard(self) -> bool:
        """把本机剪贴板文本推到被控端，被控端据此设置自己的剪贴板。"""
        from PySide6.QtGui import QGuiApplication
        text = QGuiApplication.clipboard().text()
        if not text or text == self._clip_last:
            return False
        data = text.encode("utf-8")
        if len(data) > 4 * 1024 * 1024:
            self.log.emit("剪贴板文本超过 4MB，未发送")
            return False
        self._clip_last = text
        return self._send("CLIP_SET", data, priority=True, size=len(data))

    def send_files(self, paths) -> int:
        files = [str(path) for path in paths if os.path.isfile(str(path))]
        if not files:
            self.log.emit("没有可发送的文件")
            return 0
        threading.Thread(target=self._send_files, args=(files,), daemon=True,
                         name="wd-file-tx").start()
        return len(files)

    def _send_files(self, paths):
        for path in paths:
            name = os.path.basename(path)
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                self.log.emit(f"读取 {name} 失败：{exc}")
                continue
            if size <= 0 or size > FILE_MAX_BYTES:
                self.log.emit(f"跳过 {name}：文件大小异常")
                continue
            file_id = secrets.token_hex(4)
            if not self._send("FILE_BEGIN", id=file_id, name=name, size=size, priority=True):
                return
            self.log.emit(f"开始发送 {name}（{size / 1048576:.1f} MB）")
            sent = 0
            ok = True
            base = int(self.sender.sent_bytes) if self.sender else 0
            try:
                with open(path, "rb") as handle:
                    while self.alive:
                        chunk = handle.read(FILE_CHUNK_SIZE)
                        if not chunk:
                            break
                        if not self._send("FILE_CHUNK", chunk, id=file_id, off=sent):
                            ok = False
                            break
                        sent += len(chunk)
                        self.file_progress.emit({"name": name, "sent": sent, "size": size})
                        if not self._wait_outstanding(sent, base):
                            ok = False
                            break
            except OSError as exc:
                self.log.emit(f"发送 {name} 失败：{exc}")
                ok = False
            if sent != size:
                ok = False
            self._send("FILE_END", id=file_id, ok=ok, sent=sent)
            if ok:
                self.log.emit(f"已发送 {name}（{size / 1048576:.1f} MB）")
            else:
                self.log.emit(f"{name} 发送中断（{sent / 1048576:.1f}/{size / 1048576:.1f} MB）")

    def _wait_outstanding(self, queued: int, base: int) -> bool:
        """限制发送队列积压：控制消息不设上限，链接慢时大文件会把内存顶爆。"""
        sender = self.sender
        if sender is None:
            return True
        deadline = time.monotonic() + 60.0
        while self.alive:
            outstanding = queued - (int(sender.sent_bytes) - base)
            if outstanding <= FILE_WINDOW:
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.005)
        return False

    def set_quality(self, preset: str):
        if preset in _capture().PRESETS:
            self.quality = preset
            self._send("QUALITY", preset=preset, priority=True)

    def resync(self):
        self.await_full = True
        self.last_seq = -1
        self._send("RESYNC", priority=True)

    def take_frame(self):
        with self.frame_lock:
            frame = self.latest_frame
            self.latest_frame = None
        return frame

    def _reserve_media_socket(self) -> int:
        """先绑定 UDP 端口再报给被控端，避免握手后还要二次协商。"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            sock.bind(("0.0.0.0", 0))
        except OSError as exc:
            self.log.emit(f"UDP 端口预留失败，使用图块通道：{exc}")
            return 0
        self.media_sock = sock
        return int(sock.getsockname()[1])

    def _start_media(self, ip: str, meta: dict, w: int, h: int):
        info = meta.get("media")
        sock, self.media_sock = self.media_sock, None
        if not isinstance(info, dict) or not info or sock is None:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            return
        try:
            from . import wd_media
            key = bytes.fromhex(str(info.get("key", "")))
            nonce = bytes.fromhex(str(info.get("nonce", "")))
            port = int(info.get("port", 0))
            epoch = int(info.get("epoch", 1))
            if len(key) != 16 or len(nonce) != 4 or port <= 0:
                raise ValueError("媒体参数无效")
            rx = wd_media.MediaRx(
                key, nonce, w, h, epoch,
                on_frame=self._on_media_rgb, on_snapshot=self._on_media_rgb,
                on_keyframe=self._media_key, sock=sock, peer=(ip, port),
                codec=str(info.get("codec", "h264") or "h264"))
            self.media_epoch = epoch
            rx.start()
        except Exception as exc:
            try:
                sock.close()
            except OSError:
                pass
            self.log.emit(f"视频通道未启用，使用图块：{exc}")
            return
        self.media = rx
        self.codec_name = str(info.get("codec", "h264"))
        self.mode = "H.265" if self.codec_name == "hevc" else "H.264"

    def _stop_media(self):
        rx, self.media = self.media, None
        if rx:
            rx.stop()
        sock, self.media_sock = self.media_sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self.codec_name = ""

    def _media_key(self):
        self._send("MEDIA_KEY", priority=True)

    def _on_media_rgb(self, rgb):
        if rgb is None:
            return
        height, width = int(rgb.shape[0]), int(rgb.shape[1])
        if width <= 0 or height <= 0:
            return
        stride = int(rgb.strides[0]) if rgb.ndim == 3 else width * 3
        self.stream_size = (width, height)
        img = QImage(rgb.data, width, height, stride, QImage.Format_RGB888).copy()
        if img.isNull():
            return
        if self.canvas is None or (self.canvas.width(), self.canvas.height()) != (width, height):
            self.canvas = QImage(width, height, QImage.Format_RGB888)
            self.canvas.fill(QColor("#101318"))
        self.mode = "H.265" if self.codec_name == "hevc" else "H.264"
        with self.frame_lock:
            notify = self.latest_frame is None
            self.latest_frame = img
        if notify:
            self.frame_ready.emit()
        self.frames += 1
        # 媒体模式下控制消息很少，统计若只挂在 TCP 接收循环上，间隔会被拉长、帧率显示失真。
        self._publish_stats()

    def _run(self):
        try:
            self.state.emit("正在建立加密连接…")
            ip = self.device.get("ip", "")
            port = int(self.device.get("port", 0))
            if not ip or not port:
                raise ValueError("设备地址无效")
            self.sock = socket.create_connection((ip, port), timeout=5)
            if not self.alive:
                return
            self.sock.settimeout(5.0)
            send_msg(self.sock, "HELLO_PLAIN", id=self.config.device_id,
                     name=self.config.device_name, fp=self.config.fingerprint,
                     cert=self.config.cert_pem, resume=self.resume_token)
            typ, meta, _ = recv_msg(self.sock, idle_timeout=AUTH_WAIT_SECONDS + 5)
            if typ == "AUTH_DENIED":
                raise PermissionError(str(meta.get("reason", "被控端拒绝授权")))
            if typ != "TLS_READY":
                raise ValueError("服务端未进入加密阶段")
            server_pem = str(meta.get("cert", ""))
            expected = str(self.device.get("fp", ""))
            if not server_pem or wd_crypto.fingerprint(server_pem) != expected:
                raise ValueError("服务端身份与发现信息不一致，已中止")
            ctx = wd_crypto.tls_context(wd_config.APP_DIR, False, peer_pem=server_pem)
            self.sock = ctx.wrap_socket(self.sock, server_hostname=None)
            self.sock.setblocking(True)
            _tune_tcp(self.sock)
            actual = wd_crypto.fingerprint(self.sock.getpeercert(binary_form=True) or b"")
            if actual != expected:
                raise ValueError("TLS 服务端证书校验失败")
            media_port = self._reserve_media_socket()
            self._send("HELLO", id=self.config.device_id, name=self.config.device_name,
                       fp=self.config.fingerprint, quality=self.quality,
                       media_port=media_port)
            typ, meta, _ = recv_msg(self.sock)
            if typ != "HELLO_OK":
                raise ValueError("握手失败")
            w, h = int(meta.get("w", 0)), int(meta.get("h", 0))
            if w <= 0 or h <= 0 or w > 7680 or h > 4320:
                raise ValueError("显示配置无效")
            self.canvas = QImage(w, h, QImage.Format_RGB888)
            self.canvas.fill(QColor("#101318"))
            self.remote_size = (w, h)
            self.connected_flag = True
            self.resume_token = str(meta.get("resume", "")) or self.resume_token
            self.last_seq = -1
            self.await_full = False
            self.connected.emit({"name": self.device.get("name", ""), "w": w, "h": h,
                                 "id": self.device.get("id", ""), "server_name": meta.get("name", "")})
            self.state.emit("已连接")
            self.sender = MessageSender(self.sock, on_error=self._on_send_error)
            self.sender.start()
            self._start_media(ip, meta, w, h)
            threading.Thread(target=self._recv_loop, daemon=True, name="wd-frame-recv").start()
            self._send("QUALITY", preset=self.quality, priority=True)
            while self.alive:
                self._send("PING", t=time.monotonic(), rtt=self.rtt_ms, priority=True)
                if self.media:
                    info = self.media.stats()
                    self._send("MEDIA_ACK", frames=info["frames"], lost=info["lost"],
                               err=info["decode_errors"], priority=True)
                time.sleep(HEARTBEAT_INTERVAL)
        except Exception as exc:
            self.failure_reason = str(exc)
            self.log.emit(f"连接失败：{exc}")
        finally:
            self.alive = False
            self.connected_flag = False
            if self.sender:
                self.sender.stop()
            self._stop_media()
            try:
                if self.sock:
                    self.sock.close()
            except OSError:
                pass
            if not self.manual:
                self.disconnected.emit(self.failure_reason)
            self.state.emit("已断开")

    def _recv_loop(self):
        while self.alive:
            try:
                typ, meta, payload = recv_msg(self.sock)
            except (OSError, ValueError) as exc:
                self.failure_reason = str(exc)
                self.log.emit(f"主控接收结束：{type(exc).__name__}: {exc}")
                break
            if typ in ("FRAME_TILES", "FRAME_JPEG"):
                try:
                    self.qos_level = float(meta.get("qos", self.qos_level))
                    self.qf = int(meta.get("qf", self.qf))
                except (TypeError, ValueError):
                    pass
                # 发送队列会丢弃过期视频帧；发现序号断层时请求整帧刷新，
                # 并在 full 帧到达前忽略增量瓦片，避免把增量画到旧画布上。
                seq = meta.get("seq")
                seq = int(seq) if isinstance(seq, int) else -1
                full = bool(meta.get("full", typ == "FRAME_JPEG"))
                if typ == "FRAME_TILES" and self.await_full and not full:
                    continue
                if full:
                    self.await_full = False
                if seq >= 0:
                    if self.last_seq >= 0 and seq <= self.last_seq:
                        continue
                    if self.last_seq >= 0 and seq != self.last_seq + 1 and not full:
                        self.resync()
                        continue
                    self.last_seq = seq
            if typ == "FRAME_TILES":
                if not self._apply_tiles(meta, payload):
                    self.resync()
                    continue
                self.mode = str(meta.get("mode", "TILE无损"))
            elif typ == "FRAME_JPEG":
                img = QImage.fromData(payload)
                if img.isNull():
                    self.resync()
                    continue
                self.canvas = img.convertToFormat(QImage.Format_RGB888)
                self.mode = "JPEG兼容"
            elif typ == "PING":
                self._send("PONG", t=meta.get("t", 0), priority=True)
            elif typ == "PONG":
                sent = float(meta.get("t", 0))
                if sent:
                    self.rtt_ms = round((time.monotonic() - sent) * 1000, 1)
            elif typ == "MEDIA_OFF":
                self._stop_media()
                self.mode = "图块回退"
                self.log.emit(f"视频通道停用，切回图块：{meta.get('reason', '')}")
            elif typ == "CLIP_TEXT":
                text = payload.decode("utf-8", "ignore")
                if text:
                    self.clipboard_text.emit(text)
            elif typ == "MEDIA_CODEC":
                codec = str(meta.get("codec", "h264"))
                if self.media and codec != self.codec_name:
                    self.codec_name = codec
                    self.media.set_codec(codec)
                    self.mode = "H.265" if codec == "hevc" else "H.264"
                    self.log.emit(f"编码格式切换为 {self.mode}")
            elif typ == "DISCONNECT":
                break
            else:
                continue
            if typ in ("FRAME_TILES", "FRAME_JPEG"):
                with self.frame_lock:
                    notify = self.latest_frame is None
                    # QImage(canvas) 只是浅拷贝，会与画布共享像素；UI 线程读取时
                    # 接收线程仍在画Tiles，必须深拷贝，否则画面会撕裂/闪黑块
                    self.latest_frame = self.canvas.copy()
                if notify:
                    self.frame_ready.emit()
                self.frames += 1
                self.bytes_frame += len(payload)
            self._publish_stats()
        self.alive = False

    def _apply_tiles(self, meta, payload: bytes) -> bool:
        if self.canvas is None:
            return False
        w, h = meta.get("w"), meta.get("h")
        if not isinstance(w, int) or not isinstance(h, int) or not (0 < w <= 7680 and 0 < h <= 4320):
            return False
        if (w, h) != (self.canvas.width(), self.canvas.height()):
            self.canvas = QImage(w, h, QImage.Format_RGB888)
            self.canvas.fill(QColor("#101318"))
        tiles = meta.get("tiles")
        if not isinstance(tiles, list):
            return False
        painter = QPainter(self.canvas)
        offset = 0
        ok = True
        try:
            for item in tiles:
                if not isinstance(item, list) or len(item) != 6:
                    ok = False
                    break
                x, y, tw, th, clen, rawlen = item
                x, y, tw, th, clen, rawlen = (int(x), int(y), int(tw), int(th), int(clen), int(rawlen))
                if clen <= 0 or clen > 8 * 1024 * 1024 or offset + clen > len(payload):
                    ok = False
                    break
                if x < 0 or y < 0 or x + tw > self.canvas.width() or y + th > self.canvas.height():
                    ok = False
                    break
                try:
                    raw = zlib.decompress(payload[offset:offset + clen])
                except zlib.error:
                    ok = False
                    break
                offset += clen
                if len(raw) != rawlen or rawlen != tw * th * 3:
                    ok = False
                    break
                img = QImage(raw, tw, th, tw * 3, QImage.Format_RGB888)
                if img.isNull():
                    ok = False
                    break
                painter.drawImage(x, y, img)
        finally:
            painter.end()
        return ok

    def _publish_stats(self):
        now = time.monotonic()
        elapsed = now - self.last_stat
        if elapsed < 1.0:
            return
        media_bytes = 0
        dec = 0.0
        if self.media:
            current = int(self.media.bytes)
            media_bytes = max(0, current - self._media_bytes_last)
            self._media_bytes_last = current
            dec = float(self.media.stats().get("dec", 0.0) or 0.0)
        self.stats.emit({
            "mode": self.mode,
            "fps": round(self.frames / elapsed, 1),
            "kbps": round((self.bytes_frame + media_bytes) * 8 / elapsed / 1000, 1),
            "rtt": self.rtt_ms,
            "qos": self.qos_level,
            "qf": self.qf,
            "codec": self.codec_name,
            "dec": round(dec, 1),
            "w": self.remote_size[0] or (self.canvas.width() if self.canvas else 0),
            "h": self.remote_size[1] or (self.canvas.height() if self.canvas else 0),
            "sw": self.stream_size[0],
            "sh": self.stream_size[1],
        })
        self.frames = 0
        self.bytes_frame = 0
        self.last_stat = now
