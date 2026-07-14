"""Observability retriever. | 可观测性检索器。

EN: Retrieval-Augmented Discovery. It ranks every function by how relevant it is
    to "things worth monitoring", so that at scale the LLM only sees the top-K
    high-value units instead of the whole (5000-file) repo. This is the fix for
    "LLM can't scan a huge project": retrieve, then augment top-K.
ZH: “检索增强发现”。它按“与值得监控的东西有多相关”给每个函数排序，这样大仓库下
    LLM 只看 top-K 个高价值单元，而不是整个（5000 文件）仓库。这就是“LLM 扫不动
    大工程”的解法：先检索，再只增强 top-K。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from sentinel.retrieval.code_units import CodeUnit, extract_code_units
from sentinel.retrieval.tfidf import TfidfIndex

# EN: the vocabulary of "worth monitoring" — RED/USE signals + business criticality.
# ZH: “值得监控”的词汇表 —— RED/USE 信号 + 业务关键性。
OBSERVABILITY_QUERY = (
    "api request route handler endpoint response status error exception latency "
    "database query sql transaction commit http client request external call timeout retry "
    "cache redis get set queue kafka publish consume message "
    "order payment checkout charge transaction refund login auth token session "
    "upload download process job worker task schedule background"
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


class ObservabilityRetriever:
    """EN: Rank code units by observability relevance. | ZH: 按可观测性相关度给代码单元排序。

    EN: Two backends: an in-memory TF-IDF pass (default, zero-setup) or a
        persistent incremental vector store + embedding provider (for large repos
        and cross-run reuse). Pass `provider` + `store` to enable the latter.
    ZH: 两种后端：内存 TF-IDF（默认、零配置），或持久化增量向量库 + 嵌入 provider
        （面向大仓库与跨运行复用）。传入 `provider` + `store` 即启用后者。"""

    def __init__(self, query: str = OBSERVABILITY_QUERY,
                 provider: Optional[object] = None, store: Optional[object] = None):
        self.query = query
        self.provider = provider
        self.store = store

    def rank(self, root: str | Path, top_k: int = 20) -> list[tuple[CodeUnit, float]]:
        """EN: Return the top_k (CodeUnit, score) most worth monitoring.
        ZH: 返回最值得监控的前 top_k 个 (CodeUnit, 分数)。"""
        units = extract_code_units(root)
        if not units:
            return []
        by_id = {u.unit_id: u for u in units}

        # EN: persistent path — incremental index + embedding query.
        # ZH: 持久化路径 —— 增量索引 + 嵌入查询。
        if self.provider is not None and self.store is not None:
            records = [
                (u.unit_id, _hash(u.text), u.text,
                 {"file": u.file, "symbol": u.symbol, "line": u.line})
                for u in units
            ]
            self.store.upsert(records, self.provider)
            if hasattr(self.store, "save"):
                self.store.save()
            q_vec = self.provider.embed_query(self.query)
            ranked = self.store.query(q_vec, top_k=top_k)
            return [(by_id[uid], score) for uid, score in ranked
                    if uid in by_id and score > 0]

        # EN: default path — one-shot in-memory TF-IDF. | ZH: 默认路径 —— 一次性内存 TF-IDF。
        index = TfidfIndex().fit([(u.unit_id, u.text) for u in units])
        ranked = index.query(self.query, top_k=top_k)
        return [(by_id[uid], score) for uid, score in ranked if score > 0]

