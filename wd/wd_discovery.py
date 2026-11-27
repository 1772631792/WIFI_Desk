"""UDP 局域网自动发现：仅传设备元数据，不传密钥或画面。"""
import json
import os
import random
import socket
import subprocess
import time

from PySide6.QtCore import QObject, Signal

from .wd_protocol import discovery_datagram, parse_datagram

_WIFI_CACHE = {"at": 0.0, "ips": []}


def run_quiet(args, timeout: float = 2.0) -> bytes:
    """执行系统命令且不弹控制台窗口。

    窗口模式（pythonw / PyInstaller --windowed）下直接 spawn 子进程会闪出黑框，
    必须显式指定 CREATE_NO_WINDOW 并隐藏窗口。
    """
    kwargs = {"stderr": subprocess.DEVNULL, "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startup
    return subprocess.check_output(args, **kwargs)


def _usable_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if a == 127 or (a == 169 and b == 254):
        return False
    if a == 100 and 64 <= b <= 127:  # CGNAT / Tailscale 等虚拟网卡
        return False
    return True


def wifi_ips():
    """返回已连接 WiFi 网卡的 IPv4；双网卡时优先走 WiFi。"""
    if os.name != "nt":
        return []
    now = time.monotonic()
    # netsh 探测要起子进程，缓存久一点：WiFi 地址在会话期间几乎不会变。
    if now - _WIFI_CACHE["at"] < 30.0:
        return _WIFI_CACHE["ips"]
    ips = _probe_wifi_ips()
    _WIFI_CACHE.update(at=now, ips=ips)
    return ips


def _probe_wifi_ips():
    import re

    try:
        info = run_quiet(["netsh", "wlan", "show", "interfaces"]).decode("utf-8", "ignore")
        if "已连接" not in info and "connected" not in info.lower():
            return []
        name = ""
        for line in info.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip() in ("名称", "Name"):
                name = value.strip()
                break
        if not name:
            return []
        addresses = run_quiet(
            ["netsh", "interface", "ipv4", "show", "addresses", f"name={name}"]
        ).decode("utf-8", "ignore")
        candidates = set(re.findall(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", addresses))
        known = set(local_ips())
        return sorted(ip for ip in candidates if ip in known)
    except (OSError, subprocess.SubprocessError):
        return []


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if _usable_ip(ip):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ip for ip in ips if _usable_ip(ip))


def discovery_ips():
    """发现使用的本机地址：有 WiFi 就只用 WiFi，否则退回普通局域网地址。"""
    wifi = wifi_ips()
    return wifi if wifi else local_ips()


def primary_ip():
    ips = discovery_ips()
    return ips[0] if ips else "127.0.0.1"


class DiscoveryWorker(QObject):
    device_found = Signal(dict)
    device_lost = Signal(str)
    log = Signal(str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.running = True
        self.force_probe = False
        self.devices = {}
        self.thread = None

    def probe_now(self):
        self.force_probe = True

    def start(self):
        self.thread = __import__("threading").Thread(target=self._run, daemon=True, name="wd-discovery")
        self.thread.start()

    def stop(self):
        self.running = False

    def _pid(self):
        return {
            "m": "WFD-DISC", "v": 1,
            "id": self.config.device_id,
            "name": self.config.device_name,
            "role": self.config.role,
            "port": int(self.config.get("service_port", 47382)),
            "fp": self.config.fingerprint,
            "caps": {"tile": True, "jpeg": True, "input": True},
        }

    def _targets(self):
        targets = {"255.255.255.255"}
        for ip in discovery_ips():
            parts = ip.split(".")
            if len(parts) == 4:
                targets.add(".".join(parts[:3] + ["255"]))
        return sorted(targets)

    def _run(self):
        port = int(self.config.get("discovery_port", 47381))
        local = discovery_ips()
        bind_ip = local[0] if local else ""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((bind_ip, port))
        except OSError as exc:
            self.log.emit(f"发现端口 {port} 绑定失败：{exc}")
            return
        sock.settimeout(0.2)
        via = "WiFi" if wifi_ips() else "局域网"
        self.log.emit(f"发现服务已启动（{via}），端口 {port}，本机 {bind_ip or primary_ip()}")
        next_probe = time.monotonic()
        while self.running:
            now = time.monotonic()
            if now >= next_probe or self.force_probe:
                self.force_probe = False
                if self.config.may_control():
                    packet = discovery_datagram({**self._pid(), "kind": "probe"})
                    for target in self._targets():
                        try:
                            sock.sendto(packet, (target, port))
                        except OSError:
                            pass
                next_probe = now + random.uniform(2.0, 4.0)
            try:
                raw, addr = sock.recvfrom(2048)
            except socket.timeout:
                self._expire()
                continue
            except OSError:
                break
            try:
                obj = parse_datagram(raw)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict) or obj.get("id") == self.config.device_id:
                continue
            if obj.get("kind") == "probe":
                if self.config.may_be_controlled() and self.config.get("allow_connection"):
                    try:
                        sock.sendto(discovery_datagram({**self._pid(), "kind": "reply"}), addr)
                    except OSError:
                        pass
            elif obj.get("kind") == "reply" and self.config.may_control():
                info = {
                    "id": str(obj.get("id", "")),
                    "name": str(obj.get("name", "未知设备")),
                    "role": str(obj.get("role", "")),
                    "ip": addr[0],
                    "port": int(obj.get("port", 0)),
                    "fp": str(obj.get("fp", "")),
                    "caps": obj.get("caps", {}) if isinstance(obj.get("caps"), dict) else {},
                    "seen": time.monotonic(),
                }
                if not info["id"] or not info["port"] or len(info["fp"]) != 64:
                    continue
                is_new = info["id"] not in self.devices
                self.devices[info["id"]] = info
                self.device_found.emit(dict(info))
                if is_new:
                    self.log.emit(f"发现设备 {info['name']} @ {info['ip']}")
            self._expire()
        sock.close()

    def _expire(self):
        now = time.monotonic()
        dead = [key for key, value in self.devices.items() if now - value.get("seen", 0) > 15]
        for key in dead:
            self.devices.pop(key, None)
            self.device_lost.emit(key)
