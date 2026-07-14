"""Embedding providers. | 嵌入服务。

EN: One interface, three interchangeable tiers aligned with Sentinel's privacy
    modes, trading off deployment footprint against semantic recall:
      air-gapped   -> TfidfEmbedding   (0 MB, offline, literal match)
      private-llm  -> LocalEmbedding   (local Transformer, semantic, no network)
      external-llm -> CloudEmbedding   (cloud embedding API, best recall)
    Vectors are either sparse (TF-IDF: dict[int,float]) or dense (list[float]);
    `cosine` handles both so the vector store stays representation-agnostic.
ZH: 一个接口、三档可互换，与 Sentinel 的隐私档对齐，在部署体积与语义召回间权衡：
      air-gapped   -> TfidfEmbedding  （0MB、离线、字面匹配）
      private-llm  -> LocalEmbedding  （本地 Transformer、语义、不联网）
      external-llm -> CloudEmbedding  （云 embedding API、召回最好）
    向量可为稀疏（TF-IDF: dict[int,float]）或稠密（list[float]）；`cosine` 两者通吃，
    让向量库与表示无关。
"""
from __future__ import annotations

import math
from typing import Dict, List, Protocol, Sequence, Union

from sentinel.retrieval.tfidf import TfidfIndex, tokenize

# EN: a vector is sparse (tf-idf) or dense (embedding). | ZH: 向量为稀疏或稠密。
Vector = Union[Dict[int, float], List[float]]


def cosine(a: Vector, b: Vector) -> float:
    """EN: Cosine similarity for sparse OR dense vectors (sparse assumed
        L2-normalized; dense normalized on the fly). | ZH: 稀疏或稠密向量的余弦相似度。"""
    if isinstance(a, dict) or isinstance(b, dict):
        da = a if isinstance(a, dict) else {i: v for i, v in enumerate(a)}
        db = b if isinstance(b, dict) else {i: v for i, v in enumerate(b)}
        if len(da) > len(db):
            da, db = db, da
        return sum(w * db.get(i, 0.0) for i, w in da.items())
    # EN: dense dot with normalization. | ZH: 稠密点积并归一化。
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingProvider(Protocol):
    """EN: Encode text into vectors. `fit` is a no-op for corpus-independent
        (dense) providers; TF-IDF needs it to learn IDF over the corpus.
    ZH: 把文本编码成向量。对与语料无关的（稠密）provider，`fit` 是空操作；
        TF-IDF 需要它在语料上学 IDF。"""

    name: str
    corpus_dependent: bool

    def fit(self, corpus: Sequence[str]) -> None: ...
    def embed(self, texts: Sequence[str]) -> List[Vector]: ...
    def embed_query(self, text: str) -> Vector: ...


class TfidfEmbeddingProvider:
    """EN: Air-gapped tier — reuses the from-scratch TF-IDF index (sparse).
    ZH: 离线档 —— 复用从零实现的 TF-IDF 索引（稀疏）。"""

    name = "tfidf"
    corpus_dependent = True

    def __init__(self) -> None:
        self._index = TfidfIndex()
        self._fitted = False

    def fit(self, corpus: Sequence[str]) -> None:
        # EN: fit IDF + vocab; doc vectors are built by _vectorize on demand.
        # ZH: 拟合 IDF + 词表；文档向量按需由 _vectorize 生成。
        self._index.fit([(str(i), t) for i, t in enumerate(corpus)])
        self._fitted = True

    def embed(self, texts: Sequence[str]) -> List[Vector]:
        if not self._fitted:
            self.fit(texts)
        return [self._index._vectorize(tokenize(t)) for t in texts]

    def embed_query(self, text: str) -> Vector:
        return self._index._vectorize(tokenize(text))


class LocalEmbeddingProvider:
    """EN: Private tier — local sentence-transformers, semantic, no network.
        Heavy optional dep (torch); imported lazily so air-gapped stays clean.
    ZH: 私有档 —— 本地 sentence-transformers，语义、不联网。重型可选依赖（torch），
        懒加载，保证离线档零负担。"""

    name = "local"
    corpus_dependent = False

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        self._model_name = model
        self._model = None

    def _ensure(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "LocalEmbedding needs `pip install sentence-transformers` "
                    "| 本地嵌入需安装 sentence-transformers"
                ) from e
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def fit(self, corpus: Sequence[str]) -> None:  # EN: no-op | ZH: 空操作
        return None

    def embed(self, texts: Sequence[str]) -> List[Vector]:
        model = self._ensure()
        return [list(map(float, v)) for v in model.encode(list(texts))]

    def embed_query(self, text: str) -> Vector:
        return self.embed([text])[0]


class CloudEmbeddingProvider:
    """EN: External tier — cloud embedding API (OpenAI-compatible). Best recall,
        needs network + key. Imported lazily. | ZH: 外部档 —— 云 embedding API
        （OpenAI 兼容）。召回最好，需联网+key。懒加载。"""

    name = "cloud"
    corpus_dependent = False

    def __init__(self, model: str = "text-embedding-3-small",
                 api_key: str | None = None, base_url: str | None = None) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._client = None

    def _ensure(self):
        if self._client is None:
            try:
                from openai import OpenAI  # type: ignore
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "CloudEmbedding needs `pip install openai` | 云嵌入需安装 openai"
                ) from e
            import os
            self._client = OpenAI(
                api_key=self._api_key or os.getenv("OPENAI_API_KEY"),
                base_url=self._base_url or os.getenv("OPENAI_BASE_URL"),
            )
        return self._client

    def fit(self, corpus: Sequence[str]) -> None:  # EN: no-op | ZH: 空操作
        return None

    def embed(self, texts: Sequence[str]) -> List[Vector]:
        client = self._ensure()
        resp = client.embeddings.create(model=self._model, input=list(texts))
        return [list(map(float, d.embedding)) for d in resp.data]

    def embed_query(self, text: str) -> Vector:
        return self.embed([text])[0]


# EN: privacy mode -> provider (the retrieval tier ladder).
# ZH: 隐私档 -> provider（检索分档阶梯）。
def get_embedding_provider(privacy: str = "air-gapped", **kwargs) -> EmbeddingProvider:
    """EN: Pick an embedding tier by privacy mode (falls back to TF-IDF).
    ZH: 按隐私档选嵌入档（兜底 TF-IDF）。"""
    p = (privacy or "").lower()
    if p in ("private-llm", "private", "local"):
        return LocalEmbeddingProvider(**kwargs)
    if p in ("external-llm", "external", "cloud"):
        return CloudEmbeddingProvider(**kwargs)
    return TfidfEmbeddingProvider()
