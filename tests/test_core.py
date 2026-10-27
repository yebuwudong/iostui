"""不依赖真实设备的单元测试：mbdb 打包、payload 生成、备份目录结构。"""

from __future__ import annotations

import hashlib
import plistlib
from pathlib import Path

import pytest

from iostui import mbdb, payload
from iostui.device import make_backup
from iostui.payload import Plan


# --------------------------------------------------------------------------- #
# mbdb
# --------------------------------------------------------------------------- #

def test_mbdb_roundtrip_preserves_fields():
    items = [
        mbdb.StagedItem(
            domain="SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles",
            relative_path="Library/ConfigurationProfiles/CloudConfigurationDetails.plist",
            data=b"<plist/>",
            mtime=1700000000,
        ),
        mbdb.StagedItem(domain="ManagedPreferencesDomain",
                        relative_path="mobile/com.apple.MobileAsset.plist",
                        data=b"x" * 100, mtime=1700000001),
    ]
    entries = mbdb.parse_mbdb(mbdb.build_mbdb(items))
    files = [e for e in entries if not e.is_dir]
    dirs = [e for e in entries if e.is_dir]
    assert len(files) == 2
    assert dirs, "应包含父目录记录"
    assert files[0].domain.startswith("SysSharedContainerDomain")
    assert files[0].relative_path.endswith("CloudConfigurationDetails.plist")
    assert files[0].size == len(b"<plist/>")
    assert files[0].digest == hashlib.sha1(b"<plist/>").digest()
    assert files[0].mtime == 1700000000
    assert files[1].size == 100
    # 文件权限位：普通文件 + 777
    assert files[0].mode & 0o777 == 0o777
    assert files[0].mode & 0o170000 == 0o100000


def test_mbdb_includes_parent_directories():
    """必须为文件的父目录写目录记录，与 Cowabunga 的 find 遍历一致。

    缺目录条目时设备可能不把文件放到目标路径。
    """
    data = mbdb.build_mbdb([mbdb.StagedItem("HomeDomain", "a/b/c.plist", b"1")])
    assert data.startswith(b"mbdb\x05\x00")
    entries = mbdb.parse_mbdb(data)
    paths = [e.relative_path for e in entries]
    # 第一条是 domain 根目录自身（path 为空），与 Cowabunga 的 find 输出一致
    assert paths == ["", "a", "a/b", "a/b/c.plist"]
    dirs = [e for e in entries if e.is_dir]
    assert [d.relative_path for d in dirs] == ["", "a", "a/b"]
    assert all(d.mode & 0o170000 == 0o040000 for d in dirs)


def test_mbdb_can_disable_dir_entries():
    data = mbdb.build_mbdb(
        [mbdb.StagedItem("HomeDomain", "a/b.plist", b"1")], with_dirs=False
    )
    assert len(mbdb.parse_mbdb(data)) == 1


def test_file_id_is_domain_path_sha1():
    fid = mbdb.file_id("HomeDomain", "Library/x.plist")
    assert fid == hashlib.sha1(b"HomeDomain-Library/x.plist").hexdigest()


def test_device_domain_no_longer_aliases():
    """设备自己会做 ConfigProfileDomain 的映射，工具不能替它换掉。

    ``ConfigProfileDomain`` 由设备自行映射，工具不做替换。
    """
    assert mbdb.device_domain("ConfigProfileDomain") == "ConfigProfileDomain"
    assert mbdb.device_domain("SkipSetup") == "SkipSetup"


# --------------------------------------------------------------------------- #
# payload
# --------------------------------------------------------------------------- #

def test_cloud_config_enabled():
    conf = payload.cloud_config(True, "TestOrg")
    assert conf["IsSupervised"] is True
    assert conf["OrganizationName"] == "TestOrg"
    assert conf["CloudConfigurationUIComplete"] is True
    assert "WiFi" in conf["SkipSetup"]


def test_cloud_config_disabled_is_clean():
    """关闭监督必须清掉所有「已配置」痕迹。

    只把 IsSupervised 设成 False、留下 CloudConfigurationUIComplete=True，
    会在设备上留下半成品云配置并占住名额（官方接口此后报 14002）。
    """
    # 关闭监督：保留「初始化已完成」，只把 IsSupervised 置 false。
    # 清空文件会让设备重走设置流程、卡在设置界面（实测）。
    conf = payload.cloud_config(False)
    assert conf["IsSupervised"] is False
    assert conf["OrganizationName"] == ""
    assert conf["CloudConfigurationUIComplete"] is True
    assert conf["SkipSetup"]


def test_ota_block_sets_tvos_server_and_audience():
    """屏蔽要同时改服务器地址和 audience。

    只写空 audience 在部分 iOS 版本上不生效（实测屏蔽没效果）；
    macOS 版长期采用的是「7 个键全写」的完整做法。
    """
    items = payload.ota_items(True)
    assert len(items) == 1
    blob = plistlib.loads(items[0].data)
    assert blob["MobileAssetAssetAudience"] == "65254ac3-f331-4c19-8559-cbe22f5bc1a6"
    assert blob["MobileAssetSUAllowOSVersionChange"] is False
    assert all("tvOS16DeveloperSeed" in v for k, v in blob.items()
               if k.startswith("MobileAssetServerURL"))
    assert len(blob) == 7


def test_ota_unblock_writes_empty_dict():
    """恢复时必须清空所有键，否则会一直处于屏蔽状态。"""
    items = payload.ota_items(False)
    assert plistlib.loads(items[0].data) == {}


def test_supervision_items_targets_two_files():
    items = payload.supervision_items(True, "Org", skip_setup=True)
    paths = {(i.domain, i.relative_path) for i in items}
    # ConfigProfileDomain 要换成设备域名；path 从其下一层开始
    assert (payload.DOMAIN_CONFIGPROFILE,
            "Library/ConfigurationProfiles/CloudConfigurationDetails.plist") in paths
    assert (payload.DOMAIN_MANAGED, "mobile/com.apple.purplebuddy.plist") in paths


def test_plan_items_and_describe():
    plan = Plan(supervised=True, organization="O", ota_blocked=True)
    assert len(plan.items()) == 3
    text = "\n".join(plan.describe())
    assert "监督" in text and "屏蔽系统更新" in text


def test_plan_empty_when_nothing_selected():
    assert Plan().items() == []


def test_delayed_update_profile_structure():
    data = payload.delayed_update_profile("Org", 90)
    prof = plistlib.loads(data)
    assert prof["PayloadType"] == "Configuration"
    inner = prof["PayloadContent"][0]
    assert inner["forceDelayedSoftwareUpdates"] is True
    assert inner["enforcedSoftwareUpdateDelay"] == 90


def test_delayed_update_profile_clamps_days():
    prof = plistlib.loads(payload.delayed_update_profile("O", 999))
    assert prof["PayloadContent"][0]["enforcedSoftwareUpdateDelay"] == 90


# --------------------------------------------------------------------------- #
# 备份目录
# --------------------------------------------------------------------------- #

def test_make_backup_layout(tmp_path: Path):
    items = Plan(supervised=True, organization="X", ota_blocked=True).items()
    device_dir = make_backup(tmp_path, items, udid="abc123")
    # 目录名必须是 Backup（Cowabunga 的 CreateBackup.sh 行为）
    assert device_dir == tmp_path / "Backup"
    for name in ("Info.plist", "Manifest.plist", "Status.plist", "Manifest.mbdb"):
        assert (device_dir / name).exists(), name
    # 实体文件以 sha1(domain-path) 命名
    for item in items:
        assert (device_dir / item.path_hash).read_bytes() == item.data
    entries = mbdb.parse_mbdb((device_dir / "Manifest.mbdb").read_bytes())
    files = [e for e in entries if not e.is_dir]
    assert len(files) == len(items)
    assert any(e.is_dir for e in entries)


def test_status_plist_marks_finished_backup(tmp_path: Path):
    make_backup(tmp_path, payload.ota_items(True), udid="u")
    status = plistlib.loads((tmp_path / "Backup" / "Status.plist").read_bytes())
    # idevicebackup2 会先校验这一项，否则拒绝 restore
    assert status["SnapshotState"] == "finished"


def test_stage_tree_reads_payload_tree(tmp_path: Path):
    root = tmp_path / "payload"
    target = root / "HomeDomain" / "Library" / "Preferences" / "a.plist"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"abc")
    items = mbdb.stage_tree(root)
    assert len(items) == 1
    assert items[0].domain == "HomeDomain"
    assert items[0].relative_path == "Library/Preferences/a.plist"


# --------------------------------------------------------------------------- #
# 端到端：用假后端跑完整 apply 流程（不打真设备）
# --------------------------------------------------------------------------- #

def test_apply_plan_end_to_end(tmp_path: Path, monkeypatch):
    from iostui import device

    captured = {}

    class FakeBackend:
        name = "fake"

        def list_devices(self):
            return []

        def device_info(self, udid):
            from iostui.backupmeta import DeviceInfo
            return DeviceInfo(udid=udid.upper(), serial="F2LX00000000",
                              device_name="iPhone", product_type="iPhone14,3",
                              product_version="18.3.2", build_version="22D82")

        def restore(self, udid, device_dir, *, progress=None, log=None):
            captured["udid"] = udid
            captured["dir"] = device_dir
            captured["entries"] = [e for e in mbdb.parse_mbdb(
                (device_dir / "Manifest.mbdb").read_bytes()) if not e.is_dir]
            if progress:
                progress(1.0, "restore")
            if log:
                log("done")

    monkeypatch.setattr(device, "get_backend", lambda prefer="auto": FakeBackend())
    items = Plan(supervised=True, organization="Org", ota_blocked=True).items()
    out = device.apply_plan(items, udid="UDID123", keep=tmp_path)
    assert out == tmp_path / "Backup"
    assert captured["udid"] == "UDID123"
    assert len(captured["entries"]) == 3
    # 备份目录里必须有 restore 需要的四个元数据文件
    for name in ("Info.plist", "Manifest.plist", "Status.plist", "Manifest.mbdb"):
        assert (out / name).exists()


# --------------------------------------------------------------------------- #
# 前置检查（查找我的 iPhone）
# --------------------------------------------------------------------------- #

def test_fmip_off_is_clear():
    from iostui import preflight

    chk = preflight.evaluate_fmip({"IsActive": False})
    assert chk.verdict is preflight.Verdict.CLEAR
    assert chk.ok and not chk.hard_fail


def test_fmip_on_is_hard_block():
    from iostui import preflight

    chk = preflight.evaluate_fmip({"IsActive": True})
    assert chk.verdict is preflight.Verdict.BLOCKED
    assert chk.hard_fail is True
    assert not chk.ok


def test_fmip_missing_is_unknown_not_clear():
    """查不到时必须报「未知」，绝不能当成「已关闭」。"""
    from iostui import preflight

    for values, err in ((None, ""), ({}, ""), (None, "域不存在"), ({"Foo": 1}, "")):
        chk = preflight.evaluate_fmip(values, error=err)
        assert chk.verdict is preflight.Verdict.UNKNOWN, (values, err)
        assert chk.needs_confirm
        assert not chk.ok
        assert chk.hard_fail is False


def test_fmip_conflicting_values_unknown():
    from iostui import preflight

    chk = preflight.evaluate_fmip({"IsActive": True, "FMIPActive": False})
    assert chk.verdict is preflight.Verdict.UNKNOWN


def test_passcode_locked_blocks():
    from iostui import preflight

    assert preflight.evaluate_passcode(True).hard_fail is True
    assert preflight.evaluate_passcode(False).ok is True
    assert preflight.evaluate_passcode(None).ok is True


def test_preflight_summarize():
    from iostui import preflight

    blocked = preflight.evaluate_fmip({"IsActive": True})
    unknown = preflight.evaluate_fmip({})
    clear = preflight.evaluate_passcode(False)
    ok, hard, unk = preflight.summarize([blocked, unknown, clear])
    assert ok is False and len(hard) == 1 and len(unk) == 1
    ok, hard, unk = preflight.summarize([unknown, clear])
    assert ok is True and hard == [] and len(unk) == 1


def test_apply_plan_refuses_on_hard_fail(tmp_path, monkeypatch):
    """硬失败时必须在生成/写入前抛错。"""
    from iostui import device, preflight
    from iostui.payload import Plan

    called = {"restore": False}

    class FakeBackend:
        name = "fake"

        def list_devices(self):
            return []

        def device_info(self, udid):
            from iostui.backupmeta import DeviceInfo
            return DeviceInfo(udid=udid.upper(), product_type="iPhone14,3",
                              product_version="18.3.2", build_version="22D82")

        def restore(self, *a, **k):
            called["restore"] = True

    monkeypatch.setattr(device, "get_backend", lambda prefer="auto": FakeBackend())
    items = Plan(supervised=True).items()
    checks = [preflight.evaluate_fmip({"IsActive": True})]
    with pytest.raises(device.PreflightBlocked):
        device.apply_plan(items, udid="U", keep=tmp_path, checks=checks)
    assert called["restore"] is False


def test_apply_plan_allows_unknown_with_force(tmp_path, monkeypatch):
    from iostui import device, preflight
    from iostui.payload import Plan

    class FakeBackend:
        name = "fake"

        def list_devices(self):
            return []

        def device_info(self, udid):
            from iostui.backupmeta import DeviceInfo
            return DeviceInfo(udid=udid.upper(), product_type="iPhone14,3",
                              product_version="18.3.2", build_version="22D82")

        def restore(self, *a, **k):
            return None

    monkeypatch.setattr(device, "get_backend", lambda prefer="auto": FakeBackend())
    checks = [preflight.evaluate_fmip({})]
    out = device.apply_plan(Plan(ota_blocked=True).items(), udid="U",
                            keep=tmp_path, checks=checks, force=True)
    assert out.exists()


def test_enforcement_declaration_shape():
    import json
    from iostui.payload import enforcement_declaration

    d = json.loads(enforcement_declaration("16.1", "Org", "2026-12-01T09:00:00"))
    assert d["Type"] == "com.apple.configuration.softwareupdate.enforcement.specific"
    assert d["Payload"]["TargetOSVersion"] == "16.1"
    assert d["Payload"]["TargetLocalDateTime"] == "2026-12-01T09:00:00"


def test_delay_profile_has_no_version_pin():
    """延迟更新是按天数，不针对特定版本 —— 不应出现版本字段。"""
    from iostui.payload import delayed_update_profile

    prof = plistlib.loads(delayed_update_profile("O", 45))
    inner = prof["PayloadContent"][0]
    assert inner["enforcedSoftwareUpdateDelay"] == 45
    assert "TargetOSVersion" not in inner


# --------------------------------------------------------------------------- #
# Pallas / 锁定版本
# --------------------------------------------------------------------------- #

def test_release_ordering():
    from iostui.pallas import Release
    from datetime import date

    a = Release("27.0", "24A", date(2026, 9, 14))
    b = Release("27.0.1", "24B", date(2026, 9, 28))
    c = Release("26.9", "23Z", date(2026, 8, 1))
    assert a.key < b.key
    assert c.key < a.key
    assert max([a, b, c], key=lambda r: r.key) is b


def test_compute_pin_delay_and_expiry():
    """复现 delayed-otas 页面上的真实数据（2026-10-27）。"""
    from datetime import date
    from iostui.pallas import Release, compute_pin

    rels = [
        Release("27.2", "24B6119", date(2026, 10, 26)),
        Release("27.0.1", "24A6446", date(2026, 9, 28)),
        Release("27.0", "24A6437", date(2026, 9, 14)),
        Release("26.6.2", "23G90", date(2026, 9, 8)),
        Release("26.6.1", "23G6083", date(2026, 8, 17)),
        Release("26.6", "23G6071", date(2026, 7, 27)),
    ]
    spans = {
        "27.2": (0, 1), "27.0.1": (2, 29), "27.0": (30, 43),
        "26.6.2": (44, 49), "26.6.1": (50, 71), "26.6": (72, 90),
    }
    expected = {
        "27.0.1": (29, date(2027, 1, 24)),
        "27.0": (43, date(2026, 12, 27)),
        "26.6.2": (49, date(2026, 12, 13)),
        "26.6.1": (71, date(2026, 12, 7)),
        "26.6": (90, date(2026, 11, 15)),
    }
    for version, (delay, expires) in expected.items():
        pin = compute_pin(rels, version, spans=spans, today=date(2026, 10, 27))
        assert pin is not None, version
        assert pin.delay == delay, (version, pin.delay, delay)
        assert pin.expires == expires, (version, pin.expires, expires)


def test_compute_pin_unknown_version():
    from datetime import date
    from iostui.pallas import Release, compute_pin

    rels = [Release("27.0", "24A", date(2026, 9, 14))]
    assert compute_pin(rels, "26.0", today=date(2026, 10, 27)) is None


def test_scan_window_merges_ranges(monkeypatch):
    from iostui import pallas

    table = {0: "27.2", 1: "27.2", 2: "27.0.1", 3: "27.0.1", 4: "27.0"}

    def fake(product_type, hw, delay, timeout=30):
        v = table.get(delay)
        if v is None:
            return None
        from datetime import date
        return pallas.Release(v, "B", date(2026, 9, 1))

    monkeypatch.setattr(pallas, "visible_version", fake)
    spans = pallas.scan_window("iPhone17,3", "D47AP", max_delay=4)
    assert spans["27.2"] == (0, 1)
    assert spans["27.0.1"] == (2, 3)
    assert spans["27.0"] == (4, 4)


def test_pinned_profile_content():
    from datetime import date
    from iostui.payload import pinned_profile

    prof = plistlib.loads(pinned_profile("27.0", 43, organization="Me",
                                         build="24A6437", expires=date(2026, 12, 27)))
    inner = prof["PayloadContent"][0]
    assert inner["enforcedSoftwareUpdateDelay"] == 43
    assert inner["forceDelayedSoftwareUpdates"] is True
    assert "27.0" in prof["PayloadDisplayName"]
    assert "2026-12-27" in prof["PayloadDescription"]


# --------------------------------------------------------------------------- #
# CLI 无设备场景
# --------------------------------------------------------------------------- #

def test_export_only_needs_no_device(tmp_path, monkeypatch):
    """--export-only 应当在没有设备时也能成功，而不是报「没检测到设备」。"""
    from iostui import cli, device

    def no_devices(prefer="auto"):
        raise AssertionError("export-only 不应尝试读取设备列表")

    monkeypatch.setattr(device, "list_devices", no_devices)
    codes = [
        ["supervise", "--on", "--org", "My iPhone", "--export-only", "-o", str(tmp_path / "a")],
        ["ota", "--on", "--export-only", "-o", str(tmp_path / "b")],
        ["reset", "--export-only", "-o", str(tmp_path / "c")],
    ]
    for argv in codes:
        assert cli.main(argv) == 0, argv
    # 每个都确实生成了备份
    for sub in ("a", "b", "c"):
        assert (tmp_path / sub / "Backup" / "Manifest.mbdb").exists(), sub


def test_org_with_space_preserved(tmp_path):
    from iostui import cli

    assert cli.main(["supervise", "--on", "--org", "My iPhone",
                     "--export-only", "-o", str(tmp_path)]) == 0
    import plistlib
    # 实体文件以 sha1 命名，这里直接校验目录里有内容即可
    assert any(p.name for p in (tmp_path / "Backup").iterdir())


def test_profile_and_enforce_offline(tmp_path):
    from iostui import cli

    prof = tmp_path / "d.mobileconfig"
    assert cli.main(["profile", "--days", "90", "-o", str(prof)]) == 0
    assert prof.exists() and prof.stat().st_size > 0

    js = tmp_path / "e.json"
    assert cli.main(["enforce", "--version", "16.1", "-o", str(js)]) == 0
    assert b"enforcement.specific" in js.read_bytes()


# --------------------------------------------------------------------------- #
# 备份元数据（iOS 18 上 ErrorCode 4 的根因）
# --------------------------------------------------------------------------- #

def _dev_info():
    from iostui.backupmeta import DeviceInfo
    return DeviceInfo(
        udid="00008110-000115343628401E",
        serial="F2LX12345678",
        device_name="13pm",
        product_type="iPhone14,3",
        product_version="18.3.2",
        build_version="22D82",
    )


def test_info_plist_has_target_identifier():
    """Info.plist 不能是空字典，必须带设备标识。"""
    from iostui.backupmeta import make_info_plist

    pl = plistlib.loads(make_info_plist(_dev_info()))
    assert pl["Target Identifier"] == "00008110-000115343628401E"
    assert pl["Product Version"] == "18.3.2"
    assert pl["Product Type"] == "iPhone14,3"
    assert pl["Build Version"] == "22D82"
    assert pl["Serial Number"] == "F2LX12345678"
    assert pl["Target Type"] == "Device"


def test_info_plist_rejects_incomplete_device():
    from iostui.backupmeta import BackupMetadataError, DeviceInfo, make_info_plist

    with pytest.raises(BackupMetadataError):
        make_info_plist(DeviceInfo(udid="X"))  # 缺 ProductType / Version


def test_manifest_plist_follows_device_version():
    """SystemDomainsVersion 要跟随系统版本，而不是抄死 20.0。"""
    from iostui.backupmeta import make_manifest_plist

    pl = plistlib.loads(make_manifest_plist(_dev_info()))
    assert pl["SystemDomainsVersion"] == "22.0"   # 18 + 4
    assert "BackupKeyBag" in pl and len(pl["BackupKeyBag"]) > 100
    assert pl["Version"] == "10.0"
    assert pl["IsEncrypted"] is False


def test_status_plist_finished():
    from iostui.backupmeta import make_status_plist

    st = plistlib.loads(make_status_plist(_dev_info()))
    assert st["SnapshotState"] == "finished"
    assert st["UUID"] == "00008110-000115343628401e"


def test_manifest_db_readable_by_standard_parser(tmp_path):
    """生成的 Manifest.db 必须能被 pyiosbackup 正常解析。"""
    from iostui import mbdb
    from iostui.backupmeta import make_manifest_db
    from iostui.payload import Plan

    items = Plan(supervised=True, organization="O", ota_blocked=True).items()
    data = make_manifest_db(items, tmp_path)
    db = tmp_path / "Manifest.db"
    db.write_bytes(data)

    import sqlite3
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT fileID, domain, relativePath, flags, file FROM Files"
    ).fetchall()
    conn.close()
    # 记录集合应与 mbdb 完全一致（含 domain 根记录与各级父目录）
    expected = {(it.domain, it.relative_path) for it in items}
    expected |= {(it.domain, pp) for it in items for pp in mbdb.parent_dirs(it.relative_path)}
    expected |= {(it.domain, "") for it in items}
    assert {(d, rel) for _, d, rel, _, _ in rows} == expected
    for fid, domain, rel, flags, blob in rows:
        assert fid == mbdb.file_id(domain, rel)
        assert flags == 1
        assert blob is not None
        # file 是 NSKeyedArchiver 归档，应含类名与路径
        assert blob is not None
        assert b"MBFile" in blob or b"RelativePath" in blob


def test_backup_matches_cowabunga_layout(tmp_path):
    """备份目录必须与 Cowabunga 一致：只有 mbdb + 三个原样模板。

    Cowabunga 的 Info.plist 就是空 dict，且不写 Manifest.db。
    模板原样使用，不额外生成 Manifest.db。
    """
    from iostui.device import make_backup
    from iostui.payload import Plan

    items = Plan(ota_blocked=True).items()
    out = make_backup(tmp_path, items, udid="U1", info=_dev_info())
    names = {p.name for p in out.iterdir() if not p.name[0].isalnum() or len(p.name) != 40}
    assert (out / "Manifest.mbdb").exists()
    assert not (out / "Manifest.db").exists()
    info = plistlib.loads((out / "Info.plist").read_bytes())
    assert info == {}
    status = plistlib.loads((out / "Status.plist").read_bytes())
    assert status["SnapshotState"] == "finished"


# --------------------------------------------------------------------------- #
# MCInstall 云配置（监督状态）
# --------------------------------------------------------------------------- #

def test_cloud_configuration_enable_has_required_fields():
    """少字段会被 cloudconfigurationd 静默忽略，导致「写入成功但没生效」。"""
    from iostui.mcinstall import build_cloud_configuration

    cfg = build_cloud_configuration(True, "Org")
    required = {"IsSupervised", "OrganizationName",
                "CloudConfigurationUIComplete", "SkipSetup"}
    assert required <= set(cfg)
    assert cfg["IsSupervised"] is True
    assert cfg["OrganizationName"] == "Org"
    assert "WiFi" in cfg["SkipSetup"]


def test_cloud_configuration_disable_clears():
    from iostui.mcinstall import build_cloud_configuration

    cfg = build_cloud_configuration(False)
    assert cfg["IsSupervised"] is False
    assert cfg["OrganizationName"] == ""
    assert cfg["SkipSetup"] == []


def test_cloud_configuration_default_org():
    from iostui.mcinstall import build_cloud_configuration

    assert build_cloud_configuration(True, "")["OrganizationName"]


def test_verify_reports_mismatch(monkeypatch):
    from iostui import mcinstall

    monkeypatch.setattr(mcinstall, "get_current",
                        lambda udid: {"IsSupervised": False, "OrganizationName": ""})
    ok, msg = mcinstall.verify("U", expect_supervised=True)
    assert ok is False and "不符" in msg

    monkeypatch.setattr(mcinstall, "get_current",
                        lambda udid: {"IsSupervised": True, "OrganizationName": "O"})
    ok, msg = mcinstall.verify("U", expect_supervised=True)
    assert ok is True and "O" in msg


def test_verify_handles_missing_config(monkeypatch):
    from iostui import mcinstall

    monkeypatch.setattr(mcinstall, "get_current", lambda udid: None)
    ok, _ = mcinstall.verify("U", expect_supervised=False)
    assert ok is True          # 未受管状态下「关闭监督」算达成
    ok, msg = mcinstall.verify("U", expect_supervised=True)
    assert ok is False and "未生效" in msg


def test_classify_cloud_states():
    from iostui.mcinstall import CloudState, classify

    assert classify(None) is CloudState.UNMANAGED
    assert classify({}) is CloudState.UNMANAGED
    assert classify({"IsSupervised": True}) is CloudState.SUPERVISED
    # 用户实测到的那种半成品状态
    residual = {
        "IsSupervised": False,
        "OrganizationName": "",
        "ConfigurationWasApplied": True,
        "ConfigurationSource": None,
        "CloudConfigurationUIComplete": True,
    }
    assert classify(residual) is CloudState.RESIDUAL


def test_explain_residual_warns_against_restore():
    from iostui.mcinstall import explain

    text = "\n".join(explain({
        "IsSupervised": False,
        "ConfigurationWasApplied": True,
        "CloudConfigurationUIComplete": True,
    }))
    assert "残留" in text
    assert "14002" in text
    # 必须给出无需监督的替代路径
    assert "ota --on" in text
    # 不能给出没有依据的「抹机」建议
    assert "抹" not in text


def test_explain_supervised_ok():
    from iostui.mcinstall import explain

    text = "\n".join(explain({"IsSupervised": True}))
    assert "已受监督" in text


# --------------------------------------------------------------------------- #
# 监督身份证书（IsSupervised 不被采纳的真正原因）
# --------------------------------------------------------------------------- #

def test_supervisor_identity_roundtrip(tmp_path):
    from iostui.supervisor import ensure_identity, public_der

    pem = ensure_identity(tmp_path / "sup.pem")
    assert pem.exists()
    der = public_der(pem)
    assert len(der) > 200
    # 复用同一份身份，不应重新生成
    assert ensure_identity(pem) == pem
    assert pem.read_bytes() == pem.read_bytes()


def test_identity_pem_has_cert_and_key(tmp_path):
    from iostui.supervisor import ensure_identity

    pem = ensure_identity(tmp_path / "s.pem").read_bytes()
    assert b"BEGIN CERTIFICATE" in pem
    assert b"PRIVATE KEY" in pem
    # pymobiledevice3 的 escalate() 要求两者在同一个 PEM 里
    from cryptography import x509
    x509.load_pem_x509_certificate(pem)


def test_cloud_configuration_includes_supervisor_cert(tmp_path):
    """没有 SupervisorHostCertificates，设备会忽略 IsSupervised。"""
    from iostui.mcinstall import build_cloud_configuration
    from iostui.supervisor import ensure_identity, public_der

    der = public_der(ensure_identity(tmp_path / "s.pem"))
    cfg = build_cloud_configuration(True, "Org", der)
    assert cfg["SupervisorHostCertificates"] == [der]
    # 关闭监督时不应带证书
    assert "SupervisorHostCertificates" not in build_cloud_configuration(False)


def test_plist_payload_has_supervisor_cert(tmp_path):
    from iostui.payload import Plan
    from iostui.supervisor import ensure_identity, public_der

    der = public_der(ensure_identity(tmp_path / "s.pem"))
    items = Plan(supervised=True, organization="O", supervisor_der=der).items()
    cfg = plistlib.loads(items[0].data)
    assert cfg["IsSupervised"] is True
    assert cfg["SupervisorHostCertificates"] == [der]


def test_manifest_db_matches_mbdb_entries(tmp_path):
    """Manifest.db 与 Manifest.mbdb 的记录集合必须一致。

    iOS 10.2+ 优先读 Manifest.db；两份清单不一致时设备会按有缺陷的那份恢复，
    表现为「恢复成功但文件没落到预期位置」。
    """
    import sqlite3
    from iostui.backupmeta import make_manifest_db
    from iostui.payload import Plan

    items = Plan(supervised=True, organization="O", ota_blocked=True).items()
    data = make_manifest_db(items, tmp_path)
    (tmp_path / "Manifest.db").write_bytes(data)

    conn = sqlite3.connect(str(tmp_path / "Manifest.db"))
    db_paths = {(d, p) for d, p in
                conn.execute("SELECT domain, relativePath FROM Files")}
    conn.close()

    mb_paths = {(e.domain, e.relative_path) for e in
                mbdb.parse_mbdb(mbdb.build_mbdb(items))}
    assert db_paths == mb_paths


def test_empty_path_encodes_as_zero_length_not_nofield():
    """空字符串必须编码为 length=0x0000；0xFFFF 表示「无该字段」。

    这是 mbdb 最隐蔽的坑：Cowabunga 给 domain 根目录写的是 ``path_len=0000``。
    误写成 0xFFFF 会让设备解析出的路径错位，整份清单被拒，
    idevicebackup2 报 "Could not receive from mobilebackup2"。
    """
    entry = mbdb.MbdbEntry(domain="OTAKiller", relative_path="", mode=mbdb.MODE_DIR)
    blob = entry.encode()
    # 前两字节是 domain 长度
    assert blob[0:2] == b"\x00\x09"
    assert blob[2:11] == b"OTAKiller"
    # 接下来的两字节必须是 0x0000（空路径），而不是 0xFFFF
    assert blob[11:13] == b"\x00\x00"


def test_link_target_absent_uses_nofield():
    """没有 link target 时才用 0xFFFF。"""
    entry = mbdb.MbdbEntry(domain="D", relative_path="p", mode=mbdb.MODE_FILE)
    blob = entry.encode()
    # domain(2+1) + path(2+1) 之后两字节应为 FFFF
    idx = 2 + 1 + 2 + 1
    assert blob[idx:idx + 2] == b"\xff\xff"


def test_mbdb_matches_cowabunga_reference_bytes(tmp_path):
    """与 Cowabunga 脚本产物做结构级对比（忽略随机字节与它多写的空尾记录）。"""
    import shutil
    import subprocess
    from pathlib import Path

    ref = Path("_ref/CowabungaLite-main/Cowabunga Lite/Scripts/CreateBackup.sh")
    if not ref.exists() or not shutil.which("bash"):
        pytest.skip("参考脚本不可用")

    from iostui.payload import ota_items

    payload_bytes = ota_items(True)[0].data
    enabled = tmp_path / "EnabledTweaks"
    backup = tmp_path / "Backup"
    d = enabled / "ManagedPreferencesDomain" / "mobile"
    d.mkdir(parents=True)
    (d / "com.apple.MobileAsset.plist").write_bytes(payload_bytes)
    subprocess.run(["bash", str(ref), str(enabled), str(backup)],
                   capture_output=True, check=True)

    # Cowabunga 的 shell 脚本末尾会多写几条全 0 的残缺记录，解析时跳过空 domain
    parsed = mbdb.parse_mbdb((backup / "Manifest.mbdb").read_bytes())
    cow = [e for e in parsed if e.domain]
    mine = mbdb.parse_mbdb(mbdb.build_mbdb(ota_items(True)))
    assert len(cow) == len(mine)
    for a, b in zip(cow, mine):
        assert (a.domain, a.relative_path, a.mode) == (b.domain, b.relative_path, b.mode)


# --------------------------------------------------------------------------- #
# TUI 结构检查（防止 ModalScreen/App API 混用）
# --------------------------------------------------------------------------- #

def test_pinscreen_uses_app_call_from_thread():
    """PinScreen 是 ModalScreen，没有 call_from_thread —— 只能用 app 的。

    这类错误只会在运行时炸（AttributeError: 'PinScreen' object has no
    attribute 'call_from_thread'），静态检查能提前拦住。
    """
    import inspect
    import re

    from iostui import tui

    src = inspect.getsource(tui.PinScreen)
    # PinScreen 内不允许出现裸的 self.call_from_thread
    assert not re.search(r"self\.call_from_thread\(", src), (
        "PinScreen 里不能用 self.call_from_thread，应改用 self.app.call_from_thread"
    )


def test_tui_imports_and_composes():
    """TUI 能 import 且关键类存在。"""
    from iostui import tui

    assert hasattr(tui, "SuperviseApp")
    assert hasattr(tui, "PinScreen")
    assert hasattr(tui, "ConfirmScreen")


# --------------------------------------------------------------------------- #
# 预计算索引（秒出，不再扫 91 次）
# --------------------------------------------------------------------------- #

def test_parse_release_name():
    from iostui.pallas import _parse_name

    assert _parse_name("iOS 27.0.1 (24A446)") == ("27.0.1", "24A446")
    assert _parse_name("iOS 18.7.9 (22H355)") == ("18.7.9", "22H355")
    assert _parse_name("garbage") == ("", "")


def test_plan_pin_searches_all_groups():
    """机型分组推断不准时，也要能在其它 iOS 分组里找到目标版本。"""
    from iostui.pallas import plan_pin_from_index

    index = {
        "iOS Legacy v4 (devices supporting up to iOS 18)": [
            {"name": "iOS 18.7.9 (22H355)", "delay": 90, "date": None, "latest": False},
        ],
        "iOS (devices supporting iOS 27)": [
            {"name": "iOS 27.0.1 (24A446)", "delay": 29, "date": None, "latest": False},
        ],
    }
    # iPhone14,3 会被分到 18 组，但目标在 27 组里
    pin = plan_pin_from_index("iPhone14,3", "27.0.1", index=index)
    assert pin.delay == 29


def test_plan_pin_from_index_uses_precomputed():
    from datetime import date
    from iostui.pallas import plan_pin_from_index

    index = {
        "iOS (devices supporting iOS 27)": [
            {"name": "iOS 27.2 (24B119)", "delay": 0, "date": None, "latest": True},
            {"name": "iOS 27.0.1 (24A446)", "delay": 29,
             "date": "2027-01-24T00:00:00+00:00", "latest": False},
        ],
        "iOS Legacy v4 (devices supporting up to iOS 18)": [
            {"name": "iOS 18.7.11 (22H384)", "delay": 0, "date": None, "latest": True},
            {"name": "iOS 18.7.9 (22H355)", "delay": 90,
             "date": "2026-11-15T00:00:00+00:00", "latest": False},
        ],
    }
    pin = plan_pin_from_index("iPhone14,3", "18.7.9", index=index)
    assert pin.delay == 90
    assert pin.expires == date(2026, 11, 15)
    assert pin.release.build == "22H355"


def test_plan_pin_from_index_latest_is_rejected():
    from iostui.pallas import CatalogError, plan_pin_from_index

    index = {"iOS (devices supporting iOS 27)": [
        {"name": "iOS 27.2 (24B119)", "delay": 0, "date": None, "latest": True},
    ]}
    # latest 版本不在可锁定列表里，会报「没有」而不是「最新」
    with pytest.raises(CatalogError):
        plan_pin_from_index("iPhone14,3", "27.2", index=index)


def test_plan_pin_from_index_unknown_version():
    from iostui.pallas import CatalogError, plan_pin_from_index

    index = {"iOS (devices supporting iOS 27)": [
        {"name": "iOS 27.0.1 (24A446)", "delay": 29, "date": None, "latest": False},
    ]}
    with pytest.raises(CatalogError, match="没有"):
        plan_pin_from_index("iPhone14,3", "99.9", index=index)


def test_lockable_versions_includes_all_groups():
    """不能按机型推断来过滤版本 —— 那会漏掉能升级到的系统。

    版本列表必须来自全部 iOS 分组，不做机型推断。
    """
    from iostui.pallas import lockable_versions

    index = {
        "iOS (devices supporting iOS 27)": [
            {"name": "iOS 27.0.1 (24A446)", "delay": 29, "date": None, "latest": False},
        ],
        "iOS Legacy v4 (devices supporting up to iOS 18)": [
            {"name": "iOS 18.7.9 (22H355)", "delay": 90, "date": None, "latest": False},
        ],
    }
    vs = [e["_version"] for e in lockable_versions("iPhone14,3", index)]
    assert "27.0.1" in vs
    assert "18.7.9" in vs


def test_lockable_versions_sorted_desc():
    from iostui.pallas import lockable_versions

    index = {"iOS": [
        {"name": "iOS 18.7.9 (22H355)", "delay": 90, "date": None, "latest": False},
        {"name": "iOS 27.0.1 (24A446)", "delay": 29, "date": None, "latest": False},
        {"name": "iOS 26.7 (23H24)", "delay": 43, "date": None, "latest": False},
    ]}
    vs = [e["_version"] for e in lockable_versions("iPhone14,3", index)]
    assert vs == ["27.0.1", "26.7", "18.7.9"]


def test_lockable_versions_excludes_latest():
    from iostui.pallas import lockable_versions

    index = {"iOS": [
        {"name": "iOS 27.2 (24B119)", "delay": 0, "date": None, "latest": True},
        {"name": "iOS 27.0.1 (24A446)", "delay": 29, "date": None, "latest": False},
    ]}
    vs = [e["_version"] for e in lockable_versions("iPhone14,3", index)]
    assert vs == ["27.0.1"]


# --------------------------------------------------------------------------- #
# 描述文件直接安装
# --------------------------------------------------------------------------- #

def test_install_profile_calls_service(monkeypatch):
    """install_profile 应把描述文件字节交给 MCInstall 的 InstallProfile。"""
    from iostui import mcinstall

    captured = {}

    class FakeSvc:
        async def hello(self):
            captured["hello"] = True

        async def install_profile(self, payload):
            captured["payload"] = payload

    async def fake_with_service(udid, action):
        return await action(FakeSvc())

    monkeypatch.setattr(mcinstall, "_with_service", fake_with_service)
    mcinstall.install_profile("U", b"profile-bytes")
    assert captured["hello"] is True
    assert captured["payload"] == b"profile-bytes"


def test_install_profile_silent_requires_keybag(monkeypatch):
    from iostui import mcinstall

    class FakeSvc:
        async def hello(self):
            pass

    async def fake_with_service(udid, action):
        return await action(FakeSvc())

    monkeypatch.setattr(mcinstall, "_with_service", fake_with_service)
    with pytest.raises(mcinstall.DeviceError, match="监督身份"):
        mcinstall.install_profile("U", b"x", silent=True)


def test_profile_identifier_is_tool_owned():
    """生成的描述文件标识要带 com.iostui. 前缀，便于识别和清理。"""
    from iostui.payload import pinned_profile

    prof = plistlib.loads(pinned_profile("27.0.1", 29))
    assert prof["PayloadIdentifier"].startswith("com.iostui.")


# --------------------------------------------------------------------------- #
# 绝不能写入的 DEP / MDM 字段（会导致设备卡在「远程管理」）
# --------------------------------------------------------------------------- #

# 这些字段属于 MDM / 设备注册计划（DEP）语义。写进 CloudConfigurationDetails
# 后 iOS 会认为设备属于远程管理服务器，开机会进入「远程管理」登记界面并要求
# 连接服务器，导致设备无法正常进入系统。
FORBIDDEN_DEP_FIELDS = {
    "ConfigurationSource",
    "IsMandatory",
    "IsMDMUnremovable",
    "IsMultiUser",
    "ConfigurationWasApplied",
    "PostSetupProfileWasInstalled",
    "OrganizationMagic",
    "AllowPairing",
}


def test_cloud_config_has_no_dep_fields():
    from iostui import payload

    for supervised in (True, False):
        cfg = payload.cloud_config(supervised, "Org")
        bad = FORBIDDEN_DEP_FIELDS & set(cfg)
        assert not bad, f"cloud_config(supervised={supervised}) 含 DEP 字段：{bad}"


def test_mcinstall_config_has_no_dep_fields():
    from iostui.mcinstall import build_cloud_configuration

    for supervised in (True, False):
        cfg = build_cloud_configuration(supervised, "Org")
        bad = FORBIDDEN_DEP_FIELDS & set(cfg)
        assert not bad, f"build_cloud_configuration({supervised}) 含 DEP 字段：{bad}"


def test_supervision_plist_only_expected_keys():
    """写进设备的 plist 只能是 Cowabunga 那四个键（+可选监督证书）。"""
    from iostui import payload

    cfg = plistlib.loads(payload.supervision_items(True, "Org")[0].data)
    allowed = {
        "CloudConfigurationUIComplete",
        "IsSupervised",
        "OrganizationName",
        "SkipSetup",
        "SupervisorHostCertificates",
    }
    assert set(cfg) <= allowed
    assert cfg["IsSupervised"] is True


def test_purplebuddy_always_setup_done():
    """purplebuddy 任何时候都必须保持「设置已完成」。

    清空它会让设备重走设置流程。「跳过哪些页面」由
    CloudConfigurationDetails.SkipSetup 控制，与此文件无关。
    """
    from iostui import payload

    for supervised in (True, False):
        items = payload.supervision_items(supervised, "Org")
        pb = [i for i in items if i.relative_path.endswith("purplebuddy.plist")][0]
        data = plistlib.loads(pb.data)
        assert data["SetupDone"] is True
        assert data["SetupFinishedAllSteps"] is True
