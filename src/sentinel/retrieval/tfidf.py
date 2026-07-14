"""TF-IDF index (from scratch). | TF-IDF 索引（从零实现）。

EN: A zero-dependency, air-gapped retrieval index. It answers "how relevant is
    each document to a query" using classic TF-IDF + cosine similarity. This is
    the offline fallback tier of the embedding ladder (cloud -> local -> TF-IDF),
    and the foundation of our Retrieval-Augmented Discovery: rank code units so
    the LLM only sees the top-K most observability-relevant ones.
ZH: 一个零依赖、可离线的检索索引。用经典 TF-IDF + 余弦相似度回答“每个文档与查询
    有多相关”。这是嵌入阶梯的离线兜底档（云→本地→TF-IDF），也是我们“检索增强
    发现”的地基：给代码单元排序，让 LLM 只看 top-K 个最该监控的。

Knowledge points | 知识点:
- TF  (term frequency)      | 词频：词在“本文档”出现的次数，越多越重要。
- IDF (inverse doc freq)    | 逆文档频率：词在“所有文档”里多稀有，越稀有信息量越大。
- weight = TF * IDF         | 权重 = 词频 × 逆文档频率。
- cosine similarity         | 余弦相似度：把文档/查询变成向量比夹角，cos→1 越相关。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass


# EN: split code identifiers too: snake_case, camelCase, digits.
# ZH: 连代码标识符也拆开：蛇形、驼峰、数字。
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize(text: str) -> list[str]:
    """EN: Lowercase + split identifiers into meaningful word tokens.
    ZH: 转小写 + 把标识符拆成有意义的词。
    e.g. 'createOrder get_user_by_id' -> ['create','order','get','user','by','id']"""
    # EN: break camelCase first, then lowercase, then grab alnum runs.
    # ZH: 先拆驼峰，再转小写，再抓连续字母数字。
    text = _CAMEL_RE.sub(" ", text)
    return _TOKEN_RE.findall(text.lower())


@dataclass
class _Doc:
    doc_id: str
    vector: dict[int, float]   # EN: sparse tf-idf, L2-normalized | ZH: 稀疏 tf-idf，已 L2 归一化


class TfidfIndex:
    """EN: Fit on documents, then query by relevance. | ZH: 用文档拟合，再按相关度查询。"""

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {}     # EN: term -> column index | ZH: 词 -> 列号
        self.idf: list[float] = []          # EN: idf per term | ZH: 每个词的 idf
        self.docs: list[_Doc] = []

    def fit(self, documents: list[tuple[str, str]]) -> "TfidfIndex":
        """EN: Build vocab, IDF, and normalized doc vectors.
        ZH: 建立词表、IDF 和归一化的文档向量。
        `documents` = list of (doc_id, text)."""
        tokenized = [(doc_id, tokenize(text)) for doc_id, text in documents]
        n_docs = len(tokenized)

        # EN: 1) vocabulary + document frequency (how many docs contain each term).
        # ZH: 1) 词表 + 文档频率（多少个文档包含该词）。
        df: Counter[str] = Counter()
        for _, toks in tokenized:
            for term in set(toks):          # EN: set() => count each doc once | ZH: set() 让每文档只计一次
                df[term] += 1
        self.vocab = {term: i for i, term in enumerate(sorted(df))}

        # EN: 2) smoothed IDF: rare terms score higher. | ZH: 2) 平滑 IDF：越稀有分越高。
        self.idf = [0.0] * len(self.vocab)
        for term, idx in self.vocab.items():
            self.idf[idx] = math.log((n_docs + 1) / (df[term] + 1)) + 1.0

        # EN: 3) build a normalized TF-IDF vector per document.
        # ZH: 3) 为每个文档建一个归一化的 TF-IDF 向量。
        self.docs = [
            _Doc(doc_id, self._vectorize(toks)) for doc_id, toks in tokenized
        ]
        return self

    def query(self, text: str, top_k: int = 10) -> list[tuple[str, float]]:
        """EN: Return the top_k (doc_id, score) most similar to the query text.
        ZH: 返回与查询文本最相似的前 top_k 个 (doc_id, 分数)。"""
        q_vec = self._vectorize(tokenize(text))
        if not q_vec:
            return []
        scored = [(d.doc_id, _cosine(q_vec, d.vector)) for d in self.docs]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # -- internals | 内部实现 ----------------------------------------------

    def _vectorize(self, tokens: list[str]) -> dict[int, float]:
        """EN: tokens -> sparse, L2-normalized tf-idf vector.
        ZH: 词列表 -> 稀疏、L2 归一化的 tf-idf 向量。"""
        tf = Counter(tokens)
        vec: dict[int, float] = {}
        for term, count in tf.items():
            idx = self.vocab.get(term)
            if idx is None:                 # EN: unseen term at query time | ZH: 查询时的新词，忽略
                continue
            vec[idx] = count * self.idf[idx]   # EN: weight = tf * idf | ZH: 权重 = 词频 × idf
        # EN: L2-normalize so dot product == cosine similarity.
        # ZH: L2 归一化，这样点积就等于余弦相似度。
        norm = math.sqrt(sum(w * w for w in vec.values()))
        if norm > 0:
            for idx in vec:
                vec[idx] /= norm
        return vec


def _cosine(a: dict[int, float], b: dict[int, float]) -> float:
    """EN: Dot product of two L2-normalized sparse vectors = cosine similarity.
    ZH: 两个已 L2 归一化的稀疏向量的点积 = 余弦相似度。"""
    # EN: iterate the smaller vector for speed. | ZH: 遍历较小的向量更快。
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(idx, 0.0) for idx, w in a.items())
