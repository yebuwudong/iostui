# Windows 一键运行（PowerShell）
#
#   .\run.ps1
#   .\run.ps1 list
#   .\run.ps1 supervise --on --org "My iPhone" -y
#   .\run.ps1 ota --on -y
#   .\run.ps1 reset -y
#
# 若提示禁止执行脚本：
#   powershell -ExecutionPolicy Bypass -File .\run.ps1
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$venv = if ($env:IOSTUI_VENV) { $env:IOSTUI_VENV } else { Join-Path $here ".venv" }
$py = Join-Path $venv "Scripts\python.exe"

function Find-Python {
    foreach ($c in @("py -3.13", "py -3.12", "py -3.11", "py -3.10", "python", "python3")) {
        $exe, $arg = $c.Split(" ", 2)
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            $probe = if ($arg) { & $exe $arg -c "import sys;print(sys.version_info>=(3,10))" } else { & $exe -c "import sys;print(sys.version_info>=(3,10))" }
            if ($probe -match "True") { return $c }
        }
    }
    return $null
}

if (-not (Test-Path $py)) {
    $sysPy = Find-Python
    if (-not $sysPy) {
        Write-Host "需要 Python 3.10 或更高版本。请先从 https://www.python.org/downloads/ 安装（安装时勾选 Add to PATH）。" -ForegroundColor Red
        exit 1
    }
    Write-Host "==> 首次运行，创建虚拟环境（$sysPy）" -ForegroundColor Cyan
    $exe, $arg = $sysPy.Split(" ", 2)
    if ($arg) { & $exe $arg -m venv $venv } else { & $exe -m venv $venv }
    & $py -m pip install --upgrade pip -q
    Write-Host "==> 安装依赖（约 1 分钟）" -ForegroundColor Cyan
    & $py -m pip install -q -r (Join-Path $here "requirements.txt")
    & $py -c "import iostui" 2>$null
    if ($LASTEXITCODE -ne 0) { & $py -m pip install -q -e $here }
}

if (-not $Args -or $Args.Count -eq 0) {
    & $py -m iostui.cli tui
} else {
    & $py -m iostui.cli @Args
}
exit $LASTEXITCODE
