"""Sentinel MCP server (from scratch, zero-dependency). | Sentinel MCP 服务器（从零，零依赖）。

EN: Exposes Sentinel over the Model Context Protocol so ANY MCP client
    (Claude Desktop, Cursor, VS Code) can orchestrate it — "set up monitoring for
    this repo" becomes a sequence of tool calls the LLM drives. The MCP stdio
    transport is just newline-delimited JSON-RPC 2.0, so we implement it directly
    with the standard library (no SDK, runs on Python 3.9+).

    Tools (verbs): discover, instrument, query, alerts, export, dashboard,
                   feedback, deploy_alerts (external), apply (destructive)
    Resources (read-only context):
      sentinel://catalog/{repo}          - discovered metric catalog
      sentinel://alert-state/{repo}      - designed alert policies
      sentinel://memory/{repo}           - runs + feedback + deploy ledger
      sentinel://knowledge/observability - RED/USE knowledge base

    Run (stdio):  python -m sentinel.mcp_server
ZH: 通过 Model Context Protocol 暴露 Sentinel，让任意 MCP 客户端（Claude Desktop /
    Cursor / VS Code）编排它。MCP 的 stdio 传输就是换行分隔的 JSON-RPC 2.0，所以我们
    用标准库直接实现（不依赖 SDK，Python 3.9+ 可跑）。工具=动词，资源=只读上下文。
    启动（stdio）：python -m sentinel.mcp_server
"""
from __future__ import annotations

import inspect
import json
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List

from sentinel.adapters.scanners.python_scanner import PythonScanner
from sentinel.engines.alerting import AlertingDesigner
from sentinel.engines.discovery import DiscoveryEngine
from sentinel.engines.instrument import InstrumentEngine

PROTOCOL_VERSION = "2024-11-05"

# -- tiny registries | 小型注册表 -------------------------------------------

_TOOLS: Dict[str, dict] = {}
_TEMPLATES: List[dict] = []          # {prefix, uriTemplate, name, description, func}
_STATIC_RES: Dict[str, dict] = {}    # uri -> {name, description, func}

# EN: map both real types and their names (PEP 563 makes annotations strings).
# ZH: 同时映射真实类型与其名字（PEP 563 让注解变成字符串）。
_TYPES = {
    str: "string", bool: "boolean", int: "integer", float: "number",
    "str": "string", "bool": "boolean", "int": "integer", "float": "number",
}


def tool(fn: Callable) -> Callable:
    """EN: Register a function as an MCP tool (schema inferred from its signature).
    ZH: 把函数注册为 MCP 工具（从签名推断输入 schema）。"""
    sig = inspect.signature(fn)
    props, required = {}, []
    for name, p in sig.parameters.items():
        props[name] = {"type": _TYPES.get(p.annotation, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    _TOOLS[fn.__name__] = {
        "func": fn,
        "description": (fn.__doc__ or "").strip(),
        "inputSchema": {"type": "object", "properties": props, "required": required},
    }
    return fn


def resource(uri: str) -> Callable:
    """EN: Register a resource. `{...}` in the uri => a template (fn takes one str).
    ZH: 注册资源。uri 含 `{...}` => 模板（函数收一个 str 参数）。"""
    def deco(fn: Callable) -> Callable:
        if "{" in uri:
            prefix = uri.split("{", 1)[0]     # EN: match by fixed prefix (paths have '/')
            _TEMPLATES.append({"prefix": prefix, "uriTemplate": uri, "name": fn.__name__,
                               "description": (fn.__doc__ or "").strip(), "func": fn})
        else:
            _STATIC_RES[uri] = {"name": fn.__name__,
                                "description": (fn.__doc__ or "").strip(), "func": fn}
        return fn
    return deco


# -- helpers | 辅助 ---------------------------------------------------------

def _catalog(repo_path: str):
    repo = Path(repo_path)
    if not repo.exists():
        raise FileNotFoundError(f"repo not found | 仓库不存在: {repo}")
    return DiscoveryEngine(scanners=[PythonScanner(cache=None)]).run(repo)


def _grafana_env(repo_path: str):
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(repo_path) / ".env")
    except ImportError:
        pass
    return os.getenv("GRAFANA_URL", ""), os.getenv("GRAFANA_TOKEN", "")


# ===================== Tools (verbs) | 工具（动词） =====================

@tool
def discover(repo_path: str) -> dict:
    """Scan a repository and return the observability metrics catalog (read-only).
    扫描仓库，返回可观测性指标清单（只读）。"""
    catalog = _catalog(repo_path)
    return {
        "repo": catalog.repo,
        "summary": catalog.summary(),
        "metrics": [
            {"id": m.id, "signal": m.signal.value if m.signal else None,
             "status": m.status.value, "source": f"{m.source.file}:{m.source.symbol}"}
            for m in catalog.metrics
        ],
    }


@tool
def instrument(repo_path: str) -> dict:
    """Generate OpenTelemetry instrumentation for MISSING metrics (read-only preview).
    为缺失指标生成埋点（只预览，不改文件）。"""
    patches = InstrumentEngine().generate(_catalog(repo_path))
    return {"patches": [{"path": p.output_path, "content": p.content} for p in patches]}


@tool
def query(repo_path: str, backend: str = "prometheus") -> dict:
    """Generate backend queries (prometheus|kusto) for every metric. 生成后端查询。"""
    from sentinel.adapters.backends.kusto import KustoBackend
    from sentinel.adapters.backends.prometheus import PrometheusBackend
    from sentinel.engines.query_builder import QueryBuilder
    be = KustoBackend() if backend == "kusto" else PrometheusBackend()
    qs = QueryBuilder(be).build(_catalog(repo_path))
    return {"backend": backend,
            "queries": [{"metric_id": q.metric_id, "query": q.query} for q in qs]}


@tool
def alerts(repo_path: str) -> dict:
    """Design multi-severity alert policies (thresholds are suggestions for review).
    设计多级告警策略（阈值为建议，待人审）。"""
    policies = AlertingDesigner().design(_catalog(repo_path))
    return {"policies": [
        {"metric_id": p.metric_id,
         "rules": [{"condition": r.condition, "severity": r.severity.value}
                   for r in p.rules]}
        for p in policies
    ]}


@tool
def export(repo_path: str, out: str = "", backend: str = "prometheus") -> dict:
    """Write feature-grouped observability-as-code files into the project
    (<repo>/.sentinel/ by default). 把按 feature 分组的可观测性文件写进项目。"""
    from sentinel.engines.export import ObservabilityExporter
    out_dir = out or str(Path(repo_path) / ".sentinel")
    res = ObservabilityExporter(backend=backend).export(_catalog(repo_path), out_dir)
    return {"out_dir": res.out_dir, "features": res.features, "files": res.total_files}


@tool
def dashboard(repo_path: str, deploy: bool = False) -> dict:
    """Generate a Grafana dashboard. deploy=False returns panel count; deploy=True
    pushes it to Grafana (reads GRAFANA_URL/TOKEN from repo .env). 生成/部署仪表盘。"""
    from sentinel.adapters.backends.grafana_dashboard import build_dashboard
    catalog = _catalog(repo_path)
    if not deploy:
        db = build_dashboard(catalog, "prometheus")
        return {"deployed": False,
                "panels": sum(1 for p in db["panels"] if p["type"] == "timeseries")}
    base_url, token = _grafana_env(repo_path)
    if not base_url or not token:
        return {"error": "missing GRAFANA_URL / GRAFANA_TOKEN in repo .env"}
    from sentinel.adapters.backends.grafana import GrafanaAlertingClient, GrafanaError
    client = GrafanaAlertingClient(base_url, token)
    try:
        prom_uid = client.prometheus_datasource_uid()
        folder_uid = client.ensure_folder("Sentinel")
        resp = client.create_dashboard(build_dashboard(catalog, prom_uid), folder_uid)
    except GrafanaError as e:
        return {"error": str(e)}
    return {"deployed": True, "url": base_url.rstrip("/") + resp.get("url", "")}


@tool
def feedback(repo_path: str, metric_id: str, verdict: str, reason: str = "") -> dict:
    """Record approve/reject on a metric so future discovery learns from it.
    记录对某指标的批/拒。verdict = approve | reject。"""
    from sentinel.memory.episodic import EpisodicMemory
    from sentinel.paths import episodic_db_path
    mem = EpisodicMemory(episodic_db_path(repo_path))
    mem.record_feedback(metric_id, verdict, reason)
    stats = mem.stats()
    mem.close()
    return {"recorded": {metric_id: verdict}, "stats": stats}


@tool
def deploy_alerts(repo_path: str, contact_point: str, prune: bool = False) -> dict:
    """Deploy Grafana-managed alert rules wired to a contact point (modifies an
    EXTERNAL system). prune=True lists obsolete rules WITHOUT deleting (dry-run).
    把告警规则部署到 Grafana 并绑定联络点。prune 仅列废弃、不删（dry-run）。"""
    base_url, token = _grafana_env(repo_path)
    if not base_url or not token:
        return {"error": "missing GRAFANA_URL / GRAFANA_TOKEN in repo .env"}
    from sentinel.adapters.backends.grafana import (
        GrafanaAlertingClient, GrafanaError, build_grafana_rules,
    )
    policies = AlertingDesigner().design(_catalog(repo_path))
    client = GrafanaAlertingClient(base_url, token)
    try:
        if not client.contact_point_exists(contact_point):
            return {"error": f"contact point not found: {contact_point}"}
        prom_uid = client.prometheus_datasource_uid()
        folder_uid = client.ensure_folder("Sentinel")
        existing = client.existing_rule_titles()
        created, skipped = [], 0
        for p in policies:
            for rule in build_grafana_rules(p, prom_uid, folder_uid, contact_point):
                if rule["title"] in existing:
                    skipped += 1
                else:
                    client.create_alert_rule(rule)
                    created.append(rule["title"])
        stale = []
        if prune:
            current = {p.metric_id for p in policies}
            stale = [r["title"] for r in client.list_sentinel_rules()
                     if r.get("metric") and r["metric"] not in current]
    except GrafanaError as e:
        return {"error": str(e)}
    return {"created": created, "skipped": skipped,
            "obsolete_dry_run": stale, "contact_point": contact_point}


@tool
def apply(repo_path: str, branch: str) -> dict:
    """DESTRUCTIVE: write instrumentation into the source and commit to a NEW git
    branch (you name it). Review the diff before merging. 破坏性：把埋点写进源码并
    提交到你命名的新 git 分支，合并前请评审 diff。"""
    from sentinel.engines.apply import Applier, ApplyError
    if not (branch or "").strip():
        return {"error": "branch name required"}
    try:
        res = Applier().apply(repo_path, _catalog(repo_path), branch=branch.strip())
    except ApplyError as e:
        return {"error": str(e)}
    return {"branch": branch, "message": res.message, "diff": res.diff}


# ================== Resources (read-only context) | 资源 ==================

@resource("sentinel://catalog/{repo}")
def res_catalog(repo: str) -> str:
    """The discovered metrics catalog for a repo. 某仓库发现的指标清单。"""
    return json.dumps(_catalog(repo).model_dump(mode="json"), ensure_ascii=False, indent=2)


@resource("sentinel://alert-state/{repo}")
def res_alert_state(repo: str) -> str:
    """The designed alert policies for a repo. 某仓库设计好的告警策略。"""
    policies = AlertingDesigner().design(_catalog(repo))
    return json.dumps([p.model_dump(mode="json") for p in policies],
                      ensure_ascii=False, indent=2)


@resource("sentinel://memory/{repo}")
def res_memory(repo: str) -> str:
    """Episodic memory: runs, feedback verdicts, deployment ledger. 情景记忆。"""
    from sentinel.memory.episodic import EpisodicMemory
    from sentinel.paths import episodic_db_path
    mem = EpisodicMemory(episodic_db_path(repo))
    out = {"stats": mem.stats(), "verdicts": mem.latest_verdicts(),
           "recent_runs": mem.recent_runs(), "recent_deployments": mem.recent_deployments()}
    mem.close()
    return json.dumps(out, ensure_ascii=False, indent=2)


@resource("sentinel://knowledge/observability")
def res_knowledge() -> str:
    """The RED/USE observability pattern knowledge base. 可观测性模式知识库。"""
    from sentinel.memory.semantic import SemanticMemory
    return json.dumps([p.__dict__ for p in SemanticMemory().all_patterns()],
                      ensure_ascii=False, indent=2)


# -- JSON-RPC dispatch | JSON-RPC 分发 --------------------------------------

def _handle(method: str, params: dict) -> dict:
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "sentinel", "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": t["description"],
                           "inputSchema": t["inputSchema"]} for n, t in _TOOLS.items()]}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        entry = _TOOLS.get(name)
        if not entry:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
                    "isError": True}
        try:
            result = entry["func"](**args)
            return {"content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False, indent=2)}]}
        except Exception as e:      # EN: surface as tool error | ZH: 作为 tool 错误返回
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True}
    if method == "resources/list":
        return {"resources": [{"uri": u, "name": r["name"], "description": r["description"],
                               "mimeType": "application/json"}
                              for u, r in _STATIC_RES.items()]}
    if method == "resources/templates/list":
        return {"resourceTemplates": [{"uriTemplate": t["uriTemplate"], "name": t["name"],
                                       "description": t["description"],
                                       "mimeType": "application/json"} for t in _TEMPLATES]}
    if method == "resources/read":
        uri = params.get("uri", "")
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": _read_resource(uri)}]}
    raise ValueError(f"method not found: {method}")


def _read_resource(uri: str) -> str:
    if uri in _STATIC_RES:
        return _STATIC_RES[uri]["func"]()
    for t in _TEMPLATES:
        if uri.startswith(t["prefix"]):
            return t["func"](uri[len(t["prefix"]):])
    raise ValueError(f"resource not found: {uri}")


def main() -> None:
    """EN: stdio JSON-RPC loop — one JSON message per line. | ZH: stdio JSON-RPC 循环。"""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if mid is None:             # EN: a notification — no response | ZH: 通知，不回复
            continue
        try:
            resp = {"jsonrpc": "2.0", "id": mid, "result": _handle(method, params)}
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": str(e)}}
        out.write(json.dumps(resp, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
