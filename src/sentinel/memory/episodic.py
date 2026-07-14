"""Episodic memory — cross-run history + user feedback. | 情景记忆 —— 跨运行历史 + 用户反馈。

EN: This is what turns Sentinel from a stateless scanner into a system that
    LEARNS. It records, per repository:
      - runs:     each discovery run (when, how many metrics found)
      - feedback: the user's verdict on individual metrics (approve / reject)
    On the next run, previously-rejected metrics can be suppressed or down-ranked
    (never silently — the count is reported), so the tool's suggestions improve
    with use instead of repeating rejected noise. Backed by stdlib sqlite3 (zero
    dependencies), one database file per repo.
ZH: 这是把 Sentinel 从无状态扫描器变成会**学习**的系统的关键。它按仓库记录：
      - runs：     每次发现运行（时间、发现多少指标）
      - feedback： 用户对单个指标的裁决（approve / reject）
    下次运行时，历史被拒的指标可被抑制或降权（绝不静默——会报告数量），让工具的建议
    越用越准，而不是反复推被拒的噪声。基于标准库 sqlite3（零依赖），每仓库一个库文件。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    total     INTEGER NOT NULL DEFAULT 0,
    present   INTEGER NOT NULL DEFAULT 0,
    missing   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    metric_id TEXT NOT NULL,
    verdict   TEXT NOT NULL CHECK (verdict IN ('approve','reject')),
    reason    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_feedback_metric ON feedback(metric_id, ts);
CREATE TABLE IF NOT EXISTS deployments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    target    TEXT NOT NULL DEFAULT '',
    contact   TEXT NOT NULL DEFAULT '',
    created   INTEGER NOT NULL DEFAULT 0,
    skipped   INTEGER NOT NULL DEFAULT 0,
    pruned    INTEGER NOT NULL DEFAULT 0
);
"""


class EpisodicMemory:
    """EN: SQLite-backed per-repo memory of runs and metric verdicts.
    ZH: 基于 SQLite、按仓库存运行史与指标裁决的记忆。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- writes | 写入 -----------------------------------------------------

    def record_run(self, summary: Dict[str, int]) -> None:
        """EN: Log a discovery run from a catalog summary dict.
        ZH: 从清单 summary 字典记录一次发现运行。"""
        self._conn.execute(
            "INSERT INTO runs(ts,total,present,missing) VALUES (?,?,?,?)",
            (time.time(), summary.get("total", 0),
             summary.get("present", 0), summary.get("missing", 0)),
        )
        self._conn.commit()

    def record_feedback(self, metric_id: str, verdict: str, reason: str = "") -> None:
        """EN: Record the user's approve/reject on a metric. | ZH: 记录用户对某指标的批/拒。"""
        if verdict not in ("approve", "reject"):
            raise ValueError("verdict must be 'approve' or 'reject' | 裁决须为 approve/reject")
        self._conn.execute(
            "INSERT INTO feedback(ts,metric_id,verdict,reason) VALUES (?,?,?,?)",
            (time.time(), metric_id, verdict, reason),
        )
        self._conn.commit()

    def record_deployment(self, target: str, contact: str, created: int,
                          skipped: int = 0, pruned: int = 0) -> None:
        """EN: Audit one deploy to Grafana (what/when/where). | ZH: 审计一次向 Grafana 的部署。"""
        self._conn.execute(
            "INSERT INTO deployments(ts,target,contact,created,skipped,pruned) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), target, contact, created, skipped, pruned),
        )
        self._conn.commit()

    # -- reads | 读取 ------------------------------------------------------

    def latest_verdicts(self) -> Dict[str, str]:
        """EN: metric_id -> most-recent verdict (a later approve overrides an
            earlier reject and vice-versa). | ZH: metric_id -> 最新裁决（后来的
            approve 覆盖先前的 reject，反之亦然）。"""
        rows = self._conn.execute(
            """SELECT f.metric_id, f.verdict
               FROM feedback f
               JOIN (SELECT metric_id, MAX(ts) AS mts FROM feedback GROUP BY metric_id) m
                 ON f.metric_id = m.metric_id AND f.ts = m.mts"""
        ).fetchall()
        return {mid: verdict for mid, verdict in rows}

    def rejected_metric_ids(self) -> set:
        """EN: Metric ids whose latest verdict is 'reject'. | ZH: 最新裁决为 reject 的指标 id。"""
        return {mid for mid, v in self.latest_verdicts().items() if v == "reject"}

    def verdict_of(self, metric_id: str) -> Optional[str]:
        return self.latest_verdicts().get(metric_id)

    def recent_runs(self, limit: int = 10) -> List[dict]:
        rows = self._conn.execute(
            "SELECT ts,total,present,missing FROM runs ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"ts": ts, "total": t, "present": p, "missing": m}
            for ts, t, p, m in rows
        ]

    def recent_deployments(self, limit: int = 10) -> List[dict]:
        rows = self._conn.execute(
            "SELECT ts,target,contact,created,skipped,pruned "
            "FROM deployments ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"ts": ts, "target": tg, "contact": c,
             "created": cr, "skipped": sk, "pruned": pr}
            for ts, tg, c, cr, sk, pr in rows
        ]

    def stats(self) -> Dict[str, int]:
        verdicts = self.latest_verdicts()
        runs = self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return {
            "runs": runs,
            "approved": sum(1 for v in verdicts.values() if v == "approve"),
            "rejected": sum(1 for v in verdicts.values() if v == "reject"),
        }

    def close(self) -> None:
        self._conn.close()
