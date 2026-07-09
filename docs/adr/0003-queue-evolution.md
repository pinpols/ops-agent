# ADR-0003:触发/并发模型演进 + 队列热路径原子性

**Status**: Accepted

## Context
最初 `serve /diagnose` 把整条诊断(10–60s)同步内联跑完才返回。对告警 webhook(Alertmanager/PagerDuty 期望秒级 ack)是反模式 → 超时 → 重试风暴 → 重复诊断。需要削峰、背压、可恢复、可水平扩展,但又不想一上来就引中间件、破坏零回归。

## Decision
**分步、可独立交付、默认关零回归**(详见 [architecture-evolution.md](../architecture-evolution.md)):
- **Step 1 进程内队列**(`jobqueue.py`):有界 `queue.Queue` + worker 线程池,`OPS_ASYNC_DIAGNOSE=true` 时入队 + 202;满则 429 背压。零中间件。
- **Step 2 Redis + 独立 worker**(`redisqueue.py` + `serve-worker`):跨进程共享状态,崩溃可恢复,水平扩展;DLQ + 指数退避重试。`redis` 为可选依赖。
- **Step 3 韧性收口**:trace_id 贯穿、队列指标、优雅停机排空。
- 两后端**同一对外接口**(submit/get/consume),编排核 `run_agent` 不变。

**热路径原子性**(审计后补强):Redis 的 `submit`/`dlq_requeue`/`fail_or_retry`/`complete`/`promote_due_retries`/`mark_running` 全用 WATCH/MULTI + 存在/终态守卫 —— 否则 `hincrby`/裸 `hset` 在 hash TTL 过期后会重建无 question 的僵尸 job、已 SUCCEEDED 的 job 会被迟到 retry 覆写回 QUEUED、`zrem→lpush` 崩溃窗口丢重试任务。

**崩溃安全消费(at-least-once 落实,2026-07 补强)**:此前 worker 用 BRPOP **破坏性出队**,硬崩(SIGKILL/OOM/滚动发布超 grace period)后在途任务卡 RUNNING 直到 24h TTL 蒸发 —— 声明的 at-least-once 实际是 at-most-once。现改为:
- `consume` 用 **BLMOVE(RIGHT→LEFT)** 把 job_id 原子挪进 per-worker processing list(`<queue>:processing:<worker_id>`),出队即有在途登记;
- `mark_running` 记 `running_since`/`worker`;`complete`/`fail_or_retry` 在同一 MULTI 里 LREM 摘除登记;
- worker 心跳落 Redis zset(`<queue>:workers`),**reaper**(worker 启动时 + 每 `OPS_REAPER_INTERVAL_SECONDS`)扫两处:①心跳超 `OPS_WORKER_DEAD_AFTER_SECONDS` 的死 worker 的 processing list,在途任务经 `fail_or_retry` 回灌/进 DLQ;②RUNNING 超 `OPS_STALE_RUNNING_SECONDS`(默认 2×run 预算)的卡死任务兜底回收;
- 优雅停机排空窗口 = `OPS_MAX_RUN_SECONDS`+10s,k8s `terminationGracePeriodSeconds` 相应调到 150(原 30 < 单次 run 预算 120,滚动发布必然截杀在途任务)。

## Alternatives(否决/推迟)
- **一上来就上 Kafka/SQS**:重、破坏零回归、单/少实例 YAGNI,推迟到 Step 2 用 Redis。
- **Redis 用 Lua 脚本保证原子**:fakeredis 不支持 EVAL,改用 WATCH/MULTI 乐观事务(测试友好且足够)。
- **at-most-once**:崩溃即丢任务,不可接受 → 选 at-least-once + 终态守卫去重副作用。

## Consequences
- ✅ 秒级 ack、背压、跨进程可恢复、水平扩展;memory 后端零依赖适合单机/调试。
- ✅ 原子性 + 终态守卫有 fakeredis 单测(并发背压、僵尸不复活、终态不覆写)。
- ⚠️ at-least-once → 任务可能重复处理(崩溃回收会再派发一次);靠终态守卫 + 幂等副作用消化。
- ⚠️ reaper 的 RUNNING 兜底扫描遍历 job hash(SCAN);任务量大时是背景开销(当前规模可忽略)。
- ⚠️ 单实例 Redis 是 SPOF(试生产接受,RTO 分钟级 AOF 重放;关键路径须换托管/Sentinel,见就绪清单)。
- ⚠️ `promote_due_retries` 每个 worker 每秒扫一次 retry zset,大规模下是扫描风暴(YAGNI,记着)。
