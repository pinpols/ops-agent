# Runbook：Kafka 只读诊断

ops-agent 以**只读**方式接入 Kafka:`query_kafka_rest`(GET-only Kafka REST v3)+ `query_metrics`(Prometheus,通常更通用)。
**绝不**建/删 topic、改配置、重置 offset、reassign 分区——这些是写操作,走 HITL/审批,本工具链永不涉及。

配置:`OPS_KAFKA_REST_URL=http://kafka-rest:8082`(Confluent REST Proxy / kafka-rest,或 `targets.toml` 的 `kafka_rest_url`)。
没部署 Kafka REST 也没关系——下面 PromQL 用 `query_metrics` 一样能诊断。

## query_kafka_rest 只读端点(白名单,REST v3 Admin)

| 路径 | 看什么 |
|---|---|
| `/v3/clusters` | 拿 cluster id |
| `/v3/clusters/:id/brokers` `/brokers/:n` | broker 存活、controller |
| `/v3/clusters/:id/topics` `/topics/:t` | topic 列表/配置 |
| `/v3/clusters/:id/topics/:t/partitions[/:p[/replicas]]` | 分区 leader / **ISR** / 副本 |
| `/v3/clusters/:id/consumer-groups[/:g[/consumers\|lags\|lag-summary]]` | 消费组、成员、**lag 堆积** |

## Kafka Prometheus 指标模板(query_metrics 用,kafka_exporter / jmx)

```promql
# 消费组 lag(堆积)
kafka_consumergroup_lag                                  # by (consumergroup, topic)
sum by (consumergroup) (kafka_consumergroup_lag)

# ISR / under-replicated / offline(可用性核心)
kafka_topic_partition_under_replicated_partition         # >0 = 有副本掉队
kafka_cluster_partition_underreplicated
kafka_controller_kafkacontroller_offlinepartitionscount  # >0 = 有分区无 leader → 停摆
kafka_controller_kafkacontroller_activecontrollercount   # 应=1

# broker 存活 / 吞吐
kafka_brokers                                            # 掉数 = broker down
rate(kafka_server_brokertopicmetrics_messagesinpersec[5m])
kafka_server_replicamanager_leadercount

# 请求/IO
kafka_network_requestmetrics_requests_total
kafka_log_logflushstats_logflushrateandtimems
```

## 故障场景 → 取证清单

| 现象 | 先看 | 典型根因 |
|---|---|---|
| **consumer lag 堆积** | `consumer-groups/:g/lags` 或 `kafka_consumergroup_lag` | 消费者慢/挂、反压、并行度 < 分区数、rebalance 频繁 |
| **under-replicated / ISR 收缩** | `topics/:t/partitions/:p/replicas` + `under_replicated_partition` | follower 落后(磁盘/网络慢)、broker 重启、`min.insync.replicas` 不满足 |
| **offline partitions / 无 leader** | `offlinepartitionscount` + controller 日志 | 多副本同时挂、broker 全宕、unclean leader election 关闭 |
| **broker down** | `kafka_brokers` + `/brokers` | 节点故障、OOM、磁盘满、ZK/KRaft 失联 |
| **rebalance storm** | 消费组日志 + member 数抖动 | session timeout 短、消费者频繁离/入组、处理慢触发 max.poll |
| **offset commit 失败** | 消费组日志 | poll timeout 超、rebalance 中提交、coordinator 不可达 |

## 只读 → 人工的交接
ops-agent 给:根因 + 证据(REST/指标快照)+ 分级 + `needs_human_review`。**修复动作**(扩消费者/分区、重启 broker、改 `min.insync.replicas`、reassign、重置 offset)由人执行或走审批,不自动跑。

相关:`evals/cases.py` 的 `kafka_*` 回归样本、`docs/runbook/flink-diagnosis.md`(Flink+Kafka 常一起诊断)。
