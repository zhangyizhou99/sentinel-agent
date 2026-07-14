"""Tests for the observability exporter. | 可观测性导出器测试。

Run: PYTHONPATH=src pytest tests/ -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.export import ObservabilityExporter, feature_of  # noqa: E402
from sentinel.model.metric import (  # noqa: E402
    MetricDescriptor,
    MetricKind,
    MetricsCatalog,
    Signal,
    Source,
)


def _metric(mid: str, file: str, signal: Signal, kind: MetricKind) -> MetricDescriptor:
    return MetricDescriptor(id=mid, kind=kind, signal=signal,
                            source=Source(file=file, symbol="fn"))


def test_feature_of():
    assert feature_of("app.py") == "app"
    assert feature_of("orders/service.py") == "orders"
    assert feature_of("src/users/api.py") == "users"


def test_export_groups_by_feature(tmp_path):
    cat = MetricsCatalog(repo="demo", metrics=[
        _metric("orders.errors", "orders/service.py", Signal.errors, MetricKind.counter),
        _metric("orders.latency", "orders/service.py", Signal.latency, MetricKind.histogram),
        _metric("users.errors", "users/api.py", Signal.errors, MetricKind.counter),
    ])
    res = ObservabilityExporter().export(cat, tmp_path / ".sentinel")
    # two features, one per module
    assert set(res.features) == {"orders", "users"}
    assert res.features["orders"] == 2
    # each feature dir has the expected files
    for feat in ("orders", "users"):
        d = tmp_path / ".sentinel" / feat
        assert (d / "alerts.rules.yml").exists()
        assert (d / "queries.promql").exists()
        assert (d / "metrics.json").exists()
    assert (tmp_path / ".sentinel" / "README.md").exists()


def test_export_metrics_json_roundtrips(tmp_path):
    cat = MetricsCatalog(repo="demo", metrics=[
        _metric("orders.errors", "orders/service.py", Signal.errors, MetricKind.counter),
    ])
    ObservabilityExporter().export(cat, tmp_path / ".sentinel")
    data = json.loads((tmp_path / ".sentinel" / "orders" / "metrics.json").read_text())
    assert data["metrics"][0]["id"] == "orders.errors"


def test_export_backend_extension(tmp_path):
    cat = MetricsCatalog(repo="demo", metrics=[
        _metric("orders.errors", "orders/service.py", Signal.errors, MetricKind.counter),
    ])
    ObservabilityExporter(backend="kusto").export(cat, tmp_path / ".sentinel")
    assert (tmp_path / ".sentinel" / "orders" / "queries.kql").exists()
