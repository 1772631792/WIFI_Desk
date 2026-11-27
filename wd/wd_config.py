"""本地配置：角色、授权、可信设备与传输参数。"""
import json
import os
import socket
import threading
import uuid

from . import wd_crypto

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WiFiDesk")

DEFAULTS = {
    "role": "both",
    "device_name": socket.gethostname(),
    "allow_connection": False,
    "auth_mode": "confirm",
    "discovery_port": 47381,
    "service_port": 47382,
    "theme": "dark",
    "quality": "auto",
    "media": "auto",
    "codec": "auto",
    "encoder": "",          # 上次实测选中的编码器，重连时直接用，省掉 2 秒选型
    "known_devices": {},
}


class Config:
    def __init__(self):
        os.makedirs(APP_DIR, exist_ok=True)
        self.path = os.path.join(APP_DIR, "config.json")
        # 会话线程（编码器选型回写）和界面线程都会写配置，落盘要串起来。
        self._lock = threading.RLock()
        data = dict(DEFAULTS)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data.update(loaded)
            except (OSError, ValueError):
                pass
        self.data = data
        if self.data.get("role") not in ("controller", "controlled", "both"):
            self.data["role"] = "both"
        if not self.data.get("device_id"):
            self.data["device_id"] = uuid.uuid4().hex
        self.cert_path, self.key_path, self.fingerprint, self.cert_pem = wd_crypto.ensure_material(
            APP_DIR, self.data["device_id"]
        )
        self.save()

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self.data[key] = value
            self.save()

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    @property
    def device_id(self):
        return self.data["device_id"]

    @property
    def device_name(self):
        return self.data["device_name"]

    @property
    def role(self):
        return self.data.get("role")

    def may_control(self):
        return self.role in ("controller", "both")

    def may_be_controlled(self):
        return self.role in ("controlled", "both")

    def is_auto_trusted(self, device_id: str, fingerprint: str) -> bool:
        if self.data.get("auth_mode") != "trusted":
            return False
        entry = self.data.get("known_devices", {}).get(device_id)
        return bool(entry and entry.get("auto") and entry.get("fingerprint") == fingerprint)

    def remember(self, device_id: str, name: str, fingerprint: str, auto: bool = False):
        known = self.data.setdefault("known_devices", {})
        entry = known.get(device_id, {})
        entry.update({"name": name, "fingerprint": fingerprint, "last_seen": 1})
        if auto:
            entry["auto"] = True
        known[device_id] = entry
        self.save()

    def forget(self, device_id: str):
        self.data.get("known_devices", {}).pop(device_id, None)
        self.save()
