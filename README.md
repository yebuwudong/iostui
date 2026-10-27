# iostui — 免越狱开关 iOS「监督模式」与「系统更新」

一条命令搞定，Windows / macOS / Linux 通用，带 TUI 图形界面。

```
┌ iOS 监督模式开关 ─────────────────────────────────────────────┐
│ 设备        [ iPhone 13 · iPhone14,5 · iOS 16.5 · USB      ] │
│                                                              │
│ 监督模式 (Supervision)                                        │
│   目标状态   [ 开启监督  ▾ ]    组织名称 [ My iPhone        ] │
│   ☑ 同时跳过开机引导（推荐）                                  │
│                                                              │
│ 系统更新 (OTA)                                                │
│   目标状态   [ 屏蔽更新  ▾ ]                                  │
│                                                              │
│ 执行   [ 应用修改 ]  [ 全部还原 ]  [ 退出 ]                   │
│ 设备区 [ 刷新设备 ] [ 仅生成备份文件 ] [ 锁定系统版本… ]       │
│        ████████████████████████████████ 100%                 │
└──────────────────────────────────────────────────────────────┘
```

---

## 能力

| 功能 | 说明 | 可取消 |
|---|---|---|
| **监督模式（仅开启）** | 让设备认为自己在组织管理下 | ⚠️ 关闭功能已禁用，见下 |
| **屏蔽系统更新** | 让检查更新拿不到任何可用资产 | ✅ 切到「恢复更新」 |
| **跳过开机引导** | 顺带关闭 Setup Assistant 各步骤 | ✅ |
| **一键全部还原** | 关闭监督 + 恢复更新，一次完成 | — |
| **写入前检查** | 检测「查找我的 iPhone」/ 锁屏状态，不合格时拦截 | — |
| **锁定指定版本** | 查出钉住某版本所需的延迟天数 + 有效期 | 卸载描述文件即恢复 |
| **强制更新到某版** | 生成 `enforcement.specific` 声明（需 MDM/DDM 下发） | — |

改动通过**伪造局部备份 + `restore --system`** 完成：不越狱、不刷机、不动系统分区，
副作用集中在这几个 plist 上，随时可还原。

---

## 快速开始

### 一行安装（推荐）

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/yebuwudong/iostui/master/install.sh | bash
```

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/yebuwudong/iostui/master/install.ps1 | iex
```

装完会得到一个**全局命令 `iostui`**，之后在任意目录直接敲：

```bash
iostui                # 打开界面
iostui list           # 列出设备
iostui ota --on -y    # 屏蔽系统更新
```

如果提示 `~/.local/bin 不在 PATH 中`，按脚本给出的提示加一行
`export PATH="$HOME/.local/bin:$PATH"` 即可（Windows 会自动写入用户 PATH）。

### 或者手动克隆（开发用）

```bash
git clone https://github.com/yebuwudong/iostui.git
cd iostui
./run.sh
```

Windows 把最后一行换成：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

第一次运行会自动建虚拟环境、装依赖（约 1 分钟），之后直接进界面。

### 需要的最小环境

* Python 3.10+
* `libimobiledevice`（提供 `idevicebackup2` / `ideviceinfo`）
* 数据线 + 已在手机上点「信任」
* **关闭「查找我的 iPhone」**（恢复系统文件时系统会拦）

安装脚本会**自动尝试安装** `libimobiledevice`（dnf / apt / pacman / zypper / brew）。
若自动安装失败，手动执行：

```bash
# Fedora
sudo dnf install -y libimobiledevice-utils usbmuxd

# Debian / Ubuntu
sudo apt install -y libimobiledevice-utils usbmuxd

# macOS
brew install libimobiledevice
```

Windows 随 iTunes 安装，或使用可选后端 `pymobiledevice3`。

### 可选后端 pymobiledevice3

只有 `iostui status` 和「不重启设置监督」需要它（依赖 MCInstall 服务）。
它包含 C 扩展（`pylzss`），部分系统缺编译环境会装不上 —— 不影响核心功能。

```bash
pip install pymobiledevice3
```

装不上时先补编译环境：

```bash
sudo dnf install gcc python3-devel      # Fedora
sudo apt install gcc python3-dev        # Debian / Ubuntu
xcode-select --install                  # macOS
```

其余功能（监督模式、OTA 屏蔽、锁定版本）只需 `libimobiledevice`。

---

## 一条命令用法

> 下面用 `iostui` 命令（一行安装后即全局可用）。
> 如果你是手动 clone 的，把 `iostui` 换成 `./run.sh`（Windows 用 `.\run.ps1`）。

```bash
# 打开图形界面
iostui

# 列出设备
iostui list

# 查看真实监督状态
iostui status

# 开启监督模式，组织名叫「My iPhone」
iostui supervise --on --org "My iPhone"

# 关闭监督模式 —— 已禁用（会导致设备恢复出厂设置）

# 屏蔽系统更新
iostui ota --on -y

# 恢复系统更新
iostui ota --off -y

# 全部还原（取消所有改动）
iostui reset -y

# 只生成备份、不写设备（无需连接设备，拿去另一台电脑执行）
iostui supervise --on --org "My iPhone" --export-only -o ./backup

# 锁定到指定 iOS 版本（自动算出所需延迟天数与有效期）
iostui pin --version 18.7.11

# 手工指定延迟天数（纯按天数）
iostui profile --days 90 -o delay.mobileconfig

# 生成「强制更新到指定版本」声明（需监督模式 + MDM/DDM）
iostui enforce --version 16.1 -o enforce.json
```

### 参数

| 参数 | 作用 |
|---|---|
| `-u, --udid` | 指定设备（连了多台时必填） |
| `-y, --yes` | 跳过确认提示 |
| `-o, --output` | 备份输出目录 / 输出文件 |
| `--export-only` | 只生成备份文件，不写设备 |
| `--force` | 「查找我的 iPhone」状态无法确认时仍继续（有风险） |
| `--no-preflight` | 跳过写入前的设备状态检查 |
| `--backend` | `auto`（默认）/ `cli`（libimobiledevice）/ `py`（pymobiledevice3） |

---

## 原理

`restore --system` 会按备份里的 `Manifest.mbdb` 清单把文件写到指定位置。
本工具因此只做三件事：

1. 按目标状态生成 plist（`CloudConfigurationDetails.plist` / `MobileAsset.plist`）
2. 用它们拼一个最小备份目录：

   ```
   Backup/
   ├── Manifest.mbdb        ← 文件清单（本工具生成）
   ├── Manifest.plist       ← 固定模板
   ├── Info.plist           ← 固定模板
   ├── Status.plist         ← SnapshotState=finished
   └── <sha1(domain-path)>  ← 实体文件
   ```

3. 调用 `restore --system --skip-apps` 写回设备，重启生效

不碰 App、不碰用户数据、不碰系统分区。

### 关于「屏蔽系统更新」

写入 `ManagedPreferencesDomain/mobile/com.apple.MobileAsset.plist`，把各更新资产
的服务器指向 tvOS 种子源，并把 `MobileAssetAssetAudience` 设为 tvOS 的 UUID。
iOS 查询不到适用于自己的资产，检查更新就会一直显示「已是最新」。

> 这是「让更新检查失效」，不是「删除已下载的更新包」。已下载的更新仍可能在
> 「设置」里提示安装一次，装完就干净了。

---

## 锁定到指定系统版本

设备的更新检查走 Apple 的 Pallas 接口（`gdmf.apple.com/v2/assets`），请求里可以带：

```json
{ "DelayRequested": true, "DelayPeriod": 43 }
```

意思是「我只要发布满 43 天以上的更新」，服务端只返回满足条件的版本。于是：

```
延迟天数越大 → 能看到的版本越旧 → 设备被钉在越老的版本上
```

实测（iPhone17,3，2026-10-27）：

| 延迟天数 | 实际返回 |
|---|---|
| 0-1 | iOS 27.2 |
| 2-29 | iOS 27.0.1 |
| 30-43 | iOS 27.0 |
| 44-49 | iOS 26.6.2 |
| 50-71 | iOS 26.6.1 |
| 72-90 | iOS 26.6 |

所以「停在 iOS 27.0」= 设置延迟 **43 天**。

### 用法

```bash
# 插着设备时，自动读型号并算出方案
iostui pin --version 27.0

# 也可以手动指定机型和硬件标识（无需连接设备）
iostui pin --version 27.0 --product-type iPhone17,3 --hw-model D47AP
```

输出形如：

```
  ✓ 目标版本   iOS 27.0 (24A6437)  发布于 2026-09-14
  ✓ 延迟天数   43 天
  ✓ 挡住的版本 iOS 27.0.1（2026-09-28 发布）
  ✓ 有效期至   2026-12-27（还有 61 天）

已生成描述文件：~/pin-ios27.0.mobileconfig
已发送安装请求，请在 iPhone 上确认
```

延迟天数与有效期来自社区预计算数据（`check-pallas` 项目），一次请求即可得到；
数据不可用时回退到本地扫描。

### 有效期

某版本可见的延迟区间是 `[age(下一版)+1, age(目标版)]`。随时间推移区间下界不断
上升；当下一版自己也满 90 天（Apple 允许的最大延迟）时，下界越过 90，再没有任何
延迟值能挡住它 —— 这就是终点：

```
有效期 = 下一版本发布日 + 90 天
```

到期后需卸载旧描述文件，重新生成并安装一份 delay 更大的。

### 两个前提

1. **必须已开启监督模式**，否则系统忽略该限制
2. iOS 26 起该限制废弃，iOS 27 起移除

### 想彻底不动版本？

| 目标 | 手段 | 命令 | 特点 |
|---|---|---|---|
| 停在某个版本 | 延迟天数钉版本 | `pin --version 18.7.11` | 精确，但会过期需重装 |
| 永远不升级 | 屏蔽更新源 | `ota --on` | 一劳永逸，但看不到任何版本 |

---

## 关于「查找我的 iPhone」检查

`restore --system` 在 FMI 开启时会被设备拒绝，所以工具写入前会检查。

iOS 没有给电脑侧提供可靠的 FMI 状态接口：`com.apple.fmip` 这个 lockdown 域能查，
但不总等于「是否会阻止恢复」。因此用**三态**判断，不把「查不到」当「已关闭」：

| 结果 | 含义 | 行为 |
|---|---|---|
| ✓ 已关闭 | 明确读到关闭 | 直接放行 |
| ✗ 已开启 | 明确读到开启 | **直接拦截**，不给绕过 |
| ? 无法确认 | 查不到 / 字段矛盾 | 弹窗要求你**手动确认**后才继续 |

```bash
iostui ota --on -y                  # 状态未知时会被拒绝，提示手动确认
iostui ota --on -y --force          # 你确认已关闭，强制继续
iostui ota --on -y --no-preflight   # 完全跳过检查（不建议）
```

---

## 写入后怎么确认生效

```bash
iostui status
```

输出设备的真实监督状态、已安装描述文件、以及延迟限制是否生效。
需要 `pymobiledevice3`（它提供 MCInstall 服务）；未安装时会提示替代命令。

监督模式有两种写入路径：

| 方式 | 原理 | 需要重启 | 说明 |
|---|---|---|---|
| **MCInstall**（默认） | Apple 官方 `SetCloudConfiguration`，写完立即读回校验 | 否 | 设备已有云配置时会返回 14002，此时自动回退 |
| 备份恢复（回退） | 改 plist 文件 + `restore --system` | 是 | 唯一能改写已存在云配置的路径 |

想强制用备份恢复方式，加 `--restore-method`。

`enforcedSoftwareUpdateDelay` 必须已受监督才生效，锁定版本前先用 `status` 确认
`IsSupervised = True`。

---

## ⚠️ 关闭监督已禁用

`supervise --off` 和「全部还原」里的关闭监督部分**已被移除**。

原因：关闭监督需要写入

```
CloudConfigurationUIComplete = True
IsSupervised = False
```

这是一个**非法组合**（配置已完成、但不受监督）。iOS 启动时校验失败，
会反复重启，连续失败后自动执行恢复出厂设置，**数据全部丢失**。

如果你的设备需要解除管理，用 Apple 官方方式：

* 设置 → 通用 → VPN与设备管理 → 移除配置
* 或抹机后不恢复该配置

---

## 注意

* **先关「查找我的 iPhone」**，否则恢复系统文件会失败。
* 监督模式会让「设置」顶部出现组织名称，这属于预期效果。
* 若你装了 MDM / 描述文件，请先在设置里移除，再关监督模式。
* 恢复后设备可能提示「iPhone 部分设置完成」——选 **「继续部分设置」**，
  切勿选抹掉，否则数据会丢失。
* 仅用于自有设备。改动系统配置存在风险，请自行评估。

---

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests -q     # 75 项单测，不需要真机
.venv/bin/python -m iostui.cli
```

代码结构：

```
iostui/
├── mbdb.py       Manifest.mbdb 读写 + 备份目录生成
├── payload.py    各功能的 plist 内容（监督 / OTA / 描述文件）
├── device.py     设备后端（pymobiledevice3 / libimobiledevice）+ 备份执行
├── preflight.py  写入前检查（查找我的 iPhone / 锁屏）三态判定
├── supervisor.py 监督身份证书生成
├── mcinstall.py  MCInstall 接口：读写云配置、判定监督状态
├── pallas.py     Apple 更新目录查询 + 锁定版本的天数/有效期算法
├── tui.py        Textual 界面
├── cli.py        命令行入口
└── resources/    固定模板 plist
```

## 许可

MIT
