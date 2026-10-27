# iostui 一行安装（Windows PowerShell）
#
#   irm https://raw.githubusercontent.com/yebuwudong/iostui/master/install.ps1 | iex
#
# 装完得到一个全局命令 iostui：
#   iostui              # 打开界面
#   iostui list         # 列出设备
#   iostui ota --on -y  # 屏蔽系统更新
#
# 环境变量（可选）：
#   $env:IOSTUI_DIR   安装目录，默认 $HOME\.iostui
#   $env:IOSTUI_ARGS  传给 iostui 的参数
$ErrorActionPreference = "Stop"

$repo   = "https://github.com/yebuwudong/iostui.git"
$zipUrl = "https://github.com/yebuwudong/iostui/archive/refs/heads/master.zip"
$dir    = if ($env:IOSTUI_DIR) { $env:IOSTUI_DIR } else { Join-Path $HOME ".iostui" }

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "错误: $m" -ForegroundColor Red; exit 1 }

# ---- 1. 找 Python ----
$pyCmd = $null
foreach ($cand in @(@("py","-3.13"), @("py","-3.12"), @("py","-3.11"),
                    @("py","-3.10"), @("python",$null), @("python3",$null))) {
    $exe = $cand[0]; $arg = $cand[1]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $probe = if ($arg) { & $exe $arg -c "import sys;print(sys.version_info>=(3,10))" 2>$null }
                 else     { & $exe    -c "import sys;print(sys.version_info>=(3,10))" 2>$null }
        if ("$probe" -match "True") { $pyCmd = $cand; break }
    }
}
if (-not $pyCmd) {
    Die "需要 Python 3.10+。请从 https://www.python.org/downloads/ 安装（勾选 Add to PATH）"
}
$pyExe = $pyCmd[0]; $pyArg = $pyCmd[1]

# 检查 Python 版本：3.14 上没有 pylzss 的预编译包，可选后端需要 C 编译器。
# 若同时装了 3.13，优先用它。
$verRaw = if ($pyArg) { & $pyExe $pyArg -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null }
           else        { & $pyExe      -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null }
Say ("Python: " + $pyExe + " " + $pyArg + "  (" + $verRaw + ")")

if ("$verRaw" -eq "3.14") {
    $has313 = $false
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) { $has313 = $true }
    }
    if ($has313) {
        $pyCmd = @("py","-3.13"); $pyExe = "py"; $pyArg = "-3.13"
        Warn "检测到 Python 3.14，改用 3.13（pymobiledevice3 在 3.14 上需要编译）"
        Say "Python: py -3.13"
    } else {
        Say "当前是 Python 3.14，尝试自动安装 Python 3.13（pymobiledevice3 需要）…"
        $ok313 = $false
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            # winget 静默安装官方 Python 3.13
            & winget install --id Python.Python.3.13 -e --silent `
                --accept-package-agreements --accept-source-agreements 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ok313 = $true }
        }
        if (-not $ok313) {
            # 回退：直接下载官方安装包静默安装
            $exe = Join-Path $env:TEMP "python-3.13-installer.exe"
            try {
                $rel = "https://www.python.org/ftp/python/3.13.9/python-3.13.9-amd64.exe"
                Invoke-WebRequest -Uri $rel -OutFile $exe -UseBasicParsing
                Start-Process -FilePath $exe -Wait -ArgumentList `
                    "/quiet","InstallAllUsers=0","PrependPath=1","Include_pip=1"
                $ok313 = $true
            } catch {
                Warn "自动安装 Python 3.13 失败：$_"
            }
        }
        if ($ok313) {
            & py -3.13 -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $pyExe = "py"; $pyArg = "-3.13"
                Say "Python 3.13 安装成功，改用 py -3.13"
            } else {
                Warn "Python 3.13 已安装但当前会话未生效，请重开终端后重跑本脚本"
            }
        } else {
            Warn "无法自动安装 Python 3.13。核心功能不受影响，只是 status 命令不可用。"
            Write-Host "    手动安装：https://www.python.org/downloads/release/python-3139/" -ForegroundColor DarkGray
        }
    }
}

# ---- 2. 取代码 ----
if (Test-Path (Join-Path $dir ".git")) {
    Say "更新已有安装：$dir"
    git -C $dir fetch --depth=1 origin master | Out-Null
    git -C $dir reset --hard origin/master | Out-Null
} else {
    Say "下载到 $dir"
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    if (Get-Command git -ErrorAction SilentlyContinue) {
        git clone --depth=1 $repo $dir | Out-Null
        if ($LASTEXITCODE -ne 0) { Die "git clone 失败，请检查网络" }
    } else {
        $tmp = Join-Path $env:TEMP ("iostui-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Path $tmp | Out-Null
        $zip = Join-Path $tmp "repo.zip"
        try { Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing }
        catch { Die "下载失败，请检查网络" }
        Expand-Archive -Path $zip -DestinationPath $tmp
        Move-Item (Join-Path $tmp "iostui-master") $dir
        Remove-Item -Recurse -Force $tmp
    }
}

# ---- 3. 虚拟环境 + 依赖 ----
$venv = Join-Path $dir ".venv"
$vpy  = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Say "创建虚拟环境"
    if ($pyArg) { & $pyExe $pyArg -m venv $venv } else { & $pyExe -m venv $venv }
}
Say "安装依赖（首次约 1 分钟）"
& $vpy -m pip install --upgrade pip -q
& $vpy -m pip install -q -r (Join-Path $dir "requirements.txt")
& $vpy -m pip install -q -e $dir

# 可选：pymobiledevice3（提供 status 命令）。装不上不影响核心功能。
& $vpy -c "import pymobiledevice3" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say "安装可选后端 pymobiledevice3…"
    & $vpy -m pip install -q "pymobiledevice3>=4.0" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Warn "pymobiledevice3 装不上，跳过（不影响核心功能）"
        Write-Host "    需要 status 命令时，请先安装 Visual C++ 生成工具" -ForegroundColor DarkGray
    }
}

# ---- 4. 全局命令 ----
$binDir = Join-Path $env:LOCALAPPDATA "iostui\bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$cmdPath = Join-Path $binDir "iostui.cmd"
"@echo off`r`n`"$vpy`" -m iostui.cli %*" | Set-Content -Path $cmdPath -Encoding ASCII
Say "已安装命令：$cmdPath"

# ---- 5. PATH ----
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$binDir;$userPath", "User")
    $env:Path = "$binDir;$env:Path"
    Say "已把 $binDir 加入用户 PATH（新开终端生效）"
}

# ---- 6. 自检 ----
& $cmdPath --version
if ($LASTEXITCODE -ne 0) { Die "安装后自检失败，请运行：$cmdPath --version" }
Say "安装完成"

# ---- 7. 启动 ----
if ($env:IOSTUI_ARGS) {
    & $cmdPath ($env:IOSTUI_ARGS -split " ")
} elseif ([Environment]::UserInteractive) {
    & $cmdPath
}
