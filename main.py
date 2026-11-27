"""WiFiDesk 启动入口：python main.py"""
import argparse
import os
import sys


def _report(line: str) -> None:
    print(line)
    if getattr(sys, "frozen", False):
        try:
            path = os.path.join(os.path.dirname(sys.executable), "selftest.log")
            with open(path, "a", encoding="utf-8") as f:
                _ = f.write(line + "\n")
        except OSError:
            pass


def resource_path(relative: str) -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


def self_test() -> int:
    from wd.wd_config import Config
    from wd.wd_capture import ScreenCapture
    from wd.wd_crypto import tls_context
    import wd.wd_config as wd_config

    config = Config()
    assert len(config.fingerprint) == 64, "证书指纹异常"
    capture = ScreenCapture("power")
    frame = capture.grab()
    assert frame.shape[0] > 0 and frame.shape[1] > 0, "屏幕采集失败"
    backend = capture.backend
    capture.close()
    from wd import wd_codec
    try:
        codec = wd_codec.probe(640, 360, sample=frame)
    except Exception as exc:
        raise AssertionError(f"H.264 编码器不可用：{exc}") from exc
    ctx_server = tls_context(wd_config.APP_DIR, True)
    ctx_client = tls_context(wd_config.APP_DIR, False)
    assert ctx_server and ctx_client, "TLS 上下文创建失败"
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", int(config.get("discovery_port", 47381))))
    except OSError as exc:
        _report(f"WARN 发现端口绑定失败（可能被其他实例占用）：{exc}")
    finally:
        sock.close()
    _report(f"PASS 配置 {config.path}")
    _report(f"PASS 采集 {backend} {frame.shape[1]}x{frame.shape[0]}")
    _report(f"PASS 编码器 {codec}")
    if backend != "DXGI":
        _report("WARN 未启用 DXGI，硬件加速窗口可能出现黑块")
    _report(f"PASS TLS 证书指纹 {config.fingerprint[:16]}…")
    _report("PASS 自检完成")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="WiFiDesk 局域网远程桌面")
    _ = parser.add_argument("--self-test", action="store_true", help="不启动界面，检查基础能力")
    args = parser.parse_args()

    from wd.wd_config import Config
    config = Config()

    if args.self_test:
        return self_test()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    import wd.wd_theme as wd_theme
    from wd.wd_ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("WiFiDesk")
    app.setWindowIcon(QIcon(resource_path("icon.png")))
    wd_theme.apply_theme(app, str(config.get("theme", "dark")))
    window = MainWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
