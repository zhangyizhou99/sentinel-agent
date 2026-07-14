"""Grafana dashboard builder. | Grafana 仪表盘构建器。

EN: Assembles the discovered metrics into a Grafana dashboard JSON — one panel per
    metric, grouped into rows by feature (the module the code lives in). Panels
    reuse the same PromQL the alerting/query layers emit, so the dashboard, the
    alerts, and the queries all tell one consistent story. The result can be
    pushed straight to Grafana (POST /api/dashboards/db) or saved as a file.
ZH: 把发现的指标组装成 Grafana 仪表盘 JSON —— 每个指标一个面板，按 feature（代码
    所在模块）分成一行行。面板复用告警/查询层同一套 PromQL，让仪表盘、告警、查询
    讲同一个故事。产物可直接推给 Grafana（POST /api/dashboards/db）或存成文件。
"""
from __future__ import annotations

import hashlib
from typing import Dict, List

from sentinel.adapters.backends.prometheus import _prom_base
from sentinel.engines.export import feature_of
from sentinel.model.metric import MetricDescriptor, MetricsCatalog, Signal


def panel_promql(metric: MetricDescriptor, window: str = "5m") -> str:
    """EN: The bare PromQL expression to chart for a metric (no comments).
    ZH: 给某指标画图用的裸 PromQL 表达式（不带注释）。"""
    base = _prom_base(metric.id, metric.unit)
    if metric.signal == Signal.latency:
        return f"histogram_quantile(0.99, sum(rate({base}_bucket[{window}])) by (le))"
    # EN: errors + traffic + default -> rate of the counter. | ZH: 错误+流量+兜底 -> 计数器速率。
    return f"sum(rate({base}_total[{window}]))"


def _panel(pid: int, metric: MetricDescriptor, prom_uid: str,
           x: int, y: int) -> dict:
    ds = {"type": "prometheus", "uid": prom_uid}
    unit = "s" if metric.unit == "ms" else "short"  # EN: hist is in ms->seconds via _bucket | ZH: 直方图秒
    return {
        "id": pid,
        "type": "timeseries",
        "title": metric.id,
        "datasource": ds,
        "gridPos": {"h": 8, "w": 12, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{
            "refId": "A",
            "datasource": ds,
            "expr": panel_promql(metric),
            "legendFormat": "{{route}}",
        }],
    }


def _row(pid: int, title: str, y: int) -> dict:
    return {
        "id": pid,
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "panels": [],
    }


def build_dashboard(catalog: MetricsCatalog, prom_uid: str,
                    title: str | None = None) -> dict:
    """EN: Build a Grafana dashboard model (inner `dashboard` object) from a
        catalog, grouping panels into feature rows. | ZH: 从清单构建 Grafana 仪表盘
        模型（内层 dashboard 对象），面板按 feature 分行。"""
    # EN: group distinct metrics by feature. | ZH: 按 feature 给去重后的指标分组。
    groups: Dict[str, List[MetricDescriptor]] = {}
    seen: set = set()
    for m in catalog.metrics:
        if m.id in seen:
            continue
        seen.add(m.id)
        groups.setdefault(feature_of(m.source.file), []).append(m)

    panels: list = []
    pid = 1
    y = 0
    for feat in sorted(groups):
        panels.append(_row(pid, f"{feat} | 模块", y))
        pid += 1
        y += 1
        col = 0
        for m in groups[feat]:
            panels.append(_panel(pid, m, prom_uid, x=col, y=y))
            pid += 1
            col += 12
            if col >= 24:               # EN: 2 panels per row | ZH: 每行 2 个
                col = 0
                y += 8
        if col != 0:
            y += 8

    # EN: stable uid so re-deploy UPDATES the same dashboard, not duplicates.
    # ZH: 稳定 uid，重复部署时更新同一块仪表盘而非重复创建。
    uid = "sentinel-" + hashlib.sha256(
        catalog.repo.encode("utf-8", "ignore")).hexdigest()[:12]
    return {
        "uid": uid,
        "title": title or f"Sentinel — {catalog.repo}",
        "tags": ["sentinel", "observability-as-code"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
    }
