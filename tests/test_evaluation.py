"""Tests for discovery-quality evaluation. | 发现质量评估测试。

Run: PYTHONPATH=src pytest tests/ -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.evaluation import aggregate, evaluate, evaluate_dir  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _ROOT / "eval" / "fixtures"
_SHOP = _FIXTURES / "fastapi_shop"


def test_evaluate_fixture_scores():
    expected = json.loads((_SHOP / "expected.json").read_text())
    r = evaluate(_SHOP, expected)
    # static scan finds the RED + dependency metrics but misses cache + db
    assert r.precision == 1.0                       # no false positives
    assert 0.70 <= r.recall <= 0.72                 # 5 of 7
    assert "db.query.duration" in r.missed
    assert "cache.operations" in r.missed
    assert r.extra == []


def test_per_signal_recall():
    expected = json.loads((_SHOP / "expected.json").read_text())
    r = evaluate(_SHOP, expected)
    assert r.per_signal_recall["errors"] == 1.0     # both error metrics found
    assert r.per_signal_recall["traffic"] == 0.0    # cache metric missed
    assert not r.signal_confusion                    # no signal mislabels


def test_evaluate_dir_and_aggregate():
    results = evaluate_dir(_FIXTURES)
    assert results, "expected at least one fixture"
    agg = aggregate(results)
    assert 0.0 <= agg["recall"] <= 1.0
    assert 0.0 <= agg["precision"] <= 1.0


def test_perfect_ground_truth_gives_full_recall(tmp_path):
    # a repo whose expected == exactly what discovery finds -> recall 1.0
    expected = {"repo": "shop", "metrics": [
        {"id": "app.cold_start", "signal": "latency"},
        {"id": "api.request.duration", "signal": "latency"},
        {"id": "api.errors", "signal": "errors"},
        {"id": "dep.call.duration", "signal": "latency"},
        {"id": "dep.errors", "signal": "errors"},
    ]}
    r = evaluate(_SHOP, expected)
    assert r.recall == 1.0 and r.precision == 1.0 and r.f1 == 1.0
