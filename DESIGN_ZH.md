# Sentinel — 可观测性自动化 Agent 设计文档

> 一个可以「下载即用」的开源 Agent：扫描任意代码仓库 → 发现应被监控的指标 → 自动补全缺失埋点 → 生成查询与采样策略 → 计算安全阈值与告警分级 → 推送到 Slack / Teams / 飞书等。
>
> **核心设计原则：平台无关（Backend-Agnostic）**。Kusto 只是众多后端中的一个示例，所有与具体平台耦合的能力都通过「适配器（Adapter）」隔离。

---

## 0. TL;DR（一句话架构）

```
代码仓库  ──▶  [Discovery]  ──▶  Metrics Catalog(指标清单)
                                        │
                 [Gap Analysis] ◀───────┘   ← RED/USE/Golden Signals 规则
                     │
        ┌────────────┼─────────────────────────────┐
        ▼            ▼                              ▼
 [Instrument]   [Query & Sampling]            [Threshold & Alerting]
 补全埋点(OTel)   生成Kusto/PromQL等             阈值+Sev分级+路由
        │            │                              │
        └────────────┴──────────────┬───────────────┘
                                     ▼
                        [Output & Integration]
                  PR / 配置文件 / Dashboard / 告警规则
```

---

## 1. 目标与非目标

### 1.1 目标
1. **自动发现**：静态扫描代码库，识别「值得被监控的点」（启动、API、依赖调用、后台任务、资源使用等）。
2. **缺口检测**：对照可观测性最佳实践（RED / USE / Golden Signals），找出**缺失的关键指标**。
3. **自动补全**：为缺失点生成埋点代码（默认基于 OpenTelemetry，厂商中立）。
4. **查询生成**：为每个指标生成后端查询（Kusto KQL 为主示例，同时支持 PromQL / Log Analytics / SQL）。
5. **采样策略**：判断是否需要采样、采样率、以及采样对查询/成本/精度的影响。
6. **阈值与分级**：基于基线/统计方法计算安全阈值，映射到 Sev1–Sev4，并给出告警路由。
7. **可移植**：任何人 clone 下来，改一份 `sentinel.yaml` 即可用于自己的项目和平台。

### 1.2 非目标
- 不替代成熟 APM（Datadog / App Insights）——而是**编排并生成**它们的配置。
- 不做实时 metrics 采集本身——采集交给 OTel Collector / 平台 SDK。
- 不保证 100% 自动化——高风险的埋点/阈值以 **PR + 人审** 形式落地。

---

## 2. 理论基础（决定"该监控什么"的规则来源）

Agent 的"判断力"必须建立在公认方法论上，而非拍脑袋。内置三套规则：

| 方法论 | 适用对象 | 关注指标 |
|--------|---------|---------|
| **RED** | 请求驱动的服务（API/RPC） | **R**ate 请求率、**E**rrors 错误率、**D**uration 时延 |
| **USE** | 资源（CPU/内存/磁盘/连接池） | **U**tilization 利用率、**S**aturation 饱和度、**E**rrors 错误 |
| **Golden Signals**（Google SRE） | 通用服务 | Latency、Traffic、Errors、Saturation |

补充针对你提到的具体场景：

| 场景 | 应产出的指标 |
|------|-------------|
| **冷/热启动** | `app_cold_start_ms`、`app_warm_start_ms`、启动阶段耗时分解（依赖加载/连接建立/首个请求就绪） |
| **API 成败率** | `api_requests_total{route,method,status}`、`api_errors_total`、`api_latency_ms`（p50/p95/p99） |
| **外部依赖** | `dep_call_latency_ms{dep}`、`dep_error_total{dep}`、超时/重试次数 |
| **后台任务/队列** | 队列深度、消费延迟（lag）、处理失败率、重试堆积 |
| **资源** | 内存/CPU/GC、连接池占用、句柄泄漏 |

> 这些规则以**可扩展的 YAML 规则包**存在（`rules/*.yaml`），用户可增删，Agent 只是执行者。

---

## 3. 整体架构

### 3.1 分层视图

```
┌──────────────────────────────────────────────────────────────┐
│                      Orchestrator (编排层)                      │
│      规划 → 调用工具 → 汇总 → 生成产物 (LLM + 状态机)           │
└──────────────────────────────────────────────────────────────┘
        │            │            │            │            │
        ▼            ▼            ▼            ▼            ▼
   ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │Discovery│ │  Gap     │ │Instrument│ │  Query   │ │ Alerting │
   │ Engine  │ │ Analyzer │ │  Engine  │ │ Builder  │ │ Designer │
   └─────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
        │            │            │            │            │
┌──────────────────────────────────────────────────────────────┐
│                    适配器层 (Adapter / Plugin)                 │
│  CodeScanner    MetricsBackend    AlertChannel    Instrumentor │
│  ├─ python      ├─ kusto ★        ├─ slack        ├─ otel      │
│  ├─ node/ts     ├─ prometheus     ├─ teams        ├─ prom-sdk  │
│  ├─ go          ├─ log-analytics  ├─ feishu       └─ custom    │
│  ├─ java        ├─ datadog        ├─ pagerduty                 │
│  └─ ...         └─ ...            └─ webhook                    │
└──────────────────────────────────────────────────────────────┘
        │
┌──────────────────────────────────────────────────────────────┐
│              数据模型 (Metrics Catalog / IR 中间表示)          │
└──────────────────────────────────────────────────────────────┘
```

**关键：** 五个引擎只依赖**接口**，不依赖具体平台。换 Kusto 为 Prometheus，只需换一个 `MetricsBackend` 适配器，引擎代码零改动。这是"可移植"的核心。

### 3.2 六大模块职责

| 模块 | 输入 | 输出 | 关键技术 |
|------|------|------|----------|
| **Discovery Engine** | 代码库路径 | Metrics Catalog（候选指标清单） | 语言 AST 解析 + 框架识别 + LLM 语义补充 |
| **Gap Analyzer** | Catalog + 规则包 | 缺失指标列表 + 优先级 | 规则匹配 + LLM 推理 |
| **Instrument Engine** | 缺失点 | 埋点代码 diff / PR | 代码生成 + OTel 模板 |
| **Query Builder** | Catalog + 后端类型 | 查询语句 + 采样策略 | 模板 + 后端方言适配 |
| **Alerting Designer** | Catalog + 历史数据(可选) | 阈值 + Sev分级 + 路由 | 统计基线 / 异常检测 |
| **Output & Integration** | 全部产物 | PR、配置文件、Dashboard、告警规则 | Git / IaC 模板 |

---

## 4. 数据模型（中间表示 IR — 平台无关的核心）

所有平台差异在此**归一化**。这是整个系统的"普通话"。

### 4.1 MetricDescriptor（单个指标）

```yaml
# 一个指标的平台无关描述
id: api.request.duration            # 全局唯一
kind: histogram                     # counter | gauge | histogram | summary
unit: ms
description: "HTTP API 请求处理耗时"
source:                             # 该指标来自代码的哪里（可溯源）
  file: src/routes/user.py
  symbol: get_user
  line: 42
  framework: fastapi
dimensions:                         # 维度/标签（平台无关）
  - route
  - method
  - status_code
category: RED.Duration              # 命中的方法论规则
signal: latency                     # golden signal 分类
status: present | missing | partial # 发现结果
recommended_instrumentation: otel   # 若 missing，建议方式
sampling:
  required: true
  strategy: tail                    # head | tail | none
  rate: 0.1
alerting_ref: api.request.duration.slo
```

### 4.2 AlertPolicy（告警策略）

```yaml
metric_ref: api.request.duration
threshold:
  method: percentile-baseline       # static | percentile-baseline | anomaly
  window: 5m
  rules:
    - condition: "p99 > 2000ms for 5m"
      severity: SEV2
    - condition: "p99 > 5000ms for 2m"
      severity: SEV1
    - condition: "error_rate > 1% for 10m"
      severity: SEV3
routing:
  SEV1: [pagerduty, slack#oncall-critical]
  SEV2: [slack#oncall]
  SEV3: [slack#alerts]
  SEV4: [slack#alerts-noise]
```

> **归一化的意义**：Query Builder 拿到 `MetricDescriptor` 后，Kusto 适配器把它翻译成 KQL，Prometheus 适配器翻译成 PromQL——**同一份 IR，多后端产出**。

---

## 5. 工作流详解（六个阶段）

### 阶段 1：Discovery（发现）

**混合策略 = 静态分析（准） + LLM 语义理解（广）**

1. **仓库画像**：识别语言、框架、构建工具、入口文件、服务边界（`grep`/文件树/依赖清单）。
2. **AST 级扫描**（每语言一个 `CodeScanner` 适配器）：
   - 找入口点（`main`/`app.listen`/`FastAPI()`）→ 启动指标
   - 找路由/handler（装饰器、注解、路由表）→ API 指标
   - 找外部调用（HTTP client、DB driver、cache、queue SDK）→ 依赖指标
   - 找已有埋点（`counter.inc()`、`tracer.start_span()`、日志埋点）→ 标记 `present`
3. **LLM 语义补充**：AST 覆盖不到的业务语义（"这个函数是核心下单逻辑，应监控成功率"）由 LLM 阅读代码补充。
4. 产出 **Metrics Catalog**（`MetricDescriptor[]`）。

> 为什么要 AST + LLM 双通道？纯 LLM 会漏/幻觉，纯 AST 不懂业务语义。AST 保召回，LLM 保语义。

### 阶段 2：Gap Analysis（缺口检测）

- 用 `rules/*.yaml`（RED/USE/Golden Signals）对 Catalog 做覆盖度检查。
- 例：发现一个 FastAPI 路由，但 Catalog 里该路由**没有** error 计数器 → 标记 `missing: api.errors`。
- 输出缺失清单 + 优先级（核心链路 > 边缘接口）。

### 阶段 3：Instrumentation（自动补全埋点）★ 你特别要求的

- 默认生成 **OpenTelemetry** 埋点（一次埋点、任意后端导出，天然可移植）。
- 生成方式：**最小侵入 diff**，以 PR 形式提交，绝不静默改代码。

Python + FastAPI 示例（自动补的冷启动 + API 时延埋点）：

```python
# --- Sentinel auto-instrumentation (review before merge) ---
from opentelemetry import metrics
_meter = metrics.get_meter("sentinel")

# 冷启动耗时（在入口测量）
_cold_start = _meter.create_histogram("app.cold_start", unit="ms")

# API 时延 + 计数
_api_latency = _meter.create_histogram("api.request.duration", unit="ms")
_api_errors  = _meter.create_counter("api.errors")

@app.middleware("http")
async def _sentinel_mw(request, call_next):
    start = time.perf_counter()
    try:
        resp = await call_next(request)
        return resp
    except Exception:
        _api_errors.add(1, {"route": request.url.path})
        raise
    finally:
        _api_latency.record(
            (time.perf_counter() - start) * 1000,
            {"route": request.url.path, "method": request.method},
        )
```

- **多语言**：每种语言一个 `Instrumentor` 模板包（Python/Node/Go/Java…）。
- **不想用 OTel 的用户**：可切换 `instrumentor: prometheus` 或自定义 SDK 模板。

### 阶段 4：Query & Sampling（查询与采样）★ 你特别关心的

#### 4.1 采样决策规则（Agent 内置判断逻辑）

| 信号 | 是否采样 | 原因 |
|------|---------|------|
| **计数类**（请求数、错误数） | **不采样** | 采样会破坏计数准确性，用聚合而非采样 |
| **时延/Trace**（高基数、高频） | **采样**（尾部采样优先） | 全量成本高；尾采样保留慢/错请求 |
| **低频关键事件**（支付失败） | 不采样 | 每条都重要 |
| **超高吞吐日志** | 采样 + 聚合 | 成本控制 |

> 经验值：QPS > 1000 且为 trace/时延类 → 建议采样率 1%–10%，并优先 **tail-based sampling**（保留错误与长尾），而非头部随机采样。

#### 4.2 Kusto (KQL) 示例 — 后端示例之一

API 成功/失败率 + p99 时延（含**分桶/聚合**而非采样，保证计数准确）：

```kusto
// 每 5 分钟窗口的 API 成败率与 p99
ApiRequests
| where Timestamp > ago(1h)
| summarize
    Total      = count(),
    Errors     = countif(StatusCode >= 500),
    P99Latency = percentile(DurationMs, 99)
    by Route, bin(Timestamp, 5m)
| extend ErrorRate = round(100.0 * Errors / Total, 2)
| where Total > 0
| order by Timestamp desc
```

冷启动 p95（低频、无需采样）：

```kusto
AppStartup
| where Timestamp > ago(24h) and Phase == "cold"
| summarize P95ColdStartMs = percentile(DurationMs, 95) by bin(Timestamp, 1h)
```

对**已采样**数据做还原估算（采样率 10% 时用 `weight` 校正总量）：

```kusto
Traces
| where Timestamp > ago(1h)
| extend Weight = 1.0 / SamplingRate      // SamplingRate = 0.1
| summarize EstimatedTotal = sum(Weight) by Route
```

#### 4.3 同一指标 → 多后端产出（可移植性证明）

| 后端 | 语句（p99 时延，示意） |
|------|----------------------|
| **Kusto** | `summarize percentile(DurationMs,99) by bin(Timestamp,5m)` |
| **PromQL** | `histogram_quantile(0.99, rate(api_request_duration_bucket[5m]))` |
| **Log Analytics** | 与 Kusto 同为 KQL，表名/字段映射不同 |
| **SQL** | `PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY duration_ms)` |

Query Builder 从**同一个 `MetricDescriptor`** 出发，由后端适配器翻译。

### 阶段 5：Threshold & Alerting（阈值与分级）★

#### 5.1 阈值计算方法（三选一，可组合）

1. **静态阈值**：来自 SLO（如 p99 < 500ms）。最简单，适合有明确 SLA 的接口。
2. **基线百分位**：拉取历史数据，取 p99 的历史分布，阈值 = 历史 p99 × 安全系数（如 1.5×）。
3. **异常检测**：`series_decompose_anomalies`（Kusto 原生）/ 3-sigma / EWMA，适合无固定基线的指标。

Kusto 动态基线示例：

```kusto
ApiRequests
| make-series P99=percentile(DurationMs,99) default=0
    on Timestamp from ago(14d) to now() step 1h by Route
| extend (anomalies, score, baseline) =
    series_decompose_anomalies(P99, 2.5)   // 2.5 = 灵敏度
```

#### 5.2 Severity 分级矩阵（关键：把"影响面 × 紧急度"映射到 Sev）

| Sev | 触发条件（示例） | 影响 | 响应 | 路由 |
|-----|-----------------|------|------|------|
| **SEV1** | 核心 API 错误率 > 5% 持续 2m / 服务不可用 | 大面积用户受损 | 立即 oncall，P0 | PagerDuty + `#oncall-critical` + 电话 |
| **SEV2** | p99 > 阈值×3 持续 5m / 关键依赖失败 | 显著降级 | 15 分钟内响应 | PagerDuty + `#oncall` |
| **SEV3** | 错误率 > 1% 持续 10m / 冷启动变慢 | 局部/可容忍 | 工作时间处理 | `#alerts` |
| **SEV4** | 轻微抖动 / 接近阈值预警 | 观察项 | 无需即时 | `#alerts-noise` |

**分级设计原则**：
- **持续时间（for X）** 防抖动误报。
- **多级阈值**：同一指标可同时定义 Sev3（预警）和 Sev1（严重）。
- **降噪**：分组聚合（同一根因合并）、静默窗口、告警去重、依赖抑制（上游挂了不重复报下游）。

#### 5.3 告警推送（并发多渠道）★ 你要求的

- `AlertChannel` 适配器统一接口 `send(alert, severity)`。
- **并发扇出**：一个 Sev1 事件同时推 PagerDuty + Slack + 飞书，用异步并发（`asyncio.gather`）互不阻塞，单渠道失败不影响其它。

```python
async def dispatch(alert, severity, channels):
    results = await asyncio.gather(
        *[ch.send(alert, severity) for ch in channels],
        return_exceptions=True,       # 单渠道失败不拖垮整体
    )
    return results
```

Slack 消息模板（结构化，含跳转/静默按钮）：

```json
{
  "channel": "#oncall",
  "attachments": [{
    "color": "#D00000",
    "title": "[SEV2] api.request.duration p99 超阈值",
    "fields": [
      {"title": "Route", "value": "/api/checkout", "short": true},
      {"title": "p99", "value": "3200ms (阈值 1000ms)", "short": true},
      {"title": "持续", "value": "5m", "short": true}
    ],
    "actions": [
      {"type": "button", "text": "查看 Dashboard", "url": "..."},
      {"type": "button", "text": "静默 1h", "url": "..."}
    ]
  }]
}
```

### 阶段 6：Output & Integration（落地）

产物全部是**可评审、可版本化**的文件，而非黑盒：

- 埋点代码 → **Git PR**（人审后合并）
- 查询 → `queries/*.kql` / `*.promql`
- 告警规则 → `alerts/*.yaml`（可被 Terraform/Bicep/Prometheus rules 消费）
- Dashboard → Grafana/ADX Dashboard JSON
- 报告 → `sentinel-report.md`（发现了什么、补了什么、为什么）

---

## 6. 可移植性设计（"下载即用"的关键）

### 6.1 一切由 `sentinel.yaml` 驱动

用户 clone 后**只改这一个文件**即可适配自己的项目与平台：

```yaml
# sentinel.yaml — 项目级配置
project:
  name: my-product
  root: ./src
  languages: [python, typescript]     # 留空则自动探测

discovery:
  rules: [red, use, golden-signals]   # 启用的方法论规则包
  include: ["src/**"]
  exclude: ["**/tests/**", "**/migrations/**"]

instrumentation:
  provider: opentelemetry             # opentelemetry | prometheus | custom
  mode: pr                            # pr | inline | dry-run（默认 pr，安全）

backend:                              # ← 换平台只改这里
  type: kusto                         # kusto | prometheus | log-analytics | datadog
  kusto:
    cluster: https://mycluster.kusto.windows.net
    database: prod
    tables:
      requests: ApiRequests
      startup: AppStartup

alerting:
  threshold_method: percentile-baseline
  channels:
    slack:   { webhook_env: SLACK_WEBHOOK_URL }
    feishu:  { webhook_env: FEISHU_WEBHOOK_URL }
    pagerduty: { key_env: PAGERDUTY_KEY }
  routing:
    SEV1: [pagerduty, slack]
    SEV2: [slack]
    SEV3: [slack]

output:
  dir: ./.sentinel
  emit: [pr, queries, alerts, dashboard, report]
```

### 6.2 适配器契约（换平台 = 换适配器，引擎不动）

```python
# 后端适配器接口（Kusto/Prometheus/... 都实现它）
class MetricsBackend(Protocol):
    def render_query(self, m: MetricDescriptor, agg: Aggregation) -> str: ...
    def render_alert_rule(self, policy: AlertPolicy) -> dict: ...
    def supports_sampling(self) -> SamplingCapability: ...

class AlertChannel(Protocol):
    async def send(self, alert: Alert, severity: Severity) -> DeliveryResult: ...

class CodeScanner(Protocol):
    def scan(self, root: Path) -> list[MetricDescriptor]: ...

class Instrumentor(Protocol):
    def generate(self, gap: MetricDescriptor) -> CodePatch: ...
```

> 贡献一个新平台支持 = 新增一个实现类 + 注册。核心引擎、数据模型、工作流零改动。这就是"别人下载下来能用到自己项目"的工程保证。

---

## 7. Agent 编排设计（LLM + 工具）

### 7.1 为什么是 Agent 而非脚本
- 代码语义理解、缺口推理、阈值合理性判断需要 LLM 的推理能力。
- 但**高风险动作（改代码、设阈值）走确定性工具 + 人审**，LLM 只做规划与语义判断，不直接产出最终副作用。

### 7.2 工具清单（Tool Use）

| 工具 | 作用 |
|------|------|
| `scan_repo(path)` | 调用 CodeScanner，返回 Catalog |
| `analyze_gaps(catalog)` | 规则匹配缺口 |
| `generate_instrumentation(gap)` | 生成埋点 patch |
| `build_query(metric, backend)` | 生成后端查询 |
| `design_alert(metric, history?)` | 生成阈值 + Sev |
| `open_pr(patches)` | 提交 PR |
| `send_alert(...)` | 测试告警通道 |

### 7.3 状态机（保证可控、可恢复）

```
INIT → DISCOVER → ANALYZE → (HUMAN_REVIEW?) → INSTRUMENT
     → BUILD_QUERY → DESIGN_ALERT → EMIT → DONE
```

每步产物落盘到 `.sentinel/state.json`，支持断点续跑与审计。

---

## 8. 安全与隐私

隐私是本 Agent 最关键的风险点——它要**读私有源码**、**碰可能含 PII 的指标**、**接触告警渠道密钥**。核心思路：**分级数据边界 + 默认最小外泄（secure by default）**。

### 8.1 三类敏感数据与保障

| 敏感数据 | 风险 | 保障手段 |
|---------|------|---------|
| **源代码**（最敏感） | 用 LLM 分析时代码片段发给模型厂商 | ①纯静态零外泄；②私有/自托管 LLM；③只发 AST 结构+签名，不发整文件；④发送前 secret/PII redaction。离开本地边界的只有 IR 元数据，不是原始代码 |
| **指标维度 PII** | 埋点带用户ID/邮箱/手机号 | 维度默认拒绝高基数/敏感字段（deny-list + 高基数自动检测），需显式白名单；模板内置哈希/掩码（`user_id → hash(user_id)`）；告警发出前 PII 脱敏 |
| **密钥/凭据** | webhook/token 泄漏 | 只从**环境变量**读（`*_env` 约定），绝不写入仓库或产物；后端用**只读**最小权限账号 |

### 8.2 隐私分档（`privacy.mode`，核心开关）

隐私强度可分档，由 `sentinel.yaml` 一个开关控制，让不懂配置的用户默认也安全：

| 档位 | 说明 | 适用场景 |
|------|------|---------|
| `air-gapped` | 纯静态 AST 扫描，**完全不调用 LLM**，代码零字节外泄。牺牲语义理解 | 金融 / 政府 / 强合规 |
| **`private-llm`（推荐默认）** | 接企业内 Azure OpenAI / 自托管 vLLM（带"不训练不留存"），享 LLM 能力且代码不出企业边界 | 普通企业内部项目 |
| `external-llm` | 公有 LLM API + 脱敏，图省事 | 个人 / 开源 / 低敏感项目 |

**出厂默认 `private-llm`** —— 能力与隐私的最佳平衡点。纯静态退化成普通扫描工具、丢失业务语义（Agent 核心价值）；公有 LLM 对多数企业是合规红线不宜作默认。

### 8.3 不可关闭的三条硬底线（无论哪档都强制开启）

1. **只发 AST 结构与函数签名，不发整文件** —— 从源头缩小外泄面。
2. **发送前强制 secret/PII redaction** —— 双保险。
3. **指标维度 PII deny-list 默认开** —— 埋点侧防止用户数据写进指标。

### 8.4 横切合规保障

- **默认 dry-run / PR 模式**：产物是本地文件，人审后才落地，绝不静默改代码或外传。
- **数据驻留（residency）**：可配置 LLM/后端 region，满足数据不出境要求。
- **可审计**：所有外发请求（发给哪个 LLM、发了什么摘要、谁在何时改了监控）留日志可追溯。
- **告警防滥用**：内置速率限制，防止告警风暴反噬 Slack/办公软件。

对应 `sentinel.yaml` 配置：

```yaml
privacy:
  mode: private-llm                 # air-gapped | private-llm | external-llm
  llm:
    endpoint_env: SENTINEL_LLM_ENDPOINT   # 私有/自托管 LLM 地址
    no_retention: true              # 要求厂商不留存
    no_training: true               # 要求不用于训练
  redaction:
    send_full_files: false          # 硬底线①：只发 AST，禁发整文件
    scrub_secrets: true             # 硬底线②：发送前脱敏（不可关闭）
  dimensions:
    pii_deny_list: true             # 硬底线③：维度 PII 拒绝表
    hash_fields: [user_id, email, phone]
  residency:
    region: chinanorth              # 数据驻留区域
```

---

## 9. 目录结构（开箱即用的仓库骨架）

```
sentinel-agent/
├── sentinel.yaml                 # 用户唯一需要改的配置
├── README.md
├── DESIGN_ZH.md                  # 本文档（中文）
├── DESIGN_EN.md                  # 设计文档（英文）
├── src/sentinel/
│   ├── orchestrator/             # 编排层（Agent 状态机 + LLM）
│   ├── engines/
│   │   ├── discovery.py
│   │   ├── gap_analyzer.py
│   │   ├── instrument.py
│   │   ├── query_builder.py
│   │   └── alerting.py
│   ├── model/                    # MetricDescriptor / AlertPolicy 等 IR
│   ├── adapters/
│   │   ├── scanners/             # python.py, node.py, go.py ...
│   │   ├── backends/             # kusto.py ★, prometheus.py, ...
│   │   ├── instrumentors/        # otel.py, prometheus.py, ...
│   │   └── channels/             # slack.py, teams.py, feishu.py, ...
│   └── tools/                    # 供 Agent 调用的工具封装
├── rules/                        # red.yaml, use.yaml, golden-signals.yaml
├── templates/                    # 各语言埋点模板 + 消息模板
└── tests/
```

---

## 10. 路线图（建议交付顺序）

| 阶段 | 范围（MVP → 完整） |
|------|-------------------|
| **M1（MVP）** | Python 扫描 + RED 规则 + OTel 埋点 + Kusto 查询 + 静态阈值 + Slack 告警。端到端跑通一个项目。 |
| **M2** | 缺口自动补全 PR 化 + 采样策略 + 基线阈值 + 飞书/Teams 渠道。 |
| **M3** | 多语言扫描（Node/Go/Java）+ 多后端（Prometheus/Log Analytics）+ 异常检测阈值。 |
| **M4** | Dashboard 生成 + CI 集成（PR 时自动检查监控覆盖度）+ 告警降噪/抑制。 |

---

## 11. 关键设计取舍（FAQ）

**Q：为什么埋点默认用 OpenTelemetry？**
A：一次埋点，可导出到 Kusto/Prometheus/Datadog 任意后端，是"可移植"的最优解，避免绑定单一厂商。

**Q：计数类指标为什么不能采样？**
A：采样后 `count()` 会失真。计数用聚合（`summarize`），只有高频 trace/时延才采样，且优先尾部采样保留错误样本。

**Q：阈值由 Agent 全自动设定安全吗？**
A：不。Agent 给**建议值 + 依据**，以 PR/配置形式提交人审。核心链路阈值必须人确认，避免误报或漏报。

**Q：如何保证别人的项目也能用？**
A：三层保证 —— ①统一 IR 数据模型；②适配器插件架构；③单一 `sentinel.yaml` 配置驱动。换项目换平台不动核心代码。
```

