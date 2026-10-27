"""TUI 界面（基于 Textual，Windows / macOS / Linux 通用）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
)

from . import __version__, device, payload, preflight
from .device import Device, DeviceError, get_backend
from .payload import Plan

KEEP = "keep"   # 不修改

CSS = """
#banner { background: $boost; padding: 0 1; height: auto; }
#device-line { color: $text-muted; }
.section { border: round $primary; padding: 0 1; height: auto; margin-bottom: 1; }
.section-title { text-style: bold; color: $accent; }
.row { height: auto; align-vertical: middle; }
.vlabel { width: 14; }
#org-input { width: 30; }
#sel-supervise, #sel-ota { width: 30; }
#cb-skip { width: auto; border: none; background: transparent; padding: 0; }
#cb-skip:focus { border: none; }
#log { height: 1fr; min-height: 8; border: round $secondary; }
.hint { color: $text-muted; }
"""


class ConfirmScreen(ModalScreen[bool]):
    """当「查找我的 iPhone」状态无法确认时，要求用户显式确认。"""

    CSS = """
    ConfirmScreen { align: center middle; }
    #dlg { width: 66; height: auto; border: thick $warning; padding: 1 2; background: $surface; }
    #dlg-title { text-style: bold; color: $warning; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("无法确认「查找我的 iPhone」状态", id="dlg-title")
            yield Static(
                "iOS 不提供可靠的电脑侧接口来读取该状态。\n"
                "恢复系统文件时若它仍处于开启，写入会失败。\n\n"
                "请先确认已关闭：设置 → 顶部 Apple 账户 → 查找 →\n"
                "查找我的 iPhone → 关闭（需输入 Apple ID 密码）。",
            )
            with Horizontal():
                yield Button("我已关闭，继续", id="btn-yes", variant="warning")
                yield Button("取消", id="btn-no")

    @on(Button.Pressed, "#btn-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-no")
    def _no(self) -> None:
        self.dismiss(False)


class PinScreen(ModalScreen):
    """选择要锁定的系统版本，并生成描述文件。"""

    CSS = """
    PinScreen { align: center middle; }
    #pin-dlg { width: 74; height: auto; border: thick $accent; padding: 1 2; background: $surface; }
    #pin-title { text-style: bold; color: $accent; }
    """

    def __init__(self, dev) -> None:
        super().__init__()
        self.dev = dev
        self.versions: list = []
        self.pin = None

    def compose(self) -> ComposeResult:
        with Vertical(id="pin-dlg"):
            yield Label("锁定系统版本", id="pin-title")
            yield Static("正在向 Apple 查询可用版本…", id="pin-status")
            yield Select([], id="pin-select", prompt="选择目标版本")
            with Horizontal():
                yield Button("生成描述文件", id="pin-ok", variant="primary")
                yield Button("取消", id="pin-cancel")

    def on_mount(self) -> None:
        self.load_versions()

    @work(thread=True, exclusive=True)
    def load_versions(self) -> None:
        from . import pallas

        try:
            # 用预计算索引列出全部可锁定版本（秒出）。
            # 不按机型过滤 —— 机型能升到哪个系统只有 Apple 知道，
            # 版本列表来自全部 iOS 分组，不做机型推断。
            index = pallas.load_pinned_index()
            releases = [
                pallas.Release(
                    e["_version"], e["_build"],
                    pallas.date.fromisoformat(str(e["date"])[:10])
                    if e.get("date") else pallas.date.today(),
                )
                for e in pallas.lockable_versions(self.dev.product_type, index)
            ]
        except Exception as exc:
            # 注意：worker 在后台线程，必须通过 app.call_from_thread 回到 UI 线程；
            # ModalScreen 本身没有 call_from_thread（只有 App 有）。
            self.app.call_from_thread(self._set_status, f"[red]查询失败：{exc}[/red]")
            return
        self.versions = releases[:40]
        opts = [(f"iOS {r.version} ({r.build})  {r.posting_date}", r.version)
                for r in self.versions]
        self.app.call_from_thread(self._fill, opts)

    def _set_status(self, text: str) -> None:
        self.query_one("#pin-status", Static).update(text)

    def _fill(self, opts) -> None:
        try:
            self.query_one("#pin-select", Select).set_options(opts)
            self.query_one("#pin-status", Static).update(
                "选择版本后立即生成（读取社区预计算数据，约 2 秒）。\n"
                "注意：需已开启监督模式，且描述文件有有效期。"
            )
        except Exception:
            pass

    @on(Button.Pressed, "#pin-ok")
    def _ok(self) -> None:
        sel = self.query_one("#pin-select", Select).value
        if not sel or sel is Select.BLANK:
            return
        self.query_one("#pin-status", Static).update("正在获取延迟数据…")
        self.query_one("#pin-ok", Button).disabled = True
        self.do_pin(str(sel))

    @work(thread=True, exclusive=True)
    def do_pin(self, target: str) -> None:
        from . import pallas, payload

        try:
            pt = self.dev.product_type
            pin = pallas.plan_pin(pt, self.dev.hardware_model, target,
                                  progress=lambda i, n: self.app.call_from_thread(
                                      self._progress_pin, i, n))
            # 输出到用户主目录，避免 cwd 不可写
            out = Path.home() / f"pin-ios{pin.release.version}.mobileconfig"
            out.write_bytes(payload.pinned_profile(
                pin.release.version, pin.delay,
                build=pin.release.build, expires=pin.expires,
            ))
            # 直接推送到设备安装，省去手动传输
            installed = False
            try:
                from . import mcinstall
                mcinstall.install_profile(self.dev.udid, out.read_bytes())
                installed = True
            except Exception:
                pass
            self.app.call_from_thread(self.dismiss, (out, pin, installed))
        except Exception as exc:
            self.app.call_from_thread(self._set_status, f"[red]失败：{exc}[/red]")
            self.app.call_from_thread(self._enable_ok)

    def _progress_pin(self, current: int, total: int) -> None:
        try:
            self.query_one("#pin-status", Static).update(
                f"正在扫描延迟天数 {current}/{total}…"
            )
        except Exception:
            pass

    def _enable_ok(self) -> None:
        try:
            self.query_one("#pin-ok", Button).disabled = False
        except Exception:
            pass

    @on(Button.Pressed, "#pin-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class SuperviseApp(App[None]):
    """iOS 监督模式 / OTA 开关。"""

    CSS = CSS
    TITLE = "iOS 监督模式开关"

    def __init__(self, prefer: str = "auto") -> None:
        super().__init__()
        self.prefer = prefer
        self.device: Optional[Device] = None
        self.devices: list[Device] = []
        self._busy = False
        self.checks: list = []

    # ------------------------------------------------------------------ UI #
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(
                f"[b]iOS 免越狱开关工具[/b] v{__version__}    "
                "[dim]改配置 → 伪造备份 → restore --system[/dim]",
                id="banner",
            )
            with Vertical(classes="section"):
                yield Label("设备", classes="section-title")
                yield Select([], id="device-select", prompt="选择设备")
                yield Static("正在扫描设备…", id="device-line")
                with Horizontal(classes="row"):
                    yield Button("刷新设备", id="btn-refresh")
                    yield Button("仅生成备份文件", id="btn-export")
                    yield Button("锁定系统版本…", id="btn-pin")

            with Vertical(classes="section"):
                yield Label("监督模式 (Supervision)", classes="section-title")
                with Horizontal(classes="row"):
                    yield Label("目标状态", classes="vlabel")
                    yield Select(
                        [("不修改", KEEP), ("开启监督", "on")],
                        value=KEEP, allow_blank=False, id="sel-supervise",
                    )
                with Horizontal(classes="row"):
                    yield Label("组织名称", classes="vlabel")
                    yield Input(placeholder="例如 My iPhone", id="org-input")
                yield Checkbox("同时跳过开机引导（推荐）", value=True, id="cb-skip")
                yield Static(
                    "开启后「设置」顶部会显示所属组织；关闭会清空受管配置。",
                    classes="hint",
                )

            with Vertical(classes="section"):
                yield Label("系统更新 (OTA)", classes="section-title")
                with Horizontal(classes="row"):
                    yield Label("目标状态", classes="vlabel")
                    yield Select(
                        [("不修改", KEEP), ("屏蔽更新", "block"), ("恢复更新", "unblock")],
                        value=KEEP, allow_blank=False, id="sel-ota",
                    )
                yield Static(
                    "屏蔽后不再提示新版本；恢复即重新允许检查更新。",
                    classes="hint",
                )

            with Vertical(classes="section", id="sec-check"):
                yield Label("写入前检查", classes="section-title")
                yield Static("尚未检查", id="check-status")
                with Horizontal(classes="row"):
                    yield Button("重新检查", id="btn-check")

            with Vertical(classes="section"):
                yield Label("执行", classes="section-title")
                with Horizontal(classes="row"):
                    yield Button("应用修改", id="btn-apply", variant="primary")
                    yield Button("全部还原", id="btn-reset", variant="warning")
                    yield Button("退出", id="btn-quit")
                yield ProgressBar(total=100, show_eta=False, id="progress")
            yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._log("[dim]提示：先用数据线连接 iPhone 并在手机上点「信任」。[/dim]")
        self.refresh_devices()

    # --------------------------------------------------------------- utils #
    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for bid in ("#btn-apply", "#btn-reset", "#btn-export", "#btn-refresh"):
            self.query_one(bid, Button).disabled = busy

    def _progress(self, value: float, note: str = "") -> None:
        self.query_one("#progress", ProgressBar).update(
            progress=min(100.0, value * 100.0)
        )
        if note:
            self.query_one("#device-line", Static).update(note)

    # ------------------------------------------------------------- devices #
    @work(thread=True, exclusive=True)
    def refresh_devices(self) -> None:
        try:
            devices = get_backend(self.prefer).list_devices()
        except Exception as exc:
            msg = str(exc)
            self.call_from_thread(self._log, f"[red]设备扫描失败：{msg}[/red]")
            # 后端缺失时给出可读的分步提示，避免一行挤在一起被截断
            if "后端" in msg or "backend" in msg.lower():
                for line in msg.splitlines():
                    if line.strip():
                        self.call_from_thread(self._log, f"[yellow]  {line.strip()}[/yellow]")
                self.call_from_thread(
                    self.query_one("#device-line", Static).update,
                    "[red]缺少设备后端[/red]  [dim]装好后点「刷新设备」重试[/dim]",
                )
            else:
                self.call_from_thread(
                    self.query_one("#device-line", Static).update,
                    "[red]未找到设备[/red]",
                )
            return
        self.call_from_thread(self._apply_devices, devices)

    def _apply_devices(self, devices: list[Device]) -> None:
        self.devices = devices
        sel = self.query_one("#device-select", Select)
        sel.set_options([(d.label, d.udid) for d in devices])
        line = self.query_one("#device-line", Static)
        if not devices:
            self.device = None
            line.update("[red]没有检测到设备。请连接数据线并信任本电脑。[/red]")
            return
        self.device = devices[0]
        sel.value = devices[0].udid
        line.update(f"[green]已连接[/green] {devices[0].label}")
        self._log(f"[green]发现设备[/green] {devices[0].udid} {devices[0].label}")
        self.run_checks()

    @on(Select.Changed, "#device-select")
    def _pick_device(self, event: Select.Changed) -> None:
        for dev in self.devices:
            if dev.udid == event.value:
                self.device = dev
                self.query_one("#device-line", Static).update(
                    f"[green]已连接[/green] {dev.label}"
                )
                self.run_checks()
                break

    @on(Button.Pressed, "#btn-refresh")
    def _on_refresh(self) -> None:
        self._log("[dim]重新扫描设备…[/dim]")
        self.refresh_devices()

    # ------------------------------------------------------------ preflight #
    @on(Button.Pressed, "#btn-check")
    def _on_check(self) -> None:
        self.run_checks()

    @work(thread=True, exclusive=True)
    def run_checks(self) -> None:
        if self.device is None:
            return
        checks = device.run_preflight(self.device.udid, prefer=self.prefer)
        self.call_from_thread(self._show_checks, checks)

    def _show_checks(self, checks: list) -> None:
        self.checks = checks
        widget = self.query_one("#check-status", Static)
        if not checks:
            widget.update("[dim]未执行检查[/dim]")
            return
        lines = []
        for chk in checks:
            color = {
                preflight.Verdict.CLEAR: "green",
                preflight.Verdict.BLOCKED: "red",
                preflight.Verdict.UNKNOWN: "yellow",
            }[chk.verdict]
            lines.append(f"[{color}]{chk.headline()}[/{color}]  [dim]{chk.detail}[/dim]")
        widget.update("\n".join(lines))
        hard = [c for c in checks if c.hard_fail]
        if hard:
            self._log("[red]前置检查未通过，已阻止写入。[/red]")
        elif any(c.needs_confirm for c in checks):
            self._log("[yellow]部分状态无法自动确认，应用时需在弹窗中确认。[/yellow]")

    # --------------------------------------------------------------- plan #
    def _current_plan(self) -> Plan:
        sup = self.query_one("#sel-supervise", Select).value
        ota = self.query_one("#sel-ota", Select).value
        org = self.query_one("#org-input", Input).value.strip()
        skip = self.query_one("#cb-skip", Checkbox).value
        return Plan(
            supervised=None if sup == KEEP else sup == "on",
            organization=org,
            skip_setup=skip,
            ota_blocked=None if ota == KEEP else ota == "block",
        )

    def _apply(self, plan: Plan) -> None:
        if self._busy:
            return
        if self.device is None:
            self._log("[red]没有选中设备[/red]")
            return
        items = plan.items()
        if not items:
            self._log("[yellow]没有选择任何修改项。[/yellow]")
            return
        # 前置检查闸门
        hard = [c for c in self.checks if c.hard_fail]
        if hard:
            for chk in hard:
                self._log(f"[red]{chk.headline()}[/red] {chk.detail}")
            self._log("[red]已阻止写入：请先处理上面的问题。[/red]")
            return
        if any(c.needs_confirm for c in self.checks):
            self._pending = (items, self.device.udid)
            self.push_screen(ConfirmScreen(), self._on_confirm)
            return
        self._log("[b]准备写入：[/b]" + "；".join(plan.describe()))
        self._set_busy(True)
        self._progress(0.0, "")
        self._run_apply(items, self.device.udid, self.checks)

    def _on_confirm(self, ok: bool | None) -> None:
        pending = getattr(self, "_pending", None)
        self._pending = None
        if not ok or not pending:
            self._log("[yellow]已取消。[/yellow]")
            return
        items, udid = pending
        self._set_busy(True)
        self._progress(0.0, "")
        self._run_apply(items, udid, self.checks)

    @work(thread=True, exclusive=True)
    def _run_apply(self, items, udid: str, checks=None) -> None:
        try:
            device.apply_plan(
                items,
                udid=udid,
                prefer=self.prefer,
                checks=checks or [],
                progress=lambda v, n: self.call_from_thread(self._progress, v, n),
                log=lambda m: self.call_from_thread(self._log, m),
            )
            self.call_from_thread(self._progress, 1.0, "完成，设备将自动重启")
            self.call_from_thread(
                self._log,
                "[green]写入成功。[/green]设备重启后即可在「设置」中看到结果。",
            )
        except DeviceError as exc:
            self.call_from_thread(self._log, f"[red]{exc}[/red]")
        except Exception as exc:                       # pragma: no cover
            self.call_from_thread(self._log, f"[red]写入失败：{exc}[/red]")
        finally:
            self.call_from_thread(self._set_busy, False)

    @on(Button.Pressed, "#btn-apply")
    def _on_apply(self) -> None:
        self._apply(self._current_plan())

    @on(Button.Pressed, "#btn-reset")
    def _on_reset(self) -> None:
        """一键还原：只恢复系统更新。

        关闭监督已禁用 —— 写入 CloudConfigurationUIComplete=True +
        IsSupervised=False 会被 iOS 判定为状态损坏，反复重启后自动恢复出厂设置。
        """
        self._log(
            "[yellow]注意：只还原系统更新。关闭监督已禁用"
            "（会导致设备反复重启并恢复出厂设置）。[/yellow]"
        )
        self._apply(Plan(supervised=None, skip_setup=False, ota_blocked=False))

    # -------------------------------------------------------------- export #
    @on(Button.Pressed, "#btn-export")
    def _on_export(self) -> None:
        plan = self._current_plan()
        items = plan.items()
        if not items:
            self._log("[yellow]没有选择任何修改项。[/yellow]")
            return
        udid = self.device.udid if self.device else "0" * 40
        dest = Path.cwd() / "backup"
        try:
            path = device.export_backup(items, dest, udid=udid)
        except Exception as exc:
            self._log(f"[red]生成失败：{exc}[/red]")
            return
        self._log(f"[green]已生成备份目录[/green] {path}")
        self._log(
            "在任意电脑上写入："
            f"[b]idevicebackup2 -u {udid} restore --system --skip-apps \"{dest}\"[/b]"
        )

    @on(Button.Pressed, "#btn-pin")
    def _on_pin(self) -> None:
        if self.device is None:
            self._log("[red]没有选中设备[/red]")
            return
        self.push_screen(PinScreen(self.device), self._after_pin)

    def _after_pin(self, result) -> None:
        if not result:
            return
        path, pin, installed = result
        self._log(
            f"锁定 iOS {pin.release.version}：延迟 {pin.delay} 天，"
            f"有效期至 {pin.expires}（还有 {pin.days_left} 天）"
        )
        self._log(f"[green]已生成[/green] {path}")
        if installed:
            self._log("[green]已推送到设备[/green] 请在 iPhone 上点「安装」确认")
        else:
            self._log("[yellow]自动安装未成功[/yellow] 把文件传到 iPhone 打开即可安装")

    @on(Button.Pressed, "#btn-quit")
    def _on_quit(self) -> None:
        self.exit()


def run_tui(prefer: str = "auto") -> None:
    SuperviseApp(prefer=prefer).run()
