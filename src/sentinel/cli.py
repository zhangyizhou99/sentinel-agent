"""Sentinel command-line interface. | Sentinel 命令行入口。

EN: A thin Rich-powered CLI over the Discovery engine. It renders the metrics
    catalog as a colored table and can optionally write the raw JSON to a file.
ZH: 一个基于 Rich、包在 Discovery 引擎外的轻量 CLI。它把指标清单渲染成彩色表格，
    并可选地把原始 JSON 写入文件。

Usage | 用法:
    python -m sentinel.cli discover <repo_path> [-o catalog.json]
        [--provider modelscope|deepseek|claude|...] [--privacy air-gapped|private-llm|external-llm]
        [--lang en|zh] [--no-cache]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from sentinel.adapters.scanners.cache import ScanCache
from sentinel.adapters.scanners.python_scanner import PythonScanner
from sentinel.adapters.backends.kusto import KustoBackend
from sentinel.adapters.backends.prometheus import PrometheusBackend, to_prometheus_yaml
from sentinel.engines.apply import Applier, ApplyError
from sentinel.engines.alerting import AlertingDesigner
from sentinel.engines.discovery import DiscoveryEngine
from sentinel.engines.instrument import InstrumentEngine
from sentinel.engines.query_builder import QueryBuilder
from sentinel.llm.client import PROVIDERS, LLMClient, LLMConfig, PrivacyMode
from sentinel.model.alert import Severity
from sentinel.paths import scan_cache_path
from sentinel.model.metric import MetricsCatalog, Status

console = Console()

# EN: map a metric status to a display color. | ZH: 指标状态 -> 显示颜色。
_STATUS_COLOR = {
    Status.missing: "yellow",
    Status.present: "green",
    Status.partial: "cyan",
}


def _build_llm(provider: str, privacy: str) -> LLMClient:
    """EN: Build the (optional) LLM client from CLI flags.
    ZH: 根据命令行参数构建（可选的）LLM 客户端。"""
    return LLMClient(LLMConfig(provider=provider, privacy_mode=PrivacyMode(privacy)))


def _render(catalog: MetricsCatalog, llm: LLMClient) -> None:
    """EN: Print the catalog as a colored table + a summary panel.
    ZH: 把清单打印成彩色表格 + 一个统计面板。"""
    table = Table(title="Metrics Catalog | 指标清单", show_lines=False)
    table.add_column("Status | 状态", no_wrap=True)
    table.add_column("Metric ID | 指标", style="bold")
    table.add_column("Signal | 信号")
    table.add_column("Category | 分类")
    table.add_column("Source | 来源")
    table.add_column("Sampling | 采样")

    for m in catalog.metrics:
        color = _STATUS_COLOR.get(m.status, "white")
        loc = f"{m.source.file}:{m.source.symbol}"
        if m.source.line:
            loc += f":{m.source.line}"
        sampling = m.sampling.strategy.value if m.sampling.required else "-"
        table.add_row(
            f"[{color}]{m.status.value}[/{color}]",
            m.id,
            m.signal.value if m.signal else "-",
            m.category or "-",
            loc,
            sampling,
        )

    console.print(table)

    # EN: summary + which mode actually ran. | ZH: 统计 + 实际运行的模式。
    s = catalog.summary()
    llm_line = (
        "[green]LLM augmentation: ON[/green]"
        if llm.available
        else f"[dim]LLM augmentation: OFF ({llm.why_unavailable()})[/dim]"
    )
    console.print(
        Panel(
            f"repo: {catalog.repo}\n"
            f"total: {s.get('total', 0)}   "
            f"[yellow]missing: {s.get('missing', 0)}[/yellow]   "
            f"[green]present: {s.get('present', 0)}[/green]\n"
            f"{llm_line}",
            title="Summary | 概览",
            expand=False,
        )
    )


def cmd_discover(args: argparse.Namespace) -> int:
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1

    # EN: wire the scanner with an optional on-disk cache.
    # ZH: 给扫描器接上可选的磁盘缓存。
    cache = None if args.no_cache else ScanCache(scan_cache_path(repo))
    llm = _build_llm(args.provider, args.privacy)
    engine = DiscoveryEngine(scanners=[PythonScanner(cache=cache)], llm=llm, lang=args.lang)

    catalog = engine.run(repo)
    _render(catalog, llm)

    if args.output:
        Path(args.output).write_text(
            json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"[green]written | 已写入:[/green] {args.output}")
    return 0


def cmd_instrument(args: argparse.Namespace) -> int:
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1

    # EN: discover statically, then generate instrumentation for missing metrics.
    # ZH: 先静态发现，再为缺失指标生成埋点。
    cache = None if args.no_cache else ScanCache(scan_cache_path(repo))
    catalog = DiscoveryEngine(scanners=[PythonScanner(cache=cache)]).run(repo)
    patches = InstrumentEngine().generate(catalog)

    if not patches:
        console.print("[green]no missing metrics — nothing to instrument | 无缺失指标，无需埋点[/green]")
        return 0

    table = Table(title="Instrumentation patches | 埋点补丁（待评审）")
    table.add_column("Target | 目标文件", style="bold")
    table.add_column("Output | 输出文件", style="cyan")
    table.add_column("Metrics | 指标")
    for p in patches:
        Path(p.output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(p.output_path).write_text(p.content, encoding="utf-8")
        table.add_row(p.target_file, p.output_path, p.summary)
    console.print(table)
    console.print(
        Panel(
            "[yellow]Review before merge | 合并前请评审[/yellow]\n"
            "EN: files were written to a separate folder; your source is untouched.\n"
            "ZH: 文件已写入独立目录；你的源码未被修改。",
            title="Done | 完成",
            expand=False,
        )
    )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1

    # EN: discover, then commit real edits to the user-named branch.
    # ZH: 先发现，再把真实改动提交到用户命名的分支。
    llm = _build_llm(args.provider, args.privacy)
    catalog = DiscoveryEngine(scanners=[PythonScanner(cache=None)], llm=llm, lang=args.lang).run(repo)
    try:
        res = Applier().apply(repo, catalog, branch=args.branch)
    except ApplyError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    console.print(Panel(res.message, title="Applied | 已应用", expand=False))
    if res.skipped:
        console.print(f"[dim]skipped (couldn't auto-wire) | 跳过: {', '.join(res.skipped)}[/dim]")
    if res.diff:
        console.print(Syntax(res.diff, "diff", theme="ansi_dark", word_wrap=True))
    return 0


_SEV_COLOR = {
    Severity.SEV1: "bold red",
    Severity.SEV2: "dark_orange",
    Severity.SEV3: "yellow",
    Severity.SEV4: "dim",
}


def cmd_query(args: argparse.Namespace) -> int:
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1
    catalog = DiscoveryEngine(scanners=[PythonScanner(cache=None)]).run(repo)
    backend = KustoBackend() if args.backend == "kusto" else PrometheusBackend()
    queries = QueryBuilder(backend).build(catalog, window=args.window, lookback=args.lookback)
    if not queries:
        console.print("[yellow]no metrics to query | 无指标可查询[/yellow]")
        return 0
    for q in queries:
        console.print(
            Panel(
                Syntax(q.query, "sql", theme="ansi_dark", word_wrap=True),
                title=f"{q.metric_id}  ·  {q.backend}  ·  {q.sampling_note}",
                expand=False,
            )
        )
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1
    catalog = DiscoveryEngine(scanners=[PythonScanner(cache=None)]).run(repo)
    policies = AlertingDesigner().design(catalog)
    if not policies:
        console.print("[yellow]no metrics to alert on | 无指标可告警[/yellow]")
        return 0
    table = Table(title="Alert policies | 告警策略（阈值为建议，待人审）", show_lines=True)
    table.add_column("Metric | 指标", style="bold")
    table.add_column("Condition | 触发条件")
    table.add_column("Sev", no_wrap=True)
    table.add_column("Routing | 路由")
    for p in policies:
        for r in p.rules:
            color = _SEV_COLOR.get(r.severity, "white")
            routing = ", ".join(p.routing.get(r.severity, []))
            table.add_row(p.metric_id, r.condition,
                          f"[{color}]{r.severity.value}[/{color}]", routing)
    console.print(table)

    # EN: optionally emit a real Prometheus rules file. | ZH: 可选：产出真正的 Prometheus 规则文件。
    if args.emit_prometheus:
        backend = PrometheusBackend()
        rule_dicts: list[dict] = []
        for p in policies:
            rule_dicts.extend(backend.render_alert_rule(p))
        Path(args.emit_prometheus).write_text(to_prometheus_yaml(rule_dicts), encoding="utf-8")
        console.print(
            f"[green]wrote {len(rule_dicts)} Prometheus rules | 已写入 Prometheus 规则:[/green] "
            f"{args.emit_prometheus}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="Sentinel — observability discovery CLI | 可观测性发现 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="scan a repo and list metrics | 扫描仓库并列出指标")
    d.add_argument("repo_path", help="path to the repository | 仓库路径")
    d.add_argument("-o", "--output", help="write catalog JSON to this file | 把清单 JSON 写到此文件")
    d.add_argument("--provider", default="modelscope", choices=sorted(PROVIDERS),
                   help="LLM provider (only used if privacy allows) | LLM Provider（仅隐私档允许时使用）")
    d.add_argument("--privacy", default="air-gapped",
                   choices=[m.value for m in PrivacyMode],
                   help="privacy tier | 隐私档（默认 air-gapped 纯静态）")
    d.add_argument("--lang", default="en", choices=["en", "zh"],
                   help="prompt/report language | 提示词/报告语言")
    d.add_argument("--no-cache", action="store_true", help="disable scan cache | 关闭扫描缓存")
    d.set_defaults(func=cmd_discover)

    i = sub.add_parser("instrument", help="generate instrumentation for missing metrics | 为缺失指标生成埋点")
    i.add_argument("repo_path", help="path to the repository | 仓库路径")
    i.add_argument("--no-cache", action="store_true", help="disable scan cache | 关闭扫描缓存")
    i.set_defaults(func=cmd_instrument)

    a = sub.add_parser("apply", help="commit instrumentation to a new git branch | 把埋点提交到新 git 分支")
    a.add_argument("repo_path", help="path to the repository (must be a git repo) | 仓库路径（必须是 git 仓库）")
    a.add_argument("--branch", required=True, help="branch name to create (you choose) | 要新建的分支名（你自己定）")
    a.add_argument("--provider", default="modelscope", choices=sorted(PROVIDERS),
                   help="LLM provider | LLM 接口")
    a.add_argument("--privacy", default="air-gapped", choices=[m.value for m in PrivacyMode],
                   help="privacy tier | 隐私档")
    a.add_argument("--lang", default="en", choices=["en", "zh"], help="language | 语言")
    a.set_defaults(func=cmd_apply)

    q = sub.add_parser("query", help="generate backend queries (Kusto/PromQL) | 生成后端查询")
    q.add_argument("repo_path", help="path to the repository | 仓库路径")
    q.add_argument("--backend", default="kusto", choices=["kusto", "prometheus"],
                   help="query backend | 查询后端")
    q.add_argument("--window", default="5m", help="aggregation window | 聚合窗口")
    q.add_argument("--lookback", default="1h", help="time range | 时间范围")
    q.set_defaults(func=cmd_query)

    al = sub.add_parser("alerts", help="design thresholds + Sev levels | 设计阈值+Sev分级")
    al.add_argument("repo_path", help="path to the repository | 仓库路径")
    al.add_argument("--emit-prometheus", metavar="FILE",
                    help="write a Prometheus alerting_rules.yml | 写出 Prometheus 告警规则文件")
    al.set_defaults(func=cmd_alerts)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
