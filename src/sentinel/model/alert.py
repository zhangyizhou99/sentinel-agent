"""Alert policy model. | 告警策略模型.

EN: Platform-agnostic description of "when to alert and how loud". A metric maps
    to multi-level threshold rules, each tied to a severity (Sev1–Sev4), plus
    routing (which channels each severity goes to). See DESIGN section 5.2.
ZH: 平台无关地描述“何时告警、多大声”。一个指标对应多级阈值规则，每级绑定一个严重度
    （Sev1–Sev4），外加路由（每个严重度发到哪些渠道）。参见设计文档第 5.2 节。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    # EN: blast radius × urgency. | ZH: 影响面 × 紧急度。
    SEV1 = "SEV1"   # EN: outage / widespread | ZH: 服务中断/大面积
    SEV2 = "SEV2"   # EN: significant degradation | ZH: 显著降级
    SEV3 = "SEV3"   # EN: local / tolerable | ZH: 局部/可容忍
    SEV4 = "SEV4"   # EN: watch item | ZH: 观察项


class ThresholdMethod(str, Enum):
    static = "static"                       # EN: fixed SLO | ZH: 固定 SLO
    percentile_baseline = "percentile-baseline"  # EN: history × factor | ZH: 历史×系数
    anomaly = "anomaly"                     # EN: anomaly detection | ZH: 异常检测


class ThresholdRule(BaseModel):
    """EN: One structured condition → one severity. Structured fields let a
        backend render a real alert expression (e.g. PromQL); `condition` is the
        human-readable form derived from them.
    ZH: 一个结构化条件 → 一个严重度。结构化字段让后端能渲染真实告警表达式（如
        PromQL）；`condition` 是由它们派生的人类可读形式。"""

    stat: str                 # EN: p99 | p95 | error_rate | utilization | traffic
    op: str = ">"             # EN: comparator | ZH: 比较符
    threshold: float = 0.0    # EN: numeric threshold | ZH: 数值阈值
    unit: str = ""            # EN: ms | % | "" | ZH: 单位
    duration: str = "5m"      # EN: must persist this long (for:) | ZH: 需持续多久
    severity: Severity

    @property
    def condition(self) -> str:
        """EN: human-readable, e.g. "p99 > 2000ms for 5m". | ZH: 人类可读形式。"""
        return f"{self.stat} {self.op} {self.threshold:g}{self.unit} for {self.duration}"


class AlertPolicy(BaseModel):
    """EN: The full alerting policy for one metric. | ZH: 单个指标的完整告警策略。"""

    metric_id: str
    unit: str = ""            # EN: metric unit (ms/%/…), for backend name suffixes
                              # ZH: 指标单位（ms/% 等），供后端拼指标名后缀
    method: ThresholdMethod = ThresholdMethod.static
    window: str = "5m"
    rules: list[ThresholdRule] = Field(default_factory=list)
    # EN: severity -> channels. | ZH: 严重度 -> 渠道。
    routing: dict[Severity, list[str]] = Field(default_factory=dict)
    # EN: error-rate needs a denominator (total requests). These point at the
    #     companion "request total" metric so a backend can build errors/total.
    #     Platform-agnostic: just an id + unit, never a concrete metric name.
    # ZH: 错误率需要分母（总请求数）。这两个字段指向配套的“请求总数”指标，让后端
    #     能拼出 errors/total。平台无关：只存 id + 单位，绝不存具体后端指标名。
    total_metric_id: Optional[str] = None
    total_metric_unit: str = ""


# EN: default routing matrix (DESIGN 5.2). | ZH: 默认路由矩阵（设计文档 5.2）。
DEFAULT_ROUTING: dict[Severity, list[str]] = {
    Severity.SEV1: ["pagerduty", "slack#oncall-critical"],
    Severity.SEV2: ["pagerduty", "slack#oncall"],
    Severity.SEV3: ["slack#alerts"],
    Severity.SEV4: ["slack#alerts-noise"],
}
