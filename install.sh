#!/usr/bin/env bash
# iostui 一行安装
#
#   curl -fsSL https://raw.githubusercontent.com/yebuwudong/iostui/master/install.sh | bash
#
# 装完会得到一个全局命令 `iostui`：
#   iostui              # 打开界面
#   iostui list         # 列出设备
#   iostui ota --on -y  # 屏蔽系统更新
#
# 环境变量：
#   IOSTUI_DIR     安装目录（默认 ~/.iostui）
#   IOSTUI_BIN     命令安装目录（默认 ~/.local/bin）
#   IOSTUI_PYTHON  指定 Python 解释器（默认自动选 3.13 优先）
#   IOSTUI_ARGS    传给 iostui 的参数
set -euo pipefail

REPO="https://github.com/yebuwudong/iostui.git"
TARBALL="https://github.com/yebuwudong/iostui/archive/refs/heads/master.tar.gz"
DIR="${IOSTUI_DIR:-$HOME/.iostui}"
BIN="${IOSTUI_BIN:-$HOME/.local/bin}"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

# 需要 root 的操作。
# 通过 `curl | bash` 运行时 stdin 是管道，sudo 无法从 stdin 读密码，
# 所以统一从 /dev/tty 读；无 tty 时退回 sudo -n（仅免密可用）。
run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif [ -r /dev/tty ]; then
    # 从终端读密码：curl|bash 场景下 stdin 是管道，但 /dev/tty 仍指向终端
    sudo "$@" </dev/tty
  else
    sudo -n "$@" 2>/dev/null
  fi
}
have_root() {
  [ "$(id -u)" -eq 0 ] || command -v sudo >/dev/null 2>&1
}

# ---- 1. 选 Python ----
pick_python() {
  if [ -n "${IOSTUI_PYTHON:-}" ]; then
    if $IOSTUI_PYTHON -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      echo "$IOSTUI_PYTHON"; return 0
    fi
    die "指定的 IOSTUI_PYTHON=$IOSTUI_PYTHON 不可用或版本低于 3.10"
  fi
  for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        echo "$c"; return 0
      fi
    fi
  done
  return 1
}

PY="$(pick_python)" || die "需要 Python 3.10 或更高版本：https://www.python.org/downloads/"
PYVER="$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Python: $($PY -V 2>&1)"

# Python 3.14 上 pylzss 没有预编译包（最高 cp313），pymobiledevice3 需要编译器。
# 若系统里有 / 能装上 3.13，就切换到 3.13。
if [ "$PYVER" = "3.14" ]; then
  if command -v python3.13 >/dev/null 2>&1; then
    PY="python3.13"
    say "检测到 3.14，改用已安装的 python3.13"
  elif have_root; then
    say "检测到 Python 3.14，尝试自动安装 Python 3.13…"
    if   command -v dnf     >/dev/null 2>&1; then run_root dnf install -y python3.13 || true
    elif command -v apt-get >/dev/null 2>&1; then run_root apt-get install -y python3.13 || true
    elif command -v pacman  >/dev/null 2>&1; then run_root pacman -S --noconfirm python313 || true
    elif command -v zypper  >/dev/null 2>&1; then run_root zypper --non-interactive install python313 || true
    elif command -v brew    >/dev/null 2>&1; then brew install python@3.13 || true
    fi
    hash -r
    if command -v python3.13 >/dev/null 2>&1; then
      PY="python3.13"
      say "Python 3.13 安装成功：$($PY -V 2>&1)"
    else
      warn "无法自动安装 Python 3.13，继续用 3.14"
      echo "    核心功能不受影响，只有 status 命令需要手动 pip install pymobiledevice3"
    fi
  fi
fi

# ---- 2. 取代码 ----
if [ -d "$DIR/.git" ]; then
  say "更新已有安装：$DIR"
  git -C "$DIR" fetch --depth=1 origin master \
    || die "更新失败（网络问题？）"
  git -C "$DIR" reset --hard origin/master || die "切换到最新代码失败"
  say "代码版本：$(git -C "$DIR" log -1 --format='%h %s')"
else
  say "下载到 $DIR"
  rm -rf "$DIR"
  if command -v git >/dev/null 2>&1; then
    git clone --depth=1 "$REPO" "$DIR" >/dev/null 2>&1 || die "git clone 失败，请检查网络"
  else
    mkdir -p "$DIR"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    if   command -v curl >/dev/null 2>&1; then curl -fsSL "$TARBALL" -o "$tmp/r.tar.gz"
    elif command -v wget >/dev/null 2>&1; then wget -qO "$tmp/r.tar.gz" "$TARBALL"
    else die "需要 git / curl / wget 其中之一"
    fi
    tar xzf "$tmp/r.tar.gz" -C "$tmp"
    cp -a "$tmp"/iostui-master/. "$DIR"/
  fi
fi
chmod +x "$DIR/run.sh" 2>/dev/null || true

# ---- 3. 虚拟环境 + 核心依赖 ----
VENV="$DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "创建虚拟环境"
  "$PY" -m venv "$VENV" || die "创建虚拟环境失败"
fi
VPY="$VENV/bin/python"
say "安装核心依赖"
"$VPY" -m pip install --upgrade pip -q
"$VPY" -m pip install -q -r "$DIR/requirements.txt" || die "依赖安装失败"
"$VPY" -m pip install -q -e "$DIR" || die "安装 iostui 本体失败"

# ---- 4. 可选后端 pymobiledevice3（提供 status）----
if "$VPY" -c "import pymobiledevice3" >/dev/null 2>&1; then
  say "pymobiledevice3 已就绪（status 可用）"
else
  say "安装可选后端 pymobiledevice3…"
  if "$VPY" -m pip install -q "pymobiledevice3>=4.0" 2>/dev/null; then
    say "pymobiledevice3 安装成功（status 可用）"
  else
    warn "pymobiledevice3 装不上，跳过（核心功能不受影响）"
    echo "    如需 status 命令，先补编译环境再重跑："
    echo "      Fedora:        sudo dnf install gcc python3-devel"
    echo "      Debian/Ubuntu: sudo apt install gcc python3-dev"
    echo "      macOS:         xcode-select --install"
  fi
fi

# ---- 5. 设备后端 libimobiledevice ----
install_libimobiledevice() {
  have_root || return 1
  if   command -v dnf     >/dev/null 2>&1; then
    say "安装 libimobiledevice（dnf）"; run_root dnf install -y libimobiledevice-utils usbmuxd || return 1
  elif command -v apt-get >/dev/null 2>&1; then
    say "安装 libimobiledevice（apt）"
    run_root apt-get update -qq || true
    run_root apt-get install -y libimobiledevice-utils usbmuxd || return 1
  elif command -v pacman  >/dev/null 2>&1; then
    say "安装 libimobiledevice（pacman）"; run_root pacman -S --noconfirm libimobiledevice usbmuxd || return 1
  elif command -v zypper  >/dev/null 2>&1; then
    say "安装 libimobiledevice（zypper）"; run_root zypper --non-interactive install libimobiledevice-utils usbmuxd || return 1
  elif command -v brew    >/dev/null 2>&1; then
    say "安装 libimobiledevice（brew）"; brew install libimobiledevice usbmuxd || return 1
  else
    return 1
  fi
  hash -r
  command -v idevicebackup2 >/dev/null 2>&1
}

if command -v idevicebackup2 >/dev/null 2>&1; then
  say "已找到 libimobiledevice（idevicebackup2）"
elif "$VPY" -c "import pymobiledevice3" >/dev/null 2>&1; then
  say "设备后端：pymobiledevice3"
else
  say "未找到设备后端，尝试自动安装 libimobiledevice…"
  if install_libimobiledevice; then
    say "libimobiledevice 安装完成"
  else
    warn "自动安装未成功，请手动安装："
    echo "      Fedora:        sudo dnf install libimobiledevice-utils usbmuxd"
    echo "      Debian/Ubuntu: sudo apt install libimobiledevice-utils usbmuxd"
    echo "      Arch:          sudo pacman -S libimobiledevice usbmuxd"
    echo "      macOS:         brew install libimobiledevice"
    echo "      Windows:       安装 iTunes，或 pip install pymobiledevice3"
  fi
fi

# ---- 6. usbmuxd 服务（Linux 上必须有）----
if command -v idevicebackup2 >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
  if ! systemctl is-active --quiet usbmuxd 2>/dev/null; then
    if have_root; then
      run_root systemctl enable --now usbmuxd >/dev/null 2>&1 || true
    fi
  fi
fi

# ---- 7. 安装全局命令 ----
mkdir -p "$BIN"
TARGET="$BIN/iostui"
printf '#!/usr/bin/env bash\nexec "%s" -m iostui.cli "$@"\n' "$VPY" > "$TARGET"
chmod +x "$TARGET"
say "已安装命令：$TARGET"

NEED_PATH=0
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) NEED_PATH=1 ;;
esac

"$TARGET" --version >/dev/null 2>&1 || die "自检失败，请运行：$TARGET --version"
say "安装完成：$("$TARGET" --version)"

if [ "$NEED_PATH" = "1" ]; then
  echo
  warn "$BIN 不在 PATH 中，把它加进 ~/.bashrc 或 ~/.zshrc："
  echo
  echo "      export PATH=\"$BIN:\$PATH\""
  echo
else
  echo
  echo "  在任何目录都能用："
  echo "      iostui               打开界面"
  echo "      iostui list          列出设备"
  echo "      iostui ota --on -y   屏蔽系统更新"
  echo
fi

# ---- 8. 需要时启动 ----
if [ -n "${IOSTUI_ARGS:-}" ]; then
  # shellcheck disable=SC2086
  exec "$TARGET" $IOSTUI_ARGS
elif [ -t 0 ] && [ -t 1 ]; then
  exec "$TARGET"
fi
