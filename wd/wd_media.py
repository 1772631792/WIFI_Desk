"""加密 UDP 视频通道：H.264/H.265 分片、AES-GCM、重组、NACK 重传与关键帧恢复。"""
import collections
import select
import socket
import struct
import threading
import time
import zlib

import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .wd_codec import H264Decoder

MAGIC = b"WFV1"
VERSION = 1
HEADER = struct.Struct(">4sBBIIIHHH")
HEADER_SIZE = HEADER.size
TAG_SIZE = 16
MTU = 1200
MAX_PAYLOAD = MTU - HEADER_SIZE - TAG_SIZE
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_FRAGMENTS = 20000
REPLAY_WINDOW = 4096
FLAG_KEY = 0x01
FLAG_RAW = 0x02

# 关键帧请求走同一条 UDP 通道，单独用一套 nonce 前缀和序号，
# 避免与视频分片复用 (前缀, 序号) 造成 GCM nonce 重用。
KEY_MAGIC = b"WFQ1"
KEY_HEADER = struct.Struct(">4sI")
KEY_REQUEST_INTERVAL = 0.5

# 丢片重传（NACK）：接收端发现序号缺口就按序号点名要，发送端从最近的包缓存里重发。
# 靠它把"丢一片 → 整帧丢弃 → 等关键帧（最坏 1 秒花屏）"压到一次 RTT。
NACK_MAGIC = b"WNK1"
NACK_HEADER = struct.Struct(">4sIBQ")   # magic, epoch, count, counter
NACK_INTERVAL = 0.008       # 收端两次扫描的最小间隔
NACK_RETRY = 0.06           # 同一序号重发请求的间隔
NACK_WINDOW = 192           # 回看的序号范围
NACK_BATCH = 16             # 单包最多点名几个序号
HISTORY_SIZE = 512          # 发送端缓存的包数量（约 600KB）


def _flip_prefix(prefix: bytes) -> bytes:
    return bytes(byte ^ 0xFF for byte in prefix)


def _nack_prefix(prefix: bytes) -> bytes:
    return bytes(byte ^ 0xAA for byte in prefix)


class MediaTx:
    def __init__(self, peer_ip: str, peer_port: int, key: bytes, nonce_prefix: bytes,
                 epoch: int = 1, bitrate: int = 8_000_000, sock=None):
        self.peer = (peer_ip, int(peer_port))
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", 0))
        self.sock = sock
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
        self.sock.setblocking(True)
        self.local_port = self.sock.getsockname()[1]
        self.aes = AESGCM(key)
        self.nonce_prefix = bytes(nonce_prefix[:4]).ljust(4, b"\x00")
        self.request_prefix = _flip_prefix(self.nonce_prefix)
        self.nack_prefix = _nack_prefix(self.nonce_prefix)
        self._request_seq = 0
        self.epoch = int(epoch)
        self.bitrate = max(500_000, int(bitrate))
        self.seq = 0
        self.sent_frames = 0
        self.sent_bytes = 0
        self.dropped_frames = 0
        self.key_requests = 0
        self.retransmits = 0
        self.history = collections.OrderedDict()
        self.lock = threading.Lock()
        self._pace_at = time.monotonic()
        self._tokens = 0.0
        self._control_running = False
        self._control_thread = None
        self._on_key_request = None
        self._last_request_at = 0.0

    def start_control(self, on_key_request):
        """在媒体 socket 上监听对端的关键帧请求。"""
        self._on_key_request = on_key_request
        self._control_running = True
        self._control_thread = threading.Thread(
            target=self._control_loop, daemon=True, name="wd-media-control")
        self._control_thread.start()

    def _control_loop(self):
        while self._control_running:
            try:
                ready, _, _ = select.select([self.sock], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data, addr = self.sock.recvfrom(256)
            except OSError:
                break
            try:
                self._handle_request(data, addr)
            except Exception:
                continue

    def _handle_request(self, data: bytes, addr):
        if len(data) >= 4 and data[:4] == NACK_MAGIC:
            self._handle_nack(data, addr)
            return
        if len(data) != KEY_HEADER.size + 12 + TAG_SIZE:
            return
        header = data[:KEY_HEADER.size]
        magic, epoch = KEY_HEADER.unpack(header)
        if magic != KEY_MAGIC or epoch != self.epoch:
            return
        seq = struct.unpack(">Q", data[KEY_HEADER.size:KEY_HEADER.size + 8])[0]
        nonce = self.request_prefix + struct.pack(">Q", seq)
        try:
            self.aes.decrypt(nonce, data[KEY_HEADER.size:], header)
        except Exception:
            return
        if addr[0] != self.peer[0]:
            return
        now = time.monotonic()
        if now - self._last_request_at < KEY_REQUEST_INTERVAL:
            return
        self._last_request_at = now
        self.key_requests += 1
        if self._on_key_request:
            self._on_key_request()

    def _handle_nack(self, data: bytes, addr):
        size = NACK_HEADER.size
        if len(data) < size + 8 + TAG_SIZE:
            return
        header = data[:size]
        magic, epoch, count, counter = NACK_HEADER.unpack(header)
        if magic != NACK_MAGIC or epoch != self.epoch:
            return
        if not 0 < count <= NACK_BATCH or len(data) != size + count * 8 + TAG_SIZE:
            return
        if addr[0] != self.peer[0]:
            return
        nonce = self.nack_prefix + struct.pack(">Q", counter)
        try:
            plain = self.aes.decrypt(nonce, data[size:], header)
        except Exception:
            return
        sent = 0
        for offset in range(0, len(plain), 8):
            seq, = struct.unpack(">Q", plain[offset:offset + 8])
            packet = self.history.get(seq)
            if packet is None:
                continue
            try:
                self.sock.sendto(packet, self.peer)
                sent += 1
            except OSError:
                return
        self.retransmits += sent

    def set_bitrate(self, bitrate: int):
        self.bitrate = max(500_000, min(120_000_000, int(bitrate)))

    def _pace(self, size: int):
        now = time.monotonic()
        elapsed = max(0.0, now - self._pace_at)
        self._pace_at = now
        # 突发额度按 1 秒码率累积，限制在 1–4MB：既能一次发出大关键帧，
        # 又不会在高码率下把整秒画面堆进内核缓冲，抬高接收端延迟。
        burst = min(4 * 1024 * 1024, max(1024 * 1024, self.bitrate / 8.0))
        self._tokens = min(burst, self._tokens + elapsed * self.bitrate / 8.0)
        need = size - self._tokens
        if need <= 0:
            self._tokens -= size
            return
        delay = min(0.25, need * 8.0 / self.bitrate)
        if delay > 0:
            time.sleep(delay)
        self._tokens = max(0.0, self._tokens + delay * self.bitrate / 8.0 - size)

    def send(self, payload: bytes, keyframe: bool = False, raw: bool = False,
             frame_id: int = 0):
        if len(payload) > MAX_FRAME_BYTES:
            raise ValueError("视频帧过大")
        count = max(1, (len(payload) + MAX_PAYLOAD - 1) // MAX_PAYLOAD)
        if count > MAX_FRAGMENTS:
            raise ValueError("视频帧分片过多")
        flags = (FLAG_KEY if keyframe else 0) | (FLAG_RAW if raw else 0)
        with self.lock:
            self._pace(len(payload))
            for index in range(count):
                chunk = payload[index * MAX_PAYLOAD:(index + 1) * MAX_PAYLOAD]
                self.seq = (self.seq + 1) & 0xFFFFFFFFFFFFFFFF
                header = HEADER.pack(
                    MAGIC, VERSION, flags, self.epoch, frame_id & 0xFFFFFFFF,
                    self.seq, index, count, len(chunk))
                nonce = self.nonce_prefix + struct.pack(">Q", self.seq)
                packet = header + self.aes.encrypt(nonce, chunk, header)
                try:
                    self.sock.sendto(packet, self.peer)
                except OSError:
                    self.dropped_frames += 1
                    raise
                self.sent_bytes += len(packet)
                self.history[self.seq] = packet
                while len(self.history) > HISTORY_SIZE:
                    self.history.popitem(last=False)
            self.sent_frames += 1

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class MediaRx:
    def __init__(self, key: bytes, nonce_prefix: bytes, width: int, height: int,
                 epoch: int = 1, on_frame=None, on_snapshot=None, on_keyframe=None,
                 sock=None, peer=None, codec: str = "h264"):
        self.width = int(width)
        self.height = int(height)
        self.epoch = int(epoch)
        self.codec = codec if codec in ("h264", "hevc") else "h264"
        self.aes = AESGCM(key)
        self.nonce_prefix = bytes(nonce_prefix[:4]).ljust(4, b"\x00")
        self.request_prefix = _flip_prefix(self.nonce_prefix)
        self.nack_prefix = _nack_prefix(self.nonce_prefix)
        self._request_seq = 0
        self._nack_counter = 0
        self._nack_at = 0.0
        self._nacked = {}
        self.nacks = 0
        self.on_frame = on_frame
        self.on_snapshot = on_snapshot
        self.on_keyframe = on_keyframe
        self.sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        if sock is None:
            self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(True)
        self.peer = (peer[0], int(peer[1])) if peer else None
        self.local_port = self.sock.getsockname()[1]
        self.running = True
        self.thread = None
        self.decoder = None
        self.decoder_lock = threading.Lock()
        self.pending = {}
        self.last_frame_id = 0
        self.need_keyframe = False
        self.seen = set()
        self.last_seq = 0
        self._last_key_request = 0.0
        self.frames = 0
        self.bytes = 0
        self.lost = 0
        self.decode_errors = 0
        self.dec_ema = 0.0

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="wd-media-rx")
        self.thread.start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def stats(self):
        return {
            "frames": self.frames,
            "bytes": self.bytes,
            "lost": self.lost,
            "decode_errors": self.decode_errors,
            "dec": round(self.dec_ema, 1),
            "nacks": self.nacks,
        }

    def _maybe_nack(self):
        """扫最近的序号窗口，把缺口按序号点名重发（一次 RTT 就能补齐）。"""
        now = time.monotonic()
        if now - self._nack_at < NACK_INTERVAL or not self.peer or self.sock is None:
            return
        if self.last_seq <= 0:
            return
        self._nack_at = now
        missing = []
        for seq in range(max(1, self.last_seq - NACK_WINDOW), self.last_seq + 1):
            if seq in self.seen:
                continue
            last = self._nacked.get(seq, 0.0)
            if now - last < NACK_RETRY:
                continue
            self._nacked[seq] = now
            missing.append(seq)
            if len(missing) >= NACK_BATCH:
                break
        if len(self._nacked) > 1024:
            self._nacked = {s: t for s, t in self._nacked.items() if now - t < 2.0}
        if not missing:
            return
        self._nack_counter += 1
        header = NACK_HEADER.pack(NACK_MAGIC, self.epoch, len(missing), self._nack_counter)
        body = b"".join(struct.pack(">Q", seq) for seq in missing)
        nonce = self.nack_prefix + struct.pack(">Q", self._nack_counter)
        try:
            self.sock.sendto(header + self.aes.encrypt(nonce, body, header), self.peer)
            self.nacks += 1
        except OSError:
            pass

    def set_codec(self, codec: str):
        """服务端中途换了编码格式（例如硬件编码回退），重建解码器并等关键帧。"""
        codec = codec if codec in ("h264", "hevc") else "h264"
        with self.decoder_lock:
            self.codec = codec
            if self.decoder:
                try:
                    self.decoder.close()
                except Exception:
                    pass
            self.decoder = H264Decoder(codec)
            self.need_keyframe = True

    def _accept_seq(self, seq: int) -> bool:
        if seq in self.seen:
            return False
        if self.last_seq and seq + REPLAY_WINDOW < self.last_seq:
            return False
        self.seen.add(seq)
        if seq > self.last_seq:
            self.last_seq = seq
        if len(self.seen) > REPLAY_WINDOW * 2:
            cutoff = self.last_seq - REPLAY_WINDOW
            self.seen = {item for item in self.seen if item >= cutoff}
        return True

    def _request_keyframe(self):
        now = time.monotonic()
        if now - self._last_key_request < KEY_REQUEST_INTERVAL:
            return
        self._last_key_request = now
        if self.on_keyframe:
            self.on_keyframe()
        if not self.peer:
            return
        self._request_seq = (self._request_seq + 1) & 0xFFFFFFFFFFFFFFFF
        header = KEY_HEADER.pack(KEY_MAGIC, self.epoch)
        nonce = self.request_prefix + struct.pack(">Q", self._request_seq)
        packet = header + self.aes.encrypt(nonce, b"", header)
        try:
            self.sock.sendto(packet, self.peer)
        except OSError:
            pass

    def _run(self):
        self.decoder = H264Decoder(self.codec)
        try:
            while self.running:
                try:
                    ready, _, _ = select.select([self.sock], [], [], 0.02)
                except (OSError, ValueError):
                    break
                if not ready:
                    self._maybe_nack()
                    self._expire()
                    continue
                try:
                    data, _addr = self.sock.recvfrom(4096)
                except OSError:
                    break
                self._handle(data)
                self._expire()
        finally:
            if self.decoder:
                self.decoder.close()
            try:
                self.sock.close()
            except OSError:
                pass

    def _expire(self):
        now = time.monotonic()
        dead = [key for key, item in self.pending.items()
                if now - item["at"] > 1.0 or key < self.last_frame_id - 32]
        for key in dead:
            self.pending.pop(key, None)
            self.lost += 1
        if len(self.pending) > 8:
            for key in sorted(self.pending)[:-4]:
                self.pending.pop(key, None)
                self.lost += 1

    def _handle(self, data: bytes):
        if len(data) < HEADER_SIZE + TAG_SIZE:
            return
        header = data[:HEADER_SIZE]
        magic, version, flags, epoch, frame_id, seq, frag_index, frag_count, length = \
            HEADER.unpack(header)
        if magic != MAGIC or version != VERSION or epoch != self.epoch:
            return
        if length > MAX_PAYLOAD or len(data) != HEADER_SIZE + length + TAG_SIZE:
            return
        if frag_count < 1 or frag_count > MAX_FRAGMENTS or frag_index >= frag_count:
            return
        if not self._accept_seq(seq):
            return
        self._maybe_nack()
        nonce = self.nonce_prefix + struct.pack(">Q", seq)
        try:
            chunk = self.aes.decrypt(nonce, data[HEADER_SIZE:], header)
        except Exception:
            return
        self.bytes += len(data)
        now = time.monotonic()
        entry = self.pending.get(frame_id)
        if entry is None:
            if self.last_frame_id and frame_id > self.last_frame_id + 1:
                self.need_keyframe = True
                self._request_keyframe()
                for key in [item for item in self.pending if item < frame_id]:
                    self.pending.pop(key, None)
                    self.lost += 1
            entry = {
                "parts": [None] * frag_count,
                "got": 0,
                "count": frag_count,
                "flags": flags,
                "at": now,
            }
            self.pending[frame_id] = entry
        elif entry["count"] != frag_count:
            return
        if entry["parts"][frag_index] is not None:
            return
        entry["parts"][frag_index] = chunk
        entry["got"] += 1
        if entry["got"] != entry["count"]:
            return
        payload = b"".join(entry["parts"])
        self.pending.pop(frame_id, None)
        self.last_frame_id = max(self.last_frame_id, frame_id)
        self._deliver(payload, bool(flags & FLAG_KEY), bool(flags & FLAG_RAW))

    def _deliver(self, payload: bytes, keyframe: bool, raw: bool):
        if raw:
            try:
                expected = self.width * self.height * 3
                rgb = np.frombuffer(zlib.decompress(payload), dtype=np.uint8)
                if rgb.size != expected:
                    raise ValueError("快照尺寸不符")
                rgb = rgb.reshape(self.height, self.width, 3).copy()
            except Exception:
                self.decode_errors += 1
                self._request_keyframe()
                return
            self.need_keyframe = False
            if self.decoder:
                self.decoder.reset()
            self.frames += 1
            if self.on_snapshot:
                self.on_snapshot(rgb)
            return
        if self.need_keyframe and not keyframe:
            self.lost += 1
            return
        if keyframe:
            self.need_keyframe = False
        with self.decoder_lock:
            if not self.decoder:
                return
            # 关键帧不再重建解码器：IDR 本身就能让解码器重置状态，
            # 每秒重建一次会带来 5~10ms 的停顿，看起来就是周期性的卡一下。
            started = time.perf_counter()
            decoded = self.decoder.decode(payload)
        spent = (time.perf_counter() - started) * 1000.0
        self.dec_ema = spent if self.dec_ema <= 0 else self.dec_ema * 0.85 + spent * 0.15
        if not decoded:
            self.decode_errors += 1
            self.need_keyframe = True
            self._request_keyframe()
            return
        for rgb in decoded:
            self.frames += 1
            if self.on_frame:
                self.on_frame(rgb)
