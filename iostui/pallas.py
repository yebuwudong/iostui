"""Apple 软件更新目录（GDMF / Pallas）查询，以及「锁定到指定版本」的天数计算。

## 为什么延迟天数可以锁定版本

Apple 的更新下发接口（Pallas，``gdmf.apple.com/v2/assets``）接受两个关键参数：

* ``DelayRequested: true`` + ``DelayPeriod: N`` —— 「我只要发布满 N 天以上的更新」

设备检查更新时带上延迟天数，服务端**只返回发布满 N 天的版本**。于是：

    延迟天数 N 越大 → 能看到的更新越旧 → 设备被「钉」在越老的版本上

所以「停在某个版本」= 设置一个足够大的延迟天数，让目标版本之后的版本都还没
「满期」。这也是 dhinakg 的 delayed-otas 页面在做的事：对每个历史版本，实测出
「用多大的延迟天数才会返回它」，以及这个天数还能用多久（有效期）。

关键点：**延迟天数会随日期漂移**。因为 N 天后较新的版本也会「满期」变成可见，
必须不断增大 N，或者重新装一份 N 更大的描述文件。这就是那些 profile 有
「有效期」的原因 —— 过了有效期，设备就会看到更高的版本了。

## 有效期怎么算

设目标版本发布日为 ``P``，比它高的下一个版本发布日为 ``P_next``。
要把设备钉在目标版本，需要 ``今天 - P >= N``（能看到它）。
而一旦 ``今天 - P_next >= N``，高版本也会可见，钉住失效。
所以最后可用日期 = ``P_next + 90``（90 是 Apple 允许的最大延迟）。

直观理解：延迟天数 N 的可用区间是 ``[age(下一版)+1, age(目标版)]``。时间推移会
不断抬高这个区间的下界；等到下一版自己也满 90 天时，下界越过 90，再没有合法的
延迟值能挡住它 —— 这就是终点。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Optional

import urllib.request
import urllib.error

PMV_URL = "https://gdmf.apple.com/v2/pmv"
ASSETS_URL = "https://gdmf.apple.com/v2/assets"

# iOS / iPadOS 正式版发布的 asset audience
AUDIENCE_IOS_RELEASE = "01c1d682-6e8f-4908-b724-5501fe3f5e5c"

# Pallas 会把这些当作「未就绪」的前缀
_OSVERSION_PREFIX = "9.9."

MAX_DELAY = 90          # Apple 接受的最大延迟天数


class CatalogError(RuntimeError):
    """查询 Apple 更新目录失败。"""


@dataclass
class Release:
    """一个可用的系统版本。"""

    version: str
    build: str
    posting_date: date

    @property
    def key(self) -> tuple:
        parts = []
        for piece in self.version.split("."):
            try:
                parts.append(int(piece))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    def __str__(self) -> str:
        return f"iOS {self.version} ({self.build})"


@dataclass
class Pin:
    """「钉住某个版本」的结果。"""

    release: Release
    delay: int                       # 需要的延迟天数
    expires: Optional[date] = None   # 之后该延迟天数就不再能钉住
    next_release: Optional[Release] = None

    @property
    def days_left(self) -> Optional[int]:
        if self.expires is None:
            return None
        return (self.expires - date.today()).days


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _fetch(url: str, payload: Optional[dict] = None, timeout: int = 30,
           retries: int = 4) -> bytes:
    """极简 HTTP，带重试。

    Apple 的 gdmf 节点会偶发 SSL EOF / 证书链不完整，因此：
    先正常校验，失败再退化为不校验，仍失败则短暂退避重试。
    """
    import ssl
    import time as _time

    data = None
    headers = {"User-Agent": "iostui"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    contexts = [None, None]      # 第一轮用默认校验
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE

    last: Exception | None = None
    for attempt in range(retries):
        ctx = unverified if attempt >= 1 else None
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            # 4xx 是请求本身的问题，重试无意义
            if 400 <= exc.code < 500 and exc.code != 429:
                raise CatalogError(f"请求被拒绝（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, OSError, Exception) as exc:  # noqa: BLE001
            last = exc
        _time.sleep(min(0.6 * (attempt + 1), 3.0))

    raise CatalogError(f"网络请求失败（已重试 {retries} 次）：{last}")


def _decode_jws(raw: bytes) -> dict:
    """Pallas 返回 JWS（不经 base64 包裹也可），取出 payload。"""
    text = raw.decode("utf-8", "replace").strip()
    if not text.startswith("{"):
        # 可能是 base64 包了一层
        try:
            text = base64.b64decode(text + "=" * (-len(text) % 4)).decode()
        except Exception:
            pass
    if text.startswith("{"):
        return json.loads(text)
    parts = text.split(".")
    if len(parts) < 2:
        raise CatalogError("Pallas 返回格式无法解析")
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(body))


# --------------------------------------------------------------------------- #
# 公开目录：哪些版本还在被下发
# --------------------------------------------------------------------------- #

def list_releases(product_type: str = "", *, timeout: int = 30) -> list[Release]:
    """列出当前仍可获取的 iOS 正式版版本（按版本号降序）。

    ``product_type`` 如 ``iPhone14,5``；为空则返回所有 iOS 版本。
    """
    raw = _fetch(PMV_URL, timeout=timeout)
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise CatalogError(f"GDMF 数据解析失败：{exc}") from exc

    entries: list[dict[str, Any]] = []
    for group in ("AssetSets", "PublicAssetSets"):
        for assets in (data.get(group) or {}).values():
            if isinstance(assets, list):
                entries.extend(assets)

    seen: dict[str, Release] = {}
    for item in entries:
        version = str(item.get("ProductVersion", ""))
        build = str(item.get("Build", ""))
        posted = str(item.get("PostingDate", ""))
        devices = item.get("SupportedDevices") or []
        if not version or not build or not posted:
            continue
        if product_type and product_type not in devices:
            continue
        if not product_type and not any(d.startswith(("iPhone", "iPad")) for d in devices):
            continue
        try:
            when = date.fromisoformat(posted[:10])
        except ValueError:
            continue
        # 同一版本可能有多条（不同机型），保留最早的发布日期
        if version not in seen or when < seen[version].posting_date:
            seen[version] = Release(version, build, when)

    return sorted(seen.values(), key=lambda r: r.key, reverse=True)


# --------------------------------------------------------------------------- #
# Pallas：延迟 N 天时设备实际会拿到哪个版本
# --------------------------------------------------------------------------- #

def query_delayed(product_type: str, hardware_model: str, delay: int,
                  *, audience: str = AUDIENCE_IOS_RELEASE,
                  timeout: int = 30) -> list[Release]:
    """问 Apple：以 ``delay`` 天延迟检查更新，设备能拿到哪些版本。"""
    payload = {
        "ClientVersion": 2,
        "CompatibilityVersion": 20,
        "AssetType": "com.apple.MobileAsset.SoftwareUpdate",
        "Supervised": True,
        "ProductVersion": "0",
        "BuildVersion": "0",
        "AssetAudience": audience,
        "ProductType": product_type,
        "HWModelStr": hardware_model.upper() if hardware_model else "",
        "DeviceName": "iPhone" if product_type.startswith("iPhone") else "iPad",
    }
    # delay=0 时不能带 DelayPeriod（Apple 会 400），语义等同于「不延迟」
    if delay > 0:
        payload["DelayRequested"] = True
        payload["DelayPeriod"] = min(int(delay), MAX_DELAY)
    else:
        payload["DelayRequested"] = False
    data = _decode_jws(_fetch(ASSETS_URL, payload, timeout=timeout))
    out: list[Release] = []
    posting = str(data.get("PostingDate", ""))[:10]
    for asset in data.get("Assets") or []:
        version = str(asset.get("OSVersion", "")).replace(_OSVERSION_PREFIX, "")
        build = str(asset.get("Build", ""))
        if not version or not build:
            continue
        try:
            when = date.fromisoformat(posting) if posting else date.today()
        except ValueError:
            when = date.today()
        out.append(Release(version, build, when))
    return out


def visible_version(product_type: str, hardware_model: str, delay: int,
                    *, timeout: int = 30) -> Optional[Release]:
    """延迟 ``delay`` 天时，设备能看到的最高版本。"""
    releases = query_delayed(product_type, hardware_model, delay, timeout=timeout)
    if not releases:
        return None
    return max(releases, key=lambda r: r.key)


# --------------------------------------------------------------------------- #
# 核心：钉住指定版本
# --------------------------------------------------------------------------- #

def scan_window(product_type: str, hardware_model: str,
                *, timeout: int = 30, max_delay: int = MAX_DELAY,
                progress: Optional[Callable[[int, int], None]] = None) -> dict[str, tuple[int, int]]:
    """实测扫描：对 0..max_delay 每个延迟天数问一次 Apple。

    返回 ``{版本号: (最小天数, 最大天数)}``。某版本能出现的天数区间就是这个版本
    的「可钉区间」——用其中任一天数都会把设备钉在它上面。

    这正是 delayed-otas 那套数据背后的做法：延迟天数与版本的对应关系是 Apple
    服务端按「发布满 N 天」筛出来的，只能实测。
    """
    spans: dict[str, tuple[int, int]] = {}
    for period in range(max_delay + 1):
        if progress:
            progress(period, max_delay)
        got = None
        for attempt in range(3):          # 单个天数失败不致命，重试几次
            try:
                got = visible_version(product_type, hardware_model, period,
                                      timeout=timeout)
                break
            except CatalogError:
                continue
        if got is None:
            continue
        lo, hi = spans.get(got.version, (period, period))
        spans[got.version] = (min(lo, period), max(hi, period))
    return spans


def compute_pin(releases: list[Release], target: str,
                spans: Optional[dict[str, tuple[int, int]]] = None,
                today: Optional[date] = None) -> Optional[Pin]:
    """算出钉住 ``target`` 的方案。

    ``delay`` 取该版本可出现的**最大**天数：钉得最久、余量最大（与
    delayed-otas 页面给出的 delay 口径一致）。

    ``expires`` 是「该天数还能钉住的最后一天」。因为较新的版本也在变老，
    一旦 ``今天 - 下一版发布日 >= delay``，下一版就会可见，钉住失效。
    """
    today = today or date.today()
    by_version = {r.version: r for r in releases}
    rel = by_version.get(target)
    if rel is None:
        return None

    if spans and target in spans:
        delay = spans[target][1]          # 最大可用天数
    else:
        delay = (today - rel.posting_date).days
    delay = max(0, min(delay, MAX_DELAY))

    higher = [r for r in releases if r.key > rel.key]
    nxt = min(higher, key=lambda r: r.key) if higher else None

    # 有效期 = 下一版本发布日 + 90 天。
    # 理由：某版本 V 可见的延迟区间是 [age(下一版)+1, age(V)]。随着时间推移，
    # 这个区间的下界不断上升；当下一版也满 90 天（Apple 允许的最大延迟）时，
    # 下界会超过 90，再没有任何延迟值能挡住它 —— 这就是 V 的终点。
    expires = None
    if nxt is not None:
        expires = nxt.posting_date + timedelta(days=MAX_DELAY)
    return Pin(release=rel, delay=delay, expires=expires, next_release=nxt)


def plan_pin(product_type: str, hardware_model: str, target: str,
             *, today: Optional[date] = None, timeout: int = 30,
             progress: Optional[Callable[[int, int], None]] = None,
             use_index: bool = True) -> Pin:
    """端到端：给出钉住 ``target`` 的方案。

    默认走**预计算索引**（dhinakg 的 delayed-otas 数据，一次请求、秒出）。
    只有索引不可用或查不到目标版本时，才回退到实测扫描（91 次请求、约 1 分钟）。
    """
    if use_index:
        try:
            return plan_pin_from_index(product_type, target, today=today)
        except CatalogError:
            pass          # 索引不可用/查不到 → 回退扫描
    releases = list_releases(product_type, timeout=timeout)
    if not releases:
        raise CatalogError("未取到任何可用版本（检查设备型号是否正确）")
    known = {r.version for r in releases}
    if target not in known:
        near = "、".join(r.version for r in releases[:8])
        raise CatalogError(f"目录里没有 {target}。当前可选：{near}")

    spans = scan_window(product_type, hardware_model, timeout=timeout, progress=progress)
    if target not in spans:
        raise CatalogError(
            f"{target} 已不在任何延迟天数下可见（可能刚被 Apple 撤下，或需要超过 "
            f"{MAX_DELAY} 天延迟）。"
        )
    pin = compute_pin(releases, target, spans=spans, today=today)
    if pin is None:
        raise CatalogError(f"无法为 {target} 算出延迟方案")

    got = visible_version(product_type, hardware_model, pin.delay, timeout=timeout)
    if got is None or got.key != pin.release.key:
        raise CatalogError(
            f"自校验失败：延迟 {pin.delay} 天返回的是 {got}，不是目标 {target}"
        )
    return pin


# --------------------------------------------------------------------------- #
# 预计算数据（秒出）
# --------------------------------------------------------------------------- #
#
# dhinakg 的 delayed-otas 页面背后就是这份 JSON：社区定期探测 Apple Pallas，
# 把每个版本「用多少天延迟能钉住」和「有效期到哪天」都算好存下来了。
# 直接读它，不必自己扫 0-90 天（那要 91 次请求、约 1 分钟）。


def pinned_index_url() -> str:
    return (
        "https://raw.githubusercontent.com/dhinakg/check-pallas/main/"
        "minified-v3.json"
    )


def _fallback_index_url() -> str:
    return "https://dhinakg.github.io/check-pallas/minified-v3.json"


def load_pinned_index(timeout: int = 20) -> dict[str, Any]:
    """下载预计算的延迟 OTA 索引。"""
    last: Exception | None = None
    for url in (pinned_index_url(), _fallback_index_url()):
        try:
            raw = _fetch(url, timeout=timeout)
            return json.loads(raw)
        except Exception as exc:      # noqa: BLE001
            last = exc
    raise CatalogError(f"无法获取预计算数据：{last}")


def _parse_name(name: str) -> tuple[str, str]:
    """'iOS 27.0.1 (24A446)' -> ('27.0.1', '24A446')"""
    import re

    m = re.search(r"(\d+(?:\.\d+)*)\s*\(([0-9A-Za-z]+)\)", name)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


def _group_for(product_type: str, index: dict[str, Any]) -> list[dict] | None:
    """返回**所有** iOS 分组（按版本从高到低），不做机型推断。

    机型能升到哪个系统由 Apple 决定，工具不做推断；列出全部分组，
    由 Pallas 实测确认。
    """
    groups: list[dict] = []
    for name, entries in index.items():
        if name == "_date" or not name.startswith("iOS"):
            continue
        groups.append(entries)
    return groups[0] if groups else None


def lockable_versions(product_type: str, index: dict[str, Any] | None = None
                      ) -> list[dict[str, Any]]:
    """列出可锁定的版本（合并所有 iOS 分组，按版本号降序去重）。"""
    from packaging.version import Version

    index = index if index is not None else load_pinned_index()
    seen: dict[str, dict[str, Any]] = {}
    for name, entries in index.items():
        if name == "_date" or not name.startswith("iOS"):
            continue
        for entry in entries:
            version, build = _parse_name(entry["name"])
            if not version or entry.get("latest") or not entry.get("delay"):
                continue
            if version not in seen:
                seen[version] = dict(entry, _version=version, _build=build)
    def key(v: str):
        try:
            return Version(v)
        except Exception:
            return Version("0")
    return sorted(seen.values(), key=lambda e: key(e["_version"]), reverse=True)


def plan_pin_from_index(product_type: str, target: str,
                        index: dict[str, Any] | None = None,
                        today: date | None = None) -> Pin:
    """从预计算数据直接得出方案（无需扫描）。"""
    index = index if index is not None else load_pinned_index()
    wanted = target.lstrip("iOS ").strip()

    for entry in lockable_versions(product_type, index):
        version = entry["_version"]
        build = entry["_build"]
        if version != wanted:
            continue
        if entry.get("latest") or not entry.get("delay"):
            raise CatalogError(f"{target} 已是最新版本，不需要锁定")
        delay = int(entry["delay"])
        expires = None
        if entry.get("date"):
            try:
                expires = date.fromisoformat(str(entry["date"])[:10])
            except ValueError:
                pass
        return Pin(
            release=Release(version, build, expires or (today or date.today())),
            delay=delay,
            expires=expires,
            next_release=None,
        )

    avail = "、".join(e["_version"] for e in lockable_versions(product_type, index))
    raise CatalogError(f"预计算数据里没有 {target}（当前可锁定：{avail}）")
