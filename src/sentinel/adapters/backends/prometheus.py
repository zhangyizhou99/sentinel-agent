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


class PrometheusBackend:
    name = "prometheus"

    def render_query(self, metric: MetricDescriptor, window: str = "5m", lookback: str = "1h") -> str:
        name = _metric_name(metric.id)
        by = ", ".join(metric.dimensions) if metric.dimensions else ""
        by_clause = f" by ({by})" if by else ""

        if metric.signal == Signal.errors:
            # EN: error ratio via rate() over the window. | ZH: 用 rate() 算窗口内错误比。
            return (
                f"# {metric.id}: error rate | 错误率\n"
                f"sum(rate({name}_total{{status=~\"5..\"}}[{window}])){by_clause}\n"
                f"  / sum(rate({name}_total[{window}])){by_clause}"
            )

        if metric.signal == Signal.latency:
            # EN: p99 from histogram buckets. | ZH: 从直方图桶算 p99。
            return (
                f"# {metric.id}: p99 latency | p99 时延\n"
                f"histogram_quantile(0.99,\n"
                f"  sum(rate({name}_bucket[{window}])) by (le{', ' + by if by else ''}))"
            )

        # EN: default — request rate. | ZH: 兜底 —— 请求速率。
        return (
            f"# {metric.id}: rate | 速率\n"
            f"sum(rate({name}_total[{window}])){by_clause}"
        )

    # -- alert rules | 告警规则 --------------------------------------------

    def render_alert_rule(self, policy: AlertPolicy) -> list[dict]:
        """EN: Translate an AlertPolicy into Prometheus alerting-rule dicts.
        ZH: 把 AlertPolicy 翻译成 Prometheus 告警规则字典。"""
        name = _metric_name(policy.metric_id)
        rules: list[dict] = []
        for r in policy.rules:
            expr = self._alert_expr(name, r)
            if expr is None:
                continue
            rules.append({
                "alert": f"{name}_{r.severity.value.lower()}",
                "expr": expr,
                "for": r.duration,
                "labels": {"severity": r.severity.value.lower()},
                "annotations": {"summary": f"{policy.metric_id}: {r.condition}"},
            })
        return rules

    @staticmethod
    def _alert_expr(name: str, r: ThresholdRule) -> str | None:
        # EN: error ratio over the window. | ZH: 窗口内错误比。
        if r.stat == "error_rate":
            thr = r.threshold / 100.0
            return (
                f'sum(rate({name}_total{{status=~"5.."}}[{r.duration}]))'
                f' / sum(rate({name}_total[{r.duration}])) {r.op} {thr:g}'
            )
        # EN: latency percentile from histogram (Prometheus uses seconds).
        # ZH: 从直方图算时延百分位（Prometheus 用秒）。
        if r.stat in ("p50", "p95", "p99"):
            q = {"p50": 0.5, "p95": 0.95, "p99": 0.99}[r.stat]
            thr = r.threshold / 1000.0 if r.unit == "ms" else r.threshold
            return (
                f"histogram_quantile({q}, sum(rate({name}_bucket[{r.duration}])) by (le))"
                f" {r.op} {thr:g}"
            )
        # EN: resource utilization gauge. | ZH: 资源利用率仪表。
        if r.stat == "utilization":
            return f"{name} {r.op} {r.threshold / 100.0:g}"
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
