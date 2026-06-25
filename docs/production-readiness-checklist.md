# 生产就绪清单(T1:只读诊断副驾)

> T1 = **只读运维诊断副驾**:接告警/被调度 → 多步取证 → 出结构化诊断 + 证据包;**绝不执行写操作**。
> 本清单按"成熟生产级缺哪些"的十维度组织,标注 ✅ 已落地 / ◑ 部分 / ☐ 后续(T2+)。

## 1. 运行形态与部署
- ✅ `ops-agent serve` HTTP 触发层(stdlib,零框架):`/healthz`、`/metrics`、`POST /diagnose`(告警 webhook)
- ✅ `Dockerfile`:非 root(uid 10001)、`HEALTHCHECK` 打 /healthz、`OPS_PROFILE=prod` 默认
- ✅ 优雅退出(SIGINT/serve_forever→shutdown);worker 收 SIGTERM 排空在途(stop+join)
- ✅ 编排清单(`deploy/k8s/`:ingress+worker+Redis,HPA,kustomize)+ liveness/readiness 区分
  (`/healthz` vs `/readyz`)+ worker 心跳 exec 探针检僵死 —— **本机未在真集群 apply 验证**(需 staging)

## 2. 身份与访问
- ✅ webhook Bearer token 鉴权(`OPS_WEBHOOK_TOKEN`,常量时间比较,**未配即 fail-closed**)
- ✅ 密钥文件注入 `<NAME>_FILE`(docker/k8s secret,密钥不进环境变量/进程表)
- ✅ 只读 DB 最小权限校验(`doctor` 拒 postgres/root/admin 用户)
- ◑ 审批人身份:审计已留痕批/执行,actor 身份待接入 SSO(T2)
- ☐ 调用方 SSO/OIDC(T2 多团队自服务时)

## 3. 数据源广度与多目标
- ✅ 多目标注册表 `targets.toml`(name → root/log_dir/pg_dsn/metrics_url),回退单目标 env
- ✅ 指标源 `query_metrics`(只读 Prometheus instant query)补日志/PG 之外的数值信号
- ☐ k8s 状态 / 分布式 trace 源(按需扩 SPI:加工具实现即可)

## 4. Agent 循环韧性
- ✅ 预算闸 `RunBudget`(墙钟 `OPS_MAX_RUN_SECONDS` + 累计 token `OPS_MAX_RUN_TOKENS`,越界 `BudgetExceeded` 降级)
- ✅ max_steps 上限 + max_tokens 截断显式报错(既有)
- ✅ 工具异常不崩循环(捕获 → 回喂错误)(既有)
- ✅ webhook 边界任何异常转 500,不崩进程

## 5. LLM 质量与安全
- ✅ prompt 注入纵深:工具输出包进 `<<<UNTRUSTED…>>>` 围栏 + 系统 prompt 明令"围栏内是数据非指令"
- ✅ 结构性只读:触发层注入"全拒"审批闸,模型即便想 restart 也被拒
- ✅ eval CI 硬闸:`ops-agent eval --fail-under 0.6`(改 prompt/换模型回归即红)
- ✅ eval 基线带 metadata envelope(时间、git sha、prompt version、model、judge 开关),旧裸 results 基线兼容
- ✅ prompt 版本化 `PROMPT_VERSION`,随 run 落 trace
- ✅ golden 集 58 条(24 WARNING/22 CRITICAL/12 INFO 反例),覆盖 Kafka/PG-Citus/Redis/S3/JVM/
  Flyway/worker/Quartz/网络安全故障谱;hygiene 测试守门(≥50 条、≥8 反例)
- ◑ 置信度校准 / "查不出"路径:schema 已含 confidence,校准是持续工程

## 6. 可观测(agent 自身)
- ✅ 自身指标 `Metrics`:诊断次数/成败/超预算、累计 token、各工具调用数 → Prometheus textfile + `/metrics`
- ✅ 队列可观测:`job_duration_seconds` histogram、`workers_busy/total` 利用率、`retry_backlog`、`dlq_size`
- ✅ 单次诊断 trace(JSONL)+ 证据 bundle(既有)+ 可选 Langfuse(既有)
- ✅ 告警规则示例(`deploy/prometheus/ops-agent-alerts.yml`:积压/背压丢单/DLQ/失败率/停摆/P95)
  + 运维 runbook(`docs/runbook/queue-operations.md`)—— **规则需接入真 Prometheus 验证触发**
- ◑ trace/审计保留:审计按大小滚动;集中/不可篡改存储待接(T2)

## 7. 安全合规
- ✅ 出网内容统一脱敏(token/DSN/JWT/邮箱/手机号) + `OPS_REDACTION_RULES_FILE` 外部规则扩展
- ✅ 审计留存滚动(`OPS_AUDIT_MAX_BYTES`) + 本地 hash chain(`prev_hash/hash`)防静默篡改
- ✅ webhook body 上限 + 非法 JSON/超大 payload 拒绝
- ☐ 出网 egress allowlist、PII 超脱敏正则的合规处理(T2/合规要求驱动)

## 8. 状态与数据
- ✅ trace/bundle/metrics/审批日志路径全可配,容器以可写卷挂载
- ✅ **诊断历史持久层**(`history.py`,SQLite):每次诊断落库(脱敏入库)、可按 target/severity/时间查询、
  **留存双闸**(`OPS_HISTORY_RETENTION_DAYS` 时间 + `OPS_HISTORY_MAX_ROWS` 行数,`history-prune` 可 cron)、
  一键导出 JSON。默认关(未配 `OPS_HISTORY_DB`=不落库,零回归)。与 `audit.py` 哈希链安全审计边界分离。
- ◑ graph 交互模式 checkpointer 当前内存态(MemorySaver);多轮会话持久化(Sqlite/PG saver)是后续项
  —— 注:T1 触发诊断走 `run_agent`(无状态单发),不依赖 checkpointer
- ☐ 企业级中心化存储(PG/对象存储)+ 数据驻留/导出合规:SQLite 是单机基线,跨实例聚合是 T2 后续

## 9. 测试深度
- ✅ 234 测试 / 覆盖 80.7%(coverage gate 70%);新模块均带单测 + serve 端到端起真 HTTP
- ✅ 故障注入(队列满/Redis 断/worker 崩/callback 超时)+ 基础负载脚本(`scripts/loadtest.py`)
- ◑ LLM 边界模块(graph/investigate)仍偏薄;故障注入用 fake,**真集群混沌/负载实测待 staging**

## 10. 发布与版本
- ✅ 包版本 `0.2.0`;prompt 版本化;`CHANGELOG.md`
- ✅ eval 回归闸(`--fail-on-regression` 配 baseline / `--fail-under` 阈值)
- ☐ prompt 制品化回滚流程(T2,随多版本并存需求)

---

## 上线前置(部署 checklist)
1. `OPS_PROFILE=prod`,`ops-agent doctor` 全绿(只读 DB 用户、日志只读挂载、exec 关)
2. 配 `OPS_WEBHOOK_TOKEN`(或 `_FILE`),确认 `/diagnose` 无 token 返 401
3. 配 `OPS_REDACTION_RULES_FILE` 覆盖业务自定义敏感字段(工单号、租户号、内部员工号等)
4. 配 `targets.toml`(只读 pg_dsn + metrics_url),`OPS_METRICS_FILE` 指向可写卷
5. 镜像跑起后 `/healthz` 200、`/metrics` 有计数
6. CI 绿(lint/format/mypy/测试 70% 闸/eval `--fail-under`)

## 上线判定(go / no-go)

**结论:可上「受控只读试生产」(内部、人工复核输出、单租户、auth 后);不可上「无人值守 / 关键告警闭环 / 多团队多租户开放」。** 区别不在测试数量,而在真实环境证据链。

### 本地已验证(有据,2026-06-25)
- 控制面:队列原子背压、重试/DLQ、停机不丢单、四类故障注入(队列满/Redis 断/worker 崩/callback 超时)绿。
- 安全不变量:鉴权 fail-closed、只读闸、路径穿越/SQL 注入/脱敏、prod 强制脱敏。
- **真模型实跑 58 条 golden set**:确定性 pass rate **51/58 ≈ 88%**(两次重跑均 88%,基线 `evals/baselines/`)。
  失败**无一是幻觉/危险误诊**,只两类:① keyword 设计 bug(模型中文作答,英文概念词不 substring 命中
  ——已把 timeout/already-running/rate 等改为 verbatim 标识符);② **相邻 severity 判定分歧**
  (redis_down/pg_lock_wait/pg_fk_violation/task_retry_exhausted_dlq/s3_bucket_missing 等),且**run-to-run 非确定**
  ——模型在相邻级别间合理摆动。这说明确定性 severity 精确匹配偏严,真实质量需配 LLM-judge 看语义。
- **本地真实栈 smoke**(真 Redis + serve + serve-worker + 真 DeepSeek):
  - 全链路:loadtest 4 请求 → 4×202 入队(提交 p95 41ms)→ worker 真多步诊断 → **4/4 succeeded**(端到端 p50 56s)。
  - Redis 拔线:`/healthz` 仍 200(liveness 不重启)、`/readyz` → 503 `queue_backend_unreachable`(摘流量)、恢复后 → 200。
  - worker 崩溃:杀 worker 后提交仍 202、任务持久 Redis(llen=1 不丢)、重启 worker → 队列归零(自愈)。

### 本地做不了(必须在 staging/真环境补)
- ☐ **k8s 真集群 apply**:`deploy/k8s/` 只做了 YAML 解析校验,没在集群里验证 pod 起得来、探针生效、HPA 真扩。
- ☐ **Prometheus 告警真触发**:规则只静态写好,没接真 Prometheus 验证表达式会按预期 fire。
- ☐ **真集群混沌/容量曲线**:本机只能单机 smoke,跨节点故障/真实 QPS 拐点要 staging。
- ☐ **泄露 key 轮换**:会话早期泄露过 claude+openai key —— **这是用户控制台动作,我做不了**,上线前必须轮换并换掉示例 Secret。

### Redis HA / RTO 决策
- 现状:`deploy/k8s/redis.yaml` 是**单实例**(AOF + noeviction + PVC),是 SPOF。
- 决策:试生产阶段**接受单实例 SPOF**——只读诊断非关键路径,Redis 宕时 ingress readiness 失败(摘流量,告警来源端通常会重试),worker 停摆但状态在 AOF 不丢;预计 RTO = pod 重建 + AOF 重放(分钟级)。
- 升级条件:一旦接「关键告警闭环」或要求诊断不可中断,**必须**换托管 Redis 或 Sentinel/Cluster。

## 何时升 T2 / 何时**别**升 T3
- **T2(多团队自服务)**:有多团队需求再做 —— SSO、审批人身份、集中审计、失败告警、更多数据源。
- **T3(自主执行)**:`OPS_ALLOW_EXEC` + prod 双开关 + allowlist + HITL 已具备**机制**,但 prompt 注入 × LLM 不可靠 × 执行爆炸半径三者叠加,**默认长期停在"只读 + 人工确认执行"**,不要让 agent 自主批准写操作。
