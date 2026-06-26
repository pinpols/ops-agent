# ops-agent 系统设计

> 定位:**T1 只读运维诊断副驾** —— 接告警/被调度 → 多步取证 → 出结构化诊断 + 证据,**绝不执行写操作**。
> 本文是系统的「**全貌 + 为什么这么设计**」。触发/并发模型的演进见 [architecture-evolution.md](../architecture-evolution.md);
> 关键决策的取舍记录见 [ADR 目录](../adr/)。

---

## 1. 一句话架构

不可信日志进来,经过**鉴权 → 入队 → worker 跑 ReAct 取证循环 → 结构化诊断出去**;
安全不是靠 prompt 约束,而是**结构层强制**(只读闸 + 注入围栏 + 脱敏 + 预算闸),
所以"模型被劫持"也越不过红线。

## 2. 分层

```
┌─────────────────────────────────────────────────────────────────┐
│ 触发层 (transport)   serve(HTTP webhook) · serve-worker · CLI     │  鉴权/入队/路由
├─────────────────────────────────────────────────────────────────┤
│ 编排层 (orchestration)  run_agent(多步ReAct) · diagnose_log(单发)  │  循环/预算/审批/脱敏
│                         investigate · graph_agent                 │
├─────────────────────────────────────────────────────────────────┤
│ 工具层 (tools)   read_logs · query_pg(_template) · query_metrics   │  只读取证 + restart_service(危险,默认拒)
│                  list_services · tail_recent_errors · read_app_config│
├─────────────────────────────────────────────────────────────────┤
│ 能力层 (capability)  llm(provider网关) · redaction · budget · prompts│  跨切面
│                      models(Diagnosis schema) · audit               │
├─────────────────────────────────────────────────────────────────┤
│ 状态层 (state)   jobqueue(内存) / redisqueue(Redis+DLQ) · history(SQLite)│  队列/持久化
│                  trace_io · metrics(textfile/Prometheus)           │
├─────────────────────────────────────────────────────────────────┤
│ 配置层 (config)  Settings(env, fail-fast) · targets(多目标注册表)    │  不可变 + prod 失败关闭
└─────────────────────────────────────────────────────────────────┘
```

**层间规矩**:上层依赖下层;能力层是跨切面被各层调用。已知的一处反向耦合(`worker_main` 反向 import `server.diagnosis_job_handler`)记在 [ADR-0005](../adr/0005-diagnosis-paths.md) 的"待治"里。

## 3. 端到端数据流

### 3.1 异步告警链路(生产形态,`OPS_ASYNC_DIAGNOSE=true` + `OPS_QUEUE_BACKEND=redis`)

```
告警源 ──POST /diagnose(Bearer)──▶ ingress(serve)
                                     │  鉴权 fail-closed → 入队(背压满则 429)
                                     ▼
                                   202 {job_id, trace_id}        ← 秒级 ack,不阻塞告警源
                                     │
                              Redis 队列(LIST + HASH + DLQ + retry zset)
                                     │  BRPOP
                                     ▼
                          serve-worker(独立进程,可水平扩展)
                                     │  注入 deny-all 审批闸
                                     ▼
                          run_agent ── ReAct 多步:取证工具×N → report_diagnosis
                                     │
              ┌──────────────────────┼───────────────────────────┐
              ▼                      ▼                            ▼
        history(SQLite,脱敏入库)   回调 OPS_CALLBACK_URL(SSRF守卫)   metrics/trace
                                     │
                         失败 → 指数退避重试 → 超限进 DLQ(ops-agent dlq 运维)
```

下游用 `GET /jobs/{id}`(同等鉴权)轮询到终态;响应里带 `needs_human_review` 路由信号([ADR-0004](../adr/0004-needs-human-review.md))。

### 3.2 同步/CLI 链路
`OPS_ASYNC_DIAGNOSE=false`(默认,零回归)时 `/diagnose` 内联跑完再返回;CLI `diagnose`(单发)/`investigate`/`chat`(多步)直接调编排层。

## 4. 安全模型(本项目的承重设计)

纵深五层,**任一层不依赖模型"听话"**:

| 层 | 机制 | 强度 |
|---|---|---|
| **鉴权** | webhook Bearer 常量时间比较,**未配 token 即 fail-closed**(401);`/jobs` 同等鉴权 | 硬 |
| **只读结构闸** | webhook 注入 `_deny_all_approver`,任何 `DANGEROUS_TOOLS`(restart_service)执行前过闸,deny-all 返 False → impl **永不运行**。**与模型行为无关**——模型被注入劫持也越不过 | 硬(确定性 100%) |
| **注入防御** | 工具输出包进 `<<<UNTRUSTED…>>>` 围栏(伪造闭标记被中和)+ 系统 prompt 反注入条款;四条诊断路径姿态统一 | 概率(best-effort ~94% 实测) |
| **出网脱敏** | 工具输出喂回 LLM / 落 trace / 回调前统一 `redact_text`(token/DSN/JWT/邮箱/手机号);prod 强制开 | 硬(prod) |
| **预算闸** | `RunBudget` 墙钟 + 累计 token 越界即 `BudgetExceeded` 降级;max_steps 上限 | 硬 |

**核心认知**:注入抗性是概率防御,会偶尔被绕;所以**高代价动作(写操作)的保证放在结构闸**(确定性),不放在 prompt。详见 [ADR-0001](../adr/0001-read-only-structural-gate.md) / [ADR-0002](../adr/0002-prompt-injection-defense.md)。

exec 路径(restart_service)即便被放行也还有:服务白名单 + `OPS_ALLOW_EXEC` + prod 双开关 `OPS_PROD_ALLOW_EXEC` + 命令白名单 + 非 shell argv + 审批审计链。webhook 路径下它**从网络侧完全不可达**(deny-all)。

## 5. 诊断的产出物:Diagnosis

结构化 schema(`models.py`),用 function calling 的 `tool_choice` 强制模型按 schema 填:

| 字段 | 含义 |
|---|---|
| severity | INFO/WARNING/CRITICAL(枚举约束) |
| summary / root_cause / evidence / suggested_action | 结论 + 根因 + 日志证据 + 排查方向 |
| confidence | 0~1 模型自评把握 |
| **needs_human_review** | **派生**(computed_field):confidence<0.6 或 CRITICAL<0.8 → 转人工。不进 LLM 的 input schema,模型改不了、注入篡改不了。见 [ADR-0004](../adr/0004-needs-human-review.md) |

## 6. 四条诊断路径

| 路径 | 形态 | 用途 | 安全姿态 |
|---|---|---|---|
| `run_agent` | 多步 ReAct + 工具 | **生产**(webhook/worker) | 围栏+脱敏+反注入+审批闸 |
| `diagnose_log` | 单发(无工具) | CLI 单文件 / eval | 围栏+反注入 |
| `investigate` | 单工具单回合 | 教学/CLI | 围栏+脱敏+反注入 |
| `graph_agent` | LangGraph 版 | 教学/对照 | 围栏+脱敏+反注入 |

四路一致是审计后收口的结果(曾漂移:diagnose_log/investigate/graph 一度缺围栏)。**根治方向是抽共享"诊断回合"内核**,记在 [ADR-0005](../adr/0005-diagnosis-paths.md)。

## 7. 状态与可观测

- **队列**:内存(`jobqueue`,单进程零中间件)/ Redis(`redisqueue`,跨进程可恢复 + DLQ + 退避重试)同一对外接口。原子性(WATCH/MULTI + 终态守卫)见 [ADR-0003](../adr/0003-queue-evolution.md)。
- **历史**:`history` SQLite,脱敏入库,按 target/severity/时间查询 + 留存双闸。
- **指标**:零依赖 `Metrics`(counter/gauge/histogram)→ Prometheus textfile + `/metrics`。队列深度/利用率/耗时/DLQ/转人工率;告警规则 `deploy/prometheus/`,排障 `docs/runbook/queue-operations.md`。
- **质量**:版本化 prompt(`PROMPT_VERSION`)+ 58 条 golden eval(确定性 + LLM-judge)+ CI 回归门禁;真模型基线 `evals/baselines/`。

## 8. 配置面原则

`Settings`(`config.py`)≈50 个 env,但**扁平 + 不可变 + 失败关闭**:`OPS_PROFILE` 闭集校验拒绝静默降级;prod 对脱敏/自由 SQL/exec 一律 fail-closed;密钥支持 `*_FILE` 间接注入。`doctor` 给单一就绪视图。

## 9. 部署拓扑

`deploy/k8s/`:ingress(serve)×N + Service + HPA、worker(serve-worker)×M + 心跳 exec liveness、Redis(AOF+noeviction)。`/healthz`=liveness、`/readyz`=探后端 readiness。详见 [deploy/k8s/README.md](../../deploy/k8s/README.md)。

## 10. 必须守住的不变量

1. 只读:webhook 永不执行写工具(结构闸,非 prompt)。
2. 默认关 = 零回归(async/redis/history/exec 全可灰度)。
3. 四条诊断路径安全姿态一致(围栏/脱敏/反注入)。
4. prod 安全闸 fail-closed,不静默降级。
5. `needs_human_review` 等路由信号是派生只读,模型/注入改不了。

## 关联文档
- 触发/并发演进:[architecture-evolution.md](../architecture-evolution.md)
- 决策记录:[adr/](../adr/)
- 上线判定:[production-readiness-checklist.md](../production-readiness-checklist.md)
- 队列运维:[runbook/queue-operations.md](../runbook/queue-operations.md)
- 概念教学(分阶段):`docs/phase*-concepts.md`
