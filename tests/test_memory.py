"""Tests for the memory subsystem. | 记忆子系统测试。

Run: PYTHONPATH=src pytest tests/ -q
"""
import sys
from pathlib import Path

# EN: make `src/` importable without packaging. | ZH: 无需打包即可导入 src/。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.memory.embedding import (  # noqa: E402
    TfidfEmbeddingProvider,
    cosine,
    get_embedding_provider,
)
from sentinel.memory.episodic import EpisodicMemory  # noqa: E402
from sentinel.memory.semantic import Pattern, SemanticMemory  # noqa: E402
from sentinel.memory.vector_store import LocalVectorStore  # noqa: E402


class _StubProvider:
    """EN: deterministic 3-dim dense provider for incremental tests.
    ZH: 确定性 3 维稠密 provider，用于增量测试。"""

    name = "stub"
    corpus_dependent = False

    def fit(self, corpus):
        pass

    def embed(self, texts):
        return [[float("order" in t), float("user" in t), float("cache" in t)]
                for t in texts]

    def embed_query(self, t):
        return self.embed([t])[0]


# -- embedding | 嵌入 -------------------------------------------------------

def test_cosine_sparse_and_dense():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
    assert cosine({0: 1.0}, {0: 1.0}) == 1.0


def test_tfidf_provider_ranks_related_higher():
    p = TfidfEmbeddingProvider()
    p.fit(["create order api", "unrelated helper text"])
    q = p.embed_query("order")
    v_order, v_helper = p.embed(["create order api", "unrelated helper text"])
    assert cosine(q, v_order) > cosine(q, v_helper)


def test_factory_defaults_to_tfidf():
    assert get_embedding_provider("air-gapped").name == "tfidf"
    assert get_embedding_provider("private-llm").name == "local"
    assert get_embedding_provider("external-llm").name == "cloud"


# -- vector store | 向量库 --------------------------------------------------

def test_vector_store_incremental_and_persist(tmp_path):
    path = tmp_path / "vindex.json"
    units = [
        ("f::create_order", "h1", "create order api", {}),
        ("f::get_user", "h2", "get user cache", {}),
    ]
    s = LocalVectorStore(path)
    st = s.upsert(units, _StubProvider())
    assert st.embedded == 2 and st.reused == 0
    s.save()

    # reload: same content -> all reused, nothing re-embedded
    s2 = LocalVectorStore(path)
    st2 = s2.upsert(units, _StubProvider())
    assert st2.embedded == 0 and st2.reused == 2

    # change one hash -> only that one re-embedded
    units[1] = ("f::get_user", "h2b", "get user redis cache", {})
    st3 = s2.upsert(units, _StubProvider())
    assert st3.embedded == 1 and st3.reused == 1


def test_vector_store_query(tmp_path):
    s = LocalVectorStore(tmp_path / "v.json")
    p = _StubProvider()
    s.upsert([("a", "1", "create order api", {}),
              ("b", "2", "get user cache", {})], p)
    top = s.query(p.embed_query("order"), top_k=1)
    assert top and top[0][0] == "a"


def test_vector_store_prunes_stale(tmp_path):
    s = LocalVectorStore(tmp_path / "v.json")
    p = _StubProvider()
    s.upsert([("a", "1", "order", {}), ("b", "2", "user", {})], p)
    st = s.upsert([("a", "1", "order", {})], p)  # b disappears
    assert st.removed == 1 and len(s) == 1


# -- episodic | 情景 --------------------------------------------------------

def test_episodic_latest_verdict_wins(tmp_path):
    m = EpisodicMemory(tmp_path / "ep.db")
    m.record_feedback("app.cold_start", "reject")
    m.record_feedback("app.cold_start", "approve")  # later overrides
    assert m.rejected_metric_ids() == set()
    m.record_feedback("db.query", "reject")
    assert m.rejected_metric_ids() == {"db.query"}
    m.close()


def test_episodic_stats(tmp_path):
    m = EpisodicMemory(tmp_path / "ep.db")
    m.record_run({"total": 5, "present": 2, "missing": 3})
    m.record_feedback("a", "approve")
    m.record_feedback("b", "reject")
    s = m.stats()
    assert s == {"runs": 1, "approved": 1, "rejected": 1}
    m.close()


# -- semantic | 语义 --------------------------------------------------------

def test_semantic_queries_by_signal():
    sm = SemanticMemory()
    q = sm.queries_by_signal()
    assert set(q).issuperset({"errors", "latency", "traffic"})
    assert "error" in q["errors"]


def test_semantic_extensible(tmp_path):
    f = tmp_path / "user.json"
    sm = SemanticMemory(user_file=f)
    sm.add(Pattern("custom.x", "errors", "graphql resolver error", "test"))
    # a fresh instance re-reads persisted user patterns
    sm2 = SemanticMemory(user_file=f)
    assert any(p.id == "custom.x" for p in sm2.all_patterns())
