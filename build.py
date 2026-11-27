"""WiFiDesk 一键打包脚本：python build.py"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "WiFiDesk"
ENTRY = ROOT / "main.py"
ICON_ICO = ROOT / "icon.ico"
ICON_PNG = ROOT / "icon.png"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def check_environment() -> bool:
    if not ENTRY.exists():
        print(f"错误：找不到入口 {ENTRY}")
        return False
    if not ICON_ICO.exists() or not ICON_PNG.exists():
        print("错误：缺少 icon.ico 或 icon.png")
        return False
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller，请先执行：python -m pip install pyinstaller")
        return False
    return True


def clean() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    DIST.mkdir(parents=True, exist_ok=True)


def make_zip() -> Path:
    archive = DIST / f"{APP_NAME}_portable.zip"
    if archive.exists():
        archive.unlink()
    shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=DIST, base_dir=APP_NAME)
    return archive


def build() -> int:
    if not check_environment():
        return 1
    clean()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", APP_NAME,
        "--icon", str(ICON_ICO),
        "--add-data", f"{ICON_PNG}{os.pathsep}.",
        "--collect-all", "dxcam",
        "--collect-all", "comtypes",
        "--collect-all", "av",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        str(ENTRY),
    ]
    print("开始打包：")
    print("  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"打包失败，PyInstaller 返回码 {result.returncode}")
        return result.returncode

    exe = DIST / APP_NAME / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"打包失败：未找到 {exe}")
        return 1

    log = exe.parent / "selftest.log"
    if log.exists():
        log.unlink()
    print("运行打包产物自检…")
    try:
        check = subprocess.run([str(exe), "--self-test"], cwd=exe.parent, timeout=60)
    except subprocess.TimeoutExpired:
        print("自检超时")
        return 1
    log_text = log.read_text(encoding="utf-8") if log.exists() else None
    if log_text:
        print(log_text.rstrip())
    else:
        print("警告：未生成 selftest.log（窗口模式下自检日志写入该路径）")
    if check.returncode != 0:
        print(f"自检失败，返回码 {check.returncode}")
        return check.returncode

    if log.exists():
        log.unlink()
    archive = make_zip()
    if log_text:
        _ = log.write_text(log_text, encoding="utf-8")
    size_mb = archive.stat().st_size / 1024 / 1024
    print(f"压缩包：{archive}  ({size_mb:.1f} MB)")
    print(f"完成：{exe}")
    print("运行方式：解压压缩包后双击 WiFiDesk\\WiFiDesk.exe")
    return 0


if __name__ == "__main__":
    sys.exit(build())
