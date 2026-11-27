"""证书生成、指纹计算与 Windows DPAPI 密钥保护。

使用标准 TLS + 证书指纹 TOFU，不自行实现加密算法。
"""
import base64
import ctypes
import datetime
import hashlib
import os
import ssl
from ctypes import wintypes



class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, protect: bool = True) -> bytes:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        return data
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(len(data), ctypes.cast(src, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI {'protect' if protect else 'unprotect'} failed: {ctypes.get_last_error()}")
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    kernel32.LocalFree(blob_out.pbData)
    return out


def protect(data: bytes) -> bytes:
    return _dpapi(data, True)


def unprotect(data: bytes) -> bytes:
    return _dpapi(data, False)


def _generate_certificate(device_id: str):
    # 证书只在首次启动生成，避免每次启动都加载 cryptography
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"WiFiDesk-{device_id}")])
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("wifidesk.local")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return cert_pem, key_pem


def ensure_material(config_dir: str, device_id: str):
    os.makedirs(config_dir, exist_ok=True)
    cert_path = os.path.join(config_dir, "cert.pem")
    key_path = os.path.join(config_dir, "key.protected")
    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        cert_pem, key_pem = _generate_certificate(device_id)
        with open(cert_path, "wb") as f:
            f.write(cert_pem)
        with open(key_path, "wb") as f:
            f.write(protect(key_pem))
    with open(cert_path, "r", encoding="ascii") as f:
        pem = f.read()
    der = ssl.PEM_cert_to_DER_cert(pem)
    fp = hashlib.sha256(der).hexdigest()
    return cert_path, key_path, fp, pem


def tls_context(config_dir: str, server_side: bool, peer_pem: str = None) -> ssl.SSLContext:
    """创建 TLS 上下文；peer_pem 为本连接临时信任的对端自签名证书。"""
    import tempfile

    cert_path = os.path.join(config_dir, "cert.pem")
    key_path = os.path.join(config_dir, "key.protected")
    with open(key_path, "rb") as f:
        key = unprotect(f.read())
    fd, tmp_key = tempfile.mkstemp(prefix="_key_runtime_", dir=config_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    tmp_peer = None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        if peer_pem:
            fd, tmp_peer = tempfile.mkstemp(prefix="_peer_runtime_", dir=config_dir)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(peer_pem)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=tmp_peer)
        else:
            ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(cert_path, tmp_key)
        return ctx
    finally:
        for path in (tmp_key, tmp_peer):
            if not path:
                continue
            try:
                if os.path.exists(path):
                    with open(path, "wb") as f:
                        f.write(b"\x00" * 32)
                    os.remove(path)
            except OSError:
                pass


def fingerprint(pem_or_der) -> str:
    if isinstance(pem_or_der, str):
        der = ssl.PEM_cert_to_DER_cert(pem_or_der)
    else:
        der = pem_or_der
    return hashlib.sha256(der).hexdigest()


def short_code(fp: str) -> str:
    raw = bytes.fromhex(fp)[:8]
    s = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))
