"""iostui - 免越狱 iOS 监督模式 / OTA 屏蔽 命令行工具。"""

__version__ = "1.0.0"


def build_id() -> str:
    """返回可辨识的构建标识（尽量带上 git 提交号）。

    安装方式不同、代码是否真的更新了，靠版本号本身看不出来 —— 这里把提交号
    也带上，方便确认「我装的到底是哪一版」。
    """
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    git_dir = root / ".git"
    if git_dir.exists():
        try:
            head = (git_dir / "HEAD").read_text().strip()
            if head.startswith("ref: "):
                ref = head[5:].strip()
                path = git_dir / ref
                if path.exists():
                    return path.read_text().strip()[:7]
                # packed-refs 兜底
                packed = git_dir / "packed-refs"
                if packed.exists():
                    for line in packed.read_text().splitlines():
                        if line.endswith(f" {ref}"):
                            return line.split()[0][:7]
            else:
                return head[:7]
        except Exception:
            pass
    return os.environ.get("IOSTUI_BUILD", "")


def version_string() -> str:
    bid = build_id()
    return f"iostui v{__version__}" + (f" ({bid})" if bid else "")
