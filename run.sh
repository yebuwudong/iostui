#!/usr/bin/env bash
# 在仓库目录里直接运行（开发用 / 未走一键安装时）
#
#   ./run.sh                     # 打开图形界面
#   ./run.sh list                # 列出设备
#   ./run.sh supervise --on --org "My iPhone" -y
#   ./run.sh ota --on -y
#   ./run.sh reset -y
#
# 想在任何目录使用 `iostui` 命令，请改用一键安装：
#   curl -fsSL https://raw.githubusercontent.com/yebuwudong/iostui/master/install.sh | bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="${IOSTUI_VENV:-$HERE/.venv}"
PY="$VENV/bin/python"

pick_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        echo "$c"; return 0
      fi
    fi
  done
  return 1
}

if [ ! -x "$PY" ]; then
  SYS_PY="$(pick_python)" || {
    echo "需要 Python 3.10 或更高版本，请先安装。" >&2
    exit 1
  }
  echo "==> 首次运行，创建虚拟环境（$SYS_PY）"
  "$SYS_PY" -m venv "$VENV"
  "$PY" -m pip install --upgrade pip -q
  echo "==> 安装依赖（约 1 分钟）"
  "$PY" -m pip install -q -r "$HERE/requirements.txt"
fi

# 确保包已安装（否则 resources 可能取不到，且缺 console script）
"$PY" -c "import iostui, iostui.resources" 2>/dev/null || {
  echo "==> 安装 iostui 本体"
  "$PY" -m pip install -q -e "$HERE"
}

if [ $# -eq 0 ]; then
  exec "$PY" -m iostui.cli
else
  exec "$PY" -m iostui.cli "$@"
fi
