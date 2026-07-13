# Sentinel — Observability Automation Agent Design Doc

> A "download-and-use" open-source agent: scan any code repository → discover which metrics should be monitored → auto-fill missing instrumentation → generate queries and sampling strategies → compute safe thresholds and alert severities → dispatch to Slack / Teams / Feishu, etc.
>
> **Core design principle: Backend-Agnostic.** Kusto is just one example backend among many; everything coupled to a specific platform is isolated behind an **Adapter**.

---

## 0. TL;DR (architecture in one picture)

```
Code Repo  ──▶  [Discovery]  ──▶  Metrics Catalog
                                        │
                 [Gap Analysis] ◀───────┘   ← RED/USE/Golden Signals rules
                     │
        ┌────────────┼─────────────────────────────┐
        ▼            ▼                              ▼
 [Instrument]   [Query & Sampling]            [Threshold & Alerting]
 fill gaps(OTel) gen Kusto/PromQL...           thresholds + Sev + routing
        │            │                              │
        └────────────┴──────────────┬───────────────┘
                                     ▼
                        [Output & Integration]
                  PR / config files / Dashboard / alert rules
```

---

## 1. Goals & Non-Goals

### 1.1 Goals
1. **Auto-discovery**: statically scan the codebase to identify "points worth monitoring" (startup, APIs, dependency calls, background jobs, resource usage, etc.).
2. **Gap detection**: check against observability best practices (RED / USE / Golden Signals) to find **missing critical metrics**.
3. **Auto-fill**: generate instrumentation code for gaps (defaults to OpenTelemetry, vendor-neutral).
4. **Query generation**: generate backend queries for each metric (Kusto KQL as the primary example; also PromQL / Log Analytics / SQL).
5. **Sampling strategy**: decide whether sampling is needed, the rate, and its impact on query/cost/accuracy.
6. **Thresholds & severity**: compute safe thresholds via baseline/statistical methods, map to Sev1–Sev4, and produce alert routing.
7. **Portability**: anyone can clone it and adapt to their own project and platform by editing a single `sentinel.yaml`.

### 1.2 Non-Goals
- Not a replacement for mature APMs (Datadog / App Insights) — instead it **orchestrates and generates** their configs.
- Not a metrics collector itself — collection is delegated to the OTel Collector / platform SDKs.
- No 100% automation — high-risk instrumentation/thresholds land as **PR + human review**.

---

## 2. Theoretical Basis (where "what to monitor" comes from)

The agent's "judgment" must be grounded in accepted methodology, not guesswork. Three built-in rule sets:

| Methodology | Applies to | Key metrics |
|-------------|-----------|-------------|
| **RED** | Request-driven services (API/RPC) | **R**ate, **E**rrors, **D**uration |
| **USE** | Resources (CPU/memory/disk/pools) | **U**tilization, **S**aturation, **E**rrors |
| **Golden Signals** (Google SRE) | General services | Latency, Traffic, Errors, Saturation |

Mapping to the specific scenarios you raised:

| Scenario | Metrics to produce |
|----------|--------------------|
| **Cold/warm start** | `app_cold_start_ms`, `app_warm_start_ms`, startup phase breakdown (dependency loading / connection setup / first-request readiness) |
| **API success/failure rate** | `api_requests_total{route,method,status}`, `api_errors_total`, `api_latency_ms` (p50/p95/p99) |
| **External dependencies** | `dep_call_latency_ms{dep}`, `dep_error_total{dep}`, timeouts/retries |
| **Background jobs/queues** | queue depth, consumer lag, processing failure rate, retry backlog |
| **Resources** | memory/CPU/GC, connection pool usage, handle leaks |

> These rules live as **extensible YAML rule packs** (`rules/*.yaml`); users add/remove them, and the agent is merely the executor.

---

## 3. Overall Architecture

### 3.1 Layered view

```
┌──────────────────────────────────────────────────────────────┐
│                      Orchestrator                              │
│   plan → call tools → aggregate → emit artifacts (LLM + FSM)   │
└──────────────────────────────────────────────────────────────┘
        │            │            │            │            │
        ▼            ▼            ▼            ▼            ▼
   ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │Discovery│ │  Gap     │ │Instrument│ │  Query   │ │ Alerting │
   │ Engine  │ │ Analyzer │ │  Engine  │ │ Builder  │ │ Designer │
   └─────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
        │            │            │            │            │
┌──────────────────────────────────────────────────────────────┐
│                    Adapter / Plugin Layer                     │
│  CodeScanner    MetricsBackend    AlertChannel    Instrumentor │
│  ├─ python      ├─ kusto ★        ├─ slack        ├─ otel      │
│  ├─ node/ts     ├─ prometheus     ├─ teams        ├─ prom-sdk  │
│  ├─ go          ├─ log-analytics  ├─ feishu       └─ custom    │
│  ├─ java        ├─ datadog        ├─ pagerduty                 │
│  └─ ...         └─ ...            └─ webhook                    │
└──────────────────────────────────────────────────────────────┘
        │
┌──────────────────────────────────────────────────────────────┐
│              Data Model (Metrics Catalog / IR)                │
└──────────────────────────────────────────────────────────────┘
```

**Key:** the five engines depend only on **interfaces**, never on a concrete platform. Swapping Kusto for Prometheus requires only a new `MetricsBackend` adapter — zero changes to engine code. This is the heart of "portability".

### 3.2 The six module responsibilities

| Module | Input | Output | Key tech |
|--------|-------|--------|----------|
| **Discovery Engine** | repo path | Metrics Catalog (candidate metrics) | language AST parsing + framework detection + LLM semantic augmentation |
| **Gap Analyzer** | Catalog + rule packs | missing-metric list + priority | rule matching + LLM reasoning |
| **Instrument Engine** | gaps | instrumentation diff / PR | codegen + OTel templates |
| **Query Builder** | Catalog + backend type | queries + sampling strategy | templates + backend-dialect adapters |
| **Alerting Designer** | Catalog + history (optional) | thresholds + Sev + routing | statistical baseline / anomaly detection |
| **Output & Integration** | all artifacts | PRs, config files, dashboards, alert rules | Git / IaC templates |

---

## 4. Data Model (IR — the platform-agnostic core)

All platform differences are **normalized** here. This is the system's "lingua franca".

### 4.1 MetricDescriptor (a single metric)

```yaml
# A platform-agnostic description of one metric
id: api.request.duration            # globally unique
kind: histogram                     # counter | gauge | histogram | summary
unit: ms
description: "HTTP API request handling latency"
source:                             # where in the code this came from (traceable)
  file: src/routes/user.py
  symbol: get_user
  line: 42
  framework: fastapi
dimensions:                         # dimensions/labels (platform-agnostic)
  - route
  - method
  - status_code
category: RED.Duration              # methodology rule matched
signal: latency                     # golden signal classification
status: present | missing | partial # discovery result
recommended_instrumentation: otel   # if missing, suggested approach
sampling:
  required: true
  strategy: tail                    # head | tail | none
  rate: 0.1
alerting_ref: api.request.duration.slo
```

### 4.2 AlertPolicy

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

> **Why normalize:** the Query Builder takes a `MetricDescriptor`; the Kusto adapter renders it as KQL, the Prometheus adapter as PromQL — **one IR, many backend outputs**.

---

## 5. Workflow in Detail (six phases)

### Phase 1: Discovery

**Hybrid strategy = static analysis (precise) + LLM semantics (broad)**

1. **Repo profiling**: detect languages, frameworks, build tools, entry points, service boundaries (file tree / dependency manifests / grep).
2. **AST-level scan** (one `CodeScanner` adapter per language):
   - find entry points (`main` / `app.listen` / `FastAPI()`) → startup metrics
   - find routes/handlers (decorators, annotations, route tables) → API metrics
   - find external calls (HTTP clients, DB drivers, cache, queue SDKs) → dependency metrics
   - find existing instrumentation (`counter.inc()`, `tracer.start_span()`, log points) → mark `present`
3. **LLM semantic augmentation**: business semantics AST cannot capture ("this function is the core checkout logic, monitor its success rate") are filled in by the LLM reading the code.
4. Emit the **Metrics Catalog** (`MetricDescriptor[]`).

> Why both AST + LLM? Pure LLM misses/hallucinates; pure AST lacks business semantics. AST ensures recall, the LLM ensures meaning.

### Phase 2: Gap Analysis

- Run coverage checks on the Catalog with `rules/*.yaml` (RED/USE/Golden Signals).
- Example: a FastAPI route is discovered but the Catalog has **no** error counter for it → mark `missing: api.errors`.
- Output the gap list + priority (core paths > edge endpoints).

### Phase 3: Instrumentation (auto-fill gaps) ★ specifically requested

- Generate **OpenTelemetry** instrumentation by default (instrument once, export to any backend — inherently portable).
- Delivery: **minimally-invasive diff**, submitted as a PR — never silently modifies code.

Python + FastAPI example (auto-filled cold-start + API latency instrumentation):

```python
# --- Sentinel auto-instrumentation (review before merge) ---
from opentelemetry import metrics
_meter = metrics.get_meter("sentinel")

# Cold-start duration (measured at the entry point)
_cold_start = _meter.create_histogram("app.cold_start", unit="ms")

# API latency + count
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

- **Multi-language**: one `Instrumentor` template pack per language (Python/Node/Go/Java…).
- **Not using OTel?** Switch to `instrumentor: prometheus` or a custom SDK template.

### Phase 4: Query & Sampling ★ a key concern

#### 4.1 Sampling decision rules (built-in agent logic)

| Signal | Sample? | Reason |
|--------|---------|--------|
| **Counts** (requests, errors) | **No** | Sampling breaks count accuracy; use aggregation, not sampling |
| **Latency/Traces** (high-cardinality, high-frequency) | **Yes** (prefer tail-based) | Full capture is costly; tail sampling keeps slow/erroring requests |
| **Low-frequency critical events** (payment failures) | No | Every record matters |
| **Ultra-high-throughput logs** | Sample + aggregate | Cost control |

> Rule of thumb: QPS > 1000 and trace/latency-type → suggest a 1%–10% rate, prefer **tail-based sampling** (keep errors and the long tail) over head-based random sampling.

#### 4.2 Kusto (KQL) example — one of the example backends

API success/failure rate + p99 latency (uses **binning/aggregation instead of sampling** to keep counts accurate):

```kusto
// API success/failure rate and p99 per 5-minute window
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

Cold-start p95 (low-frequency, no sampling needed):

```kusto
AppStartup
| where Timestamp > ago(24h) and Phase == "cold"
| summarize P95ColdStartMs = percentile(DurationMs, 95) by bin(Timestamp, 1h)
```

Reconstructing totals from **already-sampled** data (correct totals with `weight` when rate = 10%):

```kusto
Traces
| where Timestamp > ago(1h)
| extend Weight = 1.0 / SamplingRate      // SamplingRate = 0.1
| summarize EstimatedTotal = sum(Weight) by Route
```

#### 4.3 One metric → multiple backend outputs (proof of portability)

| Backend | Statement (p99 latency, illustrative) |
|---------|---------------------------------------|
| **Kusto** | `summarize percentile(DurationMs,99) by bin(Timestamp,5m)` |
| **PromQL** | `histogram_quantile(0.99, rate(api_request_duration_bucket[5m]))` |
| **Log Analytics** | Same KQL as Kusto, different table/field mapping |
| **SQL** | `PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY duration_ms)` |

The Query Builder starts from the **same `MetricDescriptor`** and lets the backend adapter translate.

### Phase 5: Threshold & Alerting ★

#### 5.1 Threshold methods (pick one, combinable)

1. **Static threshold**: from an SLO (e.g. p99 < 500ms). Simplest; good for endpoints with a clear SLA.
2. **Percentile baseline**: pull history, take the historical p99 distribution, threshold = historical p99 × safety factor (e.g. 1.5×).
3. **Anomaly detection**: `series_decompose_anomalies` (Kusto-native) / 3-sigma / EWMA; good for metrics without a fixed baseline.

Kusto dynamic-baseline example:

```kusto
ApiRequests
| make-series P99=percentile(DurationMs,99) default=0
    on Timestamp from ago(14d) to now() step 1h by Route
| extend (anomalies, score, baseline) =
    series_decompose_anomalies(P99, 2.5)   // 2.5 = sensitivity
```

#### 5.2 Severity matrix (key: map "blast radius × urgency" to Sev)

| Sev | Trigger (example) | Impact | Response | Routing |
|-----|-------------------|--------|----------|---------|
| **SEV1** | Core API error rate > 5% for 2m / service down | Widespread user impact | Page immediately, P0 | PagerDuty + `#oncall-critical` + phone |
| **SEV2** | p99 > threshold×3 for 5m / key dependency failing | Significant degradation | Respond within 15 min | PagerDuty + `#oncall` |
| **SEV3** | Error rate > 1% for 10m / cold start slower | Local / tolerable | Handle during business hours | `#alerts` |
| **SEV4** | Minor jitter / near-threshold warning | Watch item | No immediate action | `#alerts-noise` |

**Severity design principles**:
- **Duration (for X)** guards against flapping false positives.
- **Multi-level thresholds**: one metric can define both Sev3 (warning) and Sev1 (critical).
- **Noise reduction**: grouping (merge same root cause), silence windows, dedup, dependency suppression (don't re-alert downstream when upstream is down).

#### 5.3 Alert dispatch (concurrent, multi-channel) ★ requested

- `AlertChannel` adapters share a uniform interface `send(alert, severity)`.
- **Concurrent fan-out**: a single Sev1 event pushes to PagerDuty + Slack + Feishu at once via async concurrency (`asyncio.gather`); channels don't block each other, and one failing channel doesn't affect the rest.

```python
async def dispatch(alert, severity, channels):
    results = await asyncio.gather(
        *[ch.send(alert, severity) for ch in channels],
        return_exceptions=True,       # one channel failing won't sink the rest
    )
    return results
```

Slack message template (structured, with jump/silence buttons):

```json
{
  "channel": "#oncall",
  "attachments": [{
    "color": "#D00000",
    "title": "[SEV2] api.request.duration p99 over threshold",
    "fields": [
      {"title": "Route", "value": "/api/checkout", "short": true},
      {"title": "p99", "value": "3200ms (threshold 1000ms)", "short": true},
      {"title": "Duration", "value": "5m", "short": true}
    ],
    "actions": [
      {"type": "button", "text": "Open Dashboard", "url": "..."},
      {"type": "button", "text": "Silence 1h", "url": "..."}
    ]
  }]
}
```

### Phase 6: Output & Integration

All artifacts are **reviewable, versionable** files — not a black box:

- Instrumentation code → **Git PR** (merge after review)
- Queries → `queries/*.kql` / `*.promql`
- Alert rules → `alerts/*.yaml` (consumable by Terraform/Bicep/Prometheus rules)
- Dashboards → Grafana/ADX Dashboard JSON
- Report → `sentinel-report.md` (what was found, what was filled, why)

---

## 6. Portability Design (the key to "download-and-use")

### 6.1 Everything driven by `sentinel.yaml`

After cloning, users adapt to their own project and platform by **editing just this one file**:

```yaml
# sentinel.yaml — project-level config
project:
  name: my-product
  root: ./src
  languages: [python, typescript]     # leave empty to auto-detect

discovery:
  rules: [red, use, golden-signals]   # enabled methodology rule packs
  include: ["src/**"]
  exclude: ["**/tests/**", "**/migrations/**"]

instrumentation:
  provider: opentelemetry             # opentelemetry | prometheus | custom
  mode: pr                            # pr | inline | dry-run (default: pr, safe)

backend:                              # ← change platform only here
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

### 6.2 Adapter contracts (swap platform = swap adapter, engines untouched)

```python
# Backend adapter interface (Kusto/Prometheus/... all implement it)
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

> Contributing a new platform = add one implementation class + register it. Core engines, data model, and workflow change nothing. This is the engineering guarantee that "others can download it and use it on their own project".

---

## 7. Agent Orchestration (LLM + tools)

### 7.1 Why an agent, not a script
- Code semantic understanding, gap reasoning, and threshold sanity checks need LLM reasoning.
- But **high-risk actions (modifying code, setting thresholds) go through deterministic tools + human review**; the LLM only plans and judges semantics, never producing final side effects directly.

### 7.2 Tool list (tool use)

| Tool | Purpose |
|------|---------|
| `scan_repo(path)` | Call CodeScanner, return Catalog |
| `analyze_gaps(catalog)` | Rule-match gaps |
| `generate_instrumentation(gap)` | Generate instrumentation patch |
| `build_query(metric, backend)` | Generate backend query |
| `design_alert(metric, history?)` | Generate threshold + Sev |
| `open_pr(patches)` | Submit a PR |
| `send_alert(...)` | Test an alert channel |

### 7.3 State machine (controllable, resumable)

```
INIT → DISCOVER → ANALYZE → (HUMAN_REVIEW?) → INSTRUMENT
     → BUILD_QUERY → DESIGN_ALERT → EMIT → DONE
```

Each step's artifacts are persisted to `.sentinel/state.json`, supporting resumable runs and auditing.

---

## 8. Security & Privacy

Privacy is this agent's most critical risk — it **reads private source code**, **touches potentially PII-bearing metrics**, and **handles alert-channel secrets**. Core idea: **tiered data boundaries + minimal exfiltration by default (secure by default)**.

### 8.1 Three classes of sensitive data and their safeguards

| Sensitive data | Risk | Safeguards |
|----------------|------|-----------|
| **Source code** (most sensitive) | Code snippets sent to the model vendor during LLM analysis | ① pure-static zero-exfiltration; ② private/self-hosted LLM; ③ send AST structure + signatures only, never whole files; ④ secret/PII redaction before sending. Only IR metadata leaves the local boundary — not raw code |
| **Metric-dimension PII** | Instrumentation carries user IDs / emails / phone numbers | Dimensions reject high-cardinality/sensitive fields by default (deny-list + auto high-cardinality detection), requiring an explicit allow-list; templates build in hashing/masking (`user_id → hash(user_id)`); PII scrubbed before alerts are sent |
| **Secrets/credentials** | webhook/token leakage | Read only from **environment variables** (`*_env` convention), never written to the repo or artifacts; backends use **read-only** least-privilege accounts |

### 8.2 Privacy tiers (`privacy.mode`, the core switch)

Privacy strength is tiered, controlled by a single switch in `sentinel.yaml`, so even users who don't understand the config are safe by default:

| Tier | Description | Best for |
|------|-------------|----------|
| `air-gapped` | Pure static AST scan, **never calls an LLM**, zero bytes of code leave. Sacrifices semantic understanding | Finance / government / strict compliance |
| **`private-llm` (recommended default)** | Uses in-enterprise Azure OpenAI / self-hosted vLLM (with "no training, no retention"); enjoys LLM capability while code stays within the enterprise boundary | Typical internal enterprise projects |
| `external-llm` | Public LLM API + redaction, for convenience | Personal / open-source / low-sensitivity projects |

**Ship default: `private-llm`** — the best balance of capability and privacy. Pure-static degrades to an ordinary scanner and loses business semantics (the agent's core value); public LLMs are a compliance red line for most enterprises and unsuitable as a default.

### 8.3 Three non-negotiable hard baselines (always on, regardless of tier)

1. **Send AST structure and function signatures only, never whole files** — shrink the exfiltration surface at the source.
2. **Mandatory secret/PII redaction before sending** — a second safety net.
3. **Metric-dimension PII deny-list on by default** — prevent user data from being written into metrics at the instrumentation side.

### 8.4 Cross-cutting compliance safeguards

- **Default dry-run / PR mode**: artifacts are local files, landing only after human review — never silently modifies code or exfiltrates.
- **Data residency**: configurable LLM/backend region to satisfy data-locality requirements.
- **Auditable**: all outbound requests (which LLM, what summary was sent, who changed monitoring when) are logged and traceable.
- **Alert-abuse protection**: built-in rate limiting to prevent alert storms from backfiring on Slack/office tools.

Corresponding `sentinel.yaml` config:

```yaml
privacy:
  mode: private-llm                 # air-gapped | private-llm | external-llm
  llm:
    endpoint_env: SENTINEL_LLM_ENDPOINT   # private/self-hosted LLM address
    no_retention: true              # require vendor no-retention
    no_training: true               # require no-training
  redaction:
    send_full_files: false          # hard baseline ①: AST only, whole files forbidden
    scrub_secrets: true             # hard baseline ②: redact before sending (cannot disable)
  dimensions:
    pii_deny_list: true             # hard baseline ③: dimension PII deny-list
    hash_fields: [user_id, email, phone]
  residency:
    region: chinanorth              # data residency region
```

---

## 9. Directory Structure (ready-to-use repo skeleton)

```
sentinel-agent/
├── sentinel.yaml                 # the only file users must edit
├── README.md
├── DESIGN_ZH.md                  # design doc (Chinese)
├── DESIGN_EN.md                  # this document (English)
├── src/sentinel/
│   ├── orchestrator/             # orchestration (agent FSM + LLM)
│   ├── engines/
│   │   ├── discovery.py
│   │   ├── gap_analyzer.py
│   │   ├── instrument.py
│   │   ├── query_builder.py
│   │   └── alerting.py
│   ├── model/                    # IR: MetricDescriptor / AlertPolicy, etc.
│   ├── adapters/
│   │   ├── scanners/             # python.py, node.py, go.py ...
│   │   ├── backends/             # kusto.py ★, prometheus.py, ...
│   │   ├── instrumentors/        # otel.py, prometheus.py, ...
│   │   └── channels/             # slack.py, teams.py, feishu.py, ...
│   └── tools/                    # tool wrappers the agent calls
├── rules/                        # red.yaml, use.yaml, golden-signals.yaml
├── templates/                    # per-language instrumentation + message templates
└── tests/
```

---

## 10. Roadmap (suggested delivery order)

| Phase | Scope (MVP → complete) |
|-------|------------------------|
| **M1 (MVP)** | Python scan + RED rules + OTel instrumentation + Kusto queries + static thresholds + Slack alerts. End-to-end on one project. |
| **M2** | PR-based gap auto-fill + sampling strategy + baseline thresholds + Feishu/Teams channels. |
| **M3** | Multi-language scan (Node/Go/Java) + multi-backend (Prometheus/Log Analytics) + anomaly-detection thresholds. |
| **M4** | Dashboard generation + CI integration (auto-check monitoring coverage on PRs) + alert noise reduction/suppression. |

---

## 11. Key Design Trade-offs (FAQ)

**Q: Why default to OpenTelemetry for instrumentation?**
A: Instrument once, export to any backend (Kusto/Prometheus/Datadog) — the best portability choice, avoiding single-vendor lock-in.

**Q: Why can't count metrics be sampled?**
A: Sampling distorts `count()`. Use aggregation (`summarize`) for counts; only high-frequency trace/latency data is sampled, preferring tail sampling to retain error samples.

**Q: Is it safe for the agent to set thresholds fully automatically?**
A: No. The agent provides **suggested values + rationale**, submitted as a PR/config for human review. Core-path thresholds must be human-confirmed to avoid false positives/negatives.

**Q: How is it guaranteed to work on others' projects?**
A: Three-layer guarantee — ① a unified IR data model; ② a plugin adapter architecture; ③ a single `sentinel.yaml` driving config. Changing project or platform requires no changes to core code.
