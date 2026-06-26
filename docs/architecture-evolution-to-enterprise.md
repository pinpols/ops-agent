# ops-agent 架构演进路线图:同步请求 → 事件驱动队列

## 当前状态(T1 生产化,但并发隐患)

```
告警/CLI → HTTP(同步) → [webhook] → run_agent(ReAct) → response(阻塞 10-60s)
```

**问题**:
- 告警系统期望秒级 2xx ack,你阻塞 30s → 超时 → 重试风暴 / 重复诊断
- `ThreadingHTTPServer`(thread-per-request + GIL) → 并发顶不住
- 无背压、无重试策略、worker 崩溃诊断丢失
- 拓扑是"单进程内联" → 难水平扩

**影响等级**: 告警量 < 10/min 时无感;超 50/min 开始不稳定

---

## 目标架构(企业级,分两步达成)

### Step 1(本周期,改动<500 行代码):解耦 webhook,进程内异步 worker

```
告警/CLI ──HTTP──▶ [webhook ingress]
                    立即 202 Accepted
                        │
                    ┌───▼────┐
                    │  queue  │ (stdlib queue.Queue,线程安全)
                    └─────────┘
                        │
                 worker thread 消费
                        │
                  run_agent(ReAct)
                        │
          ┌─────────────┼─────────────┐
      落库│           Slack/工单     指标
      history                        trace
```

**交付**:
- `/diagnose` 入队 + 202(不阻塞)
- 后台 worker thread 消费、记录、告知
- **零额外依赖**(stdlib `queue` + `threading`)

**收益**:
- ✅ 告警系统秒级 ack(不再超时重试)
- ✅ 背压:队列长度可配、超限拒绝或降级
- ✅ 故障隔离:worker 崩溃不影响 ingress
- ⚠️ 仅单进程,不是分布式

---

### Step 2(T2,需中间件,下次迭代):真队列 + 独立 worker 进程

```
ingress ──▶ [Redis/SQS/Kafka] ◀── worker process(可扩多个)
             队列         后端存储(PG/S3)
```

**收益**:
- ✅ 水平扩展(多 worker 并行)
- ✅ 持久化重试(队列持久化,worker 崩溃恢复)
- ✅ 分布式观测(trace/指标跨进程聚合)
- ⚠️ 运维复杂度上升(需部署中间件)

---

## Step 1 设计细节

### 消息定义

```python
@dataclass
class DiagnosisTask:
    task_id: str
    target: str
    requested_at: datetime
    requester_ip: str
    callback_url: str | None  # 诊断完回调通知
```

### 队列配置

```yaml
ops.worker:
  queue-size: 100          # 队列长度,超限行为
  queue-overflow: reject   # reject|drop-oldest
  worker-threads: 2        # 消费并发
  timeout-per-task: 120    # worker 最多等多久(网络故障降级)
```

### Ingress 职责(新)

1. 校验 webhook token(既有)
2. 构造 `DiagnosisTask`
3. **尝试入队**(非阻塞,`queue.put_nowait` 或 timeout):
   - 成功 → 202 + `{task_id, est_time}`
   - 队列满 → 503 + retry-after(背压)
   - 配额超 → 429

### Worker 职责(新)

1. 从队列持续消费(FIFO)
2. 每个 task:
   - 设置 task 运行超时(截断、不阻塞)
   - `run_agent(diagnosis)` → 同步,无改
   - 记录结果(history DB)
   - 可选:POST callback_url 通知触发方
   - 异常 → warn + 下一个(绝不挂)

### Webhook 交互变化(只有响应变了)

**旧**(同步,阻塞 30s):
```
POST /diagnose
{target, request...}
→ 200 {diagnosis, tokens, ...}  (阻塞 30s)
```

**新**(异步,秒级返回):
```
POST /diagnose
{target, request...}
→ 202 Accepted {
    task_id: "task-20250625-abc123",
    est_seconds: 45,
    status_url: "/diagnose/task-20250625-abc123"  # 可选轮询
}
```

**轮询端点**(可选,用于需要 blocking 的场景):
```
GET /diagnose/task-{id}
→ 202 {status: "running", elapsed: 15}
   或 200 {status: "completed", diagnosis: {...}, tokens: ...}
   或 404 {status: "unknown"}
```

---

## 验收标准(Step 1)

- [ ] 新 `Worker` 类完整(入队/消费/异常处理/优雅退出)
- [ ] `/diagnose` 返回 202 + task_id(不阻塞)
- [ ] `GET /diagnose/task/{id}` 轮询端点
- [ ] Worker 能完整消费所有诊断类型(diagnose/investigate/evaluate)
- [ ] 队列满/超配额 → 503/429(背压正常工作)
- [ ] Worker 异常不打断循环,自动恢复
- [ ] 单元测试+e2e(入队→消费→查询完整链路)
- [ ] CHANGELOG + 操作文档(如何配队列大小、轮询结果)

---

## Step 1 风险与缓解

| 风险 | 缓解 |
|---|---|
| 队列内存爆炸(无法保证释放) | 配 `queue-size` + 溢出策略(reject 或 drop-oldest);超期自动清 |
| Worker 挂起(阻塞不返回) | task-level 超时(`timeout-per-task`);定期健康检查 |
| 轮询结果过期(cleanup 不及时) | 结果保留期可配(默认 1h);自动 GC |
| 多 worker 争资源(竞争 LLM API) | worker-threads 默认 2,可配;LLM 侧的 rate-limit 本就在(`TokenBucketRateLimiter`) |

---

## 代码结构变化(Step 1)

```
ops_agent/
  ├── worker.py              (NEW) Worker + DiagnosisTask + queue ops
  ├── llm.py                 (NO CHANGE) agent 推理循环留原样
  ├── agent.py               (EDIT) run_agent 不变,但增加 worker 初始化
  ├── server.py              (EDIT) /diagnose 改 202 入队;新增 GET /diagnose/{task_id}
  ├── config.py              (EDIT) worker 配置块
  └── tests/
      └── test_worker.py     (NEW) 入队/消费/超期/异常 5+ cases
```

**改动行数估计**: +300 worker.py / +150 server.py / +80 config / +80 test ≈ **610 行**

---

## Step 2 预留(不实现,仅设计)

当 ops-agent 部署规模达到:
- 单进程日诊断量 > 500
- 单个 worker 诊断平均时长 > 3min(网络/外部系统慢)
- 需要跨地域/容错恢复

升 Step 2:
1. 把 `queue.Queue` 换 Redis Stream / SQS / Kafka
2. 把 `Worker` 拆出独立进程/Celery task
3. 加分布式 tracing(OpenTelemetry)
4. 加任务持久化 + 死信队列

---

## 下一步

1. **本周期 Step 1**: 
   - impl Worker + 队列(~600 行)
   - 改 webhook /diagnose + 新增 /diagnose/{task_id}
   - e2e 验证(入队→消费→查询)
   - 性能测试(queue 满载、背压行为)
   
2. **下周期评估**:
   - 部署使用 1-2 周后,根据真实诊断量和延迟反馈,决定是否上 Step 2
   - 若未来需要分布式,Step 1 的 `Worker` + `DiagnosisTask` 接口已预留兼容

---

## 相关文档

- `docs/production-readiness-checklist.md` —— T1 涉及的 10 维度进度
- `CHANGELOG.md` —— 发布历史(包括本次 Step 1)
- 运维 runbook(待补):队列配置调优、worker 日志解读、故障排查
