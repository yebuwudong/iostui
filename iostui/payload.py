"""生成要写进设备的 plist payload。

本工具的改动全部通过「伪造局部备份 + restore --system」完成，不动系统分区、
不需要越狱。每个功能只是一组 ``StagedItem``：

* 监督模式开 / 关        -> ``CloudConfigurationDetails.plist`` (+ purplebuddy)
* 屏蔽 / 恢复系统更新    -> ``com.apple.MobileAsset.plist``
* 跳过开机引导          -> ``com.apple.purplebuddy.plist``
"""

from __future__ import annotations

import json
import plistlib
import time
from datetime import date
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable

from .mbdb import MODE_FILE, StagedItem

# 设备上的真实 domain 名（不是工作区目录名）
# Cowabunga 的流程是：把 workspace/<tweak>/ 下的**内容**拷进 EnabledTweaks/，
# 再对 EnabledTweaks/ 的每个子目录执行 CreateBackup.sh。所以工作区里的
# `SkipSetup/ConfigProfileDomain/...` 到了 EnabledTweaks 下就是
# `ConfigProfileDomain/...`，domain 取的是**这一层**。
#
# 于是：
#   · ConfigProfileDomain 会被脚本替换成 SysSharedContainerDomain-...（设备域名）
#   · 其余 domain（ManagedPreferencesDomain 等）原样保留
#
# domain 使用设备域名，path 从其下一层开始。
DOMAIN_CONFIGPROFILE = "SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles"
DOMAIN_MANAGED = "ManagedPreferencesDomain"

PATH_CLOUD_CONFIG = "Library/ConfigurationProfiles/CloudConfigurationDetails.plist"
PATH_PURPLEBUDDY = "mobile/com.apple.purplebuddy.plist"
PATH_MOBILE_ASSET = "mobile/com.apple.MobileAsset.plist"

# 屏蔽 OTA 时把更新源指向 tvOS 的种子服务器（Apple 官方地址，iOS 上没有可用资产）
OTA_ASSET_KEYS = {
    "MobileAssetServerURL-com.apple.MobileAsset.MobileSoftwareUpdate.UpdateBrain":
        "https://mesu.apple.com/assets/tvOS16DeveloperSeed",
    "MobileAssetSUAllowOSVersionChange": False,
    "MobileAssetSUAllowSameVersionFullReplacement": False,
    "MobileAssetServerURL-com.apple.MobileAsset.RecoveryOSUpdate":
        "https://mesu.apple.com/assets/tvOS16DeveloperSeed",
    "MobileAssetServerURL-com.apple.MobileAsset.RecoveryOSUpdateBrain":
        "https://mesu.apple.com/assets/tvOS16DeveloperSeed",
    "MobileAssetServerURL-com.apple.MobileAsset.SoftwareUpdate":
        "https://mesu.apple.com/assets/tvOS16DeveloperSeed",
    "MobileAssetAssetAudience": "65254ac3-f331-4c19-8559-cbe22f5bc1a6",
}


# 跳过哪些设置向导页面（Cowabunga 原版列表）
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


def _resource(name: str) -> bytes:
    return resources.files("iostui.resources").joinpath(name).read_bytes()


def _plist_bytes(obj: dict) -> bytes:
    return plistlib.dumps(obj, fmt=plistlib.FMT_XML, sort_keys=False)


def _item(domain: str, rel_path: str, data: bytes, mtime: int | None = None) -> StagedItem:
    return StagedItem(
        domain=domain,
        relative_path=rel_path,
        data=data,
        mode=MODE_FILE,
        mtime=int(mtime if mtime is not None else time.time()),
    )


# --------------------------------------------------------------------------- #
# 监督模式
# --------------------------------------------------------------------------- #

def cloud_config(supervised: bool, organization: str = "",
                 supervisor_der: bytes | None = None) -> dict:
    """构造 CloudConfigurationDetails 内容。

    **只写 Cowabunga Lite 会写的那几个字段**，不要加任何额外键：

        CloudConfigurationUIComplete / IsSupervised / OrganizationName / SkipSetup

    ``ConfigurationSource`` / ``IsMandatory`` 等属于 **MDM/DEP** 语义，
    会导致设备开机进入「远程管理」界面，不可加入。
    """
    # 底层模板只有 4 个键，从这里开始构造，天然不会带 DEP 字段
    out = plistlib.loads(_resource("CloudConfigurationDetails.plist"))

    if not supervised:
        # 关闭监督：只把 IsSupervised 置 false。
        #
        # 为什么不清空整个文件：清空后设备会把自己当成「从没配置过的新机」，
        # 配合 purplebuddy 的状态变化，开机就会重走设置流程、卡在设置界面。
        # 保留 CloudConfigurationUIComplete=True 与完整 SkipSetup，
        # 让设备认为「初始化已完成，只是不再受监督」—— 这是能正常进桌面的状态。
        out["IsSupervised"] = False
        out["OrganizationName"] = ""
        out["CloudConfigurationUIComplete"] = True
        out["SkipSetup"] = list(SKIP_SETUP_ALL)
        return out

    out["CloudConfigurationUIComplete"] = True
    out["IsSupervised"] = True
    out["OrganizationName"] = organization
    out["SkipSetup"] = list(SKIP_SETUP_ALL)
    if supervisor_der:
        out["SupervisorHostCertificates"] = [supervisor_der]
    return out


def purplebuddy(skip_setup: bool) -> dict:
    if not skip_setup:
        return {}
    return {"SetupDone": True, "SetupFinishedAllSteps": True, "UserChoseLanguage": True}


def supervision_items(supervised: bool, organization: str = "",
                      skip_setup: bool = True,
                      supervisor_der: bytes | None = None) -> list[StagedItem]:
    """监督模式要写入的文件。

    **purplebuddy.plist 永远写「设置已完成」**（不管开还是关监督）。

    「跳过哪些设置项」由 CloudConfigurationDetails 的 ``SkipSetup`` 控制；
    purplebuddy 只表示「初始化流程已经走完」，任何时候都保持为真。
    """
    return [
        _item(DOMAIN_CONFIGPROFILE, PATH_CLOUD_CONFIG,
              _plist_bytes(cloud_config(supervised, organization, supervisor_der))),
        _item(DOMAIN_MANAGED, PATH_PURPLEBUDDY,
              _plist_bytes(purplebuddy(True))),
    ]


# --------------------------------------------------------------------------- #
# OTA 系统更新
# --------------------------------------------------------------------------- #

def ota_items(blocked: bool) -> list[StagedItem]:
    """屏蔽 / 恢复系统更新。

    采用 **Cowabunga Lite macOS 版**的完整写法（各历史版本一致，长期在用）：
    把各更新资产的服务器指向 tvOS 种子源，并把 ``MobileAssetAssetAudience``
    设为 tvOS 14 developer seed 的 UUID。iOS 拿不到适用于自己的资产，
    检查更新就永远显示「已是最新」。

    关键点：**不能只写空 audience**。Windows 版那种精简写法在部分 iOS 版本上
    不生效（实测屏蔽没效果）。必须把服务器地址一起改掉。

    恢复：写空 dict，让系统重建默认配置。
    """
    if blocked:
        return [_item(DOMAIN_MANAGED, PATH_MOBILE_ASSET,
                      _plist_bytes(dict(OTA_ASSET_KEYS)))]
    return [_item(DOMAIN_MANAGED, PATH_MOBILE_ASSET, _plist_bytes({}))]


# --------------------------------------------------------------------------- #
# 附加：延迟更新的描述文件（需监督模式）
# --------------------------------------------------------------------------- #

def delayed_update_profile(organization: str = "", days: int = 90) -> bytes:
    """生成 ``forceDelayedSoftwareUpdates`` 描述文件。

    Apple 官方定义（developer.apple.com/documentation/devicemanagement/restrictions）：

        ``forceDelayedSoftwareUpdates`` — 为 true 时，系统**延迟用户看到**
        软件更新的时间。默认 30 天，可用 ``enforcedSoftwareUpdateDelay`` 改。
        ``enforcedSoftwareUpdateDelay`` — 延迟多少天。用户在该更新**发布日**
        之后经过这么多天，才会看到它。

    要点：

    * 延迟是按天数筛的，但对「钉住某个版本」非常有用：把天数设到实测值，
      比目标版本更新的版本就都还不可见（服务端按发布满 N 天筛选）。
      具体算法见 :mod:`iostui.pallas`，一般直接用 :func:`pinned_profile`。
    * 必须已处于监督模式，否则系统忽略（``Requires supervision: iOS``）。
    * iOS 26 起废弃，iOS 27 起移除；需要改用声明式
      ``com.apple.configuration.softwareupdate.settings``。
    """
    days = max(1, min(int(days), 90))
    org = organization or "Managed Device"
    payload = {
        "PayloadDisplayName": f"延迟系统更新（{days} 天）",
        "PayloadIdentifier": f"com.iostui.delayupdate.{uuid.uuid4()}",
        "PayloadOrganization": org,
        "PayloadType": "com.apple.applicationaccess",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadVersion": 1,
        "forceDelayedSoftwareUpdates": True,
        "forceDelayedAppSoftwareUpdates": True,
        "enforcedSoftwareUpdateDelay": days,
    }
    profile = {
        "PayloadContent": [payload],
        "PayloadDisplayName": f"延迟系统更新（{days} 天）",
        "PayloadIdentifier": f"com.iostui.delayupdate.{uuid.uuid4()}",
        "PayloadOrganization": org,
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadVersion": 1,
    }
    return _plist_bytes(profile)


def pinned_profile(target: str, delay: int, *, organization: str = "",
                   expires: "date | None" = None,
                   build: str = "") -> bytes:
    """生成「把设备钉在指定系统版本」的描述文件。

    原理见 :mod:`iostui.pallas`：设置 ``enforcedSoftwareUpdateDelay`` 为实测出的
    ``delay``，Apple 就会只返回发布满这么多天的更新，于是比 ``target`` 更新的
    版本都还不可见。

    注意延迟天数会随时间漂移：到 ``expires`` 之后就不再能钉住，需要卸载旧描述
    文件、重新生成并安装一份 delay 更大的。
    """
    org = organization or "Managed Device"
    label = f"锁定 iOS {target}"
    if build:
        label += f" ({build})"
    inner = {
        "PayloadDescription": "Configures restrictions",
        "PayloadDisplayName": "Restrictions",
        "PayloadIdentifier": f"com.apple.applicationaccess.{uuid.uuid4()}",
        "PayloadType": "com.apple.applicationaccess",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadVersion": 1,
        "enforcedSoftwareUpdateDelay": int(delay),
        "forceDelayedSoftwareUpdates": True,
    }
    profile = {
        "PayloadContent": [inner],
        "PayloadDisplayName": label,
        "PayloadDescription": (
            f"锁定到 iOS {target}：延迟 {delay} 天"
            + (f"，有效期至 {expires}" if expires else "")
        ),
        "PayloadIdentifier": f"com.iostui.pin.{target}.{uuid.uuid4()}",
        "PayloadOrganization": org,
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadVersion": 1,
    }
    return _plist_bytes(profile)


# --------------------------------------------------------------------------- #
# 组合
# --------------------------------------------------------------------------- #

@dataclass
class Plan:
    """一次要写入设备的完整改动集合。"""

    supervised: bool | None = None      # None = 不改
    organization: str = ""
    skip_setup: bool = True
    ota_blocked: bool | None = None     # None = 不改
    supervisor_der: bytes | None = None  # 监督身份证书（DER）

    def items(self) -> list[StagedItem]:
        items: list[StagedItem] = []
        if self.supervised is not None:
            items += supervision_items(self.supervised, self.organization,
                                       self.skip_setup, self.supervisor_der)
        if self.ota_blocked is not None:
            items += ota_items(self.ota_blocked)
        return items

    def describe(self) -> list[str]:
        lines = []
        if self.supervised is True:
            lines.append(f"开启监督模式（组织: {self.organization or '未命名'}）")
        elif self.supervised is False:
            lines.append("关闭监督模式并还原受管配置")
        if self.ota_blocked is True:
            lines.append("屏蔽系统更新（OTA）")
        elif self.ota_blocked is False:
            lines.append("恢复系统更新（OTA）")
        return lines


def summarize(items: Iterable[StagedItem]) -> list[str]:
    out = []
    for it in items:
        out.append(f"{it.domain}/{it.relative_path}  ({len(it.data)} B)")
    return out


def write_payload_tree(items: Iterable[StagedItem], root: Path) -> Path:
    """把 payload 落盘成 ``domain/relative_path`` 目录树，便于人工检查。"""
    for it in items:
        target = root / it.domain / it.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(it.data)
    return root


def enforcement_declaration(target_version: str, organization: str = "",
                            deadline: str = "") -> bytes:
    """生成「强制更新到指定版本」的声明式配置。

    Apple 定义：``com.apple.configuration.softwareupdate.enforcement.specific``
    用 ``TargetOSVersion``（如 ``16.1``）指定要装到哪个版本；可选的
    ``TargetBuildVersion``（如 ``20A242``）在测试种子期指定具体构建。

    注意这是**强制更新到该版本**，不是「停留在当前版本」。区别：

    * 想「停在某版」   → 用 :func:`pinned_profile`（延迟天数钉版本）。
    * 想「永远不升级」 → 用 ``ota_items(True)`` 屏蔽更新源。
    * 想「统一升到某版」→ 用本函数（方向相反，是催着升）。

    产出的声明需要设备处于监督模式，并通过 MDM / DDM 下发；手动安装 .mobileconfig
    无法生效（声明式配置不走描述文件通道）。
    """
    org = organization or "Managed Device"
    payload: dict = {
        "TargetOSVersion": target_version,
    }
    if deadline:
        payload["TargetLocalDateTime"] = deadline
    declaration = {
        "Type": "com.apple.configuration.softwareupdate.enforcement.specific",
        "Identifier": f"com.iostui.enforce.{uuid.uuid4()}",
        "ServerToken": str(uuid.uuid4()),
        "Payload": payload,
    }
    return json.dumps(declaration, ensure_ascii=False, indent=2).encode("utf-8")
