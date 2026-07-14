"""Discovery quality evaluation. | 发现质量评估。

EN: Answers "is the agent's output any good?" by treating metric discovery as an
    information-retrieval task and scoring it against hand-labeled ground truth:
      - precision : of what we reported, how much is actually worth monitoring
      - recall    : of what SHOULD be monitored, how much we found (completeness)
      - F1        : harmonic mean
      - per-signal recall + a signal-classification confusion of mislabels
    An ablation (static-only vs static+LLM) quantifies what the LLM actually adds.
ZH: 通过把“指标发现”当作信息检索任务、对照人工标注的标准答案打分，回答“agent 产出
    好不好”：precision（有没有噪声）、recall（完整性/齐不齐）、F1、按信号的召回，以及
    signal 分类的混淆。消融实验（纯静态 vs 静态+LLM）量化 LLM 的真实增量。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from sentinel.adapters.scanners.python_scanner import PythonScanner
from sentinel.engines.discovery import DiscoveryEngine


@dataclass
class EvalResult:
    repo: str
    precision: float
    recall: float
    f1: float
    tp: int
    n_found: int
    n_expected: int
    missed: List[str] = field(default_factory=list)      # EN: expected but not found
    extra: List[str] = field(default_factory=list)        # EN: found but not expected
    per_signal_recall: Dict[str, float] = field(default_factory=dict)
    signal_confusion: Dict[str, int] = field(default_factory=dict)  # "exp->got" -> count


def _discover_ids(repo_path: str | Path, llm=None) -> Dict[str, Optional[str]]:
    """EN: Run discovery, return {metric_id: signal} (deduped). | ZH: 跑发现，返回去重的 {指标id: 信号}。"""
    catalog = DiscoveryEngine(scanners=[PythonScanner(cache=None)], llm=llm).run(repo_path)
    found: Dict[str, Optional[str]] = {}
    for m in catalog.metrics:
        if m.id not in found:
            found[m.id] = m.signal.value if m.signal else None
    return found


def evaluate(repo_path: str | Path, expected: dict, llm=None) -> EvalResult:
    """EN: Score discovery on one repo against its ground-truth `expected`.
    ZH: 用标准答案 `expected` 给一个仓库的发现打分。"""
    found = _discover_ids(repo_path, llm=llm)
    exp = {e["id"]: e.get("signal") for e in expected.get("metrics", [])}

    found_ids, exp_ids = set(found), set(exp)
    tp = found_ids & exp_ids
    precision = len(tp) / len(found_ids) if found_ids else 0.0
    recall = len(tp) / len(exp_ids) if exp_ids else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # EN: recall broken down by golden signal. | ZH: 按黄金信号拆分的召回。
    exp_by_sig: Dict[str, set] = defaultdict(set)
    for mid, sig in exp.items():
        exp_by_sig[sig or "unknown"].add(mid)
    per_signal = {sig: len(ids & found_ids) / len(ids) if ids else 0.0
                  for sig, ids in exp_by_sig.items()}

    # EN: for correctly-found metrics, did we classify the signal right?
    # ZH: 对找到的指标，signal 分类对不对。
    confusion: Dict[str, int] = defaultdict(int)
    for mid in tp:
        e, g = exp[mid], found[mid]
        if e and g and e != g:
            confusion[f"{e}->{g}"] += 1

    return EvalResult(
        repo=str(expected.get("repo", Path(repo_path).name)),
        precision=precision, recall=recall, f1=f1,
        tp=len(tp), n_found=len(found_ids), n_expected=len(exp_ids),
        missed=sorted(exp_ids - found_ids), extra=sorted(found_ids - exp_ids),
        per_signal_recall=per_signal, signal_confusion=dict(confusion),
    )


def evaluate_dir(fixtures_dir: str | Path, llm=None) -> List[EvalResult]:
    """EN: Evaluate every fixture (a subdir with expected.json). | ZH: 评估每个 fixture。"""
    import json
    results: List[EvalResult] = []
    for d in sorted(Path(fixtures_dir).iterdir()):
        exp_file = d / "expected.json"
        if d.is_dir() and exp_file.exists():
            expected = json.loads(exp_file.read_text(encoding="utf-8"))
            results.append(evaluate(d, expected, llm=llm))
    return results


def aggregate(results: List[EvalResult]) -> Dict[str, float]:
    """EN: Macro-averaged precision/recall/F1 across fixtures. | ZH: 跨 fixture 的宏平均。"""
    if not results:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    n = len(results)
    return {
        "precision": sum(r.precision for r in results) / n,
        "recall": sum(r.recall for r in results) / n,
        "f1": sum(r.f1 for r in results) / n,
    }
