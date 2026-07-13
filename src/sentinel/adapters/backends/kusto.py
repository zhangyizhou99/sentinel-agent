"""Kusto (KQL) backend. | Kusto (KQL) 后端。

EN: Renders Kusto Query Language for each metric, chosen by its signal + kind.
    Counting uses aggregation (never sampling) to keep counts exact; latency uses
    percentiles; time is bucketed with bin(). Table/column names follow a simple
    convention and are meant to be adjusted per project. See DESIGN section 5.4.
ZH: 按指标的 signal + kind 渲染 Kusto 查询语言。计数用聚合（绝不采样）保证准确；
    时延用百分位；时间用 bin() 分桶。表名/列名用简单约定，实际项目可调整。
    参见设计文档第 5.4 节。
"""
from __future__ import annotations

from sentinel.model.metric import MetricDescriptor, Signal

# EN: metric-id prefix -> a default Kusto table. | ZH: 指标 id 前缀 -> 默认 Kusto 表名。
_TABLE_BY_PREFIX = {
    "api": "ApiRequests",
    "dep": "DependencyCalls",
    "app": "AppEvents",
}


class KustoBackend:
    name = "kusto"

    def _table(self, metric: MetricDescriptor) -> str:
        prefix = metric.id.split(".", 1)[0]
        return _TABLE_BY_PREFIX.get(prefix, "Events")

    def render_query(self, metric: MetricDescriptor, window: str = "5m", lookback: str = "1h") -> str:
        table = self._table(metric)
        # EN: dimensions become `by` columns. | ZH: 维度变成 `by` 列。
        by_cols = ", ".join(metric.dimensions + [f"bin(Timestamp, {window})"])

        if metric.signal == Signal.errors:
            # EN: error RATE via aggregation — counting must NOT be sampled.
            # ZH: 用聚合算错误RATE —— 计数绝不能采样。
            return (
                f"// {metric.id}: error rate | 错误率\n"
                f"{table}\n"
                f"| where Timestamp > ago({lookback})\n"
                f"| summarize Total = count(), Errors = countif(StatusCode >= 500)\n"
                f"    by {by_cols}\n"
                f"| extend ErrorRate = round(100.0 * Errors / Total, 2)\n"
                f"| where Total > 0\n"
                f"| order by Timestamp desc"
            )

        if metric.signal == Signal.latency:
            # EN: latency percentiles (p50/p95/p99). | ZH: 时延百分位。
            return (
                f"// {metric.id}: latency percentiles | 时延百分位\n"
                f"{table}\n"
                f"| where Timestamp > ago({lookback})\n"
                f"| summarize P50 = percentile(DurationMs, 50),\n"
                f"    P95 = percentile(DurationMs, 95),\n"
                f"    P99 = percentile(DurationMs, 99)\n"
                f"    by {by_cols}\n"
                f"| order by Timestamp desc"
            )

        # EN: default — traffic/count over the window. | ZH: 兜底 —— 窗口内计数/流量。
        return (
            f"// {metric.id}: count | 计数\n"
            f"{table}\n"
            f"| where Timestamp > ago({lookback})\n"
            f"| summarize Count = count() by {by_cols}\n"
            f"| order by Timestamp desc"
        )
