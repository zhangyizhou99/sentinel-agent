"""Alert policy state + 3-way merge. | 告警策略状态 + 三方合并。

EN: Turns alerting from "regenerate from scratch every time" into maintainable
    alerts-as-code. It persists the generated policies to a file and, on the next
    run, MERGES the freshly-generated policies with the saved ones:
      - a NEW metric        -> add it with default thresholds
      - an EXISTING metric  -> keep any human-`pinned` thresholds (never clobber
                               an operator's tuning), refresh the rest
      - a VANISHED metric   -> report it as obsolete (its code is gone)
    The returned MergeDiff is what you show the user before writing — same idea as
    a Terraform plan: propose, review, then apply.
ZH: 把告警从“每次从头重生成”变成可维护的 alerts-as-code。它把生成的策略存到文件，
    下次运行时把新生成的与已存的**合并**：
      - 新指标   -> 用默认阈值加入
      - 已有指标 -> 保留人工 `pinned` 的阈值（绝不冲掉运维的调参），其余刷新
      - 消失指标 -> 报告为 obsolete（源码已删）
    返回的 MergeDiff 就是写盘前给用户看的东西 —— 和 Terraform plan 一个思路：先提议、
    再审阅、后应用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from sentinel.model.alert import AlertPolicy


@dataclass
class MergeDiff:
    """EN: What a merge would change. | ZH: 一次合并会改动什么。"""

    added: List[str] = field(default_factory=list)      # EN: new metric ids | ZH: 新增指标
    kept: List[str] = field(default_factory=list)        # EN: unchanged | ZH: 保持
    pinned_kept: List[str] = field(default_factory=list)  # EN: had pinned thresholds preserved | ZH: 保留了人工阈值
    obsolete: List[str] = field(default_factory=list)    # EN: gone from code | ZH: 源码已删

    def is_empty(self) -> bool:
        return not (self.added or self.obsolete or self.pinned_kept)


class AlertPolicyStore:
    """EN: Persist + merge alert policies. | ZH: 持久化 + 合并告警策略。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._policies: Dict[str, AlertPolicy] = {}
        self._load()

    def policies(self) -> List[AlertPolicy]:
        return list(self._policies.values())

    # -- merge | 合并 ------------------------------------------------------

    def merge(self, generated: List[AlertPolicy]) -> MergeDiff:
        """EN: Reconcile freshly-generated policies with the saved ones, honoring
            pinned thresholds. Mutates in-memory state; call save() to persist.
        ZH: 把新生成的策略与已存的对账，尊重 pinned 阈值。改的是内存态，需 save() 落盘。"""
        diff = MergeDiff()
        gen_ids = set()

        for g in generated:
            gen_ids.add(g.metric_id)
            old = self._policies.get(g.metric_id)
            if old is None:
                self._policies[g.metric_id] = g
                diff.added.append(g.metric_id)
                continue
            merged_rules = []
            had_pin = False
            old_by_key = {(r.stat, r.severity): r for r in old.rules}
            for gr in g.rules:
                pinned = old_by_key.get((gr.stat, gr.severity))
                if pinned is not None and pinned.pinned:
                    merged_rules.append(pinned)   # EN: keep human tuning | ZH: 保留人工调参
                    had_pin = True
                else:
                    merged_rules.append(gr)       # EN: refresh from template | ZH: 用模板刷新
            self._policies[g.metric_id] = g.model_copy(update={"rules": merged_rules})
            (diff.pinned_kept if had_pin else diff.kept).append(g.metric_id)

        for mid in list(self._policies):
            if mid not in gen_ids:
                diff.obsolete.append(mid)
                del self._policies[mid]           # EN: drop obsolete | ZH: 丢弃废弃
        return diff

    def pin(self, metric_id: str) -> bool:
        """EN: Mark all of a metric's thresholds as human-owned (survive regen).
        ZH: 把某指标的全部阈值标为人工所有（重生成时存活）。"""
        p = self._policies.get(metric_id)
        if not p:
            return False
        p.rules = [r.model_copy(update={"pinned": True}) for r in p.rules]
        return True

    # -- persistence | 持久化 ----------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [p.model_dump(mode="json") for p in self._policies.values()]
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for d in raw:
                p = AlertPolicy.model_validate(d)
                self._policies[p.metric_id] = p
        except (json.JSONDecodeError, OSError, ValueError):
            self._policies = {}
