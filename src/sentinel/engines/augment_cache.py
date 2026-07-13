"""Augmentation cache. | 增强缓存。

EN: Gate ② of "don't repeat work": caches the LLM augmentation result keyed by a
    signature of the candidate code units (each unit's content hash). If the set
    of units to augment is unchanged since last run, we reuse the cached result
    and make ZERO LLM calls. This is the Chapter-8 memory principle ("unchanged →
    reuse, don't recompute") applied to the expensive LLM step.
ZH: “别重复干活”的第②道闸门：按候选代码单元的签名（每个单元的内容哈希）缓存 LLM
    增强结果。若要增强的单元集合与上次一致，就复用缓存、**零次 LLM 调用**。这是第八
    章记忆思想（“没变就复用，别重算”）用在最贵的 LLM 步骤上。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional


def signature_of(items: list[str]) -> str:
    """EN: Stable signature for a set of "unit_id:content_hash" strings.
    ZH: 一组 "unit_id:内容哈希" 字符串的稳定签名。"""
    joined = "\n".join(sorted(items))
    return hashlib.sha256(joined.encode("utf-8", "ignore")).hexdigest()


class AugmentationCache:
    """EN: signature -> cached LLM-suggested metrics (as JSON dicts).
    ZH: 签名 -> 缓存的 LLM 建议指标（JSON 字典）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, list[dict]] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        self._load()

    def get(self, signature: str) -> Optional[list[dict]]:
        """EN: Cached metrics if this candidate set was seen before, else None.
        ZH: 该候选集合此前见过则返回缓存指标，否则 None。"""
        entry = self._data.get(signature)
        if entry is not None:
            self.hits += 1
            return entry
        self.misses += 1
        return None

    def put(self, signature: str, metrics: list[dict]) -> None:
        self._data[signature] = metrics
        self._dirty = True

    def save(self) -> None:
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
