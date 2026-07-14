"""Persistent, incremental vector store. | 持久化、增量向量库。

EN: The retrieval memory. It survives across runs and updates incrementally:
    each code unit's vector is cached under its content hash, so on re-index we
    only (re-)embed the units whose content actually changed. For a large repo
    that turns "embed 100k functions every run" into "embed only the few that
    changed" — the difference between a demo and something that scales.

    Backend is pluggable:
      - LocalVectorStore  (default): a JSON-on-disk store, zero extra deps,
        works offline; cosine search in pure Python.
      - QdrantVectorStore (optional): delegates to a Qdrant server for very
        large corpora; imported lazily so the default stays dependency-free.
ZH: 检索记忆。跨运行存活、增量更新：每个代码单元的向量按其内容哈希缓存，重建索引时
    只对**内容真正变化**的单元重新嵌入。大仓库下这把“每次嵌入 10 万函数”变成“只嵌
    变化的那几个”——这正是 demo 与可扩展系统的区别。

    后端可插拔：
      - LocalVectorStore （默认）：磁盘 JSON 存储，零额外依赖，可离线；纯 Python 余弦检索。
      - QdrantVectorStore（可选）：把超大语料托付给 Qdrant 服务；懒加载，默认保持零依赖。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from sentinel.memory.embedding import EmbeddingProvider, Vector, cosine


@dataclass
class VectorItem:
    """EN: One indexed unit. | ZH: 一个被索引的单元。"""

    unit_id: str
    content_hash: str
    vector: Vector
    meta: dict = field(default_factory=dict)


@dataclass
class IndexStats:
    """EN: What happened during an upsert. | ZH: 一次 upsert 发生了什么。"""

    total: int = 0
    embedded: int = 0     # EN: units (re-)embedded this run | ZH: 本次(重)嵌入的单元数
    reused: int = 0       # EN: units served from cache | ZH: 命中缓存复用的单元数
    removed: int = 0      # EN: stale units pruned | ZH: 清除的失效单元数


class LocalVectorStore:
    """EN: JSON-backed, content-addressed vector index with cosine search.
    ZH: 基于 JSON、内容寻址的向量索引，余弦检索。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._items: Dict[str, VectorItem] = {}
        self._dirty = False
        self._load()

    # -- build / update | 构建 / 更新 --------------------------------------

    def upsert(
        self,
        units: Sequence[Tuple[str, str, str, dict]],
        provider: EmbeddingProvider,
        prune: bool = True,
    ) -> IndexStats:
        """EN: Index `units` = [(unit_id, content_hash, text, meta), ...].
            Only changed/new units are embedded. If the provider is
            corpus-dependent (TF-IDF), the whole corpus is re-embedded because
            its weights depend on the global document set.
        ZH: 索引 `units` = [(unit_id, 内容哈希, 文本, meta), ...]。只嵌入变化/新增的
            单元。若 provider 与语料相关（TF-IDF），因其权重依赖全局文档集，故整库重嵌。"""
        stats = IndexStats(total=len(units))
        ids_now = {u[0] for u in units}

        if getattr(provider, "corpus_dependent", False):
            # EN: refit on the FULL corpus, then embed everything. | ZH: 全语料重拟合后整库嵌入。
            provider.fit([u[2] for u in units])
            vectors = provider.embed([u[2] for u in units])
            self._items = {
                uid: VectorItem(uid, chash, vec, meta)
                for (uid, chash, _text, meta), vec in zip(units, vectors)
            }
            stats.embedded = len(units)
            self._dirty = True
        else:
            # EN: incremental — embed only changed/new units. | ZH: 增量——只嵌变化/新增。
            todo_idx: List[int] = []
            for i, (uid, chash, _text, _meta) in enumerate(units):
                cached = self._items.get(uid)
                if cached is not None and cached.content_hash == chash:
                    stats.reused += 1
                else:
                    todo_idx.append(i)
            if todo_idx:
                new_vecs = provider.embed([units[i][2] for i in todo_idx])
                for i, vec in zip(todo_idx, new_vecs):
                    uid, chash, _text, meta = units[i]
                    self._items[uid] = VectorItem(uid, chash, vec, meta)
                stats.embedded = len(todo_idx)
                self._dirty = True

        if prune:
            stale = [uid for uid in self._items if uid not in ids_now]
            for uid in stale:
                del self._items[uid]
            stats.removed = len(stale)
            if stale:
                self._dirty = True
        return stats

    # -- query | 查询 ------------------------------------------------------

    def query(self, query_vector: Vector, top_k: int = 20) -> List[Tuple[str, float]]:
        """EN: Top-k (unit_id, score) by cosine similarity. | ZH: 按余弦相似度取 top-k。"""
        scored = [
            (item.unit_id, cosine(query_vector, item.vector))
            for item in self._items.values()
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def meta_of(self, unit_id: str) -> dict:
        item = self._items.get(unit_id)
        return item.meta if item else {}

    def __len__(self) -> int:
        return len(self._items)

    # -- persistence | 持久化 ----------------------------------------------

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            uid: {
                "hash": it.content_hash,
                # EN: sparse dict keys -> str for JSON; dense stays a list.
                # ZH: 稀疏 dict 键转 str 以便 JSON；稠密保持 list。
                "vec": ({str(k): v for k, v in it.vector.items()}
                        if isinstance(it.vector, dict) else it.vector),
                "sparse": isinstance(it.vector, dict),
                "meta": it.meta,
            }
            for uid, it in self._items.items()
        }
        self.path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        self._dirty = False

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for uid, entry in raw.items():
            vec: Vector
            if entry.get("sparse"):
                vec = {int(k): v for k, v in entry["vec"].items()}
            else:
                vec = list(entry["vec"])
            self._items[uid] = VectorItem(
                uid, entry.get("hash", ""), vec, entry.get("meta", {})
            )


class QdrantVectorStore:
    """EN: Optional large-scale backend delegating to a Qdrant server. Imported
        lazily; use only when a repo is too big for the on-disk store.
    ZH: 可选的大规模后端，托付给 Qdrant 服务。懒加载；仅当仓库大到本地存储扛不住时用。"""

    def __init__(self, collection: str, url: Optional[str] = None,
                 api_key: Optional[str] = None, dim: int = 384):
        self.collection = collection
        self._dim = dim
        try:
            from qdrant_client import QdrantClient  # type: ignore
            from qdrant_client.models import Distance, VectorParams  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "QdrantVectorStore needs `pip install qdrant-client` "
                "| Qdrant 后端需安装 qdrant-client"
            ) from e
        import os
        self._client = QdrantClient(
            url=url or os.getenv("QDRANT_URL"),
            api_key=api_key or os.getenv("QDRANT_API_KEY"),
        )
        existing = {c.name for c in self._client.get_collections().collections}
        if collection not in existing:
            self._client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    def upsert(self, units, provider, prune: bool = True) -> IndexStats:  # pragma: no cover
        from qdrant_client.models import PointStruct  # type: ignore
        vectors = provider.embed([u[2] for u in units])
        points = [
            PointStruct(id=i, vector=list(vec),
                        payload={"unit_id": u[0], "hash": u[1], **u[3]})
            for i, (u, vec) in enumerate(zip(units, vectors))
        ]
        self._client.upsert(collection_name=self.collection, points=points)
        return IndexStats(total=len(units), embedded=len(units))

    def query(self, query_vector, top_k: int = 20):  # pragma: no cover
        hits = self._client.search(
            collection_name=self.collection,
            query_vector=list(query_vector), limit=top_k,
        )
        return [(h.payload.get("unit_id", str(h.id)), float(h.score)) for h in hits]
