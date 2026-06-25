# ops-agent k8s 部署清单(T2 形态)

把 ops-agent 的 **ingress / worker / Redis** 三件套部署到 k8s。对应架构见
`docs/architecture-evolution.md`(Step 2:真队列 + 独立 worker 进程)。

```
            告警/webhook
                │
                ▼
      ┌──────────────────┐   入队    ┌─────────┐   BRPOP   ┌──────────────────┐
      │ ingress (serve)  │ ───────▶ │  Redis  │ ◀──────── │ worker (serve-   │
      │ Deployment ×N    │  202     │ 队列/DLQ│  消费     │ worker) Deploy ×M │
      │ Service + HPA    │          │ + AOF   │           │ HPA(CPU/queue)   │
      └──────────────────┘          └─────────┘           └──────────────────┘
         /healthz live                                       心跳文件 live
         /readyz  ready(探 Redis)                            (检出僵死)
```

## 部署

```bash
# 1) 改镜像与机密(切勿提交真值)
#    - 把 config.yaml 里的 Secret 占位换成真 token/DSN/key,或用 External Secrets/SOPS
#    - kustomization.yaml 覆盖 images.newTag 指向你的 registry/tag
# 2) 一键应用
kubectl apply -k deploy/k8s/
# 3) 看状态
kubectl get deploy,po,hpa -l app.kubernetes.io/part-of=ops-agent
```

## 关键设计

| 关注点 | 处理 |
|---|---|
| **liveness vs readiness 区分** | ingress:`/healthz`=进程存活(后端抖动不重启);`/readyz`=探 Redis,不可达则摘出 Service,恢复自动回流。 |
| **worker 存活** | 无 HTTP,用**心跳文件**(`OPS_WORKER_HEARTBEAT_FILE`,主循环每秒 touch);exec 探针查 mtime<30s,检出僵死主循环(容器 Running 但不工作)。 |
| **滚动更新 worker draining** | `terminationGracePeriodSeconds: 30`;worker 收 SIGTERM → `stop.set()` + join 在途(上限 ~10s),不丢正在跑的任务。 |
| **背压** | `OPS_QUEUE_MAX` 满时 ingress 回 429,不堆积;告警 `OpsAgentBackpressureDropping`。 |
| **扩容** | ingress 按 CPU(入口轻);worker 按 CPU 起步,推荐接 prometheus-adapter 后按 `ops_agent_queue_depth` 扩(worker.yaml 注释里给了 External metric 模板)。 |
| **最小权限** | 非 root(uid 10001)、`readOnlyRootFilesystem`、`drop ALL` caps;只写 runtime emptyDir 与审计 PVC。 |
| **Redis 持久化** | AOF on + `noeviction`(队列数据不可被驱逐 = 不丢单)+ PVC。 |
| **出站边界** | `networkpolicy.yaml` 默认拒绝 ops-agent egress,只放行 DNS、Redis、公网 HTTPS;私网 DB/Prometheus/LLM gateway 用 overlay 精确加白。 |
| **审计持久化** | `OPS_APPROVAL_LOG` 按 Pod 名写入 `ops-agent-audit` RWX PVC,避免多副本争写同一 JSONL。 |

## 生产前还需补

- **Redis HA**:`redis.yaml` 是单实例起步级。生产用托管 Redis 或 Sentinel/Cluster(单点宕=队列不可用)。
- **抓指标**:worker 无 HTTP,需把 `OPS_METRICS_FILE` 用 node_exporter textfile collector 或 sidecar 暴露;ingress 已带 `prometheus.io/scrape` 注解。告警规则:`deploy/prometheus/ops-agent-alerts.yml`。
- **NetworkPolicy overlay**:默认只放行公网 HTTPS;若 LLM gateway、Prometheus 或只读 DB 在私网,需按精确 CIDR/selector 增补 egress。
- **机密管理**:用 External Secrets/Vault,别用仓库里的示例 Secret。
- **目标系统挂载**:若诊断对象日志/配置在集群外,按需挂 PV 或改走远端只读访问。

排障见 `docs/runbook/queue-operations.md`。
