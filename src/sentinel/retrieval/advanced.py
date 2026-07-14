"""Multi-query retrieval with rank fusion. | 多查询检索 + 排序融合。

EN: A single query under-recalls on large repos: "error" and "checkout" and
    "p99 latency" pull very different functions, and one blended query dilutes
    them all. This retriever instead fires ONE focused sub-query per golden
    signal (from semantic memory) — plus optional HyDE queries — and fuses the
    ranked lists with Reciprocal Rank Fusion (RRF). RRF is robust because it uses
    RANK, not raw scores, so it merges dense and lexical results cleanly.
ZH: 单条查询在大仓库上召回不足：“error”“checkout”“p99 时延”会命中截然不同的函数，
    一条混合查询把它们全稀释了。本检索器改为**每个黄金信号发一条聚焦子查询**（来自
    语义记忆）——外加可选的 HyDE 查询——再用互惠排名融合(RRF)合并各排名表。RRF 用
    “排名”而非原始分数，因此能干净地融合稠密与词法结果。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from sentinel.memory.semantic import SemanticMemory
from sentinel.retrieval.code_units import CodeUnit, extract_code_units


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def hyde_queries(llm, signals: Sequence[str], lang: str = "en") -> List[str]:
    """EN: HyDE — ask the LLM to write a short "ideal function worth monitoring"
        for each signal, then retrieve against those hypothetical documents. Only
        used when an LLM is available (non-air-gapped). Failures degrade to [].
    ZH: HyDE —— 让 LLM 为每个信号写一段“理想的、值得监控的函数”，再拿这些假想文档去
        检索。仅在有 LLM（非离线档）时启用。失败则降级为空。"""
    out: List[str] = []
    for sig in signals:
        prompt = (
            f"Write 2-3 lines of Python-ish pseudocode for a function whose "
            f"'{sig}' should be monitored (names, calls, keywords only)."
            if lang == "en" else
            f"写 2-3 行伪代码，描述一个其 '{sig}' 值得被监控的 Python 函数"
            f"（只要函数名、调用、关键词）。"
        )
        try:
            text = llm.complete(prompt)  # EN: duck-typed | ZH: 鸭子类型
            if text:
                out.append(str(text))
        except Exception:
            continue
    return out


class MultiQueryRetriever:
    """EN: MQE + RRF over a persistent vector store. | ZH: 在持久向量库上做 MQE + RRF。"""

    def __init__(self, store, provider, semantic: Optional[SemanticMemory] = None,
                 rrf_k: int = 60):
        self.store = store
        self.provider = provider
        self.semantic = semantic or SemanticMemory()
        self.rrf_k = rrf_k

    def _index(self, root: str | Path) -> Dict[str, CodeUnit]:
        units = extract_code_units(root)
        records = [
            (u.unit_id, _hash(u.text), u.text,
             {"file": u.file, "symbol": u.symbol, "line": u.line})
            for u in units
        ]
        if records:
            self.store.upsert(records, self.provider)
            if hasattr(self.store, "save"):
                self.store.save()
        return {u.unit_id: u for u in units}

    def rank(self, root: str | Path, top_k: int = 20,
             extra_queries: Optional[Sequence[str]] = None
             ) -> List[tuple[CodeUnit, float]]:
        """EN: Retrieve with per-signal sub-queries (+ optional HyDE/extra) and
            fuse with RRF. | ZH: 用每信号子查询(+可选 HyDE/额外)检索并 RRF 融合。"""
        by_id = self._index(root)
        if not by_id:
            return []

        queries = list(self.semantic.queries_by_signal().values())
        if extra_queries:
            queries += list(extra_queries)

        # EN: gather one ranked list per query. | ZH: 每条查询收一张排名表。
        per_pool = max(top_k * 3, 30)
        fused: Dict[str, float] = {}
        for q in queries:
            q_vec = self.provider.embed_query(q)
            ranked = self.store.query(q_vec, top_k=per_pool)
            for rank, (uid, _score) in enumerate(ranked):
                # EN: RRF contribution — earlier rank => bigger. | ZH: RRF 贡献——排名越前越大。
                fused[uid] = fused.get(uid, 0.0) + 1.0 / (self.rrf_k + rank + 1)

        merged = sorted(fused.items(), key=lambda x: x[1], reverse=True)
        return [(by_id[uid], score) for uid, score in merged[:top_k] if uid in by_id]
