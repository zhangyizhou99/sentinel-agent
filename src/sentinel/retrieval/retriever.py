"""Observability retriever. | 可观测性检索器。

EN: Retrieval-Augmented Discovery, Chapter-8 style. It ranks every function by
    how relevant it is to "things worth monitoring", so that at scale the LLM only
    sees the top-K high-value units instead of the whole (5000-file) repo. This is
    the fix for "LLM can't scan a huge project": retrieve, then augment top-K.
ZH: 第八章式的“检索增强发现”。它按“与值得监控的东西有多相关”给每个函数排序，
    这样大仓库下 LLM 只看 top-K 个高价值单元，而不是整个（5000 文件）仓库。这就是
    “LLM 扫不动大工程”的解法：先检索，再只增强 top-K。
"""
from __future__ import annotations

from pathlib import Path

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


class ObservabilityRetriever:
    """EN: Rank code units by observability relevance. | ZH: 按可观测性相关度给代码单元排序。"""

    def __init__(self, query: str = OBSERVABILITY_QUERY):
        self.query = query

    def rank(self, root: str | Path, top_k: int = 20) -> list[tuple[CodeUnit, float]]:
        """EN: Return the top_k (CodeUnit, score) most worth monitoring.
        ZH: 返回最值得监控的前 top_k 个 (CodeUnit, 分数)。"""
        units = extract_code_units(root)
        if not units:
            return []
        # EN: fit the TF-IDF index on the whole corpus of functions.
        # ZH: 用全部函数语料拟合 TF-IDF 索引。
        index = TfidfIndex().fit([(u.unit_id, u.text) for u in units])
        by_id = {u.unit_id: u for u in units}
        # EN: retrieve the units most similar to the observability query.
        # ZH: 检索与可观测性查询最相似的单元。
        ranked = index.query(self.query, top_k=top_k)
        return [(by_id[uid], score) for uid, score in ranked if score > 0]
