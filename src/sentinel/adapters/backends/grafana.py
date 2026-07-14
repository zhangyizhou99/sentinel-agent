"""Grafana Alerting deploy adapter (L3). | Grafana 告警上线适配器（L3）。

EN: The piece that closes the loop: instead of emitting a rules file for a human
    to paste into Grafana, this talks to Grafana's Provisioning API and CREATES
    the Grafana-managed alert rules directly, wired to an existing contact point.
    Uses only the stdlib (urllib) — no new dependency, matching Sentinel's style.
ZH: 闭环的最后一块：不再只产出规则文件让人手动粘进 Grafana，而是直接调用 Grafana
    的 Provisioning API 创建 Grafana-managed 告警规则，并接到已有的联络点。
    只用标准库（urllib）—— 不引入新依赖，符合 Sentinel 一贯风格。

Auth | 认证:
    Needs a Grafana service-account token with alerting write access. Read from
    env GRAFANA_URL + GRAFANA_TOKEN (a secret — keep it in .env, never commit).
    需要一个有告警写权限的 Grafana 服务账号 token。从环境变量 GRAFANA_URL +
    GRAFANA_TOKEN 读取（密钥 —— 放 .env，绝不提交）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from sentinel.adapters.backends.prometheus import alert_parts
from sentinel.model.alert import AlertPolicy


class GrafanaError(RuntimeError):
    """EN: A Grafana API call failed. | ZH: 一次 Grafana API 调用失败。"""


@dataclass
class DeployResult:
    created: list[str]
    skipped: list[str]


class GrafanaAlertingClient:
    """EN: Thin client over Grafana's HTTP + Provisioning API.
    ZH: Grafana HTTP + Provisioning API 的轻量客户端。"""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- low-level HTTP | 底层 HTTP -----------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 extra_headers: dict | None = None) -> object:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise GrafanaError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise GrafanaError(f"{method} {path} -> {e.reason}") from e

    # -- discovery helpers | 探测辅助 ---------------------------------------

    def prometheus_datasource_uid(self) -> str:
        """EN: Find the Prometheus datasource UID (alert queries run against it).
        ZH: 找到 Prometheus 数据源 UID（告警查询在它上面跑）。"""
        datasources = self._request("GET", "/api/datasources")
        if isinstance(datasources, list):
            for ds in datasources:
                if ds.get("type") == "prometheus":
                    return ds["uid"]
        raise GrafanaError("no Prometheus datasource found | 未找到 Prometheus 数据源")

    def ensure_folder(self, title: str = "Sentinel") -> str:
        """EN: Return the UID of a folder titled `title`, creating it if absent.
        ZH: 返回名为 `title` 的文件夹 UID，不存在则创建。"""
        folders = self._request("GET", "/api/folders")
        if isinstance(folders, list):
            for f in folders:
                if f.get("title") == title:
                    return f["uid"]
        created = self._request("POST", "/api/folders", {"title": title})
        return created["uid"]  # type: ignore[index]

    def contact_point_exists(self, name: str) -> bool:
        """EN: True if a contact point with this name exists. | ZH: 存在同名联络点则 True。"""
        cps = self._request("GET", "/api/v1/provisioning/contact-points")
        if isinstance(cps, list):
            return any(cp.get("name") == name for cp in cps)
        return False

    def list_contact_points(self) -> list[str]:
        """EN: Names of all configured contact points (for UI pickers).
        ZH: 所有已配置联络点的名字（供 UI 下拉选择）。"""
        cps = self._request("GET", "/api/v1/provisioning/contact-points")
        names: list[str] = []
        if isinstance(cps, list):
            for cp in cps:
                name = cp.get("name")
                if name and name not in names:
                    names.append(name)
        return names

    def existing_rule_titles(self) -> set[str]:
        """EN: Titles of already-provisioned alert rules (for idempotency).
        ZH: 已存在的告警规则标题（用于幂等）。"""
        rules = self._request("GET", "/api/v1/provisioning/alert-rules")
        out: set[str] = set()
        if isinstance(rules, list):
            for r in rules:
                if r.get("title"):
                    out.add(r["title"])
        return out

    def create_alert_rule(self, rule: dict) -> None:
        """EN: Create one Grafana-managed alert rule (editable in UI afterwards).
        ZH: 创建一条 Grafana-managed 告警规则（之后可在 UI 里编辑）。"""
        # EN: X-Disable-Provenance lets users still edit the rule in the UI.
        # ZH: X-Disable-Provenance 让用户之后仍能在 UI 里编辑该规则。
        self._request("POST", "/api/v1/provisioning/alert-rules", rule,
                      extra_headers={"X-Disable-Provenance": "true"})

    def list_sentinel_rules(self) -> list[dict]:
        """EN: All alert rules this tool created (label source=sentinel), as
            [{uid, title, metric}]. Used to reconcile/prune obsolete rules.
        ZH: 本工具建的全部告警规则（标签 source=sentinel），形式 [{uid,title,metric}]。
            用于对账/清理废弃规则。"""
        rules = self._request("GET", "/api/v1/provisioning/alert-rules")
        out: list[dict] = []
        if isinstance(rules, list):
            for r in rules:
                labels = r.get("labels") or {}
                if labels.get("source") == "sentinel":
                    out.append({
                        "uid": r.get("uid", ""),
                        "title": r.get("title", ""),
                        "metric": labels.get("metric", ""),
                    })
        return out

    def delete_alert_rule(self, uid: str) -> None:
        """EN: Delete a provisioned alert rule by uid. | ZH: 按 uid 删除一条 provisioned 告警规则。"""
        self._request("DELETE", f"/api/v1/provisioning/alert-rules/{uid}",
                      extra_headers={"X-Disable-Provenance": "true"})

    def create_dashboard(self, dashboard: dict, folder_uid: str = "") -> dict:
        """EN: Create/update a dashboard (upsert by its uid). Returns the API
            response (contains uid + url). | ZH: 创建/更新仪表盘（按 uid 幂等 upsert）。
            返回 API 响应（含 uid + url）。"""
        body: dict = {"dashboard": dashboard, "overwrite": True}
        if folder_uid:
            body["folderUid"] = folder_uid
        resp = self._request("POST", "/api/dashboards/db", body)
        return resp if isinstance(resp, dict) else {}


# -- policy -> Grafana rule JSON | 策略 -> Grafana 规则 JSON -----------------

_OP_TO_EVALUATOR = {">": "gt", ">=": "gt", "<": "lt", "<=": "lt"}


def build_grafana_rules(
    policy: AlertPolicy,
    prom_uid: str,
    folder_uid: str,
    contact_point: str,
    group: str = "sentinel",
) -> list[dict]:
    """EN: Turn an AlertPolicy into Grafana-managed alert-rule payloads, one per
        severity level, each routed to `contact_point`. Reuses alert_parts so the
        PromQL is identical to what `sentinel alerts` emits.
    ZH: 把一份 AlertPolicy 变成 Grafana-managed 告警规则负载，每个严重度一条，都路由到
        `contact_point`。复用 alert_parts，保证 PromQL 与 `sentinel alerts` 输出一致。"""
    name = policy.metric_id.replace(".", "_").replace("-", "_")
    rules: list[dict] = []
    for r in policy.rules:
        parts = alert_parts(policy, r)
        if parts is None:
            continue
        query, thr, op = parts
        evaluator = _OP_TO_EVALUATOR.get(op, "gt")
        title = f"sentinel: {policy.metric_id} {r.stat} {r.severity.value}"
        rules.append({
            "title": title,
            "ruleGroup": group,
            "folderUID": folder_uid,
            "condition": "C",
            "for": r.duration,
            "noDataState": "OK",       # EN: no traffic -> not firing | ZH: 无流量 -> 不触发
            "execErrState": "Error",
            "isPaused": False,
            "labels": {"severity": r.severity.value.lower(), "source": "sentinel",
                       "metric": policy.metric_id},
            "annotations": {"summary": f"{policy.metric_id}: {r.condition}"},
            "notification_settings": {"receiver": contact_point},
            "data": [
                {
                    "refId": "A",
                    "relativeTimeRange": {"from": 600, "to": 0},
                    "datasourceUid": prom_uid,
                    "model": {
                        "refId": "A",
                        "editorMode": "code",
                        "expr": query,
                        "instant": True,
                        "range": False,
                        "intervalMs": 1000,
                        "maxDataPoints": 43200,
                        "datasource": {"type": "prometheus", "uid": prom_uid},
                    },
                },
                {
                    "refId": "C",
                    "relativeTimeRange": {"from": 600, "to": 0},
                    "datasourceUid": "__expr__",
                    "model": {
                        "refId": "C",
                        "type": "threshold",
                        "expression": "A",
                        "datasource": {"type": "__expr__", "uid": "__expr__"},
                        "conditions": [
                            {
                                "type": "query",
                                "evaluator": {"type": evaluator, "params": [thr]},
                                "operator": {"type": "and"},
                                "query": {"params": ["C"]},
                                "reducer": {"type": "last", "params": []},
                            }
                        ],
                    },
                },
            ],
        })
    return rules
