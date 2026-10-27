"""应用前的前置检查。

`restore --system` 在「查找我的 iPhone」开启时会被设备拒绝，所以工具在写入前
必须尽力判断这一状态。

但这里有个现实约束：**iOS 没有公开可靠的 API 让电脑侧判断 FMI 是否开启**。
`com.apple.fmip` 这个 lockdown 域能查，但它反映的是「查找网络」相关配置，
并不总等于「查找我的 iPhone 处于开启且会阻止恢复」；而且不同 iOS 版本
行为不一致，iOS 17.4 起 MobileGestalt 也已被废弃。

因此本模块给出**三态**结论，绝不把「查不到」伪装成「已关闭」：

* :attr:`Check.VERDICT_BLOCKED` —— 明确查询到会阻止恢复，直接拦截。
* :attr:`Check.VERDICT_CLEAR`   —— 明确查询到安全。
* :attr:`Check.VERDICT_UNKNOWN` —— 查不到 / 无法判断，要求用户显式确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Verdict(str, Enum):
    BLOCKED = "blocked"
    CLEAR = "clear"
    UNKNOWN = "unknown"


@dataclass
class Check:
    """一条前置检查的结果。"""

    verdict: Verdict
    title: str
    detail: str
    # 该项是否属于「硬失败」：True 表示无论用户怎么确认都应阻止写入
    hard_fail: bool = False

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.CLEAR

    @property
    def needs_confirm(self) -> bool:
        return self.verdict is Verdict.UNKNOWN

    def headline(self) -> str:
        icon = {
            Verdict.CLEAR: "✓",
            Verdict.BLOCKED: "✗",
            Verdict.UNKNOWN: "?",
        }[self.verdict]
        return f"{icon} {self.title}"


# FMI 域里若干与「查找」相关的键；值多为 bool/int/字符串，含义随版本变化，
# 只按保守规则解读。
_FMI_ON_VALUES = {True, 1, "1", "true", "True", "YES"}
_FMI_OFF_VALUES = {False, 0, "0", "false", "False", "NO"}


def evaluate_fmip(values: dict | None, *, error: str = "") -> Check:
    """根据 ``com.apple.fmip`` 域返回值判断「查找我的 iPhone」。"""
    title = "查找我的 iPhone 已关闭"
    if error:
        return Check(
            Verdict.UNKNOWN,
            "无法读取「查找我的 iPhone」状态",
            f"查询 com.apple.fmip 失败（{error}）。该状态无法可靠自动检测，"
            "请到 设置 → 顶部 Apple 账户 → 查找 → 查找我的 iPhone 确认已关闭。",
        )
    if not values:
        return Check(
            Verdict.UNKNOWN,
            "无法读取「查找我的 iPhone」状态",
            "设备未返回 com.apple.fmip 信息。请到 设置 → Apple 账户 → 查找 "
            "→ 查找我的 iPhone 确认已关闭。",
        )

    # 只认明确的键，避免把无关字段误判
    candidates = {
        k: v for k, v in values.items()
        if isinstance(k, str) and k.lower() in {
            "isactive", "fmipactive", "findmyiphoneenabled",
            "fmiplostenabled", "isenabled", "status",
        }
    }
    if not candidates:
        return Check(
            Verdict.UNKNOWN,
            "无法判断「查找我的 iPhone」状态",
            "设备返回的 com.apple.fmip 中没有可判读的字段。"
            "请到 设置 → Apple 账户 → 查找 → 查找我的 iPhone 确认已关闭。",
        )

    on = [k for k, v in candidates.items() if v in _FMI_ON_VALUES]
    off = [k for k, v in candidates.items() if v in _FMI_OFF_VALUES]
    if on and not off:
        return Check(
            Verdict.BLOCKED,
            "「查找我的 iPhone」似乎处于开启状态",
            f"检测到 {', '.join(sorted(on))} 表示已开启。"
            "请先到 设置 → Apple 账户 → 查找 → 查找我的 iPhone 关闭它"
            "（需要输入 Apple ID 密码），再重新运行。",
            hard_fail=True,
        )
    if off and not on:
        return Check(Verdict.CLEAR, title, "已检测到查找功能处于关闭状态。")
    return Check(
        Verdict.UNKNOWN,
        "「查找我的 iPhone」状态不明确",
        "设备返回的字段互相矛盾，请手动确认已关闭后再继续。",
    )


def evaluate_passcode(password_protected: bool | None) -> Check:
    """恢复系统文件时设备需处于已解锁 / 已信任状态。"""
    if password_protected is True:
        return Check(
            Verdict.BLOCKED,
            "设备已锁屏且设了密码",
            "请先解锁 iPhone 并保持亮屏，然后重试。",
            hard_fail=True,
        )
    return Check(Verdict.CLEAR, "设备已解锁", "设备处于可写入状态。")


def summarize(checks: list[Check]) -> tuple[bool, list[Check], list[Check]]:
    """返回 (是否可以继续, 硬失败项, 需要用户确认项)。"""
    hard = [c for c in checks if c.hard_fail]
    unknown = [c for c in checks if c.needs_confirm]
    return (not hard), hard, unknown
