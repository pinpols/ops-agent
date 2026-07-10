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
#    - 生产 overlay 用 images.digest 指向已签名/已扫描的 release 镜像 digest
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
| **审计持久化** | `OPS_APPROVAL_LOG` 按 Pod 名写入 `ops-agent-audit` RWX PVC;审计模块仍有 `.lock` 文件锁、多档轮转和 hash chain,支持未来同文件多进程写入。 |
| **审计主体** | HTTP 调用方可传 `X-Ops-Actor`;worker job 会携带 actor 并写入审批/执行审计记录。 |

## 生产前还需补

- **Redis HA**:`redis.yaml` 是单实例起步级。生产用托管 Redis 或 Sentinel/Cluster(单点宕=队列不可用)。
- **镜像发布**:默认清单使用版本 tag,不是 `latest`;正式生产 overlay 应改为 digest,并配合镜像签名/准入策略。
- **抓指标**:见下节「worker 指标抓取」;ingress 已带 `prometheus.io/scrape` 注解。告警规则:`deploy/prometheus/ops-agent-alerts.yml`。
- **NetworkPolicy overlay**:默认只放行公网 HTTPS;若 LLM gateway、Prometheus 或只读 DB 在私网,需按精确 CIDR/selector 增补 egress。
- **机密管理**:用 External Secrets/Vault,别用仓库里的示例 Secret。
- **目标系统挂载**:若诊断对象日志/配置在集群外,按需挂 PV 或改走远端只读访问。

## worker 指标抓取(必配,否则指标黑洞)

worker 进程无 HTTP 端口,`OPS_METRICS_FILE`(默认 `/var/run/ops-agent/metrics.prom`)
写在 `runtime` 卷里。**默认 emptyDir 没有任何抓取方** —— jobs_succeeded/reaped/lost、
workers_busy 等队列指标写了等于没写,必须按下面二选一接上:

1. **node_exporter textfile collector(hostPath)**:把 worker.yaml 的 `runtime` 卷改成
   hostPath,指向节点上 node_exporter `--collector.textfile.directory` 的目录
   (worker.yaml volumes 段有现成注释示例)。注意:
   - 需要集群允许 hostPath(PodSecurity `privileged` namespace 或 OPA 白名单);
   - 同节点多 worker Pod 会写同一文件,用 `OPS_METRICS_FILE=/…/$(POD_NAME).prom` 区分;
   - node_exporter 会给指标自动带上节点标签,Pod 维度靠文件名/自定义 label 区分。
2. **sidecar exporter**:worker Pod 内加一个只读挂 `runtime` 卷的轻量 HTTP 容器
   (nginx 静态托管 metrics.prom 即可),Pod 加 `prometheus.io/scrape: "true"` 注解,
   Prometheus 按 Pod 抓取。无节点权限要求,Pod 维度天然隔离,代价是每 Pod 多一个容器。

worker 侧已保证 textfile 时效性:消费循环与 reaper **每轮结束都会 flush**(含空闲轮),
不再只依赖诊断 run 内部的 flush 时机。

排障见 `docs/runbook/queue-operations.md`。
