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
from sentinel.adapters.backends.grafana import (
    GrafanaAlertingClient,
    GrafanaError,
    build_grafana_rules,
)
from sentinel.adapters.backends.grafana_dashboard import build_dashboard
from sentinel.engines.apply import Applier, ApplyError
from sentinel.engines.alerting import AlertingDesigner
from sentinel.engines.discovery import DiscoveryEngine
from sentinel.engines.export import ObservabilityExporter
from sentinel.evaluation import aggregate, evaluate_dir
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

    # EN: optional persistent memory: incremental index + learn from feedback.
    # ZH: 可选持久记忆：增量索引 + 从反馈学习。
    memory = None
    if getattr(args, "memory", False):
        from sentinel.memory.manager import MemoryManager
        memory = MemoryManager(repo, privacy=args.privacy)
        engine = DiscoveryEngine(
            scanners=[PythonScanner(cache=cache)], llm=llm, lang=args.lang,
            retriever=memory.retriever(),
        )
    else:
        engine = DiscoveryEngine(
            scanners=[PythonScanner(cache=cache)], llm=llm, lang=args.lang,
        )

    catalog = engine.run(repo)

    if memory is not None:
        # EN: suppress previously-rejected metrics, then log this run.
        # ZH: 先抑制历史被拒指标，再记录本次运行。
        catalog, suppressed = memory.apply_feedback(catalog)
        memory.record_run(catalog)
        memory.consolidate()
        memory.close()
        if suppressed:
            console.print(
                f"[dim]memory: suppressed {suppressed} previously-rejected "
                f"metric(s) | 记忆：抑制了 {suppressed} 个历史被拒指标[/dim]"
            )

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

    # EN: optional maintainable state: merge with saved policies (keep pinned
    #     thresholds), report the diff, persist. | ZH: 可选可维护状态：与已存策略
    #     合并（保留 pinned 阈值），报告 diff 并落盘。
    if getattr(args, "state", None):
        from sentinel.engines.alert_state import AlertPolicyStore
        store = AlertPolicyStore(args.state)
        diff = store.merge(policies)
        if getattr(args, "pin", None):
            ok = store.pin(args.pin)
            console.print(
                f"[green]pinned | 已固定:[/green] {args.pin}" if ok
                else f"[yellow]metric not found to pin | 未找到可固定的指标:[/yellow] {args.pin}"
            )
        store.save()
        policies = store.policies()
        console.print(
            f"[bold]merge | 合并:[/bold] [green]+{len(diff.added)}[/green] added  "
            f"[cyan]~{len(diff.pinned_kept)}[/cyan] pinned-kept  "
            f"[red]-{len(diff.obsolete)}[/red] obsolete  → {args.state}"
        )
        for m in diff.added:
            console.print(f"  [green]+ {m}[/green]")
        for m in diff.obsolete:
            console.print(f"  [red]- {m} (obsolete)[/red]")

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


def cmd_deploy_alerts(args: argparse.Namespace) -> int:
    """EN: L3 — push Grafana-managed alert rules straight to Grafana via its
        Provisioning API, wired to an existing contact point. No manual UI clicks.
    ZH: L3 —— 通过 Grafana Provisioning API 直接把 Grafana-managed 告警规则推上去，
        并接到已有联络点。无需手点 UI。"""
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1

    # EN: load creds from repo/.env (kept out of git), then env. | ZH: 从 repo/.env 读凭据。
    try:
        from dotenv import load_dotenv
        load_dotenv(repo / ".env")
    except ImportError:
        pass
    import os
    base_url = args.grafana_url or os.getenv("GRAFANA_URL", "")
    token = os.getenv("GRAFANA_TOKEN", "")
    if not base_url or not token:
        console.print(
            "[red]missing GRAFANA_URL / GRAFANA_TOKEN | 缺少 GRAFANA_URL / GRAFANA_TOKEN[/red]\n"
            "set them in the repo's .env (token is a secret) | 在仓库 .env 里设置（token 是密钥）"
        )
        return 1

    catalog = DiscoveryEngine(scanners=[PythonScanner(cache=None)]).run(repo)
    policies = AlertingDesigner().design(catalog)
    if not policies:
        console.print("[yellow]no metrics to alert on | 无指标可告警[/yellow]")
        return 0

    client = GrafanaAlertingClient(base_url, token)
    try:
        if not client.contact_point_exists(args.contact_point):
            console.print(
                f"[red]contact point not found | 联络点不存在:[/red] {args.contact_point}\n"
                "create it in Grafana first | 请先在 Grafana 里创建它"
            )
            return 1
        prom_uid = client.prometheus_datasource_uid()
        folder_uid = client.ensure_folder(args.folder)
        existing = client.existing_rule_titles()
    except GrafanaError as e:
        console.print(f"[red]Grafana API error | Grafana API 错误:[/red] {e}")
        return 1

    created: list[str] = []
    skipped: list[str] = []
    for p in policies:
        for rule in build_grafana_rules(p, prom_uid, folder_uid, args.contact_point):
            title = rule["title"]
            if title in existing:
                skipped.append(title)
                continue
            try:
                client.create_alert_rule(rule)
                created.append(title)
            except GrafanaError as e:
                console.print(f"[red]failed:[/red] {title} -> {e}")
    console.print(
        f"[green]deployed {len(created)} rule(s) | 已部署 {len(created)} 条规则[/green]"
        f"  ·  skipped {len(skipped)} existing | 跳过 {len(skipped)} 条已存在"
    )
    for t in created:
        console.print(f"  [green]+[/green] {t}")
    if created:
        console.print(
            f"[dim]routed to contact point | 路由到联络点: {args.contact_point}[/dim]"
        )

    # EN: reconcile/prune obsolete sentinel-managed rules (dry-run by default).
    # ZH: 对账/清理废弃的 sentinel 规则（默认 dry-run）。
    pruned = 0
    if getattr(args, "prune", False):
        current_ids = {p.metric_id for p in policies}
        try:
            managed = client.list_sentinel_rules()
        except GrafanaError as e:
            console.print(f"[red]prune list error | 列举失败:[/red] {e}")
            managed = []
        stale = [r for r in managed if r.get("metric") and r["metric"] not in current_ids]
        if not stale:
            console.print("[dim]prune: nothing obsolete | 无废弃规则[/dim]")
        elif not args.prune_apply:
            console.print(
                f"[yellow]prune DRY-RUN — would delete {len(stale)} obsolete rule(s); "
                f"re-run with --prune-apply to delete | 模拟：将删 {len(stale)} 条，"
                f"加 --prune-apply 才真删[/yellow]"
            )
            for r in stale:
                console.print(f"  [red]- {r['title']}[/red]  ({r['metric']})")
        else:
            for r in stale:
                try:
                    client.delete_alert_rule(r["uid"])
                    pruned += 1
                    console.print(f"  [red]deleted[/red] {r['title']}")
                except GrafanaError as e:
                    console.print(f"[red]delete failed:[/red] {r['title']} -> {e}")

    # EN: audit this deploy in the episodic ledger. | ZH: 将本次部署记入情景账本。
    try:
        from sentinel.memory.episodic import EpisodicMemory
        from sentinel.paths import episodic_db_path
        led = EpisodicMemory(episodic_db_path(repo))
        led.record_deployment(base_url, args.contact_point, len(created), len(skipped), pruned)
        led.close()
    except Exception:
        pass
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    """EN: Approve/reject a metric so future `discover --memory` runs learn from
        it (rejected metrics are suppressed next time). | ZH: 批/拒某指标，让后续
        `discover --memory` 从中学习（被拒指标下次抑制）。"""
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1
    from sentinel.memory.manager import MemoryManager
    memory = MemoryManager(repo, privacy=args.privacy, enable_vector=False)

    if args.list:
        stats = memory.episodic.stats()
        verdicts = memory.episodic.latest_verdicts()
        table = Table(title="Feedback memory | 反馈记忆", show_lines=False)
        table.add_column("Metric ID | 指标", style="bold")
        table.add_column("Verdict | 裁决")
        for mid, v in sorted(verdicts.items()):
            color = "green" if v == "approve" else "red"
            table.add_row(mid, f"[{color}]{v}[/{color}]")
        console.print(table)
        console.print(
            f"[dim]runs: {stats['runs']}   approved: {stats['approved']}   "
            f"rejected: {stats['rejected']}[/dim]"
        )
        memory.close()
        return 0

    if not args.metric_id or not (args.approve or args.reject):
        console.print(
            "[red]need <metric_id> and --approve/--reject | "
            "需指定 <指标id> 与 --approve/--reject[/red]"
        )
        memory.close()
        return 1

    verdict = "approve" if args.approve else "reject"
    memory.record_feedback(args.metric_id, verdict, args.reason)
    color = "green" if verdict == "approve" else "yellow"
    console.print(f"[{color}]recorded {verdict} | 已记录 {verdict}:[/{color}] {args.metric_id}")
    memory.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """EN: Write feature-grouped observability files (alerts + queries + metrics)
        into the project as version-controllable, PR-reviewable config.
    ZH: 把按 feature 分组的可观测性文件（告警+查询+指标）写进项目，作为可版本化、
        可 PR 评审的配置。"""
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1
    catalog = DiscoveryEngine(scanners=[PythonScanner(cache=None)]).run(repo)
    out = Path(args.out) if args.out else repo / ".sentinel"
    result = ObservabilityExporter(backend=args.backend).export(catalog, out)
    if not result.features:
        console.print("[yellow]no metrics to export | 无指标可导出[/yellow]")
        return 0
    table = Table(title="Exported | 已导出（可观测性即代码）", show_lines=False)
    table.add_column("Feature | 模块", style="bold")
    table.add_column("Metrics | 指标", no_wrap=True)
    for feat, count in sorted(result.features.items()):
        table.add_row(feat, str(count))
    console.print(table)
    console.print(
        f"[green]wrote {result.total_files} file(s) | 已写入 {result.total_files} 个文件:[/green] "
        f"{result.out_dir}"
    )
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """EN: Generate a Grafana dashboard from the catalog — push it to Grafana or
        write the JSON to a file. | ZH: 从清单生成 Grafana 仪表盘 —— 推给 Grafana 或
        写成 JSON 文件。"""
    repo = Path(args.repo_path)
    if not repo.exists():
        console.print(f"[red]repo not found | 仓库不存在:[/red] {repo}")
        return 1
    catalog = DiscoveryEngine(scanners=[PythonScanner(cache=None)]).run(repo)
    if not catalog.metrics:
        console.print("[yellow]no metrics | 无指标[/yellow]")
        return 0

    if args.deploy:
        try:
            from dotenv import load_dotenv
            load_dotenv(repo / ".env")
        except ImportError:
            pass
        import os
        base_url = args.grafana_url or os.getenv("GRAFANA_URL", "")
        token = os.getenv("GRAFANA_TOKEN", "")
        if not base_url or not token:
            console.print(
                "[red]missing GRAFANA_URL / GRAFANA_TOKEN | 缺少 GRAFANA_URL / GRAFANA_TOKEN[/red]"
            )
            return 1
        client = GrafanaAlertingClient(base_url, token)
        try:
            prom_uid = client.prometheus_datasource_uid()
            folder_uid = client.ensure_folder(args.folder)
            dashboard = build_dashboard(catalog, prom_uid)
            resp = client.create_dashboard(dashboard, folder_uid)
        except GrafanaError as e:
            console.print(f"[red]Grafana API error | Grafana API 错误:[/red] {e}")
            return 1
        url = resp.get("url", "")
        console.print(
            f"[green]dashboard deployed | 仪表盘已部署:[/green] {base_url.rstrip('/')}{url}"
        )
        return 0

    # EN: file export (offline). | ZH: 文件导出（离线）。
    dashboard = build_dashboard(catalog, args.datasource_uid)
    out = Path(args.emit) if args.emit else repo / ".sentinel" / "dashboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"dashboard": dashboard, "overwrite": True},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    n_panels = sum(1 for p in dashboard["panels"] if p["type"] == "timeseries")
    console.print(
        f"[green]wrote dashboard ({n_panels} panels) | 已写入仪表盘（{n_panels} 面板）:[/green] {out}"
    )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """EN: Evaluate discovery quality vs hand-labeled ground truth (precision /
        recall / F1 + per-signal recall). --llm adds an ablation column showing
        the LLM's recall lift. | ZH: 对照人工标准答案评估发现质量（P/R/F1 + 按信号
        召回）。--llm 加一列消融，显示 LLM 带来的召回提升。"""
    fixtures = Path(args.fixtures)
    if not fixtures.exists():
        console.print(f"[red]fixtures not found | fixtures 不存在:[/red] {fixtures}")
        return 1
    static = evaluate_dir(fixtures)
    if not static:
        console.print(f"[yellow]no fixtures (need subdirs with expected.json) | "
                      f"无 fixture:[/yellow] {fixtures}")
        return 0

    llm_by_repo = {}
    if args.llm:
        llm = _build_llm(args.provider, args.privacy)
        if llm.available:
            llm_by_repo = {r.repo: r for r in evaluate_dir(fixtures, llm=llm)}
        else:
            console.print(f"[dim]ablation skipped — LLM off: {llm.why_unavailable()}[/dim]")

    table = Table(title="Discovery quality | 发现质量评估", show_lines=True)
    table.add_column("Repo | 仓库", style="bold")
    table.add_column("Precision", no_wrap=True)
    table.add_column("Recall", no_wrap=True)
    table.add_column("F1", no_wrap=True)
    table.add_column("Missed | 漏检")
    if llm_by_repo:
        table.add_column("+LLM Recall", no_wrap=True)
    for r in static:
        row = [r.repo, f"{r.precision:.2f}", f"{r.recall:.2f}", f"{r.f1:.2f}",
               ", ".join(r.missed) or "-"]
        if llm_by_repo:
            lr = llm_by_repo.get(r.repo)
            if lr:
                d = lr.recall - r.recall
                row.append(f"{lr.recall:.2f} ([green]{d:+.2f}[/green])")
            else:
                row.append("-")
        table.add_row(*row)
    console.print(table)

    agg = aggregate(static)
    line = (f"[bold]macro avg | 宏平均:[/bold] "
            f"P={agg['precision']:.2f}  R={agg['recall']:.2f}  F1={agg['f1']:.2f}")
    if llm_by_repo:
        agg_llm = aggregate(list(llm_by_repo.values()))
        line += (f"   ·   [green]+LLM R={agg_llm['recall']:.2f} "
                 f"(lift {agg_llm['recall'] - agg['recall']:+.2f})[/green]")
    console.print(line)

    # EN: per-signal recall of the first fixture (illustrative). | ZH: 首个 fixture 的按信号召回（示例）。
    if static[0].per_signal_recall:
        sig = "  ".join(f"{k}={v:.2f}" for k, v in static[0].per_signal_recall.items())
        console.print(f"[dim]{static[0].repo} per-signal recall | 按信号召回: {sig}[/dim]")
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
    d.add_argument("--memory", action="store_true",
                   help="persistent memory: incremental index + learn from feedback "
                        "| 持久记忆：增量索引+从反馈学习")
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
    al.add_argument("--state", metavar="FILE",
                    help="persist + merge policies (keep pinned thresholds) | 持久化+合并策略（保留人工阈值）")
    al.add_argument("--pin", metavar="METRIC_ID",
                    help="pin a metric's thresholds in --state so regen won't overwrite | 在 --state 里固定某指标阈值")
    al.set_defaults(func=cmd_alerts)

    dp = sub.add_parser("deploy-alerts",
                        help="push alert rules to Grafana via API | 通过 API 把告警规则推到 Grafana")
    dp.add_argument("repo_path", help="path to the repository | 仓库路径")
    dp.add_argument("--contact-point", required=True,
                    help="existing Grafana contact point name | 已有的 Grafana 联络点名")
    dp.add_argument("--grafana-url", default="",
                    help="Grafana base URL (else GRAFANA_URL env) | Grafana 地址（否则读 GRAFANA_URL）")
    dp.add_argument("--folder", default="Sentinel",
                    help="Grafana folder for the rules | 规则所在的 Grafana 文件夹")
    dp.add_argument("--prune", action="store_true",
                    help="reconcile: find obsolete sentinel rules (dry-run) | 对账：找废弃规则（模拟）")
    dp.add_argument("--prune-apply", action="store_true",
                    help="actually delete obsolete rules (with --prune) | 真删废弃规则（配 --prune）")
    dp.set_defaults(func=cmd_deploy_alerts)

    fb = sub.add_parser("feedback",
                        help="approve/reject a metric so future runs learn | 批/拒指标，让后续运行学习")
    fb.add_argument("repo_path", help="path to the repository | 仓库路径")
    fb.add_argument("metric_id", nargs="?", help="metric id, e.g. app.cold_start | 指标 id")
    grp = fb.add_mutually_exclusive_group()
    grp.add_argument("--approve", action="store_true", help="keep this metric | 保留该指标")
    grp.add_argument("--reject", action="store_true", help="suppress it next time | 下次抑制它")
    fb.add_argument("--reason", default="", help="optional note | 可选备注")
    fb.add_argument("--list", action="store_true", help="show current verdicts + stats | 显示当前裁决+统计")
    fb.add_argument("--privacy", default="air-gapped", choices=[m.value for m in PrivacyMode],
                    help="privacy tier (embedding not used here) | 隐私档")
    fb.set_defaults(func=cmd_feedback)

    ex = sub.add_parser("export",
                        help="write feature-grouped observability files into the project | 按 feature 把可观测性文件写进项目")
    ex.add_argument("repo_path", help="path to the repository | 仓库路径")
    ex.add_argument("--out", default="",
                    help="output dir (default <repo>/.sentinel) | 输出目录（默认 <repo>/.sentinel）")
    ex.add_argument("--backend", default="prometheus", choices=["prometheus", "kusto"],
                    help="query backend for the emitted queries | 导出查询用的后端")
    ex.set_defaults(func=cmd_export)

    db = sub.add_parser("dashboard",
                        help="generate a Grafana dashboard (deploy or file) | 生成 Grafana 仪表盘（部署或文件）")
    db.add_argument("repo_path", help="path to the repository | 仓库路径")
    db.add_argument("--deploy", action="store_true",
                    help="push to Grafana via API | 经 API 推给 Grafana")
    db.add_argument("--emit", default="",
                    help="write dashboard JSON to file (default <repo>/.sentinel/dashboard.json)")
    db.add_argument("--grafana-url", default="",
                    help="Grafana base URL (else GRAFANA_URL env) | Grafana 地址")
    db.add_argument("--folder", default="Sentinel",
                    help="Grafana folder | Grafana 文件夹")
    db.add_argument("--datasource-uid", default="prometheus",
                    help="prometheus datasource uid for file export | 文件导出用的数据源 uid")
    db.set_defaults(func=cmd_dashboard)

    ev = sub.add_parser("eval",
                        help="evaluate discovery quality vs ground truth | 对照标准答案评估发现质量")
    ev.add_argument("--fixtures", default="eval/fixtures",
                    help="fixtures dir (subdirs with expected.json) | fixtures 目录")
    ev.add_argument("--llm", action="store_true",
                    help="ablation: also run with LLM and show recall lift | 消融：加跑 LLM 看召回提升")
    ev.add_argument("--provider", default="openai", choices=sorted(PROVIDERS),
                    help="LLM provider for the ablation | 消融用的 Provider")
    ev.add_argument("--privacy", default="external-llm", choices=[m.value for m in PrivacyMode],
                    help="privacy tier for the ablation | 消融用的隐私档")
    ev.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
