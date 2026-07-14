"""Memory manager — the single façade. | 记忆管理器 —— 统一门面。

EN: Wires the memory subsystems together for one repository and exposes the few
    operations the pipeline actually needs:
      - retriever()          : a persistent, multi-query retriever
      - record_run()         : log a discovery run (episodic)
      - record_feedback()    : store a user verdict on a metric (episodic)
      - apply_feedback()     : suppress previously-rejected metrics from a catalog
      - consolidate()        : housekeeping (trim history)
    Chooses the embedding tier from the privacy mode and keeps all state under the
    per-repo cache dir. Nothing here is required by the core pipeline — memory is
    strictly opt-in and backwards-compatible.
ZH: 为单个仓库把各记忆子系统接线起来，只暴露流水线真正需要的几个操作：
      - retriever()          : 持久化、多查询检索器
      - record_run()         : 记录一次发现运行（情景）
      - record_feedback()    : 存用户对指标的裁决（情景）
      - apply_feedback()     : 从清单中抑制历史被拒的指标
      - consolidate()        : 收尾维护（修剪历史）
    按隐私档选嵌入档，所有状态放在按仓库分桶的缓存目录下。核心流水线并不依赖它 ——
    记忆严格是可选、向后兼容的。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from sentinel.memory.embedding import get_embedding_provider
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.semantic import SemanticMemory
from sentinel.memory.vector_store import LocalVectorStore
from sentinel.model.metric import MetricsCatalog
from sentinel.paths import (
    episodic_db_path,
    repo_cache_dir,
    vector_index_path,
)
from sentinel.retrieval.advanced import MultiQueryRetriever


class MemoryManager:
    """EN: Coordinates embedding + vector store + episodic + semantic memory.
    ZH: 协调 嵌入 + 向量库 + 情景 + 语义 记忆。"""

    def __init__(self, repo: str | Path, privacy: str = "air-gapped",
                 enable_vector: bool = True):
        self.repo = repo
        self.provider = get_embedding_provider(privacy)
        self.semantic = SemanticMemory(
            user_file=repo_cache_dir(repo) / "semantic-user.json"
        )
        self.episodic = EpisodicMemory(episodic_db_path(repo))
        self.store: Optional[LocalVectorStore] = (
            LocalVectorStore(vector_index_path(repo, self.provider.name))
            if enable_vector else None
        )

    # -- retrieval | 检索 --------------------------------------------------

    def retriever(self) -> MultiQueryRetriever:
        """EN: Persistent, multi-query, feedback-aware retriever.
        ZH: 持久化、多查询、感知反馈的检索器。"""
        store = self.store or LocalVectorStore(
            vector_index_path(self.repo, self.provider.name)
        )
        return MultiQueryRetriever(store, self.provider, self.semantic)

    # -- episodic writes | 情景写入 ----------------------------------------

    def record_run(self, catalog: MetricsCatalog) -> None:
        self.episodic.record_run(catalog.summary())

    def record_feedback(self, metric_id: str, verdict: str, reason: str = "") -> None:
        self.episodic.record_feedback(metric_id, verdict, reason)

    # -- feedback loop | 反馈闭环 ------------------------------------------

    def apply_feedback(self, catalog: MetricsCatalog) -> Tuple[MetricsCatalog, int]:
        """EN: Drop metrics the user has previously rejected. Returns the filtered
            catalog and the number suppressed (reported, never silent).
        ZH: 剔除用户此前拒绝的指标。返回过滤后的清单与被抑制数量（会报告，不静默）。"""
        rejected = self.episodic.rejected_metric_ids()
        if not rejected:
            return catalog, 0
        kept = [m for m in catalog.metrics if m.id not in rejected]
        suppressed = len(catalog.metrics) - len(kept)
        return MetricsCatalog(repo=catalog.repo, metrics=kept), suppressed

    # -- housekeeping | 收尾维护 -------------------------------------------

    def consolidate(self, keep_runs: int = 200) -> None:
        """EN: Trim run history so the DB stays small. | ZH: 修剪运行史，控库大小。"""
        conn = self.episodic._conn
        conn.execute(
            "DELETE FROM runs WHERE id NOT IN "
            "(SELECT id FROM runs ORDER BY ts DESC LIMIT ?)",
            (keep_runs,),
        )
        conn.commit()

    def close(self) -> None:
        if self.store is not None:
            self.store.save()
        self.episodic.close()
