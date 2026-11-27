# WiFiDesk 代码调用链文档（AI 接手用）

本文面向接手 AI 或二次开发者，描述**源码级调用链**：入口、模块依赖、线程模型、关键流程、信号与完整函数索引。所有行号对应当前工作区代码。

---

## 0. 快速定位

| 目标 | 位置 |
| --- | --- |
| 程序入口 | `main.py:54 main()`、`main.py:79 if __name__ == "__main__"` |
| 自检入口 | `main.py:26 self_test()`，命令 `python main.py --self-test` |
| 主界面 | `wd/wd_ui.py:587 MainWindow` |
| 远程窗口 | `wd/wd_ui.py:297 RemoteWindow` |
| 设备发现 | `wd/wd_discovery.py:107 DiscoveryWorker` |
| 被控服务 | `wd/wd_session.py:29 ServerSession`、`wd/wd_session.py:101 ClientHandler` |
| 主控连接 | `wd/wd_session.py:309 ClientSession` |
| 二进制协议 | `wd/wd_protocol.py` |
| 发送队列 | `wd/wd_protocol.py:MessageSender` |
| 采集编码 | `wd/wd_capture.py:21 ScreenCapture` |
| 自适应画质 | `wd/wd_capture.py:AdaptiveQuality` |
| 加密证书 | `wd/wd_crypto.py` |
| 配置 | `wd/wd_config.py:25 Config` |
| 键鼠注入 | `wd/wd_input.py` |
| 端口 | UDP `47381` 发现，TCP `47382` 远控 |
| 配置目录 | `%APPDATA%\WiFiDesk`（`wd/wd_config.py:9 APP_DIR`） |

### 0.1 稳定性与画质增强

参考 RustDesk 的 QoS/双通道思路与串流项目的“只留最新帧”策略，在现有 TCP + TLS 链路上做了增量改造：

- `MessageSender`：单线程写 socket，控制/心跳插队，视频只保留最新一帧；慢链路不再让旧帧无限堆积或堵住输入。
- 独立 1 秒心跳：服务端/客户端都持续发送 `PING`，并在接收端记录 `rtt`；断线不再依赖“恰好没有画面变化”。
- `AdaptiveQuality`：按 RTT、丢帧数做迟滞调节，先降帧率再降 JPEG 质量；网络恢复后慢速回升，避免画质来回跳。
- `ScreenCapture`：优先使用 DXGI Desktop Duplication（`dxcam`），修复浏览器/Electron/视频等硬件加速窗口在 GDI 采集下闪黑块的问题；DXGI 不可用时自动回退 PIL 并在日志中标注。
- 帧序号 + `full` 标记：发送队列丢帧后，主控端发现序号断层会请求 `RESYNC`，并在整帧到达前忽略增量瓦片，不会把增量画到旧画布上。
- 短时 `resume` 凭据：Wi-Fi 抖动重连时，120 秒内同一证书身份免二次授权；UI 自动重连改为指数退避（2/4/8/16/30 秒）。
- 新增“超清”预设（`sharp`，JPEG 98 + 4:4:4 + 小瓦片），默认预设也提高了压缩质量；窗口能放下原分辨率时默认 1:1 显示。

---

## 1. 启动调用链

```text
main.py:79 __main__
└─ main.py:54 main()
   ├─ argparse.ArgumentParser / parse_args()          # 解析 --self-test
   ├─ wd.wd_config.Config.__init__                    # 加载/创建配置
   │  ├─ wd_crypto.ensure_material()                  # 校验证书/生成证书
   │  │  ├─ _generate_certificate()                   # 仅首次运行（RSA 2048）
   │  │  ├─ protect() → _dpapi()                      # DPAPI 加密私钥
   │  │  └─ ssl.PEM_cert_to_DER_cert + hashlib.sha256 # 计算指纹
   │  └─ Config.save()                                # 写 config.json
   ├─ [if --self-test] self_test()
   │  ├─ Config()
   │  ├─ ScreenCapture.grab()（DXGI，失败回退 PIL/GDI）
   │  ├─ wd_crypto.tls_context(server=True/False)
   │  └─ socket.bind(UDP 47381) 检测端口
   ├─ QApplication(sys.argv)
   ├─ QIcon(resource_path("icon.png"))
   ├─ wd_theme.apply_theme(app, theme)
   │  ├─ tokens() / qss()
   │  └─ app.setStyle / setPalette / setStyleSheet
   ├─ wd_ui.MainWindow.__init__(config)
   │  ├─ DiscoveryWorker(config)
   │  ├─ ServerSession(config)
   │  ├─ _build()        # 构建 UI；启动 wd-network 线程调用 network_status()
   │  ├─ _wire()         # 连接按钮/组合框信号
   │  ├─ apply_role()    # 按角色启动/停止 ServerSession
   │  └─ DiscoveryWorker.start()
   ├─ window.show() → MainWindow.showEvent() → QPropertyAnimation 淡入
   └─ app.exec()          # Qt 事件循环
```

---

## 2. 模块依赖图

```text
main.py
├─ wd.wd_config ──→ wd.wd_crypto ──→ cryptography(懒加载) / ctypes / ssl
├─ wd.wd_theme  ──→ PySide6
└─ wd.wd_ui
   ├─ wd.wd_theme
   ├─ wd.wd_discovery ──→ wd.wd_protocol
   ├─ wd.wd_input ──────→ ctypes / user32
   └─ wd.wd_session
      ├─ wd.wd_config / wd.wd_crypto / wd.wd_input
      ├─ wd.wd_protocol
      └─ wd.wd_capture (懒加载) ──→ cv2 / numpy / PIL.ImageGrab
```

注意：`wd_session._capture()` 是懒加载入口；`wd_crypto._generate_certificate()` 内部才导入 `cryptography`，这样启动路径不加载重模块。

---

## 3. 线程与信号模型

| 线程名 | 入口 | 职责 |
| --- | --- | --- |
| Qt 主线程 | `main()` → `app.exec()` | 所有 QWidget、信号槽、UI 刷新 |
| `wd-network` | `MainWindow._build()` 创建的 Thread | 调用 `network_status()` 获取 IP/SSID |
| `wd-discovery` | `DiscoveryWorker.start()` | UDP 广播发现/应答 |
| `wd-accept` | `ServerSession.start()` | TCP accept 循环 |
| `wd-client-handler` | `ServerSession._accept_loop()` | 每个被控连接一个：`ClientHandler.run()` |
| `wd-ctrl-recv` | `ClientHandler.run()` | 被控端接收主控指令 |
| `wd-controller` | `ClientSession.start()` | 每个主控连接一个：`ClientSession._run()` 握手与 PING |
| `wd-frame-recv` | `ClientSession._run()` | 主控端接收画面帧 |

跨线程通信全部用 Qt Signal：

- `DiscoveryWorker`：device_found / device_lost / log
- `ServerSession`：auth_required / session_started / session_ended / stats / log
- `ClientSession`：connected / disconnected / frame_ready / stats / state / log

---

## 4. 关键调用链

### 4.1 设备发现（WiFi 优先）

```text
MainWindow.__init__
└─ DiscoveryWorker.start() → Thread(target=_run)
   └─ DiscoveryWorker._run()
      ├─ discovery_ips() → wifi_ips() → _probe_wifi_ips()
      │  ├─ netsh wlan show interfaces      # 找已连接 WLAN 名称
      │  └─ netsh interface ipv4 show addresses name=<WLAN>
      ├─ sock.bind((WiFi IP, 47381))        # 关键：绑定 WiFi 网卡，避免 10022
      ├─ 每 2~4 秒：
      │  ├─ _targets() → discovery_ips() → 255.255.255.255 / 192.168.x.255
      │  └─ sendto(discovery_datagram(_pid()))
      ├─ recvfrom()
      ├─ parse_datagram()
      ├─ kind=probe  → sendto(reply)（仅允许被控时）
      ├─ kind=reply  → device_found.emit(info)
      └─ _expire() → device_lost.emit(id)
```

`MainWindow` 侧：

```text
device_found → MainWindow.on_device_found() → refresh_devices()
device_lost  → MainWindow.on_device_lost()  → refresh_devices()
log          → MainWindow.append_log()
refresh_devices() → DeviceRow(...) → connect_requested → MainWindow.on_connect()
```

### 4.2 主控连接与握手

```text
用户点击「连接桌面」
└─ MainWindow.on_connect(device)
   ├─ 旧 client.stop()（如有）
   ├─ ClientSession(config, device, quality)
   ├─ 连接信号：connected / disconnected / state / log
   └─ ClientSession.start() → Thread(target=_run)
      └─ _run()
         ├─ state.emit("正在建立加密连接…")
         ├─ socket.create_connection((device["ip"], device["port"]), timeout=5)
         ├─ send_msg("HELLO_PLAIN", id/name/fp/cert)
         ├─ recv_msg() → TLS_READY
         ├─ wd_crypto.fingerprint(server_pem) 与 device["fp"] 比对
         ├─ wd_crypto.tls_context(client, peer_pem=server_pem) → wrap_socket
         ├─ send_msg("HELLO", id/name/fp/quality)
         ├─ recv_msg() → HELLO_OK(w/h/fps/name)
         ├─ QImage(w,h,Format_RGB888) 初始化画布
         ├─ connected.emit() → MainWindow.on_connected()
         │  └─ RemoteWindow(client, device, config, info, main)
         ├─ Thread(target=_recv_loop)  # wd-frame-recv
         ├─ send_msg("QUALITY", preset)
         └─ while alive: send_msg("PING") 每 2 秒
```

### 4.3 主控收帧与渲染

```text
ClientSession._recv_loop()
├─ recv_msg(sock)  → wd_protocol.recv_exact()  # 超时重试，避免协议错位
├─ FRAME_TILES → _apply_tiles(meta, payload)
│  └─ zlib.decompress → QImage(raw,...) → painter.drawImage(canvas)
│     失败 → resync() → send_msg("RESYNC")
├─ FRAME_JPEG  → QImage.fromData(payload) → convertToFormat(RGB888)
├─ PING → _send("PONG", t=...)
├─ PONG → rtt_ms = now - t
├─ frame_ready.emit(canvas.copy())
└─ _publish_stats() → stats.emit({mode,fps,kbps,rtt,w,h})
   └─ RemoteWindow.on_stats() → 更新工具栏/状态面板
```

`RemoteWindow.set_frame()`：

```text
frame_ready → set_frame(img) → update_view()
└─ QPixmap.fromImage(canvas).scaled(...) → view.setPixmap()
```

### 4.4 被控会话与画面发送

```text
MainWindow.apply_role()
└─ ServerSession.start() → Thread(target=_accept_loop)  # wd-accept
   └─ _accept_loop()
      ├─ listener.accept()
      └─ Thread(target=ClientHandler.run)  # wd-client-handler
         └─ ClientHandler.run()
            ├─ recv_msg() → HELLO_PLAIN
            ├─ wd_crypto.fingerprint(client_pem) 校验
            ├─ auth_mode=auto 或 is_auto_trusted() → approved
            │  其他模式 → auth_required.emit() → MainWindow.on_auth_required()
            │              → PairDialog.exec() → ServerSession.resolve_auth()
            ├─ wd_crypto.tls_context(server, peer_pem=client_pem) → wrap_socket
            ├─ send_msg("TLS_READY", cert=config.cert_pem)
            ├─ recv_msg() → HELLO
            ├─ config.remember(id, name, fp, auto)
            ├─ screen = self.capture     # 同一个采集对象，RESYNC/QUALITY 才能生效
            ├─ send_msg("HELLO_OK", w/h/fps/name)
            ├─ session_started.emit()
            ├─ Thread(target=_recv_loop)  # wd-ctrl-recv
            └─ _send_loop(screen)
               ├─ screen.grab()          → dxcam DXGI + np.asarray（回退 ImageGrab）
               ├─ screen.diff(cur)       → 返回脏块 tiles
               ├─ tiles 多 → screen.encode_jpeg() → send_msg("FRAME_JPEG")
               ├─ tiles 少 → screen.pack_tiles() → send_msg("FRAME_TILES")
               ├─ 无变化每 2 秒 → send_msg("PING")
               └─ stats.emit() 每秒
```

### 4.5 输入注入链

```text
RemoteView.mousePressEvent / mouseMoveEvent / mouseReleaseEvent / wheelEvent
└─ RemoteWindow.mouse_press / mouse_move / mouse_release / mouse_wheel
   ├─ native_pos(pos)  # 屏幕坐标归一化
   └─ ClientSession.send_input(event) → _send("INPUT", json)
      └─ [网络] ClientHandler._recv_loop()
         └─ typ == "INPUT" → wd_input.inject(json)
            ├─ "move"  → mouse_move()  → _send() → user32.SendInput
            ├─ "down"/"up" → mouse_button() → _send()
            ├─ "wheel" → mouse_wheel() → _send()
            ├─ "key"  → key() → _send()
            └─ "reset" → release_all 类指令
```

键盘：

```text
RemoteView.keyPressEvent → RemoteWindow.key_press()
└─ qt_key_to_vk() → ClientSession.send_input({"a":"key","vk":...,"down":True})
```

失焦释放：

```text
RemoteView.focusOutEvent → RemoteWindow.release_all() → _send("reset")
```

### 4.6 画质切换 / RESYNC

```text
RemoteWindow.quality_combo.currentIndexChanged
└─ ClientSession.set_quality(preset) → _send("QUALITY", preset=preset)
   └─ ClientHandler._recv_loop()
      ├─ capture.set_preset(preset)
      └─ 后续 screen.params 生效

ClientSession._recv_loop 发现瓦片解码失败
└─ resync() → _send("RESYNC")
   └─ ClientHandler._recv_loop()
      └─ self.capture.prev = None   # 下一帧整屏重发
```

### 4.7 断线重连链

```text
任意一端断开
→ ClientSession._run() finally: disconnected.emit(reason)
→ RemoteWindow._on_disconnected(reason)
   ├─ release_all()
   ├─ 显示 status_overlay「连接中断…2 秒后自动重连」
   └─ QTimer.singleShot(2000, _try_reconnect)
      └─ RemoteWindow._try_reconnect()
         └─ main.reconnect_client(device, old_client)
            ├─ MainWindow.on_connect(device)
            │  ├─ old.stop()
            │  └─ 创建全新 ClientSession 并连接信号
            └─ RemoteWindow.bind_client(new_client)
               ├─ 断开旧信号、old.stop()
               ├─ 绑定新信号
               ├─ canvas = QImage()
               └─ status_overlay.hide()
```

设计要点：不要复用旧 `ClientSession`，否则旧接收线程可能把新连接关掉。

### 4.8 设置与主题

```text
MainWindow.open_settings()
└─ SettingsDialog(config).exec()
   ├─ _build() 构建控件
   ├─ _load()
   │  ├─ _update_auth_hint()
   │  └─ _refresh_trusted()
   └─ accept()
      ├─ Config.set(role/allow_connection/auth_mode/quality/theme/ports)
      └─ SettingsDialog.accept
→ wd_theme.apply_theme(app, theme) → qss() → app.setStyleSheet()
→ MainWindow.apply_role() → ServerSession.start()/stop()
```

### 4.9 关闭清理

```text
MainWindow.closeEvent()
├─ client.stop()
├─ 所有 RemoteWindow.close()
│  └─ release_all() + client.stop()
├─ server.stop()
├─ discovery.stop()
└─ super().closeEvent()
```

---

## 5. 协议消息类型

`wd/wd_protocol.py` 使用 8 字节头：`>II`（header_len, payload_len），header 为 JSON。

| 方向 | 类型 | 关键字段 |
| --- | --- | --- |
| 主控→被控 | `HELLO_PLAIN` | id, name, fp, cert(PEM), resume |
| 被控→主控 | `TLS_READY` | cert(PEM) |
| 主控→被控 | `HELLO` | id, name, fp, quality |
| 被控→主控 | `HELLO_OK` | session_id, epoch, w, h, fps, name, resume |
| 被控→主控 | `FRAME_TILES` | tiles, w, h, epoch, seq, mode, full, qos, qf；payload 为各块 zlib 数据 |
| 被控→主控 | `FRAME_JPEG` | w, h, epoch, seq, mode, qos, qf；payload 为 JPEG |
| 双向 | `PING` / `PONG` | t（单调时钟）；PING 另带 rtt |
| 主控→被控 | `INPUT` | JSON payload：a=move/down/up/wheel/key/reset |
| 主控→被控 | `QUALITY` | preset |
| 主控→被控 | `RESYNC` | - |
| 双向 | `DISCONNECT` | - |
| 被控→主控 | `AUTH_DENIED` | reason |

UDP 发现报文：`{"m":"WFD-DISC","v":1,"kind":"probe|reply","id","name","role","port","fp","caps"}`。

---

## 6. 完整函数索引

以下内容由 AST 从源码抽取，列出每个函数/方法的**直接调用**（已过滤纯语法节点和部分内建函数）。

### main.py

- `main.py:7 _report()` → `path.join`、`path.dirname`、`f.write`
- `main.py:18 resource_path()` → `path.join`、`path.dirname`、`path.abspath`
- `main.py:26 self_test()` → `Config`、`ImageGrab.grab`、`tls_context`、`socket.socket`、`_report`、`sock.bind`、`sock.close`、`config.get`
- `main.py:54 main()` → `argparse.ArgumentParser`、`parser.add_argument`、`parser.parse_args`、`Config`、`QApplication`、`app.setApplicationName`、`app.setWindowIcon`、`wd_theme.apply_theme`、`MainWindow`、`window.show`、`app.exec`、`self_test`、`QIcon`、`resource_path`、`config.get`

### wd/wd_capture.py

- `wd/wd_capture.py:21 ScreenCapture.__init__()` → `PRESETS.get`
- `wd/wd_capture.py:25 ScreenCapture.set_preset()` → `PRESETS.get`
- `wd/wd_capture.py:29 ScreenCapture.grab()` → `np.asarray`、`ImageGrab.grab`
- `wd/wd_capture.py:32 ScreenCapture.diff()` → `cur.copy`、`np.array_equal`、`changed.append`
- `wd/wd_capture.py:53 ScreenCapture.snapshot()` → `cur.copy`
- `wd/wd_capture.py:62 ScreenCapture.pack_tiles()` → `tobytes`、`zlib.compress`、`meta.append`、`parts.append`、`join`
- `wd/wd_capture.py:73 ScreenCapture.encode_jpeg()` → `cv2.cvtColor`、`cv2.imencode`、`buf.tobytes`、`RuntimeError`

### wd/wd_config.py

- `wd/wd_config.py:25 Config.__init__()` → `os.makedirs`、`path.join`、`path.exists`、`wd_crypto.ensure_material`、`self.save`、`data.get`、`uuid.uuid4`、`json.load`、`data.update`
- `wd/wd_config.py:47 Config.get()` → `data.get`
- `wd/wd_config.py:50 Config.set()` → `self.save`
- `wd/wd_config.py:54 Config.save()` → `os.replace`、`json.dump`
- `wd/wd_config.py:61 Config.device_id()` → （无外部调用）
- `wd/wd_config.py:65 Config.device_name()` → （无外部调用）
- `wd/wd_config.py:69 Config.role()` → `data.get`
- `wd/wd_config.py:72 Config.may_control()` → （无外部调用）
- `wd/wd_config.py:75 Config.may_be_controlled()` → （无外部调用）
- `wd/wd_config.py:78 Config.is_auto_trusted()` → `get`、`data.get`、`entry.get`
- `wd/wd_config.py:84 Config.remember()` → `data.setdefault`、`known.get`、`entry.update`、`self.save`
- `wd/wd_config.py:93 Config.forget()` → `pop`、`self.save`、`data.get`

### wd/wd_crypto.py

- `wd/wd_crypto.py:19 _dpapi()` → `ctypes.create_string_buffer`、`DATA_BLOB`、`fn`、`ctypes.string_at`、`kernel32.LocalFree`、`ctypes.cast`、`ctypes.byref`、`OSError`、`ctypes.POINTER`、`ctypes.get_last_error`
- `wd/wd_crypto.py:36 protect()` → `_dpapi`
- `wd/wd_crypto.py:40 unprotect()` → `_dpapi`
- `wd/wd_crypto.py:44 _generate_certificate()` → `rsa.generate_private_key`、`x509.Name`、`replace`、`sign`、`key.private_bytes`、`cert.public_bytes`、`hashes.SHA256`、`serialization.NoEncryption`、`x509.NameAttribute`、`datetime.now`、`add_extension`、`x509.SubjectAlternativeName`、`not_valid_after`、`x509.DNSName`、`not_valid_before`、`datetime.timedelta`、`serial_number`、`x509.random_serial_number`、`public_key`、`key.public_key`、`issuer_name`、`subject_name`、`x509.CertificateBuilder`
- `wd/wd_crypto.py:74 ensure_material()` → `os.makedirs`、`path.join`、`ssl.PEM_cert_to_DER_cert`、`hexdigest`、`_generate_certificate`、`f.read`、`path.exists`、`f.write`、`hashlib.sha256`、`protect`
- `wd/wd_crypto.py:91 tls_context()` → `path.join`、`tempfile.mkstemp`、`unprotect`、`os.fdopen`、`f.write`、`ssl.SSLContext`、`ctx.load_cert_chain`、`f.read`、`ctx.load_verify_locations`、`path.exists`、`os.remove`
- `wd/wd_crypto.py:130 fingerprint()` → `hexdigest`、`ssl.PEM_cert_to_DER_cert`、`hashlib.sha256`
- `wd/wd_crypto.py:138 short_code()` → `rstrip`、`join`、`bytes.fromhex`、`decode`、`base64.b32encode`

### wd/wd_discovery.py

- `wd/wd_discovery.py:15 _usable_ip()` → `ip.split`
- `wd/wd_discovery.py:30 wifi_ips()` → `time.monotonic`、`_probe_wifi_ips`、`_WIFI_CACHE.update`
- `wd/wd_discovery.py:42 _probe_wifi_ips()` → `run`、`info.splitlines`、`decode`、`line.split`、`re.findall`、`local_ips`、`info.lower`、`key.strip`、`value.strip`、`subprocess.check_output`
- `wd/wd_discovery.py:47 _probe_wifi_ips.<locals>.run()` → `decode`、`subprocess.check_output`
- `wd/wd_discovery.py:72 local_ips()` → `socket.socket`、`s.connect`、`ips.add`、`s.close`、`socket.getaddrinfo`、`socket.gethostname`、`_usable_ip`、`s.getsockname`
- `wd/wd_discovery.py:91 discovery_ips()` → `wifi_ips`、`local_ips`
- `wd/wd_discovery.py:97 primary_ip()` → `discovery_ips`
- `wd/wd_discovery.py:107 DiscoveryWorker.__init__()` → `__init__`
- `wd/wd_discovery.py:115 DiscoveryWorker.probe_now()` → （无外部调用）
- `wd/wd_discovery.py:118 DiscoveryWorker.start()` → `Thread`、`thread.start`、`__import__`
- `wd/wd_discovery.py:122 DiscoveryWorker.stop()` → （无外部调用）
- `wd/wd_discovery.py:125 DiscoveryWorker._pid()` → `config.get`
- `wd/wd_discovery.py:136 DiscoveryWorker._targets()` → `discovery_ips`、`ip.split`、`targets.add`、`join`
- `wd/wd_discovery.py:144 DiscoveryWorker._run()` → `discovery_ips`、`socket.socket`、`sock.settimeout`、`log.emit`、`time.monotonic`、`sock.close`、`config.get`、`sock.setsockopt`、`sock.bind`、`wifi_ips`、`self._expire`、`config.may_control`、`sock.recvfrom`、`parse_datagram`、`obj.get`、`discovery_datagram`、`self._targets`、`random.uniform`、`config.may_be_controlled`、`device_found.emit`、`primary_ip`、`sock.sendto`、`self._pid`
- `wd/wd_discovery.py:212 DiscoveryWorker._expire()` → `time.monotonic`、`devices.pop`、`device_lost.emit`、`devices.items`、`value.get`

### wd/wd_input.py

- `wd/wd_input.py:53 _send()` → `_user32.SendInput`、`ctypes.sizeof`
- `wd/wd_input.py:60 screen_size()` → `_user32.GetSystemMetrics`
- `wd/wd_input.py:66 mouse_move()` → `screen_size`、`INPUT`、`_send`
- `wd/wd_input.py:75 mouse_button()` → `get`、`INPUT`、`_send`
- `wd/wd_input.py:88 mouse_wheel()` → `INPUT`、`DWORD`、`_send`
- `wd/wd_input.py:95 key()` → `INPUT`、`WORD`、`_send`
- `wd/wd_input.py:104 inject()` → `event.get`、`mouse_move`、`mouse_button`、`mouse_wheel`、`key`
- `wd/wd_input.py:125 qt_key_to_vk()` → `text.upper`、`ord`

### wd/wd_protocol.py

- `wd/wd_protocol.py:11 recv_exact()` → `join`、`time.monotonic`、`chunks.append`、`sock.recv`、`ConnectionError`
- `wd/wd_protocol.py:30 send_msg()` → `encode`、`sock.sendall`、`json.dumps`、`struct.pack`
- `wd/wd_protocol.py:36 recv_msg()` → `struct.unpack`、`json.loads`、`recv_exact`、`ValueError`、`decode`、`header.get`
- `wd/wd_protocol.py:45 discovery_datagram()` → `encode`、`ValueError`、`json.dumps`
- `wd/wd_protocol.py:52 parse_datagram()` → `json.loads`、`ValueError`、`raw.decode`、`obj.get`

### wd/wd_session.py

- `wd/wd_session.py:16 _capture()` → （无外部调用）
- `wd/wd_session.py:29 ServerSession.__init__()` → `__init__`、`threading.Lock`
- `wd/wd_session.py:38 ServerSession.start()` → `socket.socket`、`sock.setsockopt`、`sock.settimeout`、`start`、`log.emit`、`sock.bind`、`sock.listen`、`sock.close`、`threading.Thread`、`config.get`
- `wd/wd_session.py:57 ServerSession.stop()` → `active.stop`、`listener.close`
- `wd/wd_session.py:68 ServerSession._accept_loop()` → `ClientHandler`、`start`、`listener.accept`、`conn.close`、`threading.Thread`
- `wd/wd_session.py:87 ServerSession.resolve_auth()` → `pending.get`
- `wd/wd_session.py:94 ServerSession.unregister()` → （无外部调用）
- `wd/wd_session.py:101 ClientHandler.__init__()` → `threading.Lock`、`ScreenCapture`、`config.get`、`time.monotonic`、`_capture`
- `wd/wd_session.py:117 ClientHandler.stop()` → `sock.shutdown`
- `wd/wd_session.py:125 ClientHandler._send()` → `send_msg`、`log.emit`
- `wd/wd_session.py:135 ClientHandler.run()` → `sock.settimeout`、`recv_msg`、`wd_crypto.fingerprint`、`config.get`、`wd_crypto.tls_context`、`send_msg`、`ctx.wrap_socket`、`sock.getpeercert`、`config.remember`、`self._send`、`session_started.emit`、`start`、`self._send_loop`、`server.unregister`、`ValueError`、`meta.get`、`config.is_auto_trusted`、`secrets.token_hex`、`auth_required.emit`、`wait`、`pending.pop`、`screen.grab`、`session_ended.emit`、`threading.Event`、`pending.get`、`wd_crypto.short_code`、`threading.Thread`、`log.emit`、`sock.close`、`raw_conn.close`
- `wd/wd_session.py:221 ClientHandler._send_loop()` → `screen.diff`、`time.monotonic`、`time.sleep`、`screen.grab`、`stats.emit`、`log.emit`、`screen.encode_jpeg`、`self._send`、`screen.pack_tiles`
- `wd/wd_session.py:273 ClientHandler._recv_loop()` → `recv_msg`、`log.emit`、`wd_input.inject`、`self._send`、`json.loads`、`payload.decode`、`meta.get`、`capture.set_preset`、`_capture`
- `wd/wd_session.py:309 ClientSession.__init__()` → `__init__`、`threading.Lock`、`time.monotonic`、`_capture`
- `wd/wd_session.py:327 ClientSession.start()` → `start`、`threading.Thread`
- `wd/wd_session.py:330 ClientSession.stop()` → `sock.shutdown`
- `wd/wd_session.py:339 ClientSession._send()` → `send_msg`、`log.emit`
- `wd/wd_session.py:349 ClientSession.send_input()` → `self._send`、`encode`、`json.dumps`
- `wd/wd_session.py:352 ClientSession.set_quality()` → `self._send`、`_capture`
- `wd/wd_session.py:357 ClientSession.resync()` → `self._send`
- `wd/wd_session.py:360 ClientSession._run()` → `state.emit`、`device.get`、`socket.create_connection`、`sock.settimeout`、`send_msg`、`recv_msg`、`wd_crypto.tls_context`、`ctx.wrap_socket`、`wd_crypto.fingerprint`、`self._send`、`QImage`、`canvas.fill`、`connected.emit`、`start`、`ValueError`、`PermissionError`、`meta.get`、`QColor`、`time.sleep`、`log.emit`、`disconnected.emit`、`sock.getpeercert`、`threading.Thread`、`sock.close`、`time.monotonic`
- `wd/wd_session.py:422 ClientSession._recv_loop()` → `self._publish_stats`、`recv_msg`、`frame_ready.emit`、`log.emit`、`self._apply_tiles`、`self.resync`、`meta.get`、`QImage.fromData`、`img.isNull`、`img.convertToFormat`、`canvas.copy`、`self._send`、`time.monotonic`
- `wd/wd_session.py:460 ClientSession._apply_tiles()` → `meta.get`、`QPainter`、`painter.end`、`QImage`、`img.isNull`、`painter.drawImage`、`zlib.decompress`、`canvas.width`、`canvas.height`
- `wd/wd_session.py:500 ClientSession._publish_stats()` → `time.monotonic`、`stats.emit`、`canvas.width`、`canvas.height`

### wd/wd_theme.py

- `wd/wd_theme.py:34 tokens()` → （无外部调用）
- `wd/wd_theme.py:38 qss()` → `tokens`
- `wd/wd_theme.py:135 apply_theme()` → `tokens`、`app.setStyle`、`QPalette`、`palette.setColor`、`app.setPalette`、`app.setStyleSheet`、`QColor`、`qss`

### wd/wd_ui.py

- `wd/wd_ui.py:22 network_status()` → `primary_ip`、`decode`、`out.splitlines`、`subprocess.check_output`、`strip`、`line.split`
- `wd/wd_ui.py:39 DragBar.__init__()` → `__init__`、`self.setObjectName`、`self.setFixedHeight`
- `wd/wd_ui.py:47 DragBar.mousePressEvent()` → `event.button`、`self.setCursor`、`toPoint`、`window_ref.pos`、`QCursor`、`event.globalPosition`
- `wd/wd_ui.py:53 DragBar.mouseMoveEvent()` → `window_ref.move`、`toPoint`、`event.globalPosition`
- `wd/wd_ui.py:57 DragBar.mouseReleaseEvent()` → `self.unsetCursor`
- `wd/wd_ui.py:66 DeviceRow.__init__()` → `__init__`、`self.setObjectName`、`self.setFixedHeight`、`QHBoxLayout`、`layout.setContentsMargins`、`layout.setSpacing`、`QLabel`、`icon.setStyleSheet`、`icon.setFixedWidth`、`layout.addWidget`、`QVBoxLayout`、`info.setSpacing`、`name.setStyleSheet`、`sub.setObjectName`、`info.addWidget`、`layout.addLayout`、`state.setStyleSheet`、`QPushButton`、`button.setObjectName`、`button.setFixedWidth`、`button.setEnabled`、`clicked.connect`、`device.get`、`connect_requested.emit`
- `wd/wd_ui.py:105 DeviceRow.set_connecting()` → `button.setEnabled`、`button.setText`、`button.text`
- `wd/wd_ui.py:111 PairDialog.__init__()` → `__init__`、`self.setWindowTitle`、`self.setModal`、`self.setFixedWidth`、`QVBoxLayout`、`layout.setContentsMargins`、`layout.setSpacing`、`QLabel`、`title.setStyleSheet`、`layout.addWidget`、`code.setObjectName`、`QCheckBox`、`auto_box.setEnabled`、`QDialogButtonBox`、`setText`、`accepted.connect`、`rejected.connect`、`info.get`、`buttons.button`
- `wd/wd_ui.py:138 PairDialog.accept()` → `auto_box.isChecked`、`accept`
- `wd/wd_ui.py:144 SettingsDialog.__init__()` → `__init__`、`self.setWindowTitle`、`self.setFixedWidth`、`self._build`、`self._load`
- `wd/wd_ui.py:152 SettingsDialog._build()` → `QVBoxLayout`、`layout.setContentsMargins`、`layout.setSpacing`、`layout.addWidget`、`QComboBox`、`role_combo.addItems`、`QCheckBox`、`auth_combo.addItems`、`QLabel`、`auth_hint.setObjectName`、`auth_hint.setWordWrap`、`currentIndexChanged.connect`、`quality_combo.addItems`、`theme_combo.addItems`、`QHBoxLayout`、`QSpinBox`、`discovery_port.setRange`、`service_port.setRange`、`ports.addWidget`、`layout.addLayout`、`QTextEdit`、`trusted.setReadOnly`、`trusted.setFixedHeight`、`QLineEdit`、`revoke_id.setPlaceholderText`、`QPushButton`、`clicked.connect`、`revoke_row.addWidget`、`QDialogButtonBox`、`accepted.connect`、`rejected.connect`
- `wd/wd_ui.py:214 SettingsDialog._load()` → `role_combo.setCurrentIndex`、`allow_box.setChecked`、`get`、`auth_combo.setCurrentIndex`、`quality_combo.setCurrentIndex`、`theme_combo.setCurrentIndex`、`discovery_port.setValue`、`service_port.setValue`、`self._update_auth_hint`、`self._refresh_trusted`、`role_map.get`、`c.get`、`quality_map.get`
- `wd/wd_ui.py:229 SettingsDialog._update_auth_hint()` → `auth_hint.setText`、`auth_combo.currentIndex`
- `wd/wd_ui.py:237 SettingsDialog._refresh_trusted()` → `config.get`、`known.items`、`trusted.setPlainText`、`lines.append`、`info.get`、`join`
- `wd/wd_ui.py:245 SettingsDialog._revoke()` → `strip`、`config.forget`、`revoke_id.clear`、`self._refresh_trusted`、`revoke_id.text`
- `wd/wd_ui.py:252 SettingsDialog.accept()` → `c.set`、`accept`、`allow_box.isChecked`、`discovery_port.value`、`service_port.value`、`role_combo.currentIndex`、`auth_combo.currentIndex`、`quality_combo.currentIndex`、`theme_combo.currentIndex`
- `wd/wd_ui.py:265 RemoteView.__init__()` → `__init__`、`self.setAlignment`、`self.setStyleSheet`、`self.setMouseTracking`、`self.setFocusPolicy`
- `wd/wd_ui.py:273 RemoteView.mousePressEvent()` → `owner.mouse_press`
- `wd/wd_ui.py:276 RemoteView.mouseReleaseEvent()` → `owner.mouse_release`
- `wd/wd_ui.py:279 RemoteView.mouseMoveEvent()` → `owner.mouse_move`
- `wd/wd_ui.py:282 RemoteView.wheelEvent()` → `owner.mouse_wheel`
- `wd/wd_ui.py:285 RemoteView.keyPressEvent()` → `owner.key_press`
- `wd/wd_ui.py:288 RemoteView.keyReleaseEvent()` → `owner.key_release`
- `wd/wd_ui.py:291 RemoteView.focusOutEvent()` → `owner.release_all`、`focusOutEvent`
- `wd/wd_ui.py:297 RemoteWindow.__init__()` → `__init__`、`QImage`、`self.setWindowTitle`、`self.setMinimumSize`、`self.resize`、`self._build`、`frame_ready.connect`、`stats.connect`、`connected.connect`、`disconnected.connect`、`self.show`、`device.get`、`info.get`
- `wd/wd_ui.py:319 RemoteWindow._build()` → `QVBoxLayout`、`root.setContentsMargins`、`root.setSpacing`、`QWidget`、`toolbar.setObjectName`、`toolbar.setFixedHeight`、`QHBoxLayout`、`bar.setContentsMargins`、`bar.setSpacing`、`bar.addWidget`、`QLabel`、`mode_label.setObjectName`、`bar.addStretch`、`QComboBox`、`quality_combo.addItems`、`quality_combo.setCurrentIndex`、`currentIndexChanged.connect`、`zoom_combo.addItems`、`QPushButton`、`stats_btn.setCheckable`、`clicked.connect`、`close_btn.setObjectName`、`QTimer`、`toolbar_timer.setSingleShot`、`timeout.connect`、`root.addWidget`、`RemoteView`、`status_overlay.setAlignment`、`status_overlay.setStyleSheet`、`status_overlay.hide`、`QFrame`、`stats_panel.setObjectName`、`stats_panel.setFixedHeight`、`stats_panel.hide`、`stat_text.setObjectName`、`box.addWidget`、`index`、`device.get`、`client.set_quality`、`setattr`、`self.update_view`
- `wd/wd_ui.py:386 RemoteWindow.resizeEvent()` → `resizeEvent`、`self._place_overlay`、`self.update_view`、`self.isFullScreen`、`toolbar.isVisible`、`toolbar.resize`、`toolbar.move`、`self.width`
- `wd/wd_ui.py:394 RemoteWindow._place_overlay()` → `status_overlay.isVisible`、`view.geometry`、`status_overlay.setGeometry`、`rect.x`、`rect.y`、`rect.width`、`rect.height`
- `wd/wd_ui.py:404 RemoteWindow.set_frame()` → `self.update_view`、`img.isNull`
- `wd/wd_ui.py:410 RemoteWindow.update_view()` → `canvas.isNull`、`view.size`、`view.setPixmap`、`canvas.size`、`QPixmap.fromImage`、`scaled`、`pix.width`、`area.width`、`area.height`、`canvas.width`、`pix.height`
- `wd/wd_ui.py:426 RemoteWindow.native_pos()` → `canvas.isNull`、`pos.x`、`pos.y`、`canvas.width`、`canvas.height`
- `wd/wd_ui.py:435 RemoteWindow.mouse_press()` → `view.setFocus`、`self._peek_toolbar`、`self.native_pos`、`client.send_input`、`get`、`toPoint`、`event.button`、`event.position`
- `wd/wd_ui.py:447 RemoteWindow.mouse_release()` → `get`、`event.button`、`client.send_input`
- `wd/wd_ui.py:452 RemoteWindow.mouse_move()` → `self._peek_toolbar`、`self.native_pos`、`toPoint`、`client.send_input`、`event.position`
- `wd/wd_ui.py:458 RemoteWindow.mouse_wheel()` → `self._peek_toolbar`、`y`、`client.send_input`、`event.angleDelta`
- `wd/wd_ui.py:464 RemoteWindow.key_press()` → `qt_key_to_vk`、`self.isFullScreen`、`self.toggle_fullscreen`、`event.key`、`event.text`、`pressed_keys.add`、`client.send_input`
- `wd/wd_ui.py:473 RemoteWindow.key_release()` → `qt_key_to_vk`、`event.key`、`event.text`、`pressed_keys.discard`、`client.send_input`
- `wd/wd_ui.py:479 RemoteWindow.release_all()` → `pressed_keys.clear`、`client.send_input`
- `wd/wd_ui.py:483 RemoteWindow._peek_toolbar()` → `toolbar.show`、`toolbar.raise_`、`toolbar.resize`、`toolbar.move`、`toolbar_timer.start`、`self.isFullScreen`、`self.width`
- `wd/wd_ui.py:492 RemoteWindow._hide_fullscreen_toolbar()` → `self.isFullScreen`、`toolbar.hide`
- `wd/wd_ui.py:496 RemoteWindow.toggle_fullscreen()` → `self.isFullScreen`、`self.showFullScreen`、`stats_panel.hide`、`removeWidget`、`toolbar.setParent`、`toolbar.resize`、`toolbar.move`、`toolbar.show`、`toolbar.raise_`、`full_btn.setText`、`toolbar_timer.start`、`toolbar_timer.stop`、`self.showNormal`、`insertWidget`、`widget.hide`、`self.width`、`widget.show`、`self.layout`
- `wd/wd_ui.py:520 RemoteWindow.toggle_stats()` → `stats_panel.setVisible`
- `wd/wd_ui.py:523 RemoteWindow.on_stats()` → `mode_label.setText`、`stat_text.setText`、`data.get`
- `wd/wd_ui.py:532 RemoteWindow._on_reconnected()` → `status_overlay.hide`
- `wd/wd_ui.py:536 RemoteWindow._on_disconnected()` → `self.release_all`、`status_overlay.setText`、`status_overlay.show`、`status_overlay.raise_`、`self._place_overlay`、`QTimer.singleShot`、`self.isVisible`
- `wd/wd_ui.py:548 RemoteWindow.bind_client()` → `old.stop`、`frame_ready.connect`、`stats.connect`、`connected.connect`、`disconnected.connect`、`QImage`、`status_overlay.hide`、`signal.disconnect`
- `wd/wd_ui.py:568 RemoteWindow._try_reconnect()` → `main.reconnect_client`、`client.start`、`self.isVisible`
- `wd/wd_ui.py:578 RemoteWindow.closeEvent()` → `self.release_all`、`client.stop`、`closeEvent`
- `wd/wd_ui.py:587 MainWindow.__init__()` → `__init__`、`self.setWindowTitle`、`self.setWindowFlags`、`self.setWindowOpacity`、`self.resize`、`self.setMinimumSize`、`DiscoveryWorker`、`ServerSession`、`self._build`、`self._wire`、`self.apply_role`、`discovery.start`
- `wd/wd_ui.py:607 MainWindow._build()` → `QVBoxLayout`、`root.setContentsMargins`、`root.setSpacing`、`DragBar`、`QHBoxLayout`、`bar_layout.setContentsMargins`、`QLabel`、`windowIcon`、`bar_layout.addWidget`、`brand.setObjectName`、`bar_layout.addStretch`、`net_label.setObjectName`、`network_ready.connect`、`start`、`QPushButton`、`settings.setObjectName`、`settings.setToolTip`、`clicked.connect`、`minimize.setObjectName`、`close.setObjectName`、`root.addWidget`、`QFrame`、`info.setObjectName`、`info_layout.setContentsMargins`、`info_layout.setSpacing`、`local.setSpacing`、`dot.setObjectName`、`dot.setStyleSheet`、`name_row.addWidget`、`name_label.setStyleSheet`、`name_row.addStretch`、`local.addLayout`、`state_label.setObjectName`、`local.addWidget`、`info_layout.addLayout`、`role_label.setObjectName`、`info_layout.addWidget`、`QComboBox`、`role_combo.addItems`、`role_combo.setFixedWidth`、`allow_button.setCheckable`、`allow_button.setFixedWidth`、`title.setObjectName`、`head.addWidget`、`count_label.setObjectName`、`head.addStretch`、`root.addLayout`、`QScrollArea`、`scroll.setWidgetResizable`、`QWidget`、`list_layout.setContentsMargins`、`list_layout.setSpacing`、`list_layout.addStretch`、`scroll.setWidget`、`diag.setCheckable`、`toggled.connect`、`foot.addWidget`、`version_label.setObjectName`、`foot.addStretch`、`QTextEdit`、`diag_text.setReadOnly`、`diag_text.setFixedHeight`、`diag_text.hide`、`app_icon.isNull`、`icon_label.setPixmap`、`QApplication.instance`、`app_icon.pixmap`、`net_label.setText`、`threading.Thread`、`network_ready.emit`、`network_status`
- `wd/wd_ui.py:717 MainWindow._wire()` → `role_combo.setCurrentIndex`、`allow_button.setChecked`、`self._update_allow_text`、`currentIndexChanged.connect`、`toggled.connect`、`device_found.connect`、`device_lost.connect`、`log.connect`、`auth_required.connect`、`session_started.connect`、`session_ended.connect`、`stats.connect`、`role_map.get`、`config.get`
- `wd/wd_ui.py:733 MainWindow.append_log()` → `diag_text.append`
- `wd/wd_ui.py:736 MainWindow.on_device_found()` → `self.refresh_devices`
- `wd/wd_ui.py:740 MainWindow.on_device_lost()` → `devices.pop`、`self.refresh_devices`、`client.stop`
- `wd/wd_ui.py:746 MainWindow.refresh_devices()` → `config.get`、`config.may_control`、`count_label.setText`、`list_layout.count`、`list_layout.takeAt`、`item.widget`、`devices.values`、`DeviceRow`、`connect_requested.connect`、`list_layout.insertWidget`、`QWidget`、`QVBoxLayout`、`empty_layout.setAlignment`、`empty_layout.setSpacing`、`QLabel`、`title.setStyleSheet`、`title.setAlignment`、`hint.setObjectName`、`hint.setAlignment`、`hint.setWordWrap`、`empty_layout.addWidget`、`widget.deleteLater`、`row.set_connecting`、`lower`、`d.get`
- `wd/wd_ui.py:779 MainWindow.on_connect()` → `ClientSession`、`connected.connect`、`disconnected.connect`、`state.connect`、`log.connect`、`client.start`、`self.append_log`、`self.refresh_devices`、`client.stop`、`config.get`、`device.get`
- `wd/wd_ui.py:793 MainWindow.on_connected()` → `self.refresh_devices`、`RemoteWindow`、`remote_windows.append`、`w.isVisible`
- `wd/wd_ui.py:801 MainWindow.reconnect_client()` → `self.on_connect`、`window.bind_client`
- `wd/wd_ui.py:808 MainWindow.on_disconnected()` → `self.append_log`、`self.refresh_devices`
- `wd/wd_ui.py:813 MainWindow.on_auth_required()` → `PairDialog`、`dialog.exec`、`server.resolve_auth`
- `wd/wd_ui.py:818 MainWindow.on_session_started()` → `state_label.setText`、`state_label.setStyleSheet`、`self.append_log`、`info.get`
- `wd/wd_ui.py:823 MainWindow.on_session_ended()` → `state_label.setText`、`state_label.setStyleSheet`、`self.append_log`
- `wd/wd_ui.py:828 MainWindow.on_server_stats()` → `state_label.setText`、`data.get`
- `wd/wd_ui.py:833 MainWindow.on_role_changed()` → `config.set`、`self.apply_role`
- `wd/wd_ui.py:838 MainWindow.on_allow_toggled()` → `config.set`、`self._update_allow_text`、`self.apply_role`
- `wd/wd_ui.py:843 MainWindow._update_allow_text()` → `allow_button.isChecked`、`allow_button.setText`、`allow_button.setObjectName`、`polish`、`allow_button.style`
- `wd/wd_ui.py:849 MainWindow.apply_role()` → `role_combo.blockSignals`、`role_combo.setCurrentIndex`、`allow_button.setEnabled`、`self.refresh_devices`、`role_map.get`、`config.may_be_controlled`、`config.get`、`server.start`、`server.stop`、`state_label.setText`
- `wd/wd_ui.py:862 MainWindow.open_settings()` → `SettingsDialog`、`dialog.exec`、`wd_theme.apply_theme`、`allow_button.setChecked`、`self._update_allow_text`、`self.apply_role`、`self.append_log`、`QApplication.instance`、`config.get`
- `wd/wd_ui.py:871 MainWindow.toggle_diagnostics()` → `diag_text.setVisible`
- `wd/wd_ui.py:874 MainWindow.showEvent()` → `showEvent`、`QPropertyAnimation`、`_fade_anim.setDuration`、`_fade_anim.setStartValue`、`_fade_anim.setEndValue`、`_fade_anim.start`
- `wd/wd_ui.py:883 MainWindow.closeEvent()` → `server.stop`、`discovery.stop`、`closeEvent`、`client.stop`、`window.close`
