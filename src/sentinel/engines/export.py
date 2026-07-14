"""Observability-as-code exporter. | 可观测性即代码导出器。

EN: Instead of showing queries/alerts in a browser, write them into the scanned
    project as version-controllable files, GROUPED BY FEATURE (the module/dir the
    code lives in). The result is a `.sentinel/` tree that lives next to the code
    it monitors — reviewable in PRs, ownable via CODEOWNERS, deployable via GitOps:

        <repo>/.sentinel/
        ├── README.md                 # index of what is managed
        ├── orders/
        │   ├── alerts.rules.yml       # Prometheus alert rules for this module
        │   ├── queries.promql         # queries for dashboards / runbooks
        │   └── metrics.json           # the metric catalog subset
        └── users/ ...

ZH: 不再在浏览器里铺查询/告警，而是把它们作为**可版本化的文件**写进被扫描项目，
    **按 feature（代码所在模块/目录）分组**。产物是一棵和代码同处的 `.sentinel/` 树 ——
    可在 PR 里评审、用 CODEOWNERS 归属、按 GitOps 部署（结构见上）。
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List

from sentinel.adapters.backends.prometheus import PrometheusBackend, to_prometheus_yaml
from sentinel.engines.alerting import AlertingDesigner
from sentinel.engines.query_builder import QueryBuilder
from sentinel.model.metric import MetricDescriptor, MetricsCatalog


def feature_of(file: str) -> str:
    """EN: The "feature" a metric belongs to = the module/dir its code lives in.
        A root-level file maps to its own stem. | ZH: 指标所属的“feature” = 其代码
        所在的模块/目录；根级文件映射为其文件名主干。
        e.g. orders/service.py -> "orders";  app.py -> "app" """
    p = PurePosixPath(file)
    if p.parent == PurePosixPath("."):
        return p.stem or "root"
    return p.parent.name or p.stem


@dataclass
class ExportResult:
    out_dir: str
    features: Dict[str, int] = field(default_factory=dict)  # feature -> metric count
    files: List[str] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return len(self.files)


class ObservabilityExporter:
    """EN: Write feature-grouped observability files into a project.
    ZH: 把按 feature 分组的可观测性文件写进项目。"""

    def __init__(self, backend: str = "prometheus"):
        self.backend = backend
        self._ext = "promql" if backend == "prometheus" else "kql"

    def export(self, catalog: MetricsCatalog, out_dir: str | Path) -> ExportResult:
        out = Path(out_dir)
        groups = self._group(catalog)
        result = ExportResult(out_dir=str(out))

        for feat, metrics in sorted(groups.items()):
            sub = out / feat
            sub.mkdir(parents=True, exist_ok=True)
            subcat = MetricsCatalog(repo=catalog.repo, metrics=metrics)
            result.features[feat] = len(metrics)

            # EN: alert rules (Prometheus) | ZH: 告警规则（Prometheus）
            policies = AlertingDesigner().design(subcat)
            rule_dicts: list = []
            pb = PrometheusBackend()
            for p in policies:
                rule_dicts.extend(pb.render_alert_rule(p))
            if rule_dicts:
                self._write(result, sub / "alerts.rules.yml",
                            to_prometheus_yaml(rule_dicts, group=f"sentinel-{feat}"))

            # EN: queries for dashboards/runbooks | ZH: 供仪表盘/排障的查询
            from sentinel.adapters.backends.kusto import KustoBackend
            qbackend = pb if self.backend == "prometheus" else KustoBackend()
            queries = QueryBuilder(qbackend).build(subcat)
            if queries:
                body = "\n\n".join(
                    f"# {q.metric_id}  ({q.sampling_note})\n{q.query}" for q in queries
                )
                self._write(result, sub / f"queries.{self._ext}", body + "\n")

            # EN: the metric catalog subset (source of truth) | ZH: 指标清单子集（真相源）
            self._write(result, sub / "metrics.json",
                        json.dumps(subcat.model_dump(mode="json"),
                                   ensure_ascii=False, indent=2))

        self._write(result, out / "README.md", self._readme(catalog, result))
        return result

    # -- internals | 内部实现 ----------------------------------------------

    def _group(self, catalog: MetricsCatalog) -> Dict[str, List[MetricDescriptor]]:
        groups: Dict[str, List[MetricDescriptor]] = defaultdict(list)
        seen: set = set()
        for m in catalog.metrics:
            if m.id in seen:            # EN: dedup per metric id | ZH: 按指标 id 去重
                continue
            seen.add(m.id)
            groups[feature_of(m.source.file)].append(m)
        return groups

    def _write(self, result: ExportResult, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        result.files.append(str(path))

    def _readme(self, catalog: MetricsCatalog, result: ExportResult) -> str:
        lines = [
            "# Sentinel — observability as code",
            "",
            f"Generated observability config for `{catalog.repo}`, grouped by feature.",
            "",
            "| Feature | Metrics | Files |",
            "| --- | --- | --- |",
        ]
        for feat, count in sorted(result.features.items()):
            lines.append(f"| `{feat}` | {count} | alerts.rules.yml, "
                         f"queries.{self._ext}, metrics.json |")
        lines += [
            "",
            "## Deploy",
            "",
            "- Alerts → Grafana: `sentinel deploy-alerts <repo> --contact-point <name>`",
            "- Or load the Prometheus rules with `mimirtool rules load`.",
            "",
            "*Thresholds are suggestions — review before deploying.*",
            "",
        ]
        return "\n".join(lines)
