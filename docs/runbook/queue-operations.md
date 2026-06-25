# Runbook：ops-agent 异步队列运维

适用于 **Step 2 分布式后端**(`OPS_QUEUE_BACKEND=redis` + `serve`(ingress)+ `serve-worker`(独立 worker))。
内存后端(`memory`)无 DLQ/跨进程状态,只读单机调试用,下文 DLQ/重入部分不适用。

配套告警规则:`deploy/prometheus/ops-agent-alerts.yml`。

---

## 指标速查(前缀 `ops_agent_`)

| 指标 | 类型 | 含义 | 来源进程 |
|---|---|---|---|
| `queue_depth{backend}` | gauge | 主队列待处理数 | ingress + worker |
| `retry_backlog{backend}` | gauge | 退避重试中的任务数(delayed zset) | worker |
| `dlq_size{backend}` | gauge | 死信堆积数 | worker |
| `workers_total{backend="redis"}` | gauge | worker 消费线程总数(利用率分母) | worker |
| `workers_busy{backend="redis"}` | gauge | 在途处理中的 worker 数(利用率分子) | worker |
| `job_duration_seconds` | histogram | 单任务处理耗时(含多步 LLM) | worker |
| `jobs_submitted_total` | counter | 入队总数 | ingress |
| `jobs_rejected_total` | counter | 背压拒收(队列满,上游收 429) | ingress |
| `jobs_succeeded_total` / `jobs_failed_total` | counter | 终态计数 | worker |
| `jobs_retried_total` | counter | 重试次数 | worker |
| `jobs_lost_total` | counter | 出队但状态缺失(丢单) | worker |

> **worker 利用率** = `workers_busy / workers_total`。持续 ≈1 且 `queue_depth` 上升 = 该扩容。
> **抓取**:ingress 指标走 `serve` 的 `/metrics`;worker 指标用 `METRICS.write_textfile` 落到
> node_exporter textfile collector 目录(worker 无 HTTP 端点)。

---

## §积压:queue_depth 持续升高

**症状**:`OpsAgentQueueBacklogHigh/Critical`,或上游开始收到 429(`jobs_rejected_total` 增长)。

1. **确认是消费慢还是入队洪峰**
   ```bash
   # 当前各水位
   ops-agent dlq                      # DLQ 条数(顺带确认 redis 连得上)
   # Prometheus:对比 rate(jobs_submitted_total[5m]) vs rate(jobs_succeeded_total[5m])
   ```
   - 入队速率 >> 成功速率 → 消费跟不上(下一步)。
   - 成功速率 ≈ 0 → worker 停摆,跳 §停摆。

2. **看 worker 是否满载**:`workers_busy/workers_total ≈ 1` 且 `job_duration_seconds` P95 变大 → 算力不够。
   - **扩容**:多起几个 `serve-worker` 进程,或调大单进程 `OPS_WORKER_COUNT`。
     ```bash
     OPS_WORKER_COUNT=8 ops-agent serve-worker      # 单进程内 8 个消费线程
     ```
   - worker 水平扩展安全:状态在 Redis,`BRPOP` 保证一个任务只被一个 worker 取到。

3. **临时泄压**:上游限流 / 调大 `OPS_QUEUE_MAX`(只是把背压点后移,不解决根因)。

4. **若 P95 耗时是元凶**:单次诊断变慢通常是多步 LLM 或 PG 取证慢。看 Langfuse trace 定位哪步贵(`docs/observability-setup.md`),裁上下文 / 换小模型 / 加模板化 SQL。

---

## §死信:dlq_size > 0

**症状**:`OpsAgentDlqGrowing`。任务超 `OPS_MAX_RETRIES` 后进 DLQ。

1. **先查根因,别急着重入**(否则重入了还会再失败回 DLQ):
   ```bash
   ops-agent dlq                      # 列出死信 job_id
   ops-agent history --trace <id>     # 若开了 OPS_HISTORY_DB,看该任务诊断/错误
   ```
   常见根因:下游(LLM 余额/限流、PG 不可达)、payload 触发的诊断逻辑 bug。

2. **修好根因后重入**(逐个,会重置 attempts):
   ```bash
   ops-agent dlq --requeue <JOB_ID>
   ```
   `dlq_requeue` 是原子的(WATCH+MULTI:lrem+hset+lpush),不会"删了没回队"或"误入非死信"。

3. **批量重入**(确认根因已修):
   ```bash
   for id in $(ops-agent dlq | grep -oE '[0-9a-f]{32}'); do
     ops-agent dlq --requeue "$id"
   done
   ```

4. 重入后盯 `jobs_succeeded_total` 上升 + `dlq_size` 归零。若又落回 DLQ → 根因未除,停手再查。

---

## §重试积压:retry_backlog 高

**症状**:`OpsAgentRetryBacklogHigh`。大量任务在退避重试(指数退避 `base*2^(n-1)`,封顶 `OPS_RETRY_MAX_SECONDS`)。

- 通常是下游**间歇性**不稳(LLM 偶发 429 / PG 抖动)。重试会自愈,但持续高说明下游一直不稳。
- 看 `jobs_failed_total` 与 `jobs_retried_total` 的比例判断是"重试后成功"还是"重试耗尽进 DLQ"。
- 下游确实挂了 → 上游限流减少无谓重试;恢复后重试积压会自然消解。

---

## §停摆:有积压但零产出

**症状**:`OpsAgentDiagnoseStalled`(`queue_depth>0` 但成功/失败速率都为 0)。

1. **worker 进程是否存活**:`ps aux | grep serve-worker`。崩了就拉起(k8s 会自动重启,见 `deploy/k8s/`)。
2. **Redis 连通性**:worker 连不上 Redis 时 `BRPOP` 会异常,`_worker_loop` 吞掉异常继续循环(不崩),但不消费。查 worker 日志 `worker 循环异常`。
3. **优雅停机卡住**:`serve-worker` 收 SIGTERM 后等在途任务 join(上限 10s)。滚动更新时短暂停摆正常。
4. 确认 worker 起来后,`workers_total` 应回到配置值,`workers_busy` 开始波动。

---

## §扩 worker(容量规划)

- 单进程并发 = `OPS_WORKER_COUNT`(消费线程数)。CPU 不是瓶颈(大头是等 LLM I/O),可适当高于核数。
- 跨进程/跨节点:多起 `serve-worker`,无需协调(Redis `BRPOP` 天然分发)。
- 粗算目标 worker 数 ≈ `入队速率(/s) × 单任务 P50 耗时(s)`,再留 1.5~2x 余量给洪峰。
- k8s 下用 HPA 按 `queue_depth` 或 CPU 扩(见 `deploy/k8s/worker-deployment.yaml`)。

---

## 相关

- 告警规则:`deploy/prometheus/ops-agent-alerts.yml`
- k8s 部署:`deploy/k8s/`
- trace 下钻(单次诊断为何慢/差):`docs/observability-setup.md`
- 关键环境变量:`OPS_QUEUE_BACKEND` / `OPS_REDIS_URL` / `OPS_WORKER_COUNT` / `OPS_QUEUE_MAX` / `OPS_MAX_RETRIES` / `OPS_RETRY_BASE_SECONDS` / `OPS_RETRY_MAX_SECONDS` / `OPS_JOB_TTL_SECONDS`
