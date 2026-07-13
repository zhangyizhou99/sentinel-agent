"""Instrumentation engine. | 埋点生成引擎。

EN: Phase 3 of the pipeline. Reads the metrics catalog, takes the metrics marked
    `missing`, and generates OpenTelemetry instrumentation code to fill the gap.
    It NEVER edits the original source — output goes to a separate review file
    (PR-style), see DESIGN section 5 Phase 3 and the "human review" principle.
ZH: 流水线第三阶段。读取指标清单，取出标记为 `missing` 的指标，生成 OpenTelemetry
    埋点代码来补齐缺口。它绝不改动原始源码——产物写到独立的评审文件（PR 式），
    参见设计文档第 5 节阶段 3 与“人工评审”原则。
"""
from __future__ import annotations

from dataclasses import dataclass

from sentinel.model.metric import MetricsCatalog, Status


@dataclass
class CodePatch:
    """EN: A generated instrumentation artifact awaiting human review.
    ZH: 一份等待人工评审的埋点产物。"""

    target_file: str          # EN: source file the metrics came from | ZH: 指标来源文件
    output_path: str          # EN: where to write the helper | ZH: 助手代码写到哪
    content: str              # EN: the generated code | ZH: 生成的代码
    summary: str              # EN: one-line description | ZH: 一句话说明


class InstrumentEngine:
    """EN: Turn `missing` metrics into OpenTelemetry instrumentation code.
    ZH: 把 `missing` 指标变成 OpenTelemetry 埋点代码。"""

    def __init__(self, provider: str = "opentelemetry", output_dir: str = ".sentinel/instrumentation"):
        self.provider = provider
        self.output_dir = output_dir

    def generate(self, catalog: MetricsCatalog) -> list[CodePatch]:
        # EN: group missing metrics by their source file.
        # ZH: 按来源文件把缺失指标分组。
        by_file: dict[str, list] = {}
        for m in catalog.metrics:
            if m.status == Status.missing and m.source.file != "<llm>":
                by_file.setdefault(m.source.file, []).append(m)

        patches: list[CodePatch] = []
        for src_file, metrics in sorted(by_file.items()):
            ids = {m.id for m in metrics}
            framework = next((m.source.framework for m in metrics if m.source.framework), None)
            content = self._otel_module(src_file, ids, framework)
            stem = src_file.replace("/", "_").rsplit(".", 1)[0]
            patches.append(
                CodePatch(
                    target_file=src_file,
                    output_path=f"{self.output_dir}/{stem}_sentinel.py",
                    content=content,
                    summary=f"{len(ids)} metric(s) for {src_file}: {', '.join(sorted(ids))}",
                )
            )
        return patches

    # -- code templates | 代码模板 -----------------------------------------

    def _otel_module(self, src_file: str, ids: set[str], framework: str | None) -> str:
        """EN: Build an OpenTelemetry helper module covering the given metric ids.
        ZH: 构建一个覆盖给定指标 id 的 OpenTelemetry 助手模块。"""
        blocks: list[str] = [self._header(src_file)]
        blocks.append(self._meter_block(ids))

        if any(i.startswith("app.cold_start") for i in ids):
            blocks.append(self._cold_start_block())
        if any(i.startswith("api.") for i in ids):
            blocks.append(self._api_block(framework))
        if any(i.startswith("dep.") for i in ids):
            blocks.append(self._dep_block())

        blocks.append(self._wiring_hint(ids, framework))
        return "\n\n".join(blocks) + "\n"

    @staticmethod
    def _header(src_file: str) -> str:
        return (
            f'"""Sentinel auto-instrumentation for {src_file} (REVIEW BEFORE MERGE).\n'
            f"Sentinel 为 {src_file} 自动生成的埋点（合并前请评审）。\n\n"
            "EN: This file is generated. Import the helpers below into the target\n"
            "    module and wire them where indicated. Do not commit blindly.\n"
            "ZH: 本文件为自动生成。把下面的助手导入目标模块并按提示接线。\n"
            '    请勿盲目提交。\n"""\n'
            "import time\n"
            "from opentelemetry import metrics"
        )

    @staticmethod
    def _meter_block(ids: set[str]) -> str:
        lines = ['_meter = metrics.get_meter("sentinel")', ""]
        # EN: create one instrument per metric id. | ZH: 每个指标 id 建一个 instrument。
        if any(i.startswith("app.cold_start") for i in ids):
            lines.append('_cold_start = _meter.create_histogram("app.cold_start", unit="ms")')
        if "api.request.duration" in ids:
            lines.append('_api_latency = _meter.create_histogram("api.request.duration", unit="ms")')
        if "api.errors" in ids:
            lines.append('_api_errors = _meter.create_counter("api.errors")')
        if "dep.call.duration" in ids:
            lines.append('_dep_latency = _meter.create_histogram("dep.call.duration", unit="ms")')
        if "dep.errors" in ids:
            lines.append('_dep_errors = _meter.create_counter("dep.errors")')
        return "\n".join(lines)

    @staticmethod
    def _cold_start_block() -> str:
        return (
            "# EN: call record_cold_start(ms) once, right after startup completes.\n"
            "# ZH: 启动完成后调用一次 record_cold_start(ms)。\n"
            "def record_cold_start(duration_ms: float) -> None:\n"
            '    _cold_start.record(duration_ms, {"phase": "cold"})'
        )

    @staticmethod
    def _api_block(framework: str | None) -> str:
        if framework == "fastapi":
            return (
                "# EN: FastAPI middleware — records latency + error count per request.\n"
                "# ZH: FastAPI 中间件 —— 逐请求记录时延与错误数。\n"
                "async def sentinel_middleware(request, call_next):\n"
                "    start = time.perf_counter()\n"
                "    try:\n"
                "        response = await call_next(request)\n"
                "        return response\n"
                "    except Exception:\n"
                '        _api_errors.add(1, {"route": request.url.path})\n'
                "        raise\n"
                "    finally:\n"
                "        _api_latency.record(\n"
                "            (time.perf_counter() - start) * 1000,\n"
                '            {"route": request.url.path, "method": request.method},\n'
                "        )"
            )
        # EN: generic fallback decorator for non-FastAPI handlers.
        # ZH: 非 FastAPI 处理器的通用兜底装饰器。
        return (
            "# EN: generic decorator — wrap a request handler to record RED metrics.\n"
            "# ZH: 通用装饰器 —— 包裹处理器以记录 RED 指标。\n"
            "def instrument_endpoint(route: str, method: str = 'GET'):\n"
            "    def deco(fn):\n"
            "        def wrapper(*args, **kwargs):\n"
            "            start = time.perf_counter()\n"
            "            try:\n"
            "                return fn(*args, **kwargs)\n"
            "            except Exception:\n"
            '                _api_errors.add(1, {"route": route})\n'
            "                raise\n"
            "            finally:\n"
            '                _api_latency.record((time.perf_counter()-start)*1000, {"route": route, "method": method})\n'
            "        return wrapper\n"
            "    return deco"
        )

    @staticmethod
    def _dep_block() -> str:
        return (
            "# EN: wrap an external dependency call to record latency + errors.\n"
            "# ZH: 包裹外部依赖调用以记录时延与错误。\n"
            "def instrument_dep(dep: str, operation: str = ''):\n"
            "    def deco(fn):\n"
            "        def wrapper(*args, **kwargs):\n"
            "            start = time.perf_counter()\n"
            "            try:\n"
            "                return fn(*args, **kwargs)\n"
            "            except Exception:\n"
            '                _dep_errors.add(1, {"dep": dep, "operation": operation})\n'
            "                raise\n"
            "            finally:\n"
            '                _dep_latency.record((time.perf_counter()-start)*1000, {"dep": dep, "operation": operation})\n'
            "        return wrapper\n"
            "    return deco"
        )

    @staticmethod
    def _wiring_hint(ids: set[str], framework: str | None) -> str:
        hints = ["# --- Wiring hints | 接线提示 ---"]
        if any(i.startswith("api.") for i in ids) and framework == "fastapi":
            hints.append('# app.middleware("http")(sentinel_middleware)')
        if any(i.startswith("app.cold_start") for i in ids):
            hints.append("# record_cold_start((time.perf_counter() - _boot) * 1000)")
        if any(i.startswith("dep.") for i in ids):
            hints.append("# cache.get = instrument_dep('redis')(cache.get)")
        return "\n".join(hints)
