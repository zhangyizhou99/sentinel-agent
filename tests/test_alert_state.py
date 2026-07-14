"""Tests for alert state (merge/pin) + deployment ledger.
记忆化告警状态（合并/固定）+ 部署账本 测试。

Run: PYTHONPATH=src pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.alert_state import AlertPolicyStore  # noqa: E402
from sentinel.memory.episodic import EpisodicMemory  # noqa: E402
from sentinel.model.alert import (  # noqa: E402
    AlertPolicy,
    Severity,
    ThresholdRule,
)


def _policy(metric_id: str, threshold: float = 5.0) -> AlertPolicy:
    return AlertPolicy(
        metric_id=metric_id,
        rules=[ThresholdRule(stat="error_rate", op=">", threshold=threshold,
                             unit="%", duration="2m", severity=Severity.SEV1)],
    )


# -- merge | 合并 -----------------------------------------------------------

def test_merge_adds_new(tmp_path):
    s = AlertPolicyStore(tmp_path / "a.json")
    diff = s.merge([_policy("api.errors"), _policy("api.latency")])
    assert set(diff.added) == {"api.errors", "api.latency"}
    assert not diff.obsolete


def test_merge_detects_obsolete(tmp_path):
    s = AlertPolicyStore(tmp_path / "a.json")
    s.merge([_policy("api.errors"), _policy("gone.metric")])
    diff = s.merge([_policy("api.errors")])  # gone.metric vanished
    assert diff.obsolete == ["gone.metric"]
    assert {p.metric_id for p in s.policies()} == {"api.errors"}


def test_pin_survives_regeneration(tmp_path):
    path = tmp_path / "a.json"
    s = AlertPolicyStore(path)
    s.merge([_policy("api.errors", threshold=5.0)])
    assert s.pin("api.errors")
    # operator tunes the pinned threshold to 2%
    p = s.policies()[0]
    p.rules[0].threshold = 2.0
    s.save()

    # regeneration still proposes the template's 5% ...
    s2 = AlertPolicyStore(path)
    diff = s2.merge([_policy("api.errors", threshold=5.0)])
    # ... but the pinned 2% is preserved
    assert diff.pinned_kept == ["api.errors"]
    assert s2.policies()[0].rules[0].threshold == 2.0


def test_pin_missing_metric_returns_false(tmp_path):
    s = AlertPolicyStore(tmp_path / "a.json")
    s.merge([_policy("api.errors")])
    assert s.pin("nope") is False


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "a.json"
    AlertPolicyStore(path).merge([_policy("api.errors")]) and AlertPolicyStore(path).save()
    s = AlertPolicyStore(path)
    s.merge([_policy("api.errors")])
    s.save()
    assert any(p.metric_id == "api.errors" for p in AlertPolicyStore(path).policies())


# -- deployment ledger | 部署账本 -------------------------------------------

def test_deployment_ledger(tmp_path):
    m = EpisodicMemory(tmp_path / "ep.db")
    m.record_deployment("https://x.grafana.net", "slack-oncall",
                        created=7, skipped=0, pruned=1)
    recent = m.recent_deployments()
    assert len(recent) == 1
    assert recent[0]["created"] == 7 and recent[0]["pruned"] == 1
    assert recent[0]["contact"] == "slack-oncall"
    m.close()
