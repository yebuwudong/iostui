"""命令行入口：既可直接执行一条命令，也可进入 TUI。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, device, mcinstall, pallas, payload
from .device import DeviceError
from .payload import Plan

BANNER = f"{__import__('iostui', fromlist=['version_string']).version_string()}  ·  iOS 免越狱 监督模式 / OTA 开关"

# --export-only 时用作备份目录名的占位 UDID（写设备前会替换成真实值）
EXPORT_PLACEHOLDER = "iostui-export"


def _wrap(text: str, width: int = 66) -> list[str]:
    """按显示宽度粗略折行（中文按 2 列计）。"""
    import unicodedata

    def w(ch: str) -> int:
        return 2 if unicodedata.east_asian_width(ch) in "WF" else 1

    lines, cur, cur_w = [], "", 0
    for ch in text:
        cw = w(ch)
        if cur_w + cw > width:
            lines.append(cur)
            cur, cur_w = "", 0
        cur += ch
        cur_w += cw
    if cur:
        lines.append(cur)
    return lines or [""]


def _print_status(dev) -> str:
    parts = [dev.name or "(未知)"]
    if dev.product_type:
        parts.append(dev.product_type)
    if dev.product_version:
        parts.append(f"iOS {dev.product_version}")
    parts.append(dev.connection or "USB")
    return "  ".join(parts)


def cmd_list(args: argparse.Namespace) -> int:
    devs = device.list_devices(args.backend)
    if not devs:
        print("没有检测到设备。请连接数据线，并在手机上点「信任」。")
        return 1
    print(f"发现 {len(devs)} 台设备：")
    for dev in devs:
        print(f"  {dev.udid}  {_print_status(dev)}")
    return 0


def _apply(args: argparse.Namespace, plan: Plan, udid: str, keep: bool) -> int:
    items = plan.items()
    if not items:
        print("没有任何改动。")
        return 1
    print("将要执行：")
    for line in plan.describe():
        print(f"  · {line}")
    for line in payload.summarize(items):
        print(f"    - {line}")
    if args.export_only:
        dest = Path(args.output or "backup").expanduser().resolve()
        path = device.export_backup(items, dest, udid=udid)
        print(f"\n已生成备份目录：{path}")
        real = udid if udid != EXPORT_PLACEHOLDER else "<设备UDID>"
        print(
            "手动写入：\n"
            f"  idevicebackup2 -u {real} restore --system --skip-apps {dest}\n"
            f"  或 java 无 gui 环境也可用: python -m pymobiledevice3 backup2 restore "
            f"--system --skip-apps \"{dest}\""
        )
        return 0
    # 前置检查：查找我的 iPhone / 锁屏状态
    print()
    print("前置检查：")
    checks = device.run_preflight(udid, prefer=args.backend, skip=args.no_preflight)
    for chk in checks:
        print(f"  {chk.headline()}")
        if not chk.ok:
            for ln in _wrap(chk.detail):
                print(f"      {ln}")

    hard = [c for c in checks if c.hard_fail]
    if hard:
        print("\n已阻止写入：请先处理上面的问题。")
        return 2
    unknown = [c for c in checks if c.needs_confirm]
    if unknown and not args.force:
        if args.yes:
            print(
                "\n「查找我的 iPhone」状态无法自动确认。"
                "请手动确认已关闭，或加 --force 跳过。"
            )
            return 2
        ans = input(
            "\n无法自动确认「查找我的 iPhone」已关闭（需手动确认）。"
            "已确认关闭请输入 y 继续 [y/N] "
        ).strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return 1

    if not args.yes:
        ans = input("\n确认写入设备？设备会自动重启 [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return 1

    def _log(msg: str) -> None:
        print(f"  {msg}")

    def _progress(v: float, note: str) -> None:
        bar = int(v * 30)
        print(f"\r  [{'=' * bar}{' ' * (30 - bar)}] {v*100:5.1f}%", end="", flush=True)

    try:
        device.apply_plan(
            items,
            udid=udid,
            prefer=args.backend,
            keep=Path(args.output).expanduser().resolve() if keep else None,
            progress=_progress,
            log=_log,
            checks=checks,
            force=args.force,
        )
    except device.PreflightBlocked as exc:
        print(f"\n{exc}")
        return 2
    except DeviceError as exc:
        print(f"\n失败：{exc}")
        return 2
    print("\n完成。设备重启后即可在「设置」中看到结果。")
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    org = args.org or ""
    if args.on and not org:
        org = "Managed Device"

    # 关闭监督已被证明危险：写入 CloudConfigurationUIComplete=True +
    # IsSupervised=False 会被 iOS 判定为系统状态损坏，反复重启后自动恢复出厂设置。
    # 在拿到可靠方案前直接拒绝执行，避免再次造成数据丢失。
    if not args.on:
        print("关闭监督当前不可用。")
        print()
        print("原因：这个操作写入的云配置状态会被 iOS 判定为损坏，")
        print("      可能导致设备反复重启并自动恢复出厂设置（已确认会丢数据）。")
        print()
        print("如果你的设备确实需要关闭监督，请用 Apple 官方方式：")
        print("  设置 → 通用 → VPN与设备管理 → 移除管理配置")
        print("  或抹机后不恢复该配置")
        return 2

    # 优先用官方 MCInstall 接口：直接让设备落配置，不需要重启，并可读回校验
    if not args.restore_method:
        print("使用 MCInstall 官方接口设置监督状态…")
        der = None
        if args.on:
            from . import supervisor as sup_mod
            ident = sup_mod.ensure_identity()
            der = sup_mod.public_der(ident)
            print(f"  监督身份：{ident}")
            print(f"  证书 DER：{len(der)} 字节")
        try:
            got = mcinstall.set_supervision(args.udid, args.on, org, der)
        except DeviceError as exc:
            msg = str(exc)
            print(f"  失败：{msg}")
            if "already present" in msg.lower():
                # 14002：设备上已有云配置，官方接口拒绝覆盖
                print()
                try:
                    cur = mcinstall.get_current(args.udid)
                except DeviceError:
                    cur = None
                for line in mcinstall.explain(cur):
                    print(f"  {line}" if line else "")
                print()
                if mcinstall.classify(cur) is mcinstall.CloudState.RESIDUAL:
                    print("  ⚠ 备份恢复方式无法清除该残留状态，")
                    print("    并不会让设备真正进入监督，属于无效操作。")
                    return 2
                print("\n回退到备份恢复方式（会重启设备）…")
            plan = Plan(supervised=args.on, organization=org,
                        skip_setup=not args.no_skip_setup,
                        supervisor_der=der)
            return _apply(args, plan, args.udid, keep=False)

        sup = got.get("IsSupervised")
        print(f"  设备返回 IsSupervised = {sup}")
        if sup == args.on:
            print("  ✓ 监督状态已生效，无需重启")
            print("\n验证：设置 → 通用 → VPN与设备管理，或看「设置」顶部是否显示组织名")
            return 0
        print("  ✗ 设备未接受该配置，回退到备份恢复方式…")
        plan = Plan(supervised=args.on, organization=org,
                    skip_setup=not args.no_skip_setup, supervisor_der=der)
        return _apply(args, plan, args.udid, keep=False)

    plan = Plan(supervised=args.on, organization=org, skip_setup=not args.no_skip_setup)
    return _apply(args, plan, args.udid, keep=False)


def cmd_ota(args: argparse.Namespace) -> int:
    plan = Plan(ota_blocked=args.on)
    return _apply(args, plan, args.udid, keep=False)


def cmd_reset(args: argparse.Namespace) -> int:
    """全部还原：恢复系统更新（关闭监督已禁用，见 cmd_supervise 说明）。"""
    print("注意：本命令只恢复系统更新。关闭监督已禁用——")
    print("      那个操作可能导致设备反复重启并恢复出厂设置。")
    print()
    plan = Plan(supervised=None, skip_setup=False, ota_blocked=False)
    return _apply(args, plan, args.udid, keep=False)


def _cmd_reset_legacy(args: argparse.Namespace) -> int:
    """旧实现（含关闭监督），保留仅作参考。"""
    if not args.restore_method:
        print("尝试用 MCInstall 官方接口清除云配置…")
        try:
            got = mcinstall.set_supervision(args.udid, False, "")
            print(f"  设备返回 IsSupervised = {got.get('IsSupervised')}")
        except DeviceError as exc:
            print(f"  （无法通过官方接口处理：{exc}）")
            print("  继续用备份恢复方式写回空配置。")

    plan = Plan(supervised=False, skip_setup=False, ota_blocked=False)
    return _apply(args, plan, args.udid, keep=False)


def cmd_profile(args: argparse.Namespace) -> int:
    data = payload.delayed_update_profile(args.org or "", args.days)
    out = Path(args.output or f"delay-update-{args.days}d.mobileconfig").expanduser()
    out.write_bytes(data)
    print(f"已生成描述文件：{out.resolve()}")
    print("安装方式：通过 AirDrop / 邮件 / 网页发送到 iPhone，在「设置」中安装。")
    print("注意：该限制仅在设备已开启监督模式时生效。")
    return 0


def cmd_enforce(args: argparse.Namespace) -> int:
    data = payload.enforcement_declaration(
        args.version, args.org or "", args.deadline or ""
    )
    out = Path(args.output or f"enforce-{args.version}.json").expanduser()
    out.write_bytes(data)
    print(f"已生成声明文件：{out.resolve()}")
    print()
    print("说明：这是「强制更新到指定版本」的声明式配置，不是延迟更新。")
    print("· 需要设备处于监督模式，并通过 MDM / DDM 下发才生效")
    print("· 手动安装 .mobileconfig 无效（声明式配置不走描述文件通道）")
    print("· TargetOSVersion 只写主次版本（如 16.1）时不会自动装补丁版本")
    return 0


def _device_hw(udid: str, prefer: str) -> tuple[str, str, str]:
    """读取设备的 ProductType / HardwareModel / 系统版本。"""
    try:
        for dev in device.list_devices(prefer):
            if dev.udid == udid:
                return dev.product_type, dev.hardware_model, dev.product_version
    except Exception:
        pass
    return "", "", ""


def cmd_pin(args: argparse.Namespace) -> int:
    """把设备锁定（钉住）在指定的 iOS 版本。"""
    product_type = args.product_type
    hw_model = args.hw_model
    current = ""

    if not product_type or not hw_model:
        udid = args.udid
        if not udid:
            devs = device.list_devices(args.backend)
            if not devs:
                print("没有检测到设备。请连接设备，或用 --product-type/--hw-model 手动指定。")
                print("例如: iostui pin --version 27.0 --product-type iPhone17,3 --hw-model D47AP")
                return 2
            udid = devs[0].udid
        product_type, hw_model, current = _device_hw(udid, args.backend)
        if not product_type or not hw_model:
            print("无法读取设备型号，请用 --product-type 与 --hw-model 手动指定。")
            return 2

    print(f"设备：{product_type} / {hw_model}" + (f"  当前 iOS {current}" if current else ""))
    print(f"目标版本：iOS {args.version}")
    print("正在获取延迟更新数据…")

    try:
        pin = pallas.plan_pin(
            product_type, hw_model, args.version,
            progress=lambda i, n: print(f"\r  回退扫描 {i}/{n}", end="", flush=True),
            timeout=args.timeout,
        )
    except pallas.CatalogError as exc:
        print(f"\n失败：{exc}")
        return 2
    print()

    print(f"  ✓ 目标版本   iOS {pin.release.version} ({pin.release.build})  发布于 {pin.release.posting_date}")
    print(f"  ✓ 延迟天数   {pin.delay} 天")
    if pin.next_release:
        print(f"  ✓ 挡住的版本 iOS {pin.next_release.version}（{pin.next_release.posting_date} 发布）")
    if pin.expires:
        left = pin.days_left
        warn = "  ⚠ 即将过期，建议尽快重装新版" if left is not None and left <= 7 else ""
        print(f"  ✓ 有效期至   {pin.expires}（还有 {left} 天）{warn}")
    print()

    data = payload.pinned_profile(
        pin.release.version, pin.delay,
        organization=args.org or "", build=pin.release.build,
        expires=pin.expires,
    )
    out = Path(args.output or f"pin-ios{pin.release.version}.mobileconfig").expanduser()
    out.write_bytes(data)
    print(f"已生成描述文件：{out.resolve()}")

    # 默认直接推到设备安装，省去手动传输
    if not args.no_install:
        print()
        print("正在推送到设备安装…（手机上会弹出确认，点「安装」即可）")
        try:
            mcinstall.install_profile(udid, data)
            print("  ✓ 已发送安装请求，请在 iPhone 上确认")
            print("    设置 → 通用 → VPN与设备管理，可查看已安装的描述文件")
        except DeviceError as exc:
            print(f"  ✗ 自动安装失败：{exc}")
            print(f"    请手动安装：把 {out.name} 传到手机后打开")
    print()
    print("使用方法：")
    print("  1. 把该 .mobileconfig 传到 iPhone（AirDrop / 邮件 / 本地网页均可）")
    print("  2. 设置 → 已下载描述文件 → 安装（需输入锁屏密码）")
    print("  3. 设置 → 通用 → 软件更新，此时只应看到目标版本或「已是最新」")
    print()
    print("注意：")
    print("  · 需要设备已开启监督模式，否则系统忽略该限制（本工具 reset/supervise 可处理）")
    print("  · 延迟天数会随时间漂移，到期后需卸载旧描述文件并重新生成安装")
    print("  · iOS 27 起 Apple 移除了该限制，届时将无法使用")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """显示设备的监督 / 更新管控真实状态（读设备，不猜）。"""
    import json as _json

    from . import mcinstall

    # status 依赖 MCInstall 服务，只能走 pymobiledevice3 后端
    if not device.have_pymobiledevice3():
        print("status 需要 pymobiledevice3（它提供 MCInstall 服务）。\n")
        print("安装方法：")
        print("  pip install pymobiledevice3")
        print()
        print("如果装不上（常见于缺少编译环境），可以改用：")
        print("  iostui list          # 列出设备")
        print("  iostui supervise --on --org \"My iPhone\"   # 设置监督")
        print("  iostui ota --on -y   # 屏蔽更新")
        print()
        print("这些功能不依赖 pymobiledevice3。")
        return 2

    udid = args.udid
    if not udid:
        devs = device.list_devices(args.backend)
        if not devs:
            print("没有检测到设备")
            return 2
        udid = devs[0].udid
        print(f"设备：{devs[0].label}\n")

    ok = True

    # 1) 云配置（监督状态）
    print("监督状态（来自 MCInstall，设备真实值）：")
    try:
        cfg = mcinstall.get_current(udid)
    except DeviceError as exc:
        print(f"  ✗ 读取失败：{exc}")
        ok = False
        cfg = None
    if cfg is not None:
        sup = cfg.get("IsSupervised")
        org = cfg.get("OrganizationName", "")
        applied = cfg.get("ConfigurationWasApplied")
        print(f"  IsSupervised              = {sup}")
        print(f"  OrganizationName          = {org!r}")
        print(f"  ConfigurationWasApplied   = {applied}")
        print(f"  ConfigurationSource       = {cfg.get('ConfigurationSource')}")
        print(f"  CloudConfigurationUIComplete = {cfg.get('CloudConfigurationUIComplete')}")
        state = mcinstall.classify(cfg)
        print()
        for line in mcinstall.explain(cfg):
            print(f"  {line}" if line else "")
    else:
        print("  （设备未返回云配置，说明未受监督）")

    # 2) 已安装描述文件
    print()
    print("已安装描述文件：")
    try:
        pl = mcinstall.list_profiles(udid)
        meta = pl.get("ProfileMetadata") or {}
        if not meta:
            print("  （无）")
        for ident, info in meta.items():
            name = info.get("PayloadDisplayName") or info.get("Name") or ""
            print(f"  · {ident}")
            if name:
                print(f"      名称: {name}")
    except DeviceError as exc:
        print(f"  ✗ 读取失败：{exc}")

    # 3) 延迟更新限制是否在起作用
    print()
    print("更新延迟限制：")
    if cfg and cfg.get("IsSupervised"):
        print("  设备已受监督，描述文件中的 enforcedSoftwareUpdateDelay 会生效")
    else:
        print("  设备未受监督 —— enforcedSoftwareUpdateDelay 会被系统忽略")
        print("  （这是「锁定版本」的前提，请先开启监督模式）")

    if args.json:
        print()
        print(_json.dumps({"cloudConfiguration": cfg}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_tui(args: argparse.Namespace) -> int:
    try:
        from .tui import run_tui
    except ImportError as exc:
        print(f"TUI 依赖缺失：{exc}\n请运行 pip install -r requirements.txt")
        return 2
    run_tui(args.backend)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--backend", default=argparse.SUPPRESS, choices=["auto", "py", "cli"],
        help="设备后端：py=pymobiledevice3, cli=libimobiledevice（默认 auto）",
    )

    p = argparse.ArgumentParser(
        prog="iostui",
        description=BANNER,
        epilog="不带子命令运行时进入图形化 TUI。",
    )
    p.add_argument("-V", "--version", action="version", version=BANNER)
    p.add_argument(
        "--backend", default="auto", choices=["auto", "py", "cli"],
        help="设备后端：py=pymobiledevice3, cli=libimobiledevice（默认 auto）",
    )
    sub = p.add_subparsers(dest="command")

    def add_device_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-u", "--udid", help="目标设备 UDID（仅一台时可不填）")
        sp.add_argument("-y", "--yes", action="store_true", help="跳过确认")
        sp.add_argument("-o", "--output", help="备份输出目录")
        sp.add_argument(
            "--export-only", action="store_true",
            help="只生成备份文件，不写入设备",
        )
        sp.add_argument(
            "--force", action="store_true",
            help="「查找我的 iPhone」状态无法确认时仍然继续（有风险）",
        )
        sp.add_argument(
            "--no-preflight", action="store_true",
            help="跳过写入前的设备状态检查",
        )

    sp = sub.add_parser("list", parents=[common], help="列出已连接设备")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("status", parents=[common], help="查看设备真实监督状态")
    sp.add_argument("-u", "--udid", help="目标设备 UDID")
    sp.add_argument("--json", action="store_true", help="额外输出原始 JSON")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("tui", parents=[common], help="打开图形界面")
    sp.set_defaults(func=cmd_tui)

    sp = sub.add_parser("supervise", parents=[common], help="开启 / 关闭监督模式")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true", help="开启监督模式")
    g.add_argument("--off", action="store_true", help="关闭监督模式")
    sp.add_argument("--org", default="", help="组织名称")
    sp.add_argument("--no-skip-setup", action="store_true", help="不跳过开机引导")
    sp.add_argument(
        "--restore-method", action="store_true",
        help="强制使用备份恢复方式（默认优先 MCInstall 官方接口）",
    )
    add_device_flags(sp)
    sp.set_defaults(func=cmd_supervise)

    sp = sub.add_parser("ota", parents=[common], help="屏蔽 / 恢复系统更新")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true", help="屏蔽系统更新")
    g.add_argument("--off", action="store_true", help="恢复系统更新")
    add_device_flags(sp)
    sp.set_defaults(func=cmd_ota)

    sp = sub.add_parser("reset", parents=[common], help="全部还原（关闭监督 + 恢复更新）")
    sp.add_argument("--restore-method", action="store_true",
                    help="强制使用备份恢复方式")
    add_device_flags(sp)
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser(
        "enforce", help="生成「强制更新到指定版本」声明（需 MDM/DDM）"
    )
    sp.add_argument("--version", required=True, help="目标系统版本，如 16.1")
    sp.add_argument("--org", default="", help="组织名称")
    sp.add_argument(
        "--deadline", default="",
        help="强制安装时间，格式 2026-12-01T09:00:00（可选）",
    )
    sp.add_argument("-o", "--output", help="输出文件")
    sp.set_defaults(func=cmd_enforce)

    sp = sub.add_parser(
        "pin", help="把设备锁定在指定 iOS 版本（自动算出所需延迟天数）"
    )
    sp.add_argument("--version", required=True, help="目标 iOS 版本，如 27.0")
    sp.add_argument("--product-type", default="", help="设备型号标识，如 iPhone17,3")
    sp.add_argument("--hw-model", default="", help="硬件型号，如 D47AP")
    sp.add_argument("--org", default="", help="组织名称")
    sp.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数")
    sp.add_argument("-u", "--udid", help="从该设备读取型号信息")
    sp.add_argument("-o", "--output", help="输出 .mobileconfig 路径")
    sp.add_argument("--no-install", action="store_true",
                    help="只生成文件，不推送到设备安装")
    sp.add_argument("--backend", default=argparse.SUPPRESS,
                    choices=["auto", "py", "cli"], help="设备后端")
    sp.set_defaults(func=cmd_pin)

    sp = sub.add_parser("profile", help="生成延迟更新描述文件（.mobileconfig）")
    sp.add_argument("--org", default="", help="组织名称")
    sp.add_argument("--days", type=int, default=90, help="延迟天数（1-90，默认 90）")
    sp.add_argument("-o", "--output", help="输出文件")
    sp.set_defaults(func=cmd_profile)
    return p


def _pick_udid(args: argparse.Namespace) -> str:
    """确定目标设备。

    只生成备份（--export-only）时不需要真实设备：备份目录名前缀随便一个合法
    名字即可，写设备时再改用真实 UDID。
    """
    if getattr(args, "udid", None):
        return args.udid
    if getattr(args, "export_only", False):
        return EXPORT_PLACEHOLDER
    devs = device.list_devices(args.backend)
    if not devs:
        raise DeviceError(
            "没有检测到设备。若只想生成备份文件（不写设备），加 --export-only 即可。"
        )
    if len(devs) > 1:
        raise DeviceError("检测到多台设备，请用 -u 指定 UDID")
    return devs[0].udid


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        args.command = "tui"
        args.func = cmd_tui
        args.udid = None
    # 关闭监督的拦截要在取设备之前，避免"没有设备"的错误先冒出来
    if args.func is cmd_supervise and not getattr(args, "on", True):
        return cmd_supervise(args)

    if args.func in (cmd_supervise, cmd_ota, cmd_reset):
        try:
            args.udid = _pick_udid(args)
        except DeviceError as exc:
            print(f"错误：{exc}")
            return 2
    try:
        return args.func(args)
    except BackendMissingError as exc:
        print(f"依赖缺失：{exc}")
        return 2
    except DeviceError as exc:
        print(f"错误：{exc}")
        return 2
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


class BackendMissingError(DeviceError):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
