# Runbook：Flink 流作业只读诊断

ops-agent 以 **只读** 方式接入 Flink:`query_flink_rest`(GET-only REST)+ `query_metrics`(Prometheus)。
**绝不**做 cancel/stop、trigger savepoint、改并行度、删 state —— 这些是写操作,必须走 HITL/审批路径,本工具链永不涉及(只读 T1 边界)。

配置:`OPS_FLINK_URL=http://jobmanager:8081`(或 `targets.toml` 的 `flink_url`)+ Flink 的 Prometheus 指标已被抓取。

## query_flink_rest 只读端点(白名单)

| 路径 | 看什么 |
|---|---|
| `/overview` | 集群:slots、job 数、TM 数 |
| `/jobs` / `/jobs/overview` | 列作业 + 拿 jobid |
| `/jobs/<id>` | 状态、**restart count**、duration、vertices |
| `/jobs/<id>/exceptions` | 异常根因(最该先看的) |
| `/jobs/<id>/checkpoints` | checkpoint 成败/耗时/size、失败计数 |
| `/jobs/<id>/vertices/<vid>/backpressure` | 算子反压比 |
| `/taskmanagers` / `/taskmanagers/<id>/metrics` | TM 存活、CPU/堆/GC/managed memory |
| `/jobmanager/metrics` | JM 侧指标 |

诊断顺序:`/jobs` 拿 id → `/jobs/:id`(状态/重启)→ `/jobs/:id/exceptions`(根因)→ 按现象下钻 checkpoints / backpressure。

## Flink Prometheus 指标模板(query_metrics 用)

> 指标名取决于 Flink metrics reporter 的 `scope` 配置;下面是常见命名,按你的实际前缀调整。

```promql
# checkpoint 连续失败 / 超时
flink_jobmanager_job_numberOfFailedCheckpoints
rate(flink_jobmanager_job_numberOfCompletedCheckpoints[10m])
flink_jobmanager_job_lastCheckpointDuration            # 逼近 checkpoint timeout 即危险

# 重启循环
flink_jobmanager_job_numRestarts
flink_jobmanager_job_uptime                            # 频繁归零 = 在重启

# 反压 / 吞吐
flink_taskmanager_job_task_isBackPressured
flink_taskmanager_job_task_busyTimePerSecond
rate(flink_taskmanager_job_task_numRecordsIn[1m])
rate(flink_taskmanager_job_task_numRecordsOut[1m])

# Kafka source lag(source 堆积)
flink_taskmanager_job_task_operator_KafkaSourceReader_records_lag_max
flink_taskmanager_job_task_operator_currentEmitEventTimeLag

# TaskManager 存活 / 资源
flink_jobmanager_numRegisteredTaskManagers             # 掉数 = TM lost
flink_taskmanager_Status_JVM_Memory_Heap_Used
flink_taskmanager_Status_JVM_GarbageCollector_*_Time
flink_taskmanager_Status_Flink_Memory_Managed_Used     # RocksDB state backend 压力
```

## 故障场景 → 取证清单

| 现象 | 先看 | 典型根因 |
|---|---|---|
| **checkpoint 连续失败/超时** | `/jobs/:id/checkpoints` + `lastCheckpointDuration` | state 太大、反压拖慢 barrier、对齐慢、外部存储慢/不可达 |
| **job restart loop** | `/jobs/:id/exceptions` + `numRestarts` | 反复抛同一异常(脏数据/NPE)、外部依赖不可用、资源不足 |
| **backpressure** | `isBackPressured` + `busyTimePerSecond` + `/vertices/:vid/backpressure` | 下游算子/sink 处理慢、热点 key、并行度不足 |
| **source lag 堆积** | Kafka `records_lag_max` + `numRecordsOut` | 消费跟不上、反压、并行度 < 分区数 |
| **TaskManager lost** | `/taskmanagers` + `numRegisteredTaskManagers` + TM 日志 | OOM kill、节点故障、网络分区、心跳超时 |
| **OOM / RocksDB state** | TM `Managed_Used` + Heap + GC + exceptions | managed memory 不足、state 膨胀、写放大 |
| **Kafka offset 卡住** | offset 不前进 + `numRecordsOut=0` | 反压全堵、分区无新数据(误报)、提交失败 |
| **savepoint 超时** | exceptions + checkpoint 大小 | state 太大、目标存储慢/无权限、反压期间触发 |

## 只读 → 人工的交接
ops-agent 给到:根因方向 + 证据(REST/指标快照)+ 分级 + `needs_human_review` 信号。**修复动作**(重启 job、调并行度、扩 TM、改 SQL、清 state)由人按本表执行或走审批,不自动跑。

## 实测基线(真模型 + 真 Flink)
- **诊断质量**:9 条 `flink_*` golden 打真 DeepSeek,确定性 pass rate **8/9 = 89%**(与主 golden 集 ~88% 一致)。唯一未过是 `flink_checkpoint_failures` 的 severity 边界分歧——模型判 WARNING(作业仍 RUNNING)、本集判 CRITICAL(checkpoint 连续失败威胁 exactly-once / 恢复丢进度)。**非误诊/幻觉**,是相邻级别的校准取舍。
- **工具连真集群**:`query_flink_rest` 对真 Flink JobManager REST 实测(见 PR 描述):`/overview`、`/jobs`、`/jobs/:id` 实打实返回并解析;写路径(savepoints/stop)被白名单拦在出网前。

相关:`evals/cases.py` 的 `flink_*` 回归样本、`deploy/prometheus/ops-agent-alerts.yml`。
