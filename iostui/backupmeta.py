"""备份元数据生成：Info.plist / Manifest.plist / Status.plist / Manifest.mbdb。

## 为什么必须用设备真实信息

`restore --system` 不是「把这些文件拷过去」这么简单。设备侧 BackupAgent 会校验
备份的元数据：

* **Info.plist** —— 描述「这份备份属于哪台设备」，含 ``Target Identifier``（UDID）、
  ``Product Version``、``Build Version``、``Product Type``、``Serial Number`` 等字段。
* **Manifest.plist** —— 含 ``BackupKeyBag`` / ``Lockdown`` / ``Version`` /
  ``SystemDomainsVersion``。不同 iOS 版本期望的 ``Version`` 不同，抄一个 2023 年
  的固定值在新系统上会被拒。
* **Status.plist** —— ``SnapshotState`` 必须是 ``finished``，否则 restore 直接拒绝。

所以本模块从 ``lockdown.all_values`` 取真实值来拼这些文件，而不是硬编码。
"""

from __future__ import annotations

import base64
import plistlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# 备份 keybag：非加密备份也必须带一个合法的 BackupKeyBag。
# 这段是从 iOS 备份里取出的标准非加密 keybag，Apple 各版本通用。
_EMPTY_KEYBAG_B64 = (
    "VkVSUwAAAAQAAAAFVFlQRQAAAAQAAAABVVVJRAAAABDud41d1b9NBICR1BH9JfVtSE1D"
    "SwAAACgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAV1JBUAAA"
    "AAQAAAAAU0FMVAAAABRY5Ne2bthGQ5rf4O3gikep1e6tZUlURVIAAAAEAAAnEFVVSUQA"
    "AAAQB7R8awiGR9aba1UuVahGPENMQVMAAAAEAAAAAVdSQVAAAAAEAAAAAktUWVAAAAAE"
    "AAAAAFdQS1kAAAAoN3kQAJloFg+ukEUY+v5P+dhc/Welw/oucsyS40UBh67ZHef5ZMk9"
    "UVVVSUQAAAAQgd0cg0hSTgaxR3PVUbcEkUNMQVMAAAAEAAAAAldSQVAAAAAEAAAAAktU"
    "WVAAAAAEAAAAAFdQS1kAAAAoMiQTXx0SJlyrGJzdKZQ+SfL124w+2Tf/3d1R2i9yNj9z"
    "ZCHNJhnorVVVSUQAAAAQf7JFQiBOS12JDD7qwKNTSkNMQVMAAAAEAAAAA1dSQVAAAAAE"
    "AAAAAktUWVAAAAAEAAAAAFdQS1kAAAAoSEelorROJA46ZUdwDHhMKiRguQyqHukotrxh"
    "jIfqiZ5ESBXX9txi51VVSUQAAAAQfF0G/837QLq01xH9+66vx0NMQVMAAAAEAAAABFdS"
    "QVAAAAAEAAAAAktUWVAAAAAEAAAAAFdQS1kAAAAol0BvFhd5bu4Hr75XqzNf4g0fMqZA"
    "ie6OxI+x/pgm6Y95XW17N+ZIDVVVSUQAAAAQimkT2dp1QeadMu1KhJKNTUNMQVMAAAAE"
    "AAAABVdSQVAAAAAEAAAAA0tUWVAAAAAEAAAAAFdQS1kAAAAo2N2DZarQ6GPoWRgTiy/t"
    "djKArOqTaH0tPSG9KLbIjGTOcLodhx23xFVVSUQAAAAQQV37JVZHQFiKpoNiGmT6+ENM"
    "QVMAAAAEAAAABldSQVAAAAAEAAAAA0tUWVAAAAAEAAAAAFdQS1kAAAAofe2QSvDC2cV7"
    "Etk4fSBbgqDx5ne/z1VHwmJ6NdVrTyWi80Sy869DM1VVSUQAAAAQFzkdH+VgSOmTj3yE"
    "cfWmMUNMQVMAAAAEAAAAB1dSQVAAAAAEAAAAA0tUWVAAAAAEAAAAAFdQS1kAAAAo7kLY"
    "PQ/DnHBERGpaz37eyntIX/XzovsS0mpHW3SoHvrb9RBgOB+WblVVSUQAAAAQEBpgKOz9"
    "Tni8F9kmSXd0sENMQVMAAAAEAAAACFdSQVAAAAAEAAAAA0tUWVAAAAAEAAAAAFdQS1kA"
    "AAAo5mxVoyNFgPMzphYhm1VG8Fhsin/xX+r6mCd9gByF5SxeolAIT/ICF1VVSUQAAAAQ"
    "rfKB2uPSQtWh82yx6w4BoUNMQVMAAAAEAAAACVdSQVAAAAAEAAAAA0tUWVAAAAAEAAAA"
    "AFdQS1kAAAAo5iayZBwcRa1c1MMx7vh6lOYux3oDI/bdxFCW1WHCQR/Ub1MOv+QaYFVV"
    "SUQAAAAQiLXvK3qvQza/mea5inss/0NMQVMAAAAEAAAACldSQVAAAAAEAAAAA0tUWVAA"
    "AAAEAAAAAFdQS1kAAAAoD2wHX7KriEe1E31z7SQ7/+AVymcpARMYnQgegtZD0Mq2U55u"
    "xwNr2FVVSUQAAAAQ/Q9feZxLS++qSe/a4emRRENMQVMAAAAEAAAAC1dSQVAAAAAEAAAA"
    "A0tUWVAAAAAEAAAAAFdQS1kAAAAocYda2jyYzzSKggRPw/qgh6QPESlkZedgDUKpTr4Z"
    "Z8FDgd7YoALY1g=="
)


class BackupMetadataError(RuntimeError):
    """设备信息不足，无法生成合法备份。"""


@dataclass
class DeviceInfo:
    """生成备份元数据所需的设备信息。"""

    udid: str
    serial: str = ""
    device_name: str = ""
    product_type: str = ""
    product_version: str = ""
    build_version: str = ""
    guid: bytes = field(default_factory=lambda: uuid.uuid4().bytes)

    @classmethod
    def from_lockdown(cls, values: dict[str, Any], udid: str) -> "DeviceInfo":
        """从 ``lockdown.all_values`` 构造。"""
        return cls(
            udid=udid.upper(),
            serial=str(values.get("SerialNumber", "")),
            device_name=str(values.get("DeviceName", "")),
            product_type=str(values.get("ProductType", "")),
            product_version=str(values.get("ProductVersion", "")),
            build_version=str(values.get("BuildVersion", "")),
        )

    def validate(self) -> None:
        missing = [
            name for name, val in (
                ("UDID", self.udid),
                ("ProductType", self.product_type),
                ("ProductVersion", self.product_version),
            ) if not val
        ]
        if missing:
            raise BackupMetadataError(
                "设备信息不完整，缺少：" + "、".join(missing)
                + "。无法生成合法的备份元数据。"
            )


def make_info_plist(info: DeviceInfo, *, applications: Optional[dict] = None) -> bytes:
    """生成 Info.plist。

    字段集合对齐 Finder/iTunes 生成的真实备份：设备标识 + 版本 + 已安装 App 列表。
    ``Target Identifier`` 是设备侧最关键的校验项。
    """
    info.validate()
    payload: dict[str, Any] = {
        "iTunes Version": "12.0.0.0",
        "iTunes Files": {},
        "Unique Identifier": info.udid,
        "Target Type": "Device",
        "Target Identifier": info.udid,
        "Serial Number": info.serial,
        "Product Version": info.product_version,
        "Product Type": info.product_type,
        "Build Version": info.build_version,
        "Installed Applications": list((applications or {}).keys()),
        "Device Name": info.device_name,
        "Display Name": info.device_name,
        "GUID": info.guid,
        "Applications": applications or {},
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def make_manifest_plist(info: DeviceInfo, *, is_encrypted: bool = False) -> bytes:
    """生成 Manifest.plist。

    ``Version`` 用 ``10.0``（iOS 10+ 通用），``SystemDomainsVersion`` 跟随设备
    系统版本。
    """
    info.validate()
    major = info.product_version.split(".")[0] if info.product_version else "10"
    try:
        system_domains = f"{int(major) + 4}.0"
    except ValueError:
        system_domains = "14.0"
    payload: dict[str, Any] = {
        "BackupKeyBag": base64.b64decode(_EMPTY_KEYBAG_B64),
        "Lockdown": {},
        "SystemDomainsVersion": system_domains,
        "Version": "10.0",
        "IsEncrypted": is_encrypted,
        "WasPasscodeSet": True,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_BINARY, sort_keys=True)


def make_status_plist(info: DeviceInfo, *, is_full: bool = False) -> bytes:
    """生成 Status.plist。``SnapshotState`` 必须是 finished。"""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "BackupState": "new",
        "Date": now,
        "IsFullBackup": is_full,
        "SnapshotState": "finished",
        "UUID": info.udid.lower(),
        "Version": "3.3",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


# --------------------------------------------------------------------------- #
# Manifest.db（iOS 10.2+ 实际使用的清单，SQLite）
# --------------------------------------------------------------------------- #
#
# 设备侧 BackupAgent 在 iOS 10.2 之后改读 Manifest.db，表结构为：
#     Files(fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT,
#           flags INTEGER, file BLOB)
# ``file`` 是 Apple 的 NSKeyedArchiver 归档（类名 MBFile）。这里手写该归档，
# 避免引入额外依赖 —— 它只是「key 长度 + key + 值长度 + 值」的重复结构。

from . import mbdb as _mbdb


def _archived_length(n: int) -> bytes:
    """NSKeyedArchiver 的整数编码（短格式）。"""
    if n < 0x80:
        return bytes([n])
    if n < 0x100:
        return b"\x81" + bytes([n])
    return b"\x82" + n.to_bytes(2, "big")


def _archived_bytes_text(text: str) -> bytes:
    for encoding, marker in (("ascii", 0x50), ("utf-16-be", 0x60)):
        try:
            raw = text.encode(encoding)
        except UnicodeEncodeError:
            continue
        return bytes([marker + 1]) + _archived_length(len(text)) + raw
    return bytes([0x51]) + _archived_length(len(text)) + text.encode("utf-8")


# --------------------------------------------------------------------------- #
# Manifest.db（iOS 10.2+ 实际读取的清单，SQLite）
# --------------------------------------------------------------------------- #
#
# 设备侧 BackupAgent 在 iOS 10.2 之后改读 Manifest.db：
#     Files(fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT,
#           flags INTEGER, file BLOB)
# ``file`` 是 NSKeyedArchiver 归档的 MBFile 对象。
#
# 我们用 bpylist2（pymobiledevice3 的依赖，必然已安装）生成标准归档，
# 比手写字节编码可靠。产出 Manifest.db 的同时也保留 Manifest.mbdb，
# 两者并存无害，旧系统仍可读。

import sqlite3
import tempfile


def _mb_file_archive(relative_path: str, size: int, mode: int,
                     mtime: int) -> bytes:
    """构造一条 Files.file 的 NSKeyedArchiver 归档（类名 MBFile）。"""
    from bpylist2 import archiver

    class MBFile:
        def __init__(self) -> None:
            self.RelativePath = relative_path
            self.LastModified = mtime
            self.LastStatusChange = mtime
            self.Birth = mtime
            self.Size = size
            self.Mode = mode
            self.GroupID = 501
            self.UserID = 501
            self.ProtectionClass = 0

        def encode_archive(self, archiver_obj) -> None:
            for key in (
                "RelativePath", "LastModified", "LastStatusChange", "Birth",
                "Size", "Mode", "GroupID", "UserID", "ProtectionClass",
            ):
                archiver_obj.encode(key, getattr(self, key))

    archiver.update_class_map({"MBFile": MBFile})
    return archiver.archive(MBFile())


def make_manifest_db(items, device_dir) -> bytes:
    """生成 Manifest.db 内容。

    ``items`` 是 mbdb.StagedItem 列表；实体文件仍需按
    ``sha1(domain-path)`` 命名放在备份目录里，这一点与 mbdb 一致。
    """
    from . import mbdb as _m

    tmp = Path(tempfile.mkdtemp(prefix="iostui-manifest-"))
    db_path = tmp / "Manifest.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE Files ("
            "fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, "
            "flags INTEGER, file BLOB)"
        )
        rows: list[tuple[str, str, int, bytes | None]] = []
        seen_dirs: set[tuple[str, str]] = set()
        for item in items:
            # 目录记录必须与 Manifest.mbdb 保持一致，否则设备读到两份不同的
            # 清单（iOS 10.2+ 优先用 Manifest.db），会按有缺陷的那份恢复。
            root_key = (item.domain, "")
            if root_key not in seen_dirs:
                seen_dirs.add(root_key)
                rows.append(("", item.domain, _m.MODE_DIR, item.mtime))
            for parent in _m.parent_dirs(item.relative_path):
                key = (item.domain, parent)
                if key in seen_dirs:
                    continue
                seen_dirs.add(key)
                rows.append((parent, item.domain, _m.MODE_DIR, item.mtime))
            rows.append((item.relative_path, item.domain, item.mode, item.mtime))

        for rel_path, domain, mode, mtime in rows:
            fid = _m.file_id(domain, rel_path)
            size = 0
            for it in items:
                if it.domain == domain and it.relative_path == rel_path:
                    size = len(it.data)
                    break
            try:
                blob = _mb_file_archive(rel_path, size, mode, mtime)
            except Exception:
                # 归档失败时用 NULL，设备会退化为按 fileID 读取
                blob = None
            conn.execute(
                "INSERT OR REPLACE INTO Files "
                "(fileID, domain, relativePath, flags, file) VALUES (?,?,?,?,?)",
                (fid, domain, rel_path, 1, blob),
            )
        conn.commit()
        conn.close()
        return db_path.read_bytes()
    finally:
        import shutil as _sh

        _sh.rmtree(tmp, ignore_errors=True)
