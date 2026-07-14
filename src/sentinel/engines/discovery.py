"""Discovery engine. | 发现引擎。

EN: Phase 1 of the pipeline. Hybrid strategy = static AST scan (precise recall)
    + optional LLM semantic augmentation (business meaning). Runs fully offline
    when the LLM is unavailable or privacy.mode=air-gapped. See DESIGN section 5.
ZH: 流水线的第一阶段。混合策略 = 静态 AST 扫描（精确召回）+ 可选 LLM 语义增强
    （业务含义）。当 LLM 不可用或 privacy.mode=air-gapped 时可完全离线运行。
    参见设计文档第 5 节。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from sentinel.adapters.scanners.base import CodeScanner
from sentinel.adapters.scanners.python_scanner import PythonScanner
from sentinel.engines.augment_cache import AugmentationCache, signature_of
from sentinel.engines.prompts import discovery_prompt
from sentinel.llm.client import LLMClient
from sentinel.paths import augment_cache_path
from sentinel.retrieval.retriever import ObservabilityRetriever
from sentinel.model.metric import (
    MetricDescriptor,
    MetricKind,
    MetricsCatalog,
    Signal,
    Source,
    Status,
)


class DiscoveryEngine:
    """EN: Produce a MetricsCatalog for a repository.
    ZH: 为一个仓库产出 MetricsCatalog。"""

    def __init__(
        self,
        scanners: Optional[list[CodeScanner]] = None,
        llm: Optional[LLMClient] = None,
        lang: str = "en",
        retriever: Optional[ObservabilityRetriever] = None,
        retrieval_top_k: int = 20,
    ):
        # EN: register language scanners; Python only for the walking skeleton.
        # ZH: 注册语言扫描器；走路骨架阶段仅 Python。
        self.scanners: list[CodeScanner] = scanners or [PythonScanner()]
        self.llm = llm
        self.lang = lang
        # EN: retriever bounds LLM input to the top-K relevant units (RAG).
        # ZH: 检索器把 LLM 输入限定在 top-K 相关单元（RAG）。
        self.retriever = retriever or ObservabilityRetriever()
        self.retrieval_top_k = retrieval_top_k

    def run(self, target: str | Path | list) -> MetricsCatalog:
        # EN: `target` can be a directory path, a single file path, or a list of
        #     file paths (upload / git-incremental). See DESIGN scalability notes.
        # ZH: `target` 可为目录路径、单文件路径，或文件路径列表（上传 / git 增量）。
        raw: list[MetricDescriptor] = []
        retrieval_root: Optional[Path] = None
        if isinstance(target, (list, tuple)):
            files = [Path(p) for p in target if Path(p).exists()]
            if not files:
                raise FileNotFoundError("no valid files | 无有效文件")
            base = Path(os.path.commonpath([str(f) for f in files]))
            if base.is_file():
                base = base.parent
            catalog = MetricsCatalog(repo=f"{len(files)} file(s) | {len(files)} 个文件")
            for scanner in self.scanners:
                raw.extend(scanner.scan(base, only=files))
        else:
            root = Path(target).resolve()
            if not root.exists():
                raise FileNotFoundError(f"repo not found | 仓库不存在: {root}")
            catalog = MetricsCatalog(repo=str(root))
            retrieval_root = root      # EN: enable retrieval for directory scans | ZH: 目录扫描时启用检索
            for scanner in self.scanners:
                if scanner.matches(root):
                    raw.extend(scanner.scan(root))

        # EN: 1) static pass results are deduped into the catalog.
        # ZH: 1) 静态通道结果去重后放入清单。
        for m in self._dedup(raw):
            catalog.add(m)

        # EN: 2) semantic pass — optional LLM augmentation, bounded by retrieval.
        # ZH: 2) 语义通道 —— 可选 LLM 增强，用检索限定输入。
        if self.llm and self.llm.available:
            for m in self._augment(catalog, retrieval_root):
                catalog.add(m)

        return catalog

    # -- internals | 内部实现 ----------------------------------------------

    @staticmethod
    def _dedup(metrics: list[MetricDescriptor]) -> list[MetricDescriptor]:
        seen: set[tuple[str, str]] = set()
        out: list[MetricDescriptor] = []
        for m in metrics:
            if m.key() not in seen:
                seen.add(m.key())
                out.append(m)
        return out

    def _augment(self, catalog: MetricsCatalog, root: Optional[Path]) -> list[MetricDescriptor]:
        """EN: LLM augmentation with two "don't repeat work" gates.
        ZH: 带两道“别重复干活”闸门的 LLM 增强。"""
        assert self.llm is not None
        # EN: file-list / no-root path: no retrieval, one plain batch call.
        # ZH: 文件列表 / 无根路径：不检索，直接一次批量调用。
        if root is None:
            return self._augment_batch(self._code_summary(catalog), catalog)

        ranked = self.retriever.rank(root, top_k=self.retrieval_top_k)
        if not ranked:
            return []

        # EN: Gate ① coverage — drop units whose file is already instrumented.
        # ZH: 闸门① 覆盖度 —— 丢掉所在文件已埋点的单元。
        present_files = {m.source.file for m in catalog.metrics if m.status == Status.present}
        units = [u for u, _ in ranked if u.file not in present_files]
        if not units:
            return []      # EN: everything already covered → no LLM | ZH: 全已覆盖 → 不调 LLM

        # EN: Gate ② change-detection — reuse cached result if the set is unchanged.
        # ZH: 闸门② 变更检测 —— 候选集未变则复用缓存结果。
        sig = signature_of([f"{u.unit_id}:{_hash(u.text)}" for u in units])
        cache = AugmentationCache(augment_cache_path(root))
        cached = cache.get(sig)
        if cached is not None:
            # EN: unchanged candidate set → ZERO LLM calls. | ZH: 候选集未变 → 零次 LLM 调用。
            return [MetricDescriptor.model_validate(m) for m in cached]

        # EN: only the changed / new units actually reach the LLM.
        # ZH: 只有变化 / 新增的单元才真正到达 LLM。
        summary = "\n".join(f"{u.file}::{u.symbol}" for u in units)
        extra = self._augment_batch(summary, catalog)
        cache.put(sig, [m.model_dump(mode="json") for m in extra])
        cache.save()
        return extra

    def _augment_batch(self, code_summary: str, catalog: MetricsCatalog) -> list[MetricDescriptor]:
        """EN: One LLM call over the given code summary (AST text only).
        ZH: 对给定代码摘要做一次 LLM 调用（仅 AST 文本）。"""
        catalog_json = json.dumps(
            [m.model_dump(mode="json") for m in catalog.metrics], ensure_ascii=False
        )
        system, user = discovery_prompt(self.lang, code_summary, catalog_json)
        try:
            reply = self.llm.complete(system, user)
            return self._parse_llm(reply, catalog)
        except Exception:
            # EN: best-effort; never fail the whole run. | ZH: 尽力而为；绝不让整次运行失败。
            return []

    @staticmethod
    def _code_summary(catalog: MetricsCatalog) -> str:
        # EN: a compact, PII-free view of where metrics were found.
        # ZH: 一个紧凑、无 PII 的“指标发现位置”视图。
        lines = sorted(
            {f"{m.source.file}:{m.source.symbol} ({m.source.framework or 'n/a'})"
             for m in catalog.metrics}
        )
        return "\n".join(lines)

    def _parse_llm(self, reply: str, catalog: MetricsCatalog) -> list[MetricDescriptor]:
        """EN: Parse the LLM JSON into MetricDescriptors, defensively.
        ZH: 稳健地把 LLM 的 JSON 解析成 MetricDescriptor。"""
        text = reply.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("[") : text.rfind("]") + 1]
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            return []
        existing = {m.id for m in catalog.metrics}
        out: list[MetricDescriptor] = []
        for it in items if isinstance(items, list) else []:
            mid = str(it.get("id", "")).strip()
            if not mid or mid in existing:
                continue
            out.append(
                MetricDescriptor(
                    id=mid,
                    kind=_safe_kind(it.get("kind")),
                    description=str(it.get("description", "")),
                    source=Source(file="<llm>", symbol="semantic"),
                    category=str(it.get("category", "")),
                    signal=_safe_signal(it.get("signal")),
                    status=Status.missing,
                )
            )
        return out


def _hash(text: str) -> str:
    """EN: short content hash of a code unit's text. | ZH: 代码单元文本的短内容哈希。"""
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


def _safe_kind(v: object) -> MetricKind:
    try:
        return MetricKind(str(v))
    except ValueError:
        return MetricKind.counter


def _safe_signal(v: object) -> Optional[Signal]:
    try:
        return Signal(str(v))
    except ValueError:
        return None
