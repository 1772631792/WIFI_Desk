"""定长头的轻量二进制控制协议；图像走独立压缩块，不传 Base64。"""
import collections
import hashlib
import json
import os
import select
import socket
import struct
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"WFD1"
VERSION = 1
SEND_CHUNK_SIZE = 256 * 1024

# 发现报文加密：同一局域网的其它扫描器只能看到随机字节，只有 WiFiDesk 能解开。
# 这不是给攻击者的强保证（密钥在程序里），目的是让设备名/端口/指纹不裸奔，
# 并挡住随手伪造的假设备；真正的身份校验仍由 TLS 证书指纹完成。
_DISC_MAGIC = b"WFD2"
_DISC_SECRET = b"wifi-desk/discovery/2f9a1c07c4e35b81"
_DISC_AES = AESGCM(hashlib.sha256(_DISC_SECRET).digest())
_DISC_NONCE = 12


def _pending(sock) -> int:
    """TLS 握手/读取可能把应用数据缓存在 SSL 对象里，select 看不到这些字节。"""
    try:
        return int(sock.pending())
    except (AttributeError, OSError, ValueError):
        return 0


def recv_exact(sock, n: int, idle_timeout: float = 30.0) -> bytes:
    """优先读 SSL 缓冲，再等可读，避免一个线程阻塞在 SSL_read 时另一个线程写入。

    TLS 的 SSL 对象不允许并发的阻塞读和写；Windows 上这种写法会让连接
    被对端直接断掉。这里只在确认可读时才调用 recv，正常情况下不会长时间
    停在 SSL_read 内部。
    """
    chunks = []
    left = n
    deadline = time.monotonic() + idle_timeout
    while left > 0:
        if _pending(sock) <= 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("接收超时，对端长时间未发送数据")
            try:
                ready, _, _ = select.select([sock], [], [], min(remaining, 0.5))
            except (OSError, ValueError) as exc:
                raise ConnectionError(f"连接已关闭（{exc}）") from exc
            if not ready:
                continue
        try:
            part = sock.recv(left)
        except socket.timeout:
            continue
        if not part:
            raise ConnectionError("连接已关闭")
        chunks.append(part)
        left -= len(part)
        deadline = time.monotonic() + idle_timeout
    return b"".join(chunks)


def send_msg(sock, msg_type: str, payload: bytes = b"", **meta) -> None:
    header_obj = {**meta, "t": msg_type, "p": len(payload), "v": VERSION}
    if msg_type in ("PING", "PONG"):
        # 旧客户端用 t 读取时间戳，同时也用它识别类型。
        # 保留数值字段，让旧版本安全跳过心跳。
        header_obj["t"] = meta.get("t", 0)
        header_obj["heartbeat"] = msg_type
    header = json.dumps(header_obj, ensure_ascii=False).encode("utf-8")
    prefix = struct.pack(">II", len(header), len(payload)) + header
    if len(prefix) + len(payload) <= SEND_CHUNK_SIZE:
        sock.sendall(prefix + payload)
        return
    sock.sendall(prefix)
    # 每段单独计时，慢链路仍能持续发送大帧。
    data = memoryview(payload)
    for offset in range(0, len(data), SEND_CHUNK_SIZE):
        sock.sendall(data[offset:offset + SEND_CHUNK_SIZE])


class MessageSender:
    """单写者发送队列：视频帧可丢弃，控制/心跳不丢弃。

    TCP 上让采集线程直接 sendall 会让慢链路的旧帧无限排队，最终心跳和
    输入也被堵住。参考串流项目的做法，这里只保留最新的一帧视频，控制
    消息始终排队（必要时插队），写 socket 只发生在同一个线程里。
    """

    def __init__(self, sock, on_error=None, max_frames: int = 1):
        self.sock = sock
        self.on_error = on_error
        self.max_frames = max(1, int(max_frames))
        self._items = collections.deque()
        self._cond = threading.Condition()
        self._video_count = 0
        self.alive = True
        self.dropped_frames = 0
        self.sent_bytes = 0
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="wd-sender")
            self._thread.start()

    def stop(self):
        with self._cond:
            self.alive = False
            self._items.clear()
            self._video_count = 0
            self._cond.notify_all()

    def send(self, msg_type: str, payload: bytes = b"", video: bool = False,
             priority: bool = False, **meta) -> bool:
        return self.send_batch(((msg_type, payload, meta),), video=video, priority=priority)

    def send_batch(self, messages, video: bool = False, priority: bool = False) -> bool:
        batch = [(item[0], item[1], item[2] if len(item) > 2 else {}) for item in messages]
        with self._cond:
            if not self.alive or not batch:
                return False
            if video:
                while self._video_count >= self.max_frames:
                    if not self._drop_oldest_video_locked():
                        break
                self._video_count += 1
            item = (batch, video)
            if priority:
                self._items.appendleft(item)
            else:
                self._items.append(item)
            self._cond.notify()
            return True

    def pending_frames(self) -> int:
        with self._cond:
            return self._video_count

    def flush(self, timeout: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cond:
                if not self._items:
                    return True
            time.sleep(0.01)
        return False

    def _drop_oldest_video_locked(self) -> bool:
        for index, (_batch, is_video) in enumerate(self._items):
            if is_video:
                del self._items[index]
                self._video_count -= 1
                self.dropped_frames += 1
                return True
        return False

    def _run(self):
        while True:
            with self._cond:
                while not self._items and self.alive:
                    self._cond.wait(0.5)
                if not self.alive:
                    return
                batch, video = self._items.popleft()
                if video:
                    self._video_count -= 1
            try:
                for msg_type, payload, meta in batch:
                    send_msg(self.sock, msg_type, payload, **meta)
                    with self._cond:
                        self.sent_bytes += len(payload)
            except (OSError, ValueError) as exc:
                with self._cond:
                    self.alive = False
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except Exception:
                        pass
                return


def recv_msg(sock, idle_timeout: float = 30.0):
    hlen, plen = struct.unpack(">II", recv_exact(sock, 8, idle_timeout))
    if hlen > 8192 or plen > 128 * 1024 * 1024:
        raise ValueError("消息长度超出限制")
    header = json.loads(recv_exact(sock, hlen, idle_timeout).decode("utf-8"))
    payload = recv_exact(sock, plen, idle_timeout) if plen else b""
    return header.get("heartbeat", header.get("t", "")), header, payload


def discovery_datagram(obj: dict) -> bytes:
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(raw) > 400:
        raise ValueError("发现报文过大")
    nonce = os.urandom(_DISC_NONCE)
    return _DISC_MAGIC + nonce + _DISC_AES.encrypt(nonce, raw, _DISC_MAGIC)


def parse_datagram(raw: bytes) -> dict:
    head = len(_DISC_MAGIC) + _DISC_NONCE
    if len(raw) < head + 16 or len(raw) > 512 or not raw.startswith(_DISC_MAGIC):
        raise ValueError("发现报文无效")
    nonce = raw[len(_DISC_MAGIC):head]
    try:
        plain = _DISC_AES.decrypt(nonce, raw[head:], _DISC_MAGIC)
    except Exception as exc:
        raise ValueError("发现报文无法解密") from exc
    obj = json.loads(plain.decode("utf-8"))
    if obj.get("m") != "WFD-DISC" or obj.get("v") != VERSION:
        raise ValueError("发现报文无效")
    return obj
