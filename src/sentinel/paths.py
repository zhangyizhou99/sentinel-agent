"""Cache locations. | 缓存位置。

EN: Centralizes where on-disk caches live. Caches are keyed by the target repo's
    ABSOLUTE path (hashed) and stored under a per-user cache dir — NOT relative to
    the current working directory. This fixes two problems: (1) scanning repo A
    then repo B from the same shell no longer thrashes one shared cache, and (2)
    we don't pollute the target repo with a `.sentinel/` folder.
ZH: 集中管理磁盘缓存的位置。缓存按目标仓库的**绝对路径**（哈希）分桶，存到用户级
    缓存目录——**不再相对当前工作目录**。这修复两个问题：(1) 在同一个终端里先扫仓库
    A 再扫仓库 B 不再共用同一份缓存互相踩踏；(2) 不往目标仓库塞 `.sentinel/` 目录。

Override the base dir with SENTINEL_CACHE_DIR. | 用 SENTINEL_CACHE_DIR 覆盖根目录。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def _base_dir() -> Path:
    # EN: default ~/.cache/sentinel, override via env. | ZH: 默认 ~/.cache/sentinel，可用环境变量覆盖。
    env = os.getenv("SENTINEL_CACHE_DIR")
    return Path(env) if env else Path.home() / ".cache" / "sentinel"


def repo_cache_dir(repo: str | Path) -> Path:
    """EN: A stable, per-repo cache directory (keyed by absolute path hash).
    ZH: 一个稳定的、按仓库分桶的缓存目录（用绝对路径哈希做键）。"""
    abs_path = str(Path(repo).resolve())
    key = hashlib.sha256(abs_path.encode("utf-8", "ignore")).hexdigest()[:16]
    return _base_dir() / key


def scan_cache_path(repo: str | Path) -> Path:
    return repo_cache_dir(repo) / "scan-cache.json"


def augment_cache_path(repo: str | Path) -> Path:
    return repo_cache_dir(repo) / "augment-cache.json"


def vector_index_path(repo: str | Path, provider: str) -> Path:
    """EN: Per-provider vector index (dims differ per embedding tier).
    ZH: 按 provider 分文件的向量索引（不同嵌入档维度不同）。"""
    return repo_cache_dir(repo) / f"vindex-{provider}.json"


def episodic_db_path(repo: str | Path) -> Path:
    """EN: SQLite file holding cross-run history + user feedback for this repo.
    ZH: 存该仓库跨运行历史 + 用户反馈的 SQLite 文件。"""
    return repo_cache_dir(repo) / "episodic.db"
