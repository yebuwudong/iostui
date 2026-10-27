"""Manifest.mbdb（BackupAgent 元数据）读写。

iOS 的慢速备份恢复流程只认 ``Manifest.mbdb``：它按顺序记录每个条目的
``domain`` / ``relative_path`` / 属性 / SHA1 / 大小，实体文件则以
``sha1("<domain>-<path>")`` 为文件名存放在备份目录里。

记录布局（大端）：

===================  ==========================================
domain_len (u16)     0xFFFF 表示无
domain
path_len (u16)       0xFFFF 表示无
path
link_len (u16)       0xFFFF 表示无
link_target
hash_len (u16)       0xFFFF 表示无
data_hash            SHA1
key_len (u16)        0xFFFF 表示无
encryption_key
mode (u16)           0x81FF=普通文件 777，0x41FF=目录 777
unknown2 (u32)       固定 0
unknown3 (u32)       固定 0
uid (u32)            501
gid (u32)            501
mtime (u32)
atime (u32)
ctime (u32)
size (u64)
flags (u8)           0x04
properties_count     固定 0
===================  ==========================================
"""

from __future__ import annotations

import hashlib
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

MBDB_HEADER = b"mbdb\x05\x00"

# 工作区目录名 → 设备上的真实 domain 名
# 设备自己会把 ConfigProfileDomain 映射到
# SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles。
# 工具**不能**替它做这个替换：Cowabunga 的 mbdb 里 domain=SkipSetup、
# path 保留 ConfigProfileDomain/ 前缀，设备据此定位。换掉就会写到错误位置。
DOMAIN_ALIASES: dict[str, str] = {}

MODE_FILE = 0o100777
MODE_DIR = 0o40777
UID = 501
GID = 501
FLAGS = 0x04
NO_FIELD = 0xFFFF


def device_domain(domain: str) -> str:
    """工作区目录名 → mbdb 里的 domain 名。

    当前无需任何替换（映射由设备完成），保留函数以便将来扩展。
    """
    return DOMAIN_ALIASES.get(domain, domain)


def _swrite(out: bytearray, text: str) -> None:
    """写 2 字节长度前缀 + UTF-8 内容。

    **空字符串与"无字段"在 mbdb 里是两种不同的编码**：
    空字符串写 ``0x0000``（长度 0），无字段写 ``0xFFFF``。
    domain 根目录的 path 是空字符串，必须用 ``0x0000``。
    """
    raw = text.encode("utf-8")
    if len(raw) > 0xFFFE:
        raise ValueError(f"字段过长，无法写入 mbdb: {text[:40]!r}…")
    out += struct.pack(">H", len(raw))
    out += raw


@dataclass
class MbdbEntry:
    """一条备份记录。"""

    domain: str
    relative_path: str
    mode: int = MODE_FILE
    size: int = 0
    mtime: int = 0
    digest: bytes = b""
    link_target: str = ""

    @property
    def is_dir(self) -> bool:
        return self.mode & 0o170000 == 0o040000

    def encode(self) -> bytes:
        out = bytearray()
        _swrite(out, self.domain)
        _swrite(out, self.relative_path)
        if self.link_target:
            _swrite(out, self.link_target)
        else:
            out += struct.pack(">H", NO_FIELD)          # 无 link target
        if self.digest:
            out += struct.pack(">H", len(self.digest)) + self.digest
        else:
            out += struct.pack(">H", NO_FIELD)
        out += struct.pack(">H", NO_FIELD)              # encryption key
        out += struct.pack(">H", self.mode & 0xFFFF)
        out += struct.pack(">I", 0)                     # unknown2
        out += struct.pack(">I", 0)                     # unknown3
        out += struct.pack(">I", UID)
        out += struct.pack(">I", GID)
        out += struct.pack(">I", self.mtime)
        out += struct.pack(">I", self.mtime)
        out += struct.pack(">I", self.mtime)
        out += struct.pack(">Q", self.size)
        out += bytes([FLAGS, 0])
        return bytes(out)


@dataclass
class MbdbWriter:
    entries: list[MbdbEntry] = field(default_factory=list)

    def add(self, entry: MbdbEntry) -> None:
        self.entries.append(entry)

    def build(self) -> bytes:
        out = bytearray(MBDB_HEADER)
        for entry in self.entries:
            out += entry.encode()
        return bytes(out)


# --------------------------------------------------------------------------- #
# 解析（用于预览 / 自检）
# --------------------------------------------------------------------------- #

class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) != n:
            raise ValueError("mbdb 数据不完整")
        self.pos += n
        return chunk

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack(">Q", self.take(8))[0]

    def field_str(self) -> str:
        n = self.u16()
        if n == NO_FIELD:
            return ""
        return self.take(n).decode("utf-8", "replace")

    def field_bytes(self) -> bytes:
        n = self.u16()
        if n == NO_FIELD:
            return b""
        return self.take(n)

    def at_end(self) -> bool:
        return self.pos >= len(self.data)


def parse_mbdb(data: bytes, *, tolerant: bool = True) -> list[MbdbEntry]:
    """解析 Manifest.mbdb，主要用于预览与单元测试。

    ``tolerant=True`` 时，遇到尾部残缺记录（Cowabunga 的 shell 脚本会多写
    几条全 0 的垃圾字节）就停止，不抛异常。
    """
    if not data.startswith(b"mbdb"):
        raise ValueError("不是 mbdb 文件")
    r = _Reader(data)
    r.take(6)
    entries: list[MbdbEntry] = []
    while not r.at_end():
        try:
            entry = _parse_one(r)
        except (ValueError, UnicodeDecodeError):
            if tolerant:
                break
            raise
        entries.append(entry)
    return entries


def _parse_one(r: "_Reader") -> MbdbEntry:
    """解析单条记录。"""
    if True:
        domain = r.field_str()
        path = r.field_str()
        link = r.field_str()
        digest = r.field_bytes()
        r.field_bytes()                                # encryption key
        mode = r.u16()
        r.u32(); r.u32()                                # unknown2/3
        r.u32(); r.u32()                                # uid/gid
        mtime = r.u32()
        r.u32(); r.u32()                                # atime/ctime
        size = r.u64()
        r.take(1)                                       # flags
        props = r.take(1)[0]
        for _ in range(props):
            r.field_str(); r.field_str()
        return MbdbEntry(domain, path, mode, size, mtime, digest, link)


# --------------------------------------------------------------------------- #
# 备份目录生成
# --------------------------------------------------------------------------- #

def sha1_bytes(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def file_id(domain: str, relative_path: str) -> str:
    """备份目录里实体文件的文件名。"""
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


@dataclass
class StagedItem:
    """一个待写入备份的设备文件。"""

    domain: str            # 设备侧 domain 名
    relative_path: str     # 设备侧相对路径
    data: bytes
    mode: int = MODE_FILE
    mtime: int = 0

    @property
    def path_hash(self) -> str:
        return file_id(self.domain, self.relative_path)


def stage_tree(root: Path) -> list[StagedItem]:
    """把 payload 工作区展开成 StagedItem 列表。

    **domain / path 的切分必须与 Cowabunga 的 CreateBackup.sh 完全一致**：

        Backup/SkipSetup/ConfigProfileDomain/Library/.../x.plist
               └ domain ┘└────── path（含 ConfigProfileDomain 前缀）─────┘

    也就是说 domain 取**顶层目录名**（如 ``SkipSetup``、``OTAKiller``），
    第二层起的全部内容（含 ``ConfigProfileDomain`` 这类名字）算 path。

    ``ConfigProfileDomain`` 由设备自行映射为
    ``SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles``，
    工具不替换，只按原样写入。
    """
    items: list[StagedItem] = []
    if not root.exists():
        return items
    now = int(time.time())
    for top in sorted(p for p in root.iterdir() if p.is_dir()):
        domain = top.name
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames.sort()
            base = Path(dirpath)
            for name in sorted(filenames):
                if name.startswith(".") or name.startswith("hiddendot"):
                    # hiddendot* 需要替换成 "." 前缀，见下
                    if not name.startswith("hiddendot"):
                        continue
                full = base / name
                rel = full.relative_to(top).as_posix()
                rel = rel.replace("hiddendot", ".")
                items.append(
                    StagedItem(
                        domain=domain,
                        relative_path=rel,
                        data=full.read_bytes(),
                        mtime=int(full.stat().st_mtime) or now,
                    )
                )
    return items


def parent_dirs(relative_path: str) -> list[str]:
    """返回某路径需要的所有父目录（由浅到深），不含自身。"""
    parts = [p for p in relative_path.split("/") if p]
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def build_mbdb(items: Iterable[StagedItem], *, with_dirs: bool = True) -> bytes:
    """构造 Manifest.mbdb。

    ``with_dirs=True`` 时会为每个文件的父目录补上目录记录 —— 这与 Cowabunga
    Lite 的 ``CreateBackup.sh`` 一致（它用 ``find`` 遍历整个目录树，目录本身
    也会写一条记录）。缺目录条目时设备可能不把文件放到目标路径，表现为
    「备份恢复了、文件却不在预期位置」。
    """
    writer = MbdbWriter()
    seen_dirs: set[tuple[str, str]] = set()
    for item in items:
        if with_dirs:
            # 先写 domain 根目录自身（path 为空）。
            # Cowabunga 的 `find "$domain" ! -name ".*"` 第一行就是 domain 目录本身，
            # path 被剥成空串并写成一条目录记录。缺这条时设备不会把文件
            # 挂到该 domain 下 —— 表现为「恢复成功、文件却没落地」。
            root_key = (item.domain, "")
            if root_key not in seen_dirs:
                seen_dirs.add(root_key)
                writer.add(
                    MbdbEntry(
                        domain=item.domain,
                        relative_path="",
                        mode=MODE_DIR,
                        size=0,
                        mtime=item.mtime,
                        digest=b"",
                    )
                )
            for parent in parent_dirs(item.relative_path):
                key = (item.domain, parent)
                if key in seen_dirs:
                    continue
                seen_dirs.add(key)
                writer.add(
                    MbdbEntry(
                        domain=item.domain,
                        relative_path=parent,
                        mode=MODE_DIR,
                        size=0,
                        mtime=item.mtime,
                        digest=b"",
                    )
                )
        writer.add(
            MbdbEntry(
                domain=item.domain,
                relative_path=item.relative_path,
                mode=item.mode,
                size=len(item.data),
                mtime=item.mtime,
                digest=sha1_bytes(item.data),
            )
        )
    return writer.build()


def write_backup_dir(
    device_dir: Path,
    items: Iterable[StagedItem],
    *,
    info_plist: bytes,
    status_plist: bytes,
    manifest_plist: bytes,
    manifest_db: Optional[bytes] = None,
) -> Path:
    """生成可被 ``restore --system`` 消费的最小备份目录（含 UDID 子目录）。

    同时写出 ``Manifest.mbdb``（旧格式）与 ``Manifest.db``（iOS 10.2+ 主用），
    两者共存无害，可覆盖更多系统版本。
    """
    device_dir.mkdir(parents=True, exist_ok=True)
    items = list(items)
    for item in items:
        (device_dir / item.path_hash).write_bytes(item.data)
    (device_dir / "Manifest.mbdb").write_bytes(build_mbdb(items))
    if manifest_db is not None:
        (device_dir / "Manifest.db").write_bytes(manifest_db)
    (device_dir / "Info.plist").write_bytes(info_plist)
    (device_dir / "Status.plist").write_bytes(status_plist)
    (device_dir / "Manifest.plist").write_bytes(manifest_plist)
    return device_dir


def iter_files(directory: Path) -> Iterator[Path]:
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.name not in {"Info.plist", "Status.plist", "Manifest.plist", "Manifest.mbdb"}:
            yield p
