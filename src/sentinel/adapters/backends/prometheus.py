"""Prometheus (PromQL) backend. | Prometheus (PromQL) 后端。

EN: The SAME MetricDescriptor rendered as PromQL instead of KQL — proof that the
    engine is backend-agnostic. Only this adapter differs. See DESIGN section 5.4.
ZH: 同一个 MetricDescriptor 渲染成 PromQL 而非 KQL —— 证明引擎与平台无关。只有这个
    适配器不同。参见设计文档第 5.4 节。
"""
from __future__ import annotations

from sentinel.model.alert import AlertPolicy, ThresholdRule
from sentinel.model.metric import MetricDescriptor, Signal


def _metric_name(metric_id: str) -> str:
    # EN: PromQL metric names use underscores. | ZH: PromQL 指标名用下划线。
    return metric_id.replace(".", "_").replace("-", "_")


# EN: OTLP -> Prometheus unit words (Grafana appends these to metric names).
# ZH: OTLP -> Prometheus 单位词（Grafana 会把它们拼进指标名）。
_UNIT_WORD = {
    "ms": "milliseconds", "s": "seconds", "us": "microseconds",
    "ns": "nanoseconds", "min": "minutes", "h": "hours", "d": "days",
    "by": "bytes", "kby": "kilobytes", "mby": "megabytes", "%": "percent",
}


def _prom_base(metric_id: str, unit: str = "") -> str:
    """EN: Base Prometheus name including OTLP unit suffix, e.g.
        (api.request.duration, ms) -> api_request_duration_milliseconds.
    ZH: 含 OTLP 单位后缀的 Prometheus 基名，如
        (api.request.duration, ms) -> api_request_duration_milliseconds。"""
    base = _metric_name(metric_id)
    u = (unit or "").strip().lower()
    if u:
        base = f"{base}_{_UNIT_WORD.get(u, u)}"
    return base



class PrometheusBackend:
    name = "prometheus"

    def render_query(self, metric: MetricDescriptor, window: str = "5m", lookback: str = "1h") -> str:
        base = _prom_base(metric.id, metric.unit)
        by = ", ".join(metric.dimensions) if metric.dimensions else ""
        by_clause = f" by ({by})" if by else ""

        if metric.signal == Signal.errors:
            # EN: our errors counter increments only on failure -> rate of errors.
            # ZH: 我们的错误计数器只在失败时+1 -> 错误发生速率。
            return (
                f"# {metric.id}: error rate (errors/sec) | 错误速率（每秒）\n"
                f"sum(rate({base}_total[{window}])){by_clause}"
            )

        if metric.signal == Signal.latency:
            # EN: p99 from histogram buckets. | ZH: 从直方图桶算 p99。
            return (
                f"# {metric.id}: p99 latency | p99 时延\n"
                f"histogram_quantile(0.99,\n"
                f"  sum(rate({base}_bucket[{window}])) by (le{', ' + by if by else ''}))"
            )

        # EN: default — request rate. | ZH: 兜底 —— 请求速率。
        return (
            f"# {metric.id}: rate | 速率\n"
            f"sum(rate({base}_total[{window}])){by_clause}"
        )

    # -- alert rules | 告警规则 --------------------------------------------

    def render_alert_rule(self, policy: AlertPolicy) -> list[dict]:
        """EN: Translate an AlertPolicy into Prometheus alerting-rule dicts.
        ZH: 把 AlertPolicy 翻译成 Prometheus 告警规则字典。"""
        name = _metric_name(policy.metric_id)
        rules: list[dict] = []
        for r in policy.rules:
            parts = alert_parts(policy, r)
            if parts is None:
                continue
            query, thr, op = parts
            rules.append({
                "alert": f"{name}_{r.severity.value.lower()}",
                "expr": f"{query} {op} {thr:g}",
                "for": r.duration,
                "labels": {"severity": r.severity.value.lower()},
                "annotations": {"summary": f"{policy.metric_id}: {r.condition}"},
            })
        return rules


def alert_parts(policy: AlertPolicy, r: ThresholdRule) -> tuple[str, float, str] | None:
    """EN: Split an alert into (query, threshold, comparator) so both the
        Prometheus YAML path and the Grafana API path can reuse the SAME logic.
        The query has NO comparator baked in — Grafana needs them separate.
    ZH: 把一条告警拆成 (查询, 阈值, 比较符)，让 Prometheus YAML 路径和 Grafana API
        路径复用同一逻辑。查询里不含比较符 —— Grafana 需要分开。"""
    base = _prom_base(policy.metric_id, policy.unit)
    total = None
    if policy.total_metric_id:
        total = _prom_base(policy.total_metric_id, policy.total_metric_unit) + "_count"

    # EN: error ratio = errors / total requests over the window.
    # ZH: 错误比 = 窗口内 错误数 / 总请求数。
    if r.stat == "error_rate":
        thr = r.threshold / 100.0
        if total:
            query = (
                f"sum(rate({base}_total[{r.duration}]))"
                f" / sum(rate({total}[{r.duration}]))"
            )
            return query, thr, r.op
        # EN: no total metric -> "any errors". | ZH: 无总数 -> “有错即报”。
        return f"sum(rate({base}_total[{r.duration}]))", 0.0, r.op

    # EN: latency percentile from histogram (Prometheus uses seconds).
    # ZH: 从直方图算时延百分位（Prometheus 用秒）。
    if r.stat in ("p50", "p95", "p99"):
        q = {"p50": 0.5, "p95": 0.95, "p99": 0.99}[r.stat]
        thr = r.threshold / 1000.0 if r.unit == "ms" else r.threshold
        query = f"histogram_quantile({q}, sum(rate({base}_bucket[{r.duration}])) by (le))"
        return query, thr, r.op

    # EN: resource utilization gauge. | ZH: 资源利用率仪表。
    if r.stat == "utilization":
        return base, r.threshold / 100.0, r.op

    # EN: traffic/others — not auto-expressible yet. | ZH: 流量/其它 —— 暂不自动表达。
    return None


def to_prometheus_yaml(rule_dicts: list[dict], group: str = "sentinel") -> str:
    """EN: Hand-render a Prometheus rules file (zero YAML dependency).
    ZH: 手工渲染 Prometheus 规则文件（零 YAML 依赖）。"""
    lines = ["groups:", f"  - name: {group}", "    rules:"]
    for rd in rule_dicts:
        lines.append(f"      - alert: {rd['alert']}")
        lines.append(f"        expr: {rd['expr']}")
        lines.append(f"        for: {rd['for']}")
        lines.append("        labels:")
        for k, v in rd["labels"].items():
            lines.append(f"          {k}: {v}")
        lines.append("        annotations:")
        for k, v in rd["annotations"].items():
            lines.append(f'          {k}: "{v}"')
    return "\n".join(lines) + "\n"
