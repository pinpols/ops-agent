# 架构演进:从同步请求驱动 → 事件驱动 + 队列 + 异步 worker

> 目标:让 ops-agent 的**触发与并发模型**达到企业级主流、健壮的形态。
> 推理核(ReAct 工具调用循环)是主流的、**保持不变**;演进的是它外面的"怎么被触发、怎么并发跑"。

## 1. 现状与问题

**推理核(`run_agent`)= 阻塞式 ReAct 工具调用循环**:`for step in range(max_steps)` → 等 LLM → 执行 tool_use → 喂回 → 收敛出结论。阻塞在 I/O 上,**不是忙轮询**,是主流模式(对齐 Anthropic SDK 范式 / LangGraph `create_react_agent`)。**保留。**

**问题在系统的触发与并发模型**:
- `serve /diagnose` 把整条诊断(10–60s)**同步内联跑完**才返回。对告警 webhook(Alertmanager/PagerDuty 期望秒级 2xx ack)是反模式 → **超时 → 重试风暴 → 重复诊断**。
- `ThreadingHTTPServer` = thread-per-request 阻塞 + stdlib `http.server`,并发上量后线程开销 + GIL 顶不住。
- 单进程内联:触发与处理耦合,无削峰、无背压、worker 崩溃不可恢复。

## 2. 目标架构

```
告警/触发 ──HTTP──▶ ingress: 校验 + 入队 ──▶ 立即 202 Accepted {job_id}
                                  │
                              [queue]
                                  │
                         worker pool(异步消费)
                                  │
                         run_agent(ReAct 循环不变)
                                  │
            ┌─────────────────────┼─────────────────────┐
        落 history DB         回调/Slack/工单         指标/trace
```

收益:**秒级 ack、削峰、重试/退避、背压、水平扩展、worker 崩溃可恢复**。

## 3. 分步推进(低风险、可独立交付)

### Step 1 —— 进程内解耦(本轮做,零中间件、零回归)
- 新增 `jobqueue.py`:`queue.Queue`(有界,满则背压)+ worker 线程池;`DiagnosisJob`(id/状态/结果);结果有界缓存(TTL/上限)。
- `serve` 启动时拉起 worker pool;`/diagnose`(`OPS_ASYNC_DIAGNOSE=true` 时)**入队 + 立即 202 {job_id}**,队列满 → 429。
- 新增 `GET /jobs/{id}` 查状态/结果(queued/running/succeeded/failed)。
- 默认仍同步(`OPS_ASYNC_DIAGNOSE` 默认 false)→ **零回归**;开关一开即得"快 ack + 异步处理 + 背压"。
- worker 跑 `run_agent` → 落 history → 可选回调(`OPS_CALLBACK_URL`)。

**消掉的真隐患**:同步阻塞告警 webhook。

### Step 2 —— 真队列 + 独立 worker 进程(T2 规模才需要)
- 队列换 Redis / SQS / Kafka;worker 拆成独立进程,可水平扩展。
- ingress 进程只做"校验 + 入队 + 202",彻底无状态、轻量。
- 至此达成完整事件驱动、可独立扩缩、跨实例可恢复。

### Step 3 —— 可观测与韧性收口(可并入既有可观测线)
- trace_id 贯穿 ingress→queue→worker→回调;队列深度/worker 利用率/处理时延进 `/metrics`。
- 死信队列(DLQ)+ 重试退避;worker 优雅停机时排空在途。

## 4. 不变量(演进中必须守住)
- 推理核 `run_agent` 接口与行为不变;只是被 worker 调用而非被 HTTP handler 直接调用。
- 只读/审批闸/脱敏/预算闸等安全控制全部保留(worker 同样注入"全拒"审批闸)。
- 默认关 = 零回归;开关与配置可逐环境灰度。

## 5. 进度
- [x] **Step 1**:进程内有界队列 + 异步 webhook(202+job_id)+ `GET /jobs/{id}` + 背压(429)+ 优雅停机
  + 可选回调 + 队列指标(jobs_submitted/succeeded/failed/rejected 进 `/metrics`)。默认关零回归。
- [ ] Step 2:真队列(Redis/SQS)+ 独立 worker 进程(T2 规模才需要;`JobQueue` 接口已为换后端预留)
- [ ] Step 3:trace_id 贯穿 ingress→queue→worker→回调 + DLQ/重试退避(队列计数已先落地)
