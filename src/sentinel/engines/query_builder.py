"""Query Builder (D2). | 查询生成器（D2）。

EN: Phase 4 of the pipeline. Given a catalog and a chosen backend, render a query
    per metric. Also decides sampling advice (counts never sampled; high-frequency
    latency/traces prefer tail sampling). See DESIGN section 5.4.
ZH: 流水线第四阶段。给定清单和选定后端，为每个指标渲染查询。同时给出采样建议
    （计数绝不采样；高频时延/trace 优先尾采样）。参见设计文档第 5.4 节。
"""
from __future__ import annotations

from dataclasses import dataclass

from sentinel.adapters.backends.base import MetricsBackend
from sentinel.model.metric import MetricDescriptor, MetricsCatalog


@dataclass
class RenderedQuery:
    metric_id: str
    backend: str
    query: str
    sampling_note: str


class QueryBuilder:
    """EN: Render backend queries for every metric in a catalog.
    ZH: 为清单里每个指标渲染后端查询。"""

    def __init__(self, backend: MetricsBackend):
        self.backend = backend

    def build(self, catalog: MetricsCatalog, window: str = "5m", lookback: str = "1h") -> list[RenderedQuery]:
        out: list[RenderedQuery] = []
        seen: set[str] = set()
        for m in catalog.metrics:
            # EN: one query per distinct metric id (dedup across locations).
            # ZH: 每个不同指标 id 一条查询（跨位置去重）。
            if m.id in seen:
                continue
            seen.add(m.id)
            out.append(
                RenderedQuery(
                    metric_id=m.id,
                    backend=self.backend.name,
                    query=self.backend.render_query(m, window=window, lookback=lookback),
                    sampling_note=_sampling_note(m),
                )
            )
        return out


def _sampling_note(metric: MetricDescriptor) -> str:
    """EN: Human-readable sampling advice per metric. | ZH: 每个指标的可读采样建议。"""
    if metric.kind.value == "counter":
        return "no sampling — counts must stay exact | 不采样：计数须精确"
    if metric.sampling.required:
        return (
            f"sample: {metric.sampling.strategy.value} @ {metric.sampling.rate:.0%} "
            f"| 采样：{metric.sampling.strategy.value} {metric.sampling.rate:.0%}"
        )
    return "no sampling needed | 无需采样"
