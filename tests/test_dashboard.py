"""Tests for the Grafana dashboard builder. | Grafana 仪表盘构建器测试。

Run: PYTHONPATH=src pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.adapters.backends.grafana_dashboard import (  # noqa: E402
    build_dashboard,
    panel_promql,
)
from sentinel.model.metric import (  # noqa: E402
    MetricDescriptor,
    MetricKind,
    MetricsCatalog,
    Signal,
    Source,
)


def _metric(mid, file, signal, kind, unit=""):
    return MetricDescriptor(id=mid, kind=kind, signal=signal, unit=unit,
                            source=Source(file=file, symbol="fn"))


def test_panel_promql_latency_vs_errors():
    lat = _metric("api.request.duration", "app.py", Signal.latency,
                  MetricKind.histogram, unit="ms")
    err = _metric("api.errors", "app.py", Signal.errors, MetricKind.counter)
    assert "histogram_quantile" in panel_promql(lat)
    assert "api_request_duration_milliseconds_bucket" in panel_promql(lat)
    assert panel_promql(err) == "sum(rate(api_errors_total[5m]))"


def test_build_dashboard_groups_and_panels():
    cat = MetricsCatalog(repo="demo", metrics=[
        _metric("orders.errors", "orders/service.py", Signal.errors, MetricKind.counter),
        _metric("orders.latency", "orders/service.py", Signal.latency, MetricKind.histogram, "ms"),
        _metric("users.errors", "users/api.py", Signal.errors, MetricKind.counter),
    ])
    db = build_dashboard(cat, prom_uid="PROM")
    rows = [p for p in db["panels"] if p["type"] == "row"]
    panels = [p for p in db["panels"] if p["type"] == "timeseries"]
    assert {r["title"].split(" ")[0] for r in rows} == {"orders", "users"}
    assert len(panels) == 3
    # panels reference the given datasource uid
    assert all(p["datasource"]["uid"] == "PROM" for p in panels)


def test_dashboard_uid_is_stable():
    cat = MetricsCatalog(repo="demo", metrics=[
        _metric("a", "app.py", Signal.errors, MetricKind.counter)])
    assert build_dashboard(cat, "X")["uid"] == build_dashboard(cat, "Y")["uid"]
