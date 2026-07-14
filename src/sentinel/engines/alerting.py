"""Alerting Designer (D1). | 告警设计器（D1）。

EN: Phase 5 of the pipeline. Turns each metric into an AlertPolicy: multi-level
    thresholds mapped to Sev1–Sev4, plus routing. This skeleton uses per-signal
    STATIC templates (clear, safe defaults). Thresholds are SUGGESTIONS for human
    review — never silently enforced. Baseline/anomaly methods can replace the
    templates later. See DESIGN section 5.1/5.2.
ZH: 流水线第五阶段。把每个指标变成一份 AlertPolicy：多级阈值映射到 Sev1–Sev4，外加
    路由。骨架用按信号的**静态模板**（清晰、安全的默认值）。阈值是给人审阅的**建议**，
    绝不静默强制执行。基线/异常方法可后续替换模板。参见设计文档第 5.1/5.2 节。
"""
from __future__ import annotations

from sentinel.model.alert import (
    DEFAULT_ROUTING,
    AlertPolicy,
    Severity,
    ThresholdMethod,
    ThresholdRule,
)
from sentinel.model.metric import MetricsCatalog, Signal


class AlertingDesigner:
    """EN: Produce an AlertPolicy per distinct metric. | ZH: 为每个不同指标产出一份 AlertPolicy。"""

    def __init__(self, routing: dict[Severity, list[str]] | None = None, window: str = "5m"):
        self.routing = routing or DEFAULT_ROUTING
        self.window = window

    def design(self, catalog: MetricsCatalog) -> list[AlertPolicy]:
        out: list[AlertPolicy] = []
        seen: set[str] = set()
        # EN: find the "request total" metric once — its _count is the error-rate
        #     denominator. Prefer request-duration (RED.Duration) over cold_start.
        # ZH: 先找一次“请求总数”指标 —— 它的 _count 作错误率分母。优先请求时延
        #     （RED.Duration），排除冷启动。
        total_id, total_unit = self._find_request_total(catalog)
        for m in catalog.metrics:
            if m.id in seen:
                continue
            seen.add(m.id)
            is_err = m.signal == Signal.errors
            out.append(
                AlertPolicy(
                    metric_id=m.id,
                    unit=m.unit,
                    method=ThresholdMethod.static,
                    window=self.window,
                    rules=self._rules_for(m.id, m.signal),
                    routing=self.routing,
                    total_metric_id=total_id if is_err else None,
                    total_metric_unit=total_unit if is_err else "",
                )
            )
        return out

    @staticmethod
    def _find_request_total(catalog: MetricsCatalog) -> tuple[str | None, str]:
        """EN: The metric whose _count = total requests (error-rate denominator).
        ZH: 其 _count = 总请求数的指标（错误率分母）。"""
        for m in catalog.metrics:
            if m.signal == Signal.latency and not m.id.startswith("app.cold_start"):
                return m.id, m.unit
        return None, ""

    # -- per-signal threshold templates | 按信号的阈值模板 -----------------

    def _rules_for(self, metric_id: str, signal: Signal | None) -> list[ThresholdRule]:
        # EN: errors — the loudest signal; rate-based, two levels.
        # ZH: 错误 —— 最响的信号；按错误率，两级。
        if signal == Signal.errors:
            return [
                ThresholdRule(stat="error_rate", op=">", threshold=5, unit="%",
                              duration="2m", severity=Severity.SEV1),
                ThresholdRule(stat="error_rate", op=">", threshold=1, unit="%",
                              duration="10m", severity=Severity.SEV3),
            ]
        # EN: latency — cold start is less urgent than request latency.
        # ZH: 时延 —— 冷启动没请求时延那么紧急。
        if signal == Signal.latency:
            if metric_id.startswith("app.cold_start"):
                return [
                    ThresholdRule(stat="p95", op=">", threshold=5000, unit="ms",
                                  duration="10m", severity=Severity.SEV3),
                    ThresholdRule(stat="p95", op=">", threshold=2000, unit="ms",
                                  duration="15m", severity=Severity.SEV4),
                ]
            return [
                ThresholdRule(stat="p99", op=">", threshold=5000, unit="ms",
                              duration="2m", severity=Severity.SEV1),
                ThresholdRule(stat="p99", op=">", threshold=2000, unit="ms",
                              duration="5m", severity=Severity.SEV2),
                ThresholdRule(stat="p99", op=">", threshold=1000, unit="ms",
                              duration="10m", severity=Severity.SEV3),
            ]
        # EN: saturation — resource pressure.
        # ZH: 饱和度 —— 资源压力。
        if signal == Signal.saturation:
            return [
                ThresholdRule(stat="utilization", op=">", threshold=90, unit="%",
                              duration="5m", severity=Severity.SEV2),
                ThresholdRule(stat="utilization", op=">", threshold=80, unit="%",
                              duration="15m", severity=Severity.SEV3),
            ]
        # EN: traffic — usually anomaly; keep a mild watch rule.
        # ZH: 流量 —— 通常靠异常检测；这里留一个温和的观察规则。
        return [
            ThresholdRule(stat="traffic", op="<", threshold=50, unit="%",
                          duration="10m", severity=Severity.SEV3),
        ]
