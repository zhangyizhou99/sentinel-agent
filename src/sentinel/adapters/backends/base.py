"""Metrics backend adapter contract. | 指标后端适配器契约。

EN: A backend translates a platform-agnostic MetricDescriptor into a concrete
    query language (KQL, PromQL, ...) and alert rule. Engines depend only on this
    interface, so swapping Kusto for Prometheus means swapping ONE adapter — the
    Query Builder and Alerting Designer never change. This is the core of
    "backend-agnostic" (DESIGN section 3/6).
ZH: 后端把平台无关的 MetricDescriptor 翻译成具体查询语言（KQL、PromQL…）和告警
    规则。引擎只依赖这个接口，所以把 Kusto 换成 Prometheus 只需换一个适配器——
    Query Builder 和告警设计器都不用动。这是“平台无关”的核心（设计文档第 3/6 节）。
"""
from __future__ import annotations

from typing import Protocol

from sentinel.model.metric import MetricDescriptor


class MetricsBackend(Protocol):
    """EN: Renders queries (and later alert rules) for one backend.
    ZH: 为某一个后端渲染查询（以及后续的告警规则）。"""

    name: str

    def render_query(self, metric: MetricDescriptor, window: str = "5m", lookback: str = "1h") -> str:
        """EN: Translate a metric into a backend query string.
        ZH: 把一个指标翻译成后端查询语句。"""
        ...
