"""Semantic memory — observability knowledge base. | 语义记忆 —— 可观测性知识库。

EN: Long-term, general knowledge about "what is worth monitoring", expressed as
    a structured set of patterns grouped by golden signal (RED/USE). It replaces
    a single hard-coded query string with an extensible catalog that:
      - yields one focused sub-query PER signal (fuel for multi-query retrieval)
      - yields a combined query (backwards-compatible)
      - can be extended at runtime and persisted, so domain knowledge accrues.
ZH: 关于“什么值得监控”的长期通用知识，表达为按黄金信号（RED/USE）分组的结构化模式集。
    它用一个可扩展目录取代单一硬编码查询串：
      - 为每个信号产出一条聚焦子查询（多查询检索的燃料）
      - 产出一条合并查询（向后兼容）
      - 可运行时扩展并持久化，让领域知识不断积累。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Pattern:
    """EN: One monitoring pattern. | ZH: 一条监控模式。"""

    id: str
    signal: str                       # errors | latency | traffic | saturation
    keywords: str                     # EN: retrieval terms | ZH: 检索词
    rationale: str = ""               # EN: why it matters | ZH: 为什么重要
    frameworks: List[str] = field(default_factory=list)


# EN: built-in RED/USE knowledge across common Python frameworks & clients.
# ZH: 内置的 RED/USE 知识，覆盖常见 Python 框架与客户端。
_DEFAULT_PATTERNS: List[Pattern] = [
    Pattern("http.errors", "errors",
            "api request route handler endpoint response status http error exception 5xx failure raise",
            "Request error rate is the loudest health signal.",
            ["fastapi", "flask", "django", "starlette"]),
    Pattern("http.latency", "latency",
            "api request route handler endpoint response duration latency slow p99 timeout",
            "Tail latency drives user-perceived reliability.",
            ["fastapi", "flask", "django", "starlette"]),
    Pattern("http.traffic", "traffic",
            "api request route handler endpoint throughput qps rate requests count",
            "Traffic gives denominator + anomaly detection.",
            ["fastapi", "flask", "django"]),
    Pattern("db.calls", "latency",
            "database query sql select insert update transaction commit rollback session cursor orm",
            "DB calls are the usual latency + error hotspot.",
            ["sqlalchemy", "psycopg2", "asyncpg", "pymongo", "django-orm"]),
    Pattern("external.calls", "errors",
            "http client request get post external call third party api requests httpx aiohttp timeout retry",
            "Downstream dependencies fail independently — track them.",
            ["requests", "httpx", "aiohttp", "urllib"]),
    Pattern("cache.ops", "traffic",
            "cache redis memcached get set delete hit miss expire ttl",
            "Cache hit/miss ratio reveals efficiency + stampede risk.",
            ["redis", "aioredis", "pymemcache"]),
    Pattern("queue.ops", "traffic",
            "queue kafka rabbitmq celery publish consume produce message broker enqueue dequeue ack",
            "Async pipelines need lag + throughput visibility.",
            ["celery", "kafka", "pika", "kombu"]),
    Pattern("business.critical", "errors",
            "order payment checkout charge transaction refund login auth token session signup subscribe",
            "Money + auth paths deserve first-class monitoring.",
            []),
    Pattern("jobs.work", "latency",
            "process job worker task schedule background cron batch pipeline run",
            "Background work fails silently without instrumentation.",
            ["celery", "apscheduler", "rq"]),
    Pattern("resource.saturation", "saturation",
            "memory cpu pool connection threads workers queue depth backlog utilization capacity",
            "Saturation predicts outages before they happen.",
            []),
]


class SemanticMemory:
    """EN: The observability knowledge base. | ZH: 可观测性知识库。"""

    def __init__(self, patterns: Optional[List[Pattern]] = None,
                 user_file: Optional[str | Path] = None):
        self._patterns: List[Pattern] = list(patterns or _DEFAULT_PATTERNS)
        self.user_file = Path(user_file) if user_file else None
        if self.user_file:
            self._load_user()

    def all_patterns(self) -> List[Pattern]:
        return list(self._patterns)

    def queries_by_signal(self) -> Dict[str, str]:
        """EN: One merged sub-query per signal — the inputs for multi-query
            retrieval (MQE). | ZH: 每个信号一条合并子查询 —— 多查询检索(MQE)的输入。"""
        out: Dict[str, List[str]] = {}
        for p in self._patterns:
            out.setdefault(p.signal, []).append(p.keywords)
        return {sig: " ".join(kw) for sig, kw in out.items()}

    def combined_query(self) -> str:
        """EN: All keywords in one string (drop-in for the legacy query).
        ZH: 所有关键词合成一串（可直接替换旧查询）。"""
        return " ".join(p.keywords for p in self._patterns)

    def add(self, pattern: Pattern, persist: bool = True) -> None:
        """EN: Extend the knowledge base (optionally persisted).
        ZH: 扩展知识库（可选持久化）。"""
        self._patterns.append(pattern)
        if persist and self.user_file:
            self._save_user()

    # -- persistence of user-added patterns | 用户新增模式的持久化 ----------

    def _load_user(self) -> None:
        if self.user_file and self.user_file.exists():
            try:
                raw = json.loads(self.user_file.read_text(encoding="utf-8"))
                for d in raw:
                    self._patterns.append(Pattern(**d))
            except (json.JSONDecodeError, OSError, TypeError):
                pass

    def _save_user(self) -> None:
        if not self.user_file:
            return
        # EN: persist ONLY user additions (beyond the built-in defaults).
        # ZH: 只持久化用户新增的（内置默认之外的）。
        defaults = {p.id for p in _DEFAULT_PATTERNS}
        extra = [p for p in self._patterns if p.id not in defaults]
        self.user_file.parent.mkdir(parents=True, exist_ok=True)
        self.user_file.write_text(
            json.dumps([p.__dict__ for p in extra], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
