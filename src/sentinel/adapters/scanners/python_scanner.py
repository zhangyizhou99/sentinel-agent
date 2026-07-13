"""Python static scanner. | Python 静态扫描器。

EN: Uses the stdlib `ast` module to find "points worth monitoring" without ever
    running the code or calling an LLM (air-gapped safe). It detects:
ZH: 用标准库 `ast` 模块找出“值得监控的点”，全程不运行代码、不调 LLM
    （air-gapped 安全）。它识别：

- HTTP routes (FastAPI / Flask)     -> api.request.duration + api.errors
- App entry points | 应用入口     -> app.cold_start
- External dependency calls | 外部依赖调用 -> dep.call.duration + dep.errors
- Existing instrumentation | 已有埋点 -> marks metrics as `present`

See DESIGN section 5, Phase 1. | 参见设计文档第 5 节阶段 1。
"""
from __future__ import annotations

import ast
import fnmatch
import os
from pathlib import Path
from typing import Optional

from sentinel.adapters.scanners.cache import ScanCache
from sentinel.model.metric import (
    MetricDescriptor,
    MetricKind,
    Sampling,
    SamplingStrategy,
    Signal,
    Source,
    Status,
)

# EN: Root identifiers commonly used to declare routes.
# ZH: 声明路由时常用的根标识符。
_ROUTE_OBJS = {"app", "router", "api", "blueprint", "bp"}
_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "route"}

# EN: Map a dependency library root name -> a coarse dependency category.
# ZH: 依赖库根名 -> 粗粒度依赖类别 的映射。
_DEP_LIBS: dict[str, str] = {
    "requests": "http",
    "httpx": "http",
    "aiohttp": "http",
    "urllib": "http",
    "redis": "cache",
    "aioredis": "cache",
    "memcache": "cache",
    "psycopg2": "db",
    "psycopg": "db",
    "pymysql": "db",
    "sqlite3": "db",
    "sqlalchemy": "db",
    "asyncpg": "db",
    "boto3": "aws",
    "pika": "queue",
    "kafka": "queue",
    "confluent_kafka": "queue",
}

# EN: Import roots that indicate the code already has instrumentation.
# ZH: 表明代码已有埋点的导入根名。
_INSTRUMENTATION_HINTS = {"opentelemetry", "prometheus_client", "statsd", "datadog"}

# EN: Directory names skipped by default (dependencies, build output, VCS, ...).
# ZH: 默认跳过的目录名（依赖、构建产物、版本控制 …）。
_DEFAULT_EXCLUDE_DIRS = {
    "tests", "test", ".venv", "venv", "__pycache__", "migrations",
    "node_modules", "dist", "build", ".git", "site-packages", "vendor",
    ".tox", ".mypy_cache", ".pytest_cache", ".eggs",
}


class PythonScanner:
    name = "python"

    def __init__(
        self,
        exclude_dirs: Optional[set[str]] = None,
        exclude_globs: Optional[list[str]] = None,
        cache: Optional[ScanCache] = None,
    ):
        # EN: directory names to skip (exact match on any path part).
        # ZH: 要跳过的目录名（对任意路径段做精确匹配）。
        self.exclude_dirs = exclude_dirs if exclude_dirs is not None else set(_DEFAULT_EXCLUDE_DIRS)
        # EN: extra fnmatch patterns matched against the relative posix path.
        # ZH: 额外的 fnmatch 模式，匹配相对 posix 路径。
        self.exclude_globs = exclude_globs or []
        # EN: optional file-hash cache to skip unchanged files.
        # ZH: 可选的文件哈希缓存，跳过未变更的文件。
        self.cache = cache

    def matches(self, root: Path) -> bool:
        root = Path(root)
        if root.is_file():
            return root.suffix == ".py"
        return any(root.rglob("*.py"))

    def scan(self, root: Path, only: Optional[list[Path]] = None) -> list[MetricDescriptor]:
        # EN: `root` may be a directory OR a single .py file; `only` restricts
        #     scanning to an explicit file list (used by upload / git-incremental).
        # ZH: `root` 可以是目录，也可以是单个 .py 文件；`only` 把扫描限定到显式文件
        #     列表（用于上传 / git 增量）。
        root = Path(root)
        base, files = self._resolve_targets(root, only)
        metrics: list[MetricDescriptor] = []
        for path in sorted(files):
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                rel = path.name
            if self._excluded(path, rel):
                continue
            metrics.extend(self._scan_one(path, rel))
        if self.cache is not None:
            self.cache.save()
        return metrics

    def _resolve_targets(self, root: Path, only: Optional[list[Path]]) -> tuple[Path, list[Path]]:
        # EN: figure out the base dir (for relative paths) and the file set.
        # ZH: 算出基准目录（用于相对路径）与文件集合。
        if only is not None:
            files = [Path(p) for p in only if Path(p).suffix == ".py" and Path(p).exists()]
            if root.is_dir():
                base = root
            elif files:
                base = Path(os.path.commonpath([str(f.parent) for f in files]))
            else:
                base = root
            return base, files
        if root.is_file():
            return root.parent, [root]
        return root, list(root.rglob("*.py"))

    def _scan_one(self, path: Path, rel: str) -> list[MetricDescriptor]:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        # EN: fast path — reuse cached metrics if the file is unchanged.
        # ZH: 快路径 —— 文件未变则复用缓存指标。
        if self.cache is not None:
            cached = self.cache.get(rel, content)
            if cached is not None:
                return cached
        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError:
            return []
        file_metrics = self._scan_module(tree, rel)
        if self.cache is not None:
            self.cache.put(rel, content, file_metrics)
        return file_metrics

    # -- internals | 内部实现 ------------------------------------------

    def _excluded(self, path: Path, rel: str) -> bool:
        # EN: 1) skip if any path segment is an excluded directory name.
        # ZH: 1) 路径中任一段命中排除目录名则跳过。
        if set(path.parts) & self.exclude_dirs:
            return True
        # EN: 2) skip if the relative path matches any custom glob.
        # ZH: 2) 相对路径命中任一自定义 glob 则跳过。
        return any(fnmatch.fnmatch(rel, pat) for pat in self.exclude_globs)

    def _scan_module(self, tree: ast.Module, rel: str) -> list[MetricDescriptor]:
        imports = self._collect_imports(tree)
        framework = self._detect_framework(imports)
        # EN: "instrumented" = direct OTel/Prometheus/... OR a Sentinel helper
        #     (our Apply injects `from <x>_sentinel import ...`) — so re-scans
        #     recognize code we already instrumented. Fixes gate ① coverage.
        # ZH: “已埋点” = 直接用 OTel/Prometheus/… 或 引入了 Sentinel 助手
        #     （我们 Apply 会插 `from <x>_sentinel import ...`）——这样重扫能认出
        #     自己补过的代码。修复闸门① 覆盖度。
        has_instrumentation = bool(imports & _INSTRUMENTATION_HINTS) or any(
            name.endswith("_sentinel") for name in imports
        )
        out: list[MetricDescriptor] = []

        # EN: Entry point -> cold start. | ZH: 入口点 -> 冷启动。
        if self._has_entrypoint(tree, imports):
            out.append(self._cold_start(rel, framework, has_instrumentation))

        # EN: Routes + dependency calls per function.
        # ZH: 逐函数提取路由与依赖调用。
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                route = self._route_of(node)
                if route is not None:
                    out.extend(self._api_metrics(rel, node.name, node.lineno,
                                                 framework, has_instrumentation))
                out.extend(self._dep_metrics(node, rel, imports, has_instrumentation))
        return out

    @staticmethod
    def _collect_imports(tree: ast.Module) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    @staticmethod
    def _detect_framework(imports: set[str]) -> str | None:
        for fw in ("fastapi", "flask", "django", "starlette", "aiohttp"):
            if fw in imports:
                return fw
        return None

    @staticmethod
    def _has_entrypoint(tree: ast.Module, imports: set[str]) -> bool:
        if {"fastapi", "flask", "starlette"} & imports:
            return True
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                root = _root_name(node.func)
                if root in {"uvicorn", "gunicorn"}:
                    return True
        return False

    @staticmethod
    def _route_of(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
        for dec in func.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            target = call.func if call else dec
            if isinstance(target, ast.Attribute) and target.attr in _HTTP_METHODS:
                base = _root_name(target.value)
                if base in _ROUTE_OBJS:
                    return target.attr
        return None

    def _dep_metrics(self, func: ast.AST, rel: str, imports: set[str],
                     has_instr: bool) -> list[MetricDescriptor]:
        seen: set[str] = set()
        out: list[MetricDescriptor] = []
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                root = _root_name(node.func)
                if root in _DEP_LIBS and root in imports and root not in seen:
                    seen.add(root)
                    out.extend(self._dep_pair(rel, root, _DEP_LIBS[root],
                                              node.lineno, has_instr))
        return out

    # -- metric factories | 指标工厂方法 -----------------------------------

    @staticmethod
    def _status(has_instr: bool) -> Status:
        return Status.present if has_instr else Status.missing

    def _cold_start(self, rel: str, fw: str | None, has_instr: bool) -> MetricDescriptor:
        return MetricDescriptor(
            id="app.cold_start",
            kind=MetricKind.histogram,
            unit="ms",
            description="Application cold start duration",
            source=Source(file=rel, symbol="<entrypoint>", framework=fw),
            dimensions=["phase"],
            category="Startup",
            signal=Signal.latency,
            status=self._status(has_instr),
            alerting_ref="app.cold_start.slo",
        )

    def _api_metrics(self, rel: str, symbol: str, line: int, fw: str | None,
                     has_instr: bool) -> list[MetricDescriptor]:
        status = self._status(has_instr)
        src = Source(file=rel, symbol=symbol, line=line, framework=fw)
        return [
            MetricDescriptor(
                id="api.request.duration",
                kind=MetricKind.histogram,
                unit="ms",
                description=f"HTTP request latency for {symbol}",
                source=src,
                dimensions=["route", "method", "status_code"],
                category="RED.Duration",
                signal=Signal.latency,
                status=status,
                sampling=Sampling(required=True, strategy=SamplingStrategy.tail, rate=0.1),
                alerting_ref="api.request.duration.slo",
            ),
            MetricDescriptor(
                id="api.errors",
                kind=MetricKind.counter,
                description=f"HTTP error count for {symbol}",
                source=src,
                dimensions=["route", "method", "status_code"],
                category="RED.Errors",
                signal=Signal.errors,
                status=status,
                alerting_ref="api.errors.slo",
            ),
        ]

    def _dep_pair(self, rel: str, dep: str, category: str, line: int,
                  has_instr: bool) -> list[MetricDescriptor]:
        status = self._status(has_instr)
        src = Source(file=rel, symbol=f"call:{dep}", line=line)
        return [
            MetricDescriptor(
                id="dep.call.duration",
                kind=MetricKind.histogram,
                unit="ms",
                description=f"Latency of calls to {dep} ({category})",
                source=src,
                dimensions=["dep", "operation"],
                category="RED.Duration",
                signal=Signal.latency,
                status=status,
                sampling=Sampling(required=True, strategy=SamplingStrategy.tail, rate=0.1),
            ),
            MetricDescriptor(
                id="dep.errors",
                kind=MetricKind.counter,
                description=f"Error count of calls to {dep} ({category})",
                source=src,
                dimensions=["dep", "operation"],
                category="RED.Errors",
                signal=Signal.errors,
                status=status,
            ),
        ]


def _root_name(node: ast.AST) -> str | None:
    """EN: Return the leftmost identifier of an attribute/name chain.
        e.g. `requests.get` -> "requests", `self.app.get` -> "self".
    ZH: 返回属性/名称链的最左侧标识符。
        例如 `requests.get` -> "requests"，`self.app.get` -> "self"。
    """
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _root_name(node.func)
    return None
