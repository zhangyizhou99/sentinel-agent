"""Source resolver. | 来源解析器。

EN: Turns whatever the user provides — a local path OR a git URL — into a local
    directory the Discovery engine can scan. Git repos are shallow-cloned into a
    per-URL cache dir and updated on subsequent runs. This is what lets Sentinel
    work both on your own machine AND on other people's repos.
ZH: 把用户给的东西——本地路径 或 git URL——统一变成一个 Discovery 引擎能扫的本地
    目录。git 仓库会按 URL 浅克隆到缓存目录，后续运行再更新。这就是让 Sentinel
    既能扫自己机器、又能扫别人仓库的关键。

Security | 安全:
EN: Only http(s)/git/ssh git URLs are allowed, and `git` is invoked with an
    argument list (never a shell string) to prevent command injection.
ZH: 只允许 http(s)/git/ssh 的 git URL，且用参数数组调用 `git`（绝不用 shell
    字符串），防止命令注入。
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# EN: recognize common git URL shapes. | ZH: 识别常见的 git URL 形态。
_GIT_URL_RE = re.compile(
    r"^(https?://|git://|ssh://|git@)"      # scheme | 协议
    r".+",
)


@dataclass
class ResolvedSource:
    """EN: A local directory ready to scan, plus provenance. | ZH: 可直接扫描的本地目录及来源信息。"""

    path: Path                 # EN: local dir to scan | ZH: 要扫的本地目录
    kind: str                  # EN: "local" | "git" | ZH: 来源类型
    ref: str                   # EN: human-readable origin | ZH: 可读来源描述


def is_git_url(source: str) -> bool:
    """EN: True if `source` looks like a git URL. | ZH: `source` 像 git URL 则为真。"""
    return bool(_GIT_URL_RE.match(source.strip()))


def resolve_source(source: str, cache_dir: str = ".sentinel/repos") -> ResolvedSource:
    """EN: Resolve a local path or git URL to a local directory.
    ZH: 把本地路径或 git URL 解析成本地目录。"""
    source = source.strip()
    if is_git_url(source):
        return _resolve_git(source, cache_dir)

    # EN: local path — must exist. | ZH: 本地路径 —— 必须存在。
    path = Path(source).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"path not found | 路径不存在: {source}")
    return ResolvedSource(path=path.resolve(), kind="local", ref=str(path.resolve()))


# -- git | git 处理 --------------------------------------------------------

def _resolve_git(url: str, cache_dir: str) -> ResolvedSource:
    # EN: one stable cache dir per URL (hash keeps it filesystem-safe).
    # ZH: 每个 URL 一个稳定缓存目录（哈希保证文件系统安全）。
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    dest = Path(cache_dir) / digest

    if (dest / ".git").exists():
        # EN: already cloned — fetch latest & hard reset to origin HEAD.
        # ZH: 已克隆 —— 拉取最新并硬重置到 origin HEAD。
        _git(["-C", str(dest), "fetch", "--depth", "1", "origin"])
        _git(["-C", str(dest), "reset", "--hard", "origin/HEAD"])
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # EN: shallow clone — we only need the current tree, not full history.
        # ZH: 浅克隆 —— 只需当前代码树，不要完整历史。
        _git(["clone", "--depth", "1", url, str(dest)])

    return ResolvedSource(path=dest.resolve(), kind="git", ref=url)


def _git(args: list[str]) -> None:
    """EN: Run a git command safely (arg list, no shell). | ZH: 安全执行 git（参数数组，无 shell）。"""
    try:
        subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError as exc:  # EN: git not installed | ZH: 未装 git
        raise RuntimeError("git not found | 未找到 git，请先安装 git") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"git failed | git 失败: {exc.stderr.strip()}") from exc
