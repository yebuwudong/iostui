"""设备访问层：列出设备、生成备份、执行 restore --system。

两条后端：

* ``cli`` —— libimobiledevice 命令行（idevicebackup2 / ideviceinfo），核心功能
* ``py``  —— pymobiledevice3（可选，提供 status 与 MCInstall 能力）

上层只依赖 :class:`Device` 与 :class:`Backend` 的少量方法。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from . import backupmeta, mbdb, preflight
from .payload import StagedItem

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]


class DeviceError(RuntimeError):
    """设备访问失败（未连接 / 未信任 / 非配对）。"""


class BackendMissing(DeviceError):
    """缺少后端依赖。"""


@dataclass
class Device:
    udid: str
    name: str = ""
    product_type: str = ""
    product_version: str = ""
    connection: str = ""          # USB / Network
    paired: bool = True
    hardware_model: str = ""      # 如 D47AP，查询更新目录时需要

    @property
    def label(self) -> str:
        parts = [self.name or self.product_type or self.udid[:12]]
        if self.product_type and self.name:
            parts.append(self.product_type)
        if self.product_version:
            parts.append(f"iOS {self.product_version}")
        if self.connection:
            parts.append(self.connection)
        return " · ".join(parts)


def _noop_log(_: str) -> None:
    pass


# --------------------------------------------------------------------------- #
# 依赖探测
# --------------------------------------------------------------------------- #

def _run(cmd: Sequence[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
        errors="replace",
    )


def have_pymobiledevice3() -> bool:
    try:
        import pymobiledevice3  # noqa: F401
        return True
    except Exception:
        return False


def have_libimobiledevice() -> bool:
    return shutil.which("idevicebackup2") is not None and shutil.which("idevice_id") is not None


# --------------------------------------------------------------------------- #
# pymobiledevice3 后端
# --------------------------------------------------------------------------- #

class PyBackend:
    name = "pymobiledevice3"

    def __init__(self) -> None:
        if not have_pymobiledevice3():
            raise BackendMissing(
                "未安装 pymobiledevice3，请先运行安装脚本（install.sh / install.ps1）"
            )

    # -- 设备 ------------------------------------------------------------- #

    def list_devices(self) -> list[Device]:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.usbmux import list_devices

        async def _go() -> list[Device]:
            found: list[Device] = []
            for mux_dev in await list_devices():
                dev = Device(udid=mux_dev.serial, connection=mux_dev.connection_type)
                try:
                    # autopair=True：未信任时会在手机上弹「信任」提示
                    async with await create_using_usbmux(serial=mux_dev.serial) as lockdown:
                        vals = lockdown.all_values
                        dev.name = vals.get("DeviceName", "")
                        dev.product_type = vals.get("ProductType", "")
                        dev.product_version = vals.get("ProductVersion", "")
                        dev.hardware_model = vals.get("HardwareModel", "")
                        dev.paired = True
                except Exception:
                    dev.paired = False
                found.append(dev)
            return found

        return asyncio.run(_go())

    # -- 设备信息 --------------------------------------------------------- #

    def device_info(self, udid: str) -> backupmeta.DeviceInfo:
        """读取生成备份元数据所需的真实设备信息。"""
        from pymobiledevice3.lockdown import create_using_usbmux

        async def _go() -> dict:
            async with await create_using_usbmux(serial=udid) as lockdown:
                return dict(lockdown.all_values)

        try:
            values = asyncio.run(_go())
        except Exception as exc:
            raise DeviceError(f"读取设备信息失败：{exc}") from exc
        info = backupmeta.DeviceInfo.from_lockdown(values, udid)
        info.validate()
        return info

    # -- 前置检查 --------------------------------------------------------- #

    def preflight(self, udid: str) -> list:
        """读取 com.apple.fmip / 锁屏状态，返回检查项列表。"""
        from pymobiledevice3.lockdown import create_using_usbmux

        values: dict | None = None
        error = ""
        locked: bool | None = None

        async def _go() -> tuple[dict | None, str, bool | None]:
            async with await create_using_usbmux(serial=udid) as lockdown:
                fmi: dict | None = None
                err = ""
                try:
                    got = await lockdown.get_value("com.apple.fmip")
                    fmi = got if isinstance(got, dict) else None
                except Exception as exc:      # 域不存在 / 权限不足
                    err = str(exc) or exc.__class__.__name__
                prot: bool | None = None
                try:
                    val = await lockdown.get_value(key="PasswordProtected")
                    if isinstance(val, bool):
                        prot = val
                except Exception:
                    pass
                return fmi, err, prot

        values, error, locked = asyncio.run(_go())
        return [
            preflight.evaluate_fmip(values, error=error),
            preflight.evaluate_passcode(locked),
        ]

    # -- 恢复 ------------------------------------------------------------- #

    def restore(self, udid: str, device_dir: Path, *,
                progress: Optional[ProgressFn] = None,
                log: LogFn = _noop_log) -> None:
        """把 device_dir（含 Info/Manifest/Manifest.mbdb/Status）恢复到设备。"""
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

        # pmd3 的 restore(backup_directory) 要求传入「包含 <UDID>/ 的父目录」，
        # 而我们的布局与 Cowabunga 一致（Backup/ 直接放文件）。
        # 走 pmd3 时临时造一层 <UDID>/ 软链接，避免复制数据。
        import shutil as _sh

        stage = Path(tempfile.mkdtemp(prefix="iostui-pmd3-"))
        link = stage / udid
        try:
            link.symlink_to(device_dir.resolve())
        except OSError:
            _sh.copytree(device_dir, link)
        backup_root = stage

        def _cb(value) -> None:
            # 设备回传的是 0..100 的百分比（少数版本给 0..1）
            try:
                pct = float(value)
            except (TypeError, ValueError):
                return
            if pct > 1.0:
                pct /= 100.0
            if progress:
                progress(max(0.0, min(1.0, pct)), "restore")

        async def _go() -> None:
            async with await create_using_usbmux(serial=udid) as lockdown:
                async with Mobilebackup2Service(lockdown) as svc:
                    log("正在写入设备，请保持连接…设备可能自动重启")
                    await svc.restore(
                        backup_directory=backup_root,
                        system=True,        # 关键：允许写系统路径
                        reboot=True,        # 重启才会重新加载配置
                        copy=False,         # 不让设备留存这份备份
                        settings=True,      # 保留设备设置
                        remove=False,       # 不删除未参与恢复的文件
                        skip_apps=True,     # 不要动 App
                        progress_callback=_cb,
                    )

        try:
            asyncio.run(_go())
        finally:
            _sh.rmtree(stage, ignore_errors=True)


# --------------------------------------------------------------------------- #
# libimobiledevice 后端
# --------------------------------------------------------------------------- #

class CliBackend:
    name = "libimobiledevice"

    def __init__(self) -> None:
        if not have_libimobiledevice():
            raise BackendMissing(
                "未找到 idevicebackup2 / idevice_id。"
                "Linux: apt install libimobiledevice-utils；macOS: brew install libimobiledevice"
            )

    def list_devices(self) -> list[Device]:
        res = _run(["idevice_id", "-l"])
        if res.returncode != 0:
            raise DeviceError(f"idevice_id 失败: {res.stderr.strip()}")
        devices: list[Device] = []
        for line in res.stdout.splitlines():
            udid = line.strip()
            if not udid:
                continue
            dev = Device(udid=udid)
            for key, attr in (
                ("DeviceName", "name"),
                ("ProductType", "product_type"),
                ("ProductVersion", "product_version"),
                ("HardwareModel", "hardware_model"),
            ):
                r = _run(["ideviceinfo", "-u", udid, "-k", key])
                if r.returncode == 0:
                    setattr(dev, attr, r.stdout.strip())
            devices.append(dev)
        return devices

    def device_info(self, udid: str) -> backupmeta.DeviceInfo:
        """用 ideviceinfo 读取设备信息。"""
        values: dict[str, str] = {}
        for key in ("SerialNumber", "DeviceName", "ProductType",
                    "ProductVersion", "BuildVersion", "UniqueDeviceID"):
            res = _run(["ideviceinfo", "-u", udid, "-k", key])
            if res.returncode == 0:
                values[key] = res.stdout.strip()
        if not values.get("ProductType"):
            raise DeviceError("无法读取设备信息（ProductType 为空）")
        info = backupmeta.DeviceInfo.from_lockdown(values, udid)
        info.validate()
        return info

    def preflight(self, udid: str) -> list:
        """用 ideviceinfo 读取 com.apple.fmip 与锁屏状态。"""
        values: dict | None = None
        error = ""
        res = _run(["ideviceinfo", "-u", udid, "-q", "com.apple.fmip", "-x"])
        if res.returncode == 0 and res.stdout.strip():
            try:
                import plistlib

                data = plistlib.loads(res.stdout.encode())
                values = data if isinstance(data, dict) else None
            except Exception as exc:
                error = f"解析失败: {exc}"
        else:
            error = (res.stderr or "该域不可用").strip()[:120]

        locked: bool | None = None
        res = _run(["ideviceinfo", "-u", udid, "-k", "PasswordProtected"])
        if res.returncode == 0:
            locked = res.stdout.strip().lower() == "true"

        return [
            preflight.evaluate_fmip(values, error=error),
            preflight.evaluate_passcode(locked),
        ]

    def restore(self, udid: str, device_dir: Path, *,
                progress: Optional[ProgressFn] = None,
                log: LogFn = _noop_log) -> None:
        backup_root = device_dir.parent
        log("正在写入设备，请保持连接…设备可能自动重启")
        # 与 Cowabunga 完全一致：cwd 是 Backup 的父目录，-s Backup，目录参数 "."
        proc = subprocess.Popen(
            ["idevicebackup2", "-u", udid, "-s", "Backup",
             "restore", "--system", "--skip-apps", "."],
            cwd=str(backup_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if "[=============] " in line and "%" in line:
                try:
                    pct = float(line.split("%")[0].split()[-1])
                    if progress:
                        progress(pct / 100.0, "restore")
                except ValueError:
                    pass
            log(line)
        code = proc.wait()
        if code != 0:
            raise DeviceError(f"idevicebackup2 恢复失败，退出码 {code}")


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #

def get_backend(prefer: str = "auto") -> "PyBackend | CliBackend":
    """选择后端；``auto`` 优先 pymobiledevice3。"""
    order: list[str]
    if prefer == "auto":
        # 优先 libimobiledevice 的 idevicebackup2 —— 与 Cowabunga Lite 使用的
        # 是同一套恢复实现。pymobiledevice3 虽然也能对话，但行为存在差异，
        # 在 iOS 18 上出现过「恢复成功、配置不生效」。
        order = ["cli", "py"]
    else:
        order = [prefer]
    errors = []
    for key in order:
        try:
            if key == "py" and have_pymobiledevice3():
                return PyBackend()
            if key == "cli" and have_libimobiledevice():
                return CliBackend()
        except BackendMissing as exc:      # pragma: no cover - 依赖缺失
            errors.append(str(exc))
    if errors:
        raise BackendMissing(errors[0])
    raise BackendMissing(
        "没有可用的设备后端。请安装 libimobiledevice：\n"
        "  Fedora:        sudo dnf install libimobiledevice-utils\n"
        "  Debian/Ubuntu: sudo apt install libimobiledevice-utils\n"
        "  macOS:         brew install libimobiledevice\n"
        "  Windows:       随 iTunes 安装，或用 pymobiledevice3 后端"
    )


def list_devices(prefer: str = "auto") -> list[Device]:
    return get_backend(prefer).list_devices()


# --------------------------------------------------------------------------- #
# 备份生成
# --------------------------------------------------------------------------- #

def _resource(name: str) -> bytes:
    return resources.files("iostui.resources").joinpath(name).read_bytes()


def make_backup(
    workdir: Path,
    items: Iterable[StagedItem],
    *,
    udid: str,
    info: Optional[backupmeta.DeviceInfo] = None,
) -> Path:
    """在工作区生成 ``workdir/<udid>/`` 备份目录，返回该目录。

    ``info`` 是设备真实信息；缺失时会抛错 —— 用空模板生成的备份会被设备拒绝
    （iOS 18 上表现为 ``ErrorCode: 4, 'link error'``）。
    只有在纯导出、且明确知道要手动修补时才给 ``udid`` 传占位值。
    """
    # 目录名必须是 Backup —— Cowabunga 的 CreateBackup.sh 把文件直接写进
    # documentsDirectory/Backup/（没有 <UDID> 子目录），随后用
    # `-s Backup` 指定 source。idevicebackup2 会按 -s 的值去拼路径。
    device_dir = Path(workdir) / "Backup"
    items = list(items)

    # 与 Cowabunga Lite 完全一致：只写 mbdb + 三个**原样**的元数据模板。
    #
    # 不要"改进"这些模板：
    #   · Info.plist   —— Cowabunga 用空 dict，实测可用
    #   · Manifest.plist / Status.plist —— 抄它脚本里的原文
    # 模板原样使用，不做改动，以保持 restore 兼容性。
    mbdb.write_backup_dir(
        device_dir,
        items,
        info_plist=_resource("Info.plist"),
        status_plist=_resource("Status.plist"),
        manifest_plist=_resource("Manifest.plist"),
        manifest_db=None,
    )
    return device_dir


class PreflightBlocked(DeviceError):
    """前置检查未通过，拒绝写入。"""

    def __init__(self, checks: list) -> None:
        self.checks = checks
        lines = [f"  {c.headline()}：{c.detail}" for c in checks]
        super().__init__("前置检查未通过：\n" + "\n".join(lines))


def run_preflight(udid: str, *, prefer: str = "auto",
                  skip: bool = False) -> list:
    """读取设备的写入前置条件。``skip=True`` 时直接返回空列表。"""
    if skip:
        return []
    backend = get_backend(prefer)
    fn = getattr(backend, "preflight", None)
    if fn is None:
        return []
    try:
        return fn(udid)
    except Exception:
        # 检查本身失败不应阻断流程，按「未知」处理
        return [preflight.Check(
            preflight.Verdict.UNKNOWN,
            "无法执行前置检查",
            "读取设备状态失败，请自行确认「查找我的 iPhone」已关闭。",
        )]


def apply_plan(
    plan_items: Iterable[StagedItem],
    *,
    udid: str,
    prefer: str = "auto",
    keep: Optional[Path] = None,
    progress: Optional[ProgressFn] = None,
    log: LogFn = _noop_log,
    checks: Optional[list] = None,
    force: bool = False,
) -> Path:
    """生成备份并写入设备；返回备份目录（keep 为空则用临时目录）。

    ``checks`` 为前置检查结果；有硬失败项时抛 :class:`PreflightBlocked`。
    ``force=True`` 允许在「状态未知」时继续（硬失败仍然拦截）。
    """
    items = list(plan_items)
    if not items:
        raise ValueError("没有需要写入的内容")
    if checks:
        blocked = [c for c in checks if c.hard_fail]
        if blocked:
            raise PreflightBlocked(blocked)
    backend = get_backend(prefer)
    # 生成备份前先取设备真实信息：空模板会被设备拒绝
    try:
        info = backend.device_info(udid)
    except Exception as exc:
        raise DeviceError(f"无法读取设备信息，无法生成可用的备份：{exc}") from exc
    root = keep or Path(tempfile.mkdtemp(prefix="iostui-"))
    try:
        device_dir = make_backup(root, items, udid=udid, info=info)
        log(f"已生成备份：{device_dir}（{len(items)} 个文件）")
        backend.restore(udid, device_dir, progress=progress, log=log)
        return device_dir
    finally:
        if keep is None:
            shutil.rmtree(root, ignore_errors=True)


def export_backup(plan_items: Iterable[StagedItem], dest: Path,
                  udid: str = "iostui-export",
                  info: Optional[backupmeta.DeviceInfo] = None) -> Path:
    """只生成备份文件（不写设备）。

    不给 ``info`` 时用占位信息生成，仅用于检查产物；**真要拿到别的电脑上执行
    restore，必须在目标设备上重新生成**（否则元数据里的 UDID 不匹配）。
    """
    return make_backup(Path(dest), plan_items, udid=udid, info=info)
