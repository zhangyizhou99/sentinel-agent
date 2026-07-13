"""File-hash scan cache. | 文件哈希扫描缓存。

EN: Caches scan results keyed by (relative path + content hash). If a file's
    content is unchanged, its metrics are reused and the (expensive) AST parse
    is skipped. This is what makes re-scanning a large repo cheap. See DESIGN
    scalability notes.
ZH: 以（相对路径 + 内容哈希）为键缓存扫描结果。文件内容没变时直接复用其指标，
    跳过（昂贵的）AST 解析。这就是让大仓库重复扫描变便宜的关键。

EN: Format on disk is a single JSON file, so it is human-inspectable and easy to
    delete to force a full rescan.
ZH: 磁盘格式是单个 JSON 文件，可人工查看，删掉即可强制全量重扫。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from sentinel.model.metric import MetricDescriptor


class ScanCache:
    """EN: A tiny JSON-backed cache of per-file scan results.
    ZH: 一个基于 JSON、按文件缓存扫描结果的小缓存。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        self._dirty = False
        self.hits = 0          # EN: cache hits this run | ZH: 本次命中数
        self.misses = 0        # EN: cache misses this run | ZH: 本次未命中数
        self._load()

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8", "ignore")).hexdigest()

    def get(self, rel: str, content: str) -> Optional[list[MetricDescriptor]]:
        """EN: Return cached metrics if the file content is unchanged, else None.
        ZH: 文件内容未变则返回缓存指标，否则返回 None。"""
        entry = self._data.get(rel)
        if entry and entry.get("hash") == self._hash(content):
            self.hits += 1
            return [MetricDescriptor.model_validate(m) for m in entry["metrics"]]
        self.misses += 1
        return None

    def put(self, rel: str, content: str, metrics: list[MetricDescriptor]) -> None:
        """EN: Store fresh scan results for a file. | ZH: 存入某文件的最新扫描结果。"""
        self._data[rel] = {
            "hash": self._hash(content),
            "metrics": [m.model_dump(mode="json") for m in metrics],
        }
        self._dirty = True

    def save(self) -> None:
        """EN: Persist to disk only if something changed. | ZH: 有变更才落盘。"""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._dirty = False

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}
