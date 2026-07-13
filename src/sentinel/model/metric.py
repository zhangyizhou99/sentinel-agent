"""Platform-agnostic metric intermediate representation (IR).

EN: This is the "lingua franca" of Sentinel. Every engine and adapter speaks in
    terms of these models, never in terms of a concrete backend (Kusto,
    Prometheus, ...). See DESIGN section 4.
ZH: 这是 Sentinel 的“普通话”。所有引擎与适配器都只用这些模型交流，而不直接
    依赖具体后端（Kusto/Prometheus …）。参见设计文档第 4 节。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MetricKind(str, Enum):
    counter = "counter"
    gauge = "gauge"
    histogram = "histogram"
    summary = "summary"


class Signal(str, Enum):
    """EN: Google SRE golden signals. | ZH: Google SRE 黄金信号。"""

    latency = "latency"
    traffic = "traffic"
    errors = "errors"
    saturation = "saturation"


class Status(str, Enum):
    present = "present"      # EN: instrumentation already exists | ZH: 代码中已有埋点
    missing = "missing"      # EN: should exist but does not | ZH: 应有但缺失
    partial = "partial"      # EN: partially instrumented | ZH: 部分埋点


class SamplingStrategy(str, Enum):
    none = "none"
    head = "head"
    tail = "tail"


class Source(BaseModel):
    """EN: Where in the code a metric was discovered (traceable).
    ZH: 指标在代码中的发现位置（可溯源）。"""

    file: str
    symbol: Optional[str] = None
    line: Optional[int] = None
    framework: Optional[str] = None


class Sampling(BaseModel):
    required: bool = False
    strategy: SamplingStrategy = SamplingStrategy.none
    rate: float = 1.0


class MetricDescriptor(BaseModel):
    """EN: A single, platform-agnostic metric description.
    ZH: 单个、平台无关的指标描述。"""

    id: str = Field(..., description="Globally unique, e.g. api.request.duration")
    kind: MetricKind
    unit: str = ""
    description: str = ""
    source: Source
    dimensions: list[str] = Field(default_factory=list)
    category: str = ""                 # methodology rule matched, e.g. RED.Duration
    signal: Optional[Signal] = None
    status: Status = Status.missing
    recommended_instrumentation: str = "otel"
    sampling: Sampling = Field(default_factory=Sampling)
    alerting_ref: Optional[str] = None

    def key(self) -> tuple[str, str]:
        """EN: Identity for dedup: same metric id at the same source location.
        ZH: 去重用的身份：相同指标 id + 相同源位置视为同一个。"""
        return (self.id, f"{self.source.file}:{self.source.symbol}")


class MetricsCatalog(BaseModel):
    """EN: The output of the Discovery engine: all candidate metrics for a repo.
    ZH: Discovery 引擎的产出：一个仓库的全部候选指标。"""

    repo: str
    metrics: list[MetricDescriptor] = Field(default_factory=list)

    def add(self, m: MetricDescriptor) -> None:
        self.metrics.append(m)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {"total": len(self.metrics)}
        for m in self.metrics:
            out[m.status.value] = out.get(m.status.value, 0) + 1
        return out
