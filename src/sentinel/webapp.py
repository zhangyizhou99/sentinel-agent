"""Sentinel Gradio web UI. | Sentinel Gradio 网页界面。

EN: A minimal, good-looking web UI over the SAME engine the CLI/TUI use. Pick a
    repo path, an LLM provider and a privacy tier, then Discover / Instrument.
    Run: `python -m sentinel.webapp` then open the printed local URL.
ZH: 一个极简、好看的网页界面，底层是和 CLI/TUI 完全相同的引擎。选仓库路径、
    LLM Provider 和隐私档，然后点 Discover / Instrument。
    运行：`python -m sentinel.webapp`，打开终端里打印的本地地址。
"""
from __future__ import annotations

from pathlib import Path

import gradio as gr

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
from sentinel.paths import scan_cache_path

_HEADERS = ["Status | 状态", "Metric | 指标", "Signal | 信号",
            "Category | 分类", "Source | 来源", "Sampling | 采样"]

# EN: emoji marker per status for a nicer table. | ZH: 每个状态一个 emoji，表格更好看。
_STATUS_ICON = {"missing": "🟡 missing", "present": "🟢 present", "partial": "🔵 partial"}


def _engine(provider: str, privacy: str, repo: str) -> tuple[DiscoveryEngine, LLMClient]:
    llm = LLMClient(LLMConfig(provider=provider, privacy_mode=PrivacyMode(privacy)))
    cache = ScanCache(scan_cache_path(repo))
    return DiscoveryEngine(scanners=[PythonScanner(cache=cache)], llm=llm), llm


def run_discover(repo: str, provider: str, privacy: str):
    """EN: Scan and return (table rows, summary markdown). | ZH: 扫描并返回(表格行, 概览)。"""
    if not repo or not Path(repo).exists():
        return [], f"❌ **repo not found | 仓库不存在:** `{repo}`"

    engine, llm = _engine(provider, privacy, repo)
    catalog = engine.run(repo)

    rows = []
    for m in catalog.metrics:
        loc = f"{m.source.file}:{m.source.symbol}"
        if m.source.line:
            loc += f":{m.source.line}"
        rows.append([
            _STATUS_ICON.get(m.status.value, m.status.value),
            m.id,
            m.signal.value if m.signal else "-",
            m.category or "-",
            loc,
            m.sampling.strategy.value if m.sampling.required else "-",
        ])

    s = catalog.summary()
    llm_state = "🟢 ON" if llm.available else f"⚪ OFF ({llm.why_unavailable()})"
    summary = (
        f"**repo:** `{catalog.repo}`  \n"
        f"**total:** {s.get('total', 0)}　"
        f"🟡 **missing:** {s.get('missing', 0)}　"
        f"🟢 **present:** {s.get('present', 0)}  \n"
        f"**LLM augmentation:** {llm_state}"
    )
    return rows, summary


def run_instrument(repo: str, provider: str, privacy: str):
    """EN: Generate OTel instrumentation for missing metrics; return code.
    ZH: 为缺失指标生成 OTel 埋点；返回代码。"""
    if not repo or not Path(repo).exists():
        return f"# repo not found | 仓库不存在: {repo}"
    engine, _ = _engine(provider, privacy, repo)
    patches = InstrumentEngine().generate(engine.run(repo))
    if not patches:
        return "# no missing metrics — nothing to instrument | 无缺失指标，无需埋点"
    blocks = []
    for p in patches:
        Path(p.output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(p.output_path).write_text(p.content, encoding="utf-8")
        blocks.append(f"# === {p.output_path} ===\n{p.content}")
    return "\n\n".join(blocks)


def run_apply(repo: str, provider: str, privacy: str, branch: str):
    """EN: L2 — commit real edits to a user-named git branch; return the diff.
    ZH: L2 —— 把真实改动提交到用户命名的 git 分支；返回 diff。"""
    if not repo or not Path(repo).exists():
        return f"# repo not found | 仓库不存在: {repo}", ""
    if not (branch or "").strip():
        return "# 请先输入分支名 | please enter a branch name", ""
    engine, _ = _engine(provider, privacy, repo)
    try:
        res = Applier().apply(repo, engine.run(repo), branch=branch.strip())
    except ApplyError as exc:
        return f"# ❌ {exc}", ""
    note = res.message + (f"\n跳过 skipped: {', '.join(res.skipped)}" if res.skipped else "")
    return res.diff or "# no changes | 无改动", note


_ALERT_HEADERS = ["Metric | 指标", "Condition | 触发条件", "Sev", "Routing | 路由"]


def _static_catalog(repo: str):
    """EN: Static-only discovery (no LLM) for query/alerts. | ZH: 仅静态发现(不接LLM),供查询/告警用。"""
    cache = ScanCache(scan_cache_path(repo))
    return DiscoveryEngine(scanners=[PythonScanner(cache=cache)]).run(repo)


def run_query(repo: str, backend_name: str, window: str, lookback: str):
    """EN: D2 — render backend queries for the catalog. | ZH: D2 —— 为清单渲染后端查询。"""
    if not repo or not Path(repo).exists():
        return f"# repo not found | 仓库不存在: {repo}"
    backend = KustoBackend() if backend_name == "kusto" else PrometheusBackend()
    qs = QueryBuilder(backend).build(_static_catalog(repo), window=window, lookback=lookback)
    if not qs:
        return "# no metrics | 无指标"
    return "\n\n".join(f"-- [{q.metric_id}]  ({q.sampling_note})\n{q.query}" for q in qs)


def run_export(repo: str, backend_name: str, out: str) -> str:
    """EN: Write feature-grouped observability files into the project (as-code).
    ZH: 把按 feature 分组的可观测性文件写进项目（即代码）。"""
    if not repo or not Path(repo).exists():
        return f"❌ **repo not found | 仓库不存在:** `{repo}`"
    from sentinel.engines.export import ObservabilityExporter
    out_dir = out.strip() or str(Path(repo) / ".sentinel")
    res = ObservabilityExporter(backend=backend_name).export(_static_catalog(repo), out_dir)
    if not res.features:
        return "⚠️ 无指标可导出 | no metrics to export"
    feats = "\n".join(f"- `{f}`: {c} metrics" for f, c in sorted(res.features.items()))
    return (f"✅ **导出 {len(res.features)} 个 feature、{res.total_files} 个文件** → "
            f"`{res.out_dir}`\n\n{feats}")


def _grafana_creds(repo: str):
    """EN: Load (base_url, token) from the repo's .env. | ZH: 从仓库 .env 读 (地址, token)。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(repo) / ".env")
    except ImportError:
        pass
    import os
    return os.getenv("GRAFANA_URL", ""), os.getenv("GRAFANA_TOKEN", "")


def load_contact_points(repo: str):
    """EN: Fetch existing Grafana contact points to fill the dropdown — no manual
        typing. | ZH: 拉取 Grafana 已有联络点填下拉——不用手填。"""
    if not repo or not Path(repo).exists():
        return gr.update(choices=[]), f"❌ repo not found | 仓库不存在: `{repo}`"
    base_url, token = _grafana_creds(repo)
    if not base_url or not token:
        return gr.update(choices=[]), (
            "❌ 缺 **GRAFANA_URL / GRAFANA_TOKEN**（放仓库 `.env`）"
            " | missing in the repo's .env"
        )
    from sentinel.adapters.backends.grafana import GrafanaAlertingClient, GrafanaError
    try:
        names = GrafanaAlertingClient(base_url, token).list_contact_points()
    except GrafanaError as e:
        return gr.update(choices=[]), f"❌ Grafana API 错误 | error: {e}"
    if not names:
        return gr.update(choices=[]), "⚠️ Grafana 里没有联络点 | no contact points found"
    return (gr.update(choices=names, value=names[0]),
            f"✅ 加载了 {len(names)} 个联络点，选一个后点部署 | loaded {len(names)}")


def run_alerts(repo: str):
    """EN: D1 — design thresholds + Sev + routing. | ZH: D1 —— 设计阈值+Sev+路由。"""
    if not repo or not Path(repo).exists():
        return []
    rows = []
    for p in AlertingDesigner().design(_static_catalog(repo)):
        for r in p.rules:
            rows.append([p.metric_id, r.condition, r.severity.value,
                         ", ".join(p.routing.get(r.severity, []))])
    return rows


def run_emit_prometheus(repo: str) -> str:
    """EN: Render a Prometheus alerting_rules.yml for the catalog.
    ZH: 为清单渲染 Prometheus alerting_rules.yml。"""
    if not repo or not Path(repo).exists():
        return f"# repo not found | 仓库不存在: {repo}"
    policies = AlertingDesigner().design(_static_catalog(repo))
    backend = PrometheusBackend()
    rule_dicts: list = []
    for p in policies:
        rule_dicts.extend(backend.render_alert_rule(p))
    if not rule_dicts:
        return "# no rules | 无规则"
    return to_prometheus_yaml(rule_dicts)


def run_deploy_alerts(repo: str, contact_point: str) -> str:
    """EN: L3 — push Grafana-managed alert rules via the Provisioning API, wired
        to an existing contact point. Reads GRAFANA_URL/GRAFANA_TOKEN from the
        repo's .env. | ZH: L3 —— 经 Provisioning API 把 Grafana-managed 告警规则
        推上去并绑定已有联络点。从仓库 .env 读 GRAFANA_URL/GRAFANA_TOKEN。"""
    if not repo or not Path(repo).exists():
        return f"❌ **repo not found | 仓库不存在:** `{repo}`"
    if not (contact_point or "").strip():
        return "❌ 请先选/填 Grafana 联络点名字 | pick a Grafana contact point"

    base_url, token = _grafana_creds(repo)
    if not base_url or not token:
        return ("❌ 缺 **GRAFANA_URL / GRAFANA_TOKEN**（放仓库 `.env`，token 是密钥）\n\n"
                "missing GRAFANA_URL / GRAFANA_TOKEN in the repo's .env")

    from sentinel.adapters.backends.grafana import (
        GrafanaAlertingClient, GrafanaError, build_grafana_rules,
    )
    cp = contact_point.strip()
    policies = AlertingDesigner().design(_static_catalog(repo))
    if not policies:
        return "⚠️ 无指标可告警 | no metrics to alert on"

    client = GrafanaAlertingClient(base_url, token)
    try:
        if not client.contact_point_exists(cp):
            return f"❌ 联络点不存在 | contact point not found: **{cp}**"
        prom_uid = client.prometheus_datasource_uid()
        folder_uid = client.ensure_folder("Sentinel")
        existing = client.existing_rule_titles()
    except GrafanaError as e:
        return f"❌ Grafana API 错误 | error: {e}"

    created, skipped = [], []
    for p in policies:
        for rule in build_grafana_rules(p, prom_uid, folder_uid, cp):
            if rule["title"] in existing:
                skipped.append(rule["title"])
                continue
            try:
                client.create_alert_rule(rule)
                created.append(rule["title"])
            except GrafanaError as e:
                return f"❌ 失败 | failed: {rule['title']} → {e}"
    lines = "\n".join(f"- ✅ {t}" for t in created) or "(全部已存在 | all already present)"
    return (f"✅ **部署 {len(created)} 条**，跳过 {len(skipped)} 条已存在 → 联络点 **{cp}**\n\n"
            f"{lines}")


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Sentinel", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            "# 🛡️ Sentinel — 可观测性发现 Agent\n"
            "Scan a repo → discover metrics → auto-instrument. "
            "扫描仓库 → 发现指标 → 自动补埋点。"
        )
        with gr.Row():
            repo = gr.Textbox(value="../sentinel-sample-app", label="Repo path | 仓库路径", scale=3)
            provider = gr.Dropdown(sorted(PROVIDERS), value="modelscope",
                                   label="Provider | 接口", scale=2)
            privacy = gr.Dropdown([m.value for m in PrivacyMode],
                                  value=PrivacyMode.air_gapped.value,
                                  label="Privacy | 隐私档", scale=2)

        with gr.Tab("🔍 Discover | 发现"):
            btn_discover = gr.Button("🔍 Discover | 发现", variant="primary")
            summary = gr.Markdown()
            table = gr.Dataframe(headers=_HEADERS, label="Metrics Catalog | 指标清单",
                                 wrap=True, interactive=False)

        with gr.Tab("🧩 Instrument / Apply | 补埋点"):
            btn_instrument = gr.Button("🧩 Instrument | 补埋点(预览)", variant="secondary")
            code = gr.Code(label="Generated instrumentation | 生成的埋点", language="python")
            gr.Markdown("### 🌿 Apply to a git branch (L2) | 提交到 git 分支")
            with gr.Row():
                branch = gr.Textbox(label="Branch name (you choose) | 分支名（你自己定）",
                                    placeholder="e.g. sentinel/instrument", scale=3)
                btn_apply = gr.Button("🌿 Apply to branch | 改到分支", variant="stop", scale=1)
            apply_note = gr.Markdown()
            diff = gr.Code(label="git diff (review before merge | 合并前请评审)", language="python")

        with gr.Tab("📊 Query | 查询"):
            with gr.Row():
                backend = gr.Dropdown(["kusto", "prometheus"], value="kusto",
                                      label="Backend | 后端", scale=2)
                window = gr.Textbox(value="5m", label="Window | 聚合窗口", scale=1)
                lookback = gr.Textbox(value="1h", label="Lookback | 时间范围", scale=1)
                btn_query = gr.Button("📊 Generate queries | 生成查询", variant="primary", scale=1)
            queries = gr.Code(label="Backend queries | 后端查询", language="sql")

            gr.Markdown("### 📁 Export to project (observability-as-code) | 生成到项目")
            gr.Markdown(
                "*按模块分组写出 `.sentinel/<feature>/{alerts.rules.yml, queries, metrics.json}` "
                "— 可进 git、可 PR 评审 | grouped files, version-controllable*"
            )
            with gr.Row():
                export_out = gr.Textbox(
                    label="Output dir (default <repo>/.sentinel) | 输出目录", scale=3,
                )
                btn_export = gr.Button("📁 Export to project | 生成到项目",
                                       variant="stop", scale=1)
            export_status = gr.Markdown()

        with gr.Tab("🚨 Alerts | 告警"):
            btn_alerts = gr.Button("🚨 Design alerts | 设计告警", variant="primary")
            gr.Markdown("*阈值为建议，待人审 | thresholds are suggestions for review*")
            alerts = gr.Dataframe(headers=_ALERT_HEADERS, label="Alert policies | 告警策略",
                                  wrap=True, interactive=False)

            gr.Markdown("### 🚀 Deploy to Grafana (L3) | 一键部署到 Grafana")
            gr.Markdown(
                "*需在仓库 `.env` 配 `GRAFANA_URL` + `GRAFANA_TOKEN`（服务账号 token）"
                "| set GRAFANA_URL + GRAFANA_TOKEN in the repo's .env*"
            )
            with gr.Row():
                contact_point = gr.Dropdown(
                    choices=[], label="Grafana contact point | 联络点（从 Grafana 拉取）",
                    allow_custom_value=True, scale=2,
                )
                btn_load_cp = gr.Button("🔄 加载联络点 | Load", scale=1)
                btn_deploy = gr.Button("🚀 Deploy to Grafana | 部署", variant="stop", scale=1)
            deploy_status = gr.Markdown()
            btn_emit = gr.Button("⬇️ Export alerting_rules.yml | 导出 Prometheus 规则",
                                 variant="secondary")
            prom_yaml = gr.Code(label="Prometheus alerting_rules.yml", language="yaml")

        btn_discover.click(run_discover, [repo, provider, privacy], [table, summary])
        btn_instrument.click(run_instrument, [repo, provider, privacy], [code])
        btn_apply.click(run_apply, [repo, provider, privacy, branch], [diff, apply_note])
        btn_query.click(run_query, [repo, backend, window, lookback], [queries])
        btn_export.click(run_export, [repo, backend, export_out], [export_status])
        btn_alerts.click(run_alerts, [repo], [alerts])
        btn_deploy.click(run_deploy_alerts, [repo, contact_point], [deploy_status])
        btn_load_cp.click(load_contact_points, [repo], [contact_point, deploy_status])
        btn_emit.click(run_emit_prometheus, [repo], [prom_yaml])
    return demo


def main() -> None:
    build_ui().launch()


if __name__ == "__main__":
    main()
