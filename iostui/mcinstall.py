"""MCInstall 服务：读写设备的云配置（监督状态）。

## 为什么需要它

「伪造备份 + restore --system」是把文件写到磁盘，但设备上有些状态是由守护进程
缓存在内存/数据库里的，光改文件不一定立刻生效，甚至会被忽略：

* 监督状态由 ``cloudconfigurationd`` 维护，权威副本在它自己的数据库里；
* 恢复完还要重启，且部分系统版本对「非正式渠道写入」的 plist 不买账。

``com.apple.mobile.MCInstall`` 是 Apple 给 Apple Configurator / MDM 用的管理服务，
``SetCloudConfiguration`` 是**官方接口**，直接让设备自己把监督配置落到正确位置，
不需要重启、也不依赖文件写入是否被接受。

本模块优先用它，失败再回退到备份恢复方式。
"""

from __future__ import annotations

import asyncio
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .device import DeviceError

SERVICE = "com.apple.mobile.MCInstall"

# 配置来源：2 = 通过云端配置（DEP/监督）下发
SOURCE_CLOUD = 2

SKIP_SETUP_ALL = [
    "WiFi", "Location", "Restore", "SIMSetup", "Android", "AppleID",
    "IntendedUser", "TOS", "Siri", "ScreenTime", "Diagnostics",
    "SoftwareUpdate", "Passcode", "Biometric", "Payment", "Zoom",
    "DisplayTone", "MessagingActivationUsingPhoneNumber",
    "HomeButtonSensitivity", "CloudStorage", "ScreenSaver", "TapToSetup",
    "Keyboard", "PreferredLanguage", "SpokenLanguage", "WatchMigration",
    "OnBoarding", "TVProviderSignIn", "TVHomeScreenSync", "Privacy",
    "TVRoom", "iMessageAndFaceTime", "AppStore", "Safety", "Multitasking",
    "ActionButton", "TermsOfAddress", "AccessibilityAppearance",
    "Welcome", "Appearance", "RestoreCompleted", "UpdateCompleted",
]


def build_cloud_configuration(supervised: bool, organization: str = "",
                              supervisor_der: Optional[bytes] = None) -> dict[str, Any]:
    """构造 SetCloudConfiguration 的结构。

    **只写必要字段**。``ConfigurationSource`` / ``IsMandatory`` /
    ``PostSetupProfileWasInstalled`` 等属于 MDM/DEP 语义，写进去会让设备开机
    进入「远程管理」登记流程并要求连接服务器。
    """
    if not supervised:
        return {
            "CloudConfigurationUIComplete": False,
            "IsSupervised": False,
            "OrganizationName": "",
            "SkipSetup": [],
        }
    config: dict[str, Any] = {
        "CloudConfigurationUIComplete": True,
        "IsSupervised": True,
        "OrganizationName": organization or "Managed Device",
        "SkipSetup": list(SKIP_SETUP_ALL),
    }
    if supervisor_der:
        config["SupervisorHostCertificates"] = [supervisor_der]
    return config


async def _with_service(udid: str, action):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobile_config import MobileConfigService

    async with await create_using_usbmux(serial=udid) as lockdown:
        async with MobileConfigService(lockdown) as svc:
            return await action(svc)


def get_current(udid: str) -> Optional[dict[str, Any]]:
    """读取设备当前生效的云配置（None 表示未受管）。"""
    try:
        return asyncio.run(_with_service(udid, lambda svc: svc.get_cloud_configuration()))
    except Exception as exc:
        raise DeviceError(f"读取云配置失败：{exc}") from exc


def set_supervision(udid: str, supervised: bool, organization: str = "",
                    supervisor_der: Optional[bytes] = None) -> dict[str, Any]:
    """用官方接口直接设置监督状态，返回写入后的配置。"""
    config = build_cloud_configuration(supervised, organization, supervisor_der)

    async def _do(svc):
        await svc.hello()
        await svc.set_cloud_configuration(config)
        # 立刻读回，确认设备真的接受了
        return await svc.get_cloud_configuration()

    try:
        return asyncio.run(_with_service(udid, _do)) or {}
    except Exception as exc:
        raise DeviceError(f"设置云配置失败：{exc}") from exc


def verify(udid: str, expect_supervised: bool) -> tuple[bool, str]:
    """读回校验。返回 (是否符合预期, 说明)。"""
    try:
        current = get_current(udid)
    except DeviceError as exc:
        return False, str(exc)
    if not current:
        return (not expect_supervised), "设备当前无云配置" + (
            "（符合预期）" if not expect_supervised else "，监督未生效"
        )
    actual = bool(current.get("IsSupervised"))
    org = current.get("OrganizationName", "")
    if actual == expect_supervised:
        return True, f"IsSupervised={actual}，组织={org or '(空)'}"
    return False, f"设备报告 IsSupervised={actual}，与预期 {expect_supervised} 不符"


def list_profiles(udid: str) -> dict[str, Any]:
    """列出设备上已安装的描述文件。"""

    async def _do(svc):
        await svc.hello()
        return await svc.get_profile_list()

    try:
        return asyncio.run(_with_service(udid, _do)) or {}
    except Exception as exc:
        raise DeviceError(f"读取描述文件列表失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# 状态判定：区分「真受监督」与「残留的半成品云配置」
# --------------------------------------------------------------------------- #

class CloudState(str, Enum):
    UNMANAGED = "unmanaged"          # 完全没有云配置
    SUPERVISED = "supervised"        # 真正受监督，可用
    RESIDUAL = "residual"            # 有残留云配置但未受监督 —— 卡住了


def classify(cfg: Optional[dict[str, Any]]) -> CloudState:
    """判断设备当前云配置处于哪种状态。

    残留状态（RESIDUAL）是我们在 iOS 18 上踩到的坑：用备份恢复硬写 plist 后，
    设备留下了 ``CloudConfigurationUIComplete=True`` 但 ``IsSupervised=False``、
    ``ConfigurationSource`` 缺失的半成品。它既不算受监督，又占住了
    「云配置已存在」这个名额，使得官方 ``SetCloudConfiguration`` 报
    ``14002 / A cloud configuration is already present``，且只能抹机解除。
    """
    if not cfg:
        return CloudState.UNMANAGED
    if cfg.get("IsSupervised") is True:
        return CloudState.SUPERVISED
    # 有任何「已配置」痕迹，但 IsSupervised 不为真 → 残留
    looks_configured = bool(
        cfg.get("CloudConfigurationUIComplete")
        or cfg.get("OrganizationName")
        or cfg.get("ConfigurationWasApplied")
        or cfg.get("OrganizationMagic")
    )
    return CloudState.RESIDUAL if looks_configured else CloudState.UNMANAGED


def explain(cfg: Optional[dict[str, Any]]) -> list[str]:
    """把状态翻译成人能看懂的说明与建议。"""
    state = classify(cfg)
    if state is CloudState.SUPERVISED:
        return ["✓ 设备已受监督，延迟更新等管控可以生效。"]

    if state is CloudState.UNMANAGED:
        return [
            "设备没有任何云配置，可以直接用 MCInstall 接口开启监督：",
            "    iostui supervise --on --org \"My iPhone\"",
        ]

    # RESIDUAL
    return [
        "⚠ 设备上存在残留的云配置：有配置痕迹，但 IsSupervised 不为真。",
        "  设备上存在配置痕迹但未真正受监督。",
        "",
        "  官方 SetCloudConfiguration 在有云配置时会返回 14002 拒绝覆盖。",
        "",
        "  建议：",
        "  1) 先试 iostui supervise --off 把残留清掉，再 iostui supervise --on 重设",
        "  2) 若仍被拒，用 iostui ota --on 处理系统更新（不需要监督状态）",
    ]


def install_profile(udid: str, payload_bytes: bytes,
                    *, silent: bool = False, keybag: Optional[str] = None) -> None:
    """把描述文件推到设备上安装。

    ``silent=False`` 时用 ``InstallProfile``：设备会弹确认框，用户在手机上点
    「安装」即可（不需要数据线传文件）。这是最省事的路径。
    ``silent=True`` 需要监督身份（keybag），只在已受监督的设备上可用。
    """
    async def _do(svc):
        await svc.hello()
        if silent:
            if not keybag:
                raise DeviceError("静默安装需要监督身份文件")
            await svc.install_profile_silent(Path(keybag), payload_bytes)
        else:
            await svc.install_profile(payload_bytes)

    try:
        asyncio.run(_with_service(udid, _do))
    except DeviceError:
        raise
    except Exception as exc:
        raise DeviceError(f"安装描述文件失败：{exc}") from exc


def has_profile(udid: str, identifier_prefix: str = "com.iostui.") -> list[str]:
    """列出设备上由本工具安装的描述文件标识。"""
    try:
        pl = list_profiles(udid)
    except DeviceError:
        return []
    meta = pl.get("ProfileMetadata") or {}
    return [k for k in meta if identifier_prefix in str(k)]
