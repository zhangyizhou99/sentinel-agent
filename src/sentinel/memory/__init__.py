"""Sentinel memory system. | Sentinel 记忆系统。

EN: A layered memory: an embedding service (3 tiers), a persistent incremental
    vector store (retrieval memory), episodic memory (cross-run feedback),
    semantic memory (observability knowledge), all coordinated by a MemoryManager.
ZH: 分层记忆：嵌入服务（三档）、持久化增量向量库（检索记忆）、情景记忆（跨运行反馈）、
    语义记忆（可观测性知识），统一由 MemoryManager 协调。
"""
from sentinel.memory.embedding import (
    CloudEmbeddingProvider,
    EmbeddingProvider,
    LocalEmbeddingProvider,
    TfidfEmbeddingProvider,
    Vector,
    cosine,
    get_embedding_provider,
)
from sentinel.memory.episodic import EpisodicMemory
from sentinel.memory.manager import MemoryManager
from sentinel.memory.semantic import Pattern, SemanticMemory
from sentinel.memory.vector_store import (
    IndexStats,
    LocalVectorStore,
    QdrantVectorStore,
    VectorItem,
)

__all__ = [
    "EmbeddingProvider",
    "TfidfEmbeddingProvider",
    "LocalEmbeddingProvider",
    "CloudEmbeddingProvider",
    "get_embedding_provider",
    "Vector",
    "cosine",
    "LocalVectorStore",
    "QdrantVectorStore",
    "VectorItem",
    "IndexStats",
    "EpisodicMemory",
    "SemanticMemory",
    "Pattern",
    "MemoryManager",
]
