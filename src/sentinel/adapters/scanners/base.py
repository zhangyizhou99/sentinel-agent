"""Scanner adapter contract.

EN: Every language gets its own CodeScanner implementation. Discovery only
    depends on this interface, never on a concrete language parser.
ZH: 每种语言一个 CodeScanner 实现。Discovery 只依赖这个接口，不依赖具体语言
    解析器。参见设计文档第 3/6 节。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from sentinel.model.metric import MetricDescriptor


class CodeScanner(Protocol):
    """EN: Static, LLM-free discovery of candidate metrics from source code.
    ZH: 静态、不调 LLM 地从源码中发现候选指标。"""

    name: str

    def matches(self, root: Path) -> bool:
        """EN: Return True if this scanner can handle the given repo.
        ZH: 若本扫描器能处理该仓库则返回 True。"""
        ...

    def scan(self, root: Path) -> list[MetricDescriptor]:
        """EN: Statically extract candidate metrics.
        ZH: 静态抽取候选指标。"""
        ...
