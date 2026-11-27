"""WiFiDesk 一键打包脚本：python build.py [版本号]

默认版本 1.0，产物：
  dist/WiFiDesk_V1.0/WiFiDesk.exe
  dist/WiFiDesk_V1.0_portable.zip
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "WiFiDesk"
APP_VERSION = "1.0"
ENTRY = ROOT / "main.py"
ICON_ICO = ROOT / "icon.ico"
ICON_PNG = ROOT / "icon.png"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

VERSION_FILE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v0}, {v1}, {v2}, 0),
    prodvers=({v0}, {v1}, {v2}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', 'WiFiDesk'),
      StringStruct('FileDescription', 'WiFiDesk 局域网远程桌面'),
      StringStruct('FileVersion', '{version}.0'),
      StringStruct('InternalName', 'WiFiDesk'),
      StringStruct('LegalCopyright', 'WiFiDesk'),
      StringStruct('OriginalFilename', 'WiFiDesk.exe'),
      StringStruct('ProductName', 'WiFiDesk'),
      StringStruct('ProductVersion', 'V{version}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def release_name(version: str) -> str:
    return f"{APP_NAME}_V{version}"


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


def clean(version: str) -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    DIST.mkdir(parents=True, exist_ok=True)
    stale = [DIST / release_name(version), DIST / f"{release_name(version)}_portable.zip",
             DIST / f"{APP_NAME}_portable.zip"]
    for path in stale:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


def move_dir(src: Path, dst: Path) -> None:
    """PyInstaller 刚结束时杀软可能还在扫新目录，重命名会短暂被占用。"""
    import time
    last = None
    for _ in range(20):
        try:
            src.rename(dst)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.5)
    raise last


def write_version_file(version: str) -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    parts = [int(p) for p in version.split(".") if p.isdigit()][:3]
    while len(parts) < 3:
        parts.append(0)
    target = BUILD / "version_info.txt"
    target.write_text(VERSION_FILE.format(
        v0=parts[0], v1=parts[1], v2=parts[2], version=version), encoding="utf-8")
    return target


def make_zip(version: str) -> Path:
    name = release_name(version)
    archive = DIST / f"{name}_portable.zip"
    if archive.exists():
        archive.unlink()
    shutil.make_archive(str(archive.with_suffix("")), "zip", root_dir=DIST, base_dir=name)
    return archive


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build(version: str) -> int:
    if not check_environment():
        return 1
    clean(version)
    info = write_version_file(version)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", APP_NAME,
        "--icon", str(ICON_ICO),
        "--version-file", str(info),
        "--add-data", f"{ICON_PNG}{os.pathsep}.",
        "--collect-all", "dxcam",
        "--collect-all", "comtypes",
        "--collect-all", "av",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        str(ENTRY),
    ]
    print(f"开始打包 V{version}：")
    print("  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"打包失败，PyInstaller 返回码 {result.returncode}")
        return result.returncode

    built = DIST / APP_NAME
    exe = built / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"打包失败：未找到 {exe}")
        return 1

    # 产物目录带上版本号，exe 名字保持干净
    final_dir = DIST / release_name(version)
    if final_dir.exists():
        shutil.rmtree(final_dir, ignore_errors=True)
    move_dir(built, final_dir)
    exe = final_dir / f"{APP_NAME}.exe"

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
    archive = make_zip(version)
    if log_text:
        _ = log.write_text(log_text, encoding="utf-8")

    size_mb = archive.stat().st_size / 1024 / 1024
    print("=" * 56)
    print(f"WiFiDesk V{version} 打包完成")
    print(f"程序目录：{final_dir}")
    print(f"便携压缩包：{archive}（{size_mb:.1f} MB）")
    print(f"SHA-256：{sha256_of(archive)}")
    print(f"运行方式：解压后双击 {release_name(version)}\\{APP_NAME}.exe")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    arg = sys.argv[1].lstrip("vV") if len(sys.argv) > 1 else APP_VERSION
    sys.exit(build(arg or APP_VERSION))
