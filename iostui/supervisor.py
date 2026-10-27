"""监督身份（supervisor identity）证书。

## 为什么需要它

`IsSupervised: true` 这一行本身没有说服力。设备（`cloudconfigurationd`）要确认
「谁在监督我」—— 依据是云配置里的 ``SupervisorHostCertificates``，它必须是
一份真实证书的 DER 公钥字节。

这正是我们之前失败的原因：只写了 ``IsSupervised: true`` 却没带证书，设备读到
配置后把该项当成无效，回读时 ``IsSupervised`` 仍是 ``false``。

Apple Configurator 的做法是引入一份「监督身份」（.p12 / 含私钥的 PEM），
把其中的证书登记进云配置。本模块负责生成这样一份自签证书，并缓存复用
（同一台设备/同一份安装应该用同一身份，避免重复监督时冲突）。

生成的 PEM 同时含证书与私钥，格式与 pymobiledevice3 的 keybag_file 一致，
因此也能直接交给它的 ``MobileConfigService.supervise()``。
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Optional

DEFAULT_DIR_NAME = ".iostui"
KEY_NAME = "supervisor.pem"


def identity_path(directory: Optional[Path] = None) -> Path:
    """监督身份的默认存放位置。"""
    base = Path(directory) if directory else Path.home() / DEFAULT_DIR_NAME
    return base / KEY_NAME


def generate_identity(common_name: str = "iostui Supervisor") -> tuple[bytes, bytes]:
    """生成一份自签监督身份，返回 (证书 PEM, 私钥 PEM)。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, common_name),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def ensure_identity(path: Optional[Path] = None,
                    common_name: str = "iostui Supervisor") -> Path:
    """取得监督身份文件；不存在则生成（证书+私钥写进同一个 PEM）。"""
    target = Path(path) if path else identity_path()
    if target.exists() and target.stat().st_size > 0:
        return target
    cert_pem, key_pem = generate_identity(common_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(cert_pem + key_pem)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def public_der(path: Path) -> bytes:
    """取出证书的 DER 公钥字节（写进 SupervisorHostCertificates 用）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    data = Path(path).read_bytes()
    cert = x509.load_pem_x509_certificate(data)
    return cert.public_bytes(serialization.Encoding.DER)
