# ops-agent — 生产化只读运维诊断副驾

ops-agent 是面向内部运维场景的只读诊断服务:接收告警或人工问题后,基于日志、
只读 SQL、Prometheus 指标和目标系统配置做多步取证,输出结构化诊断、证据 trace 和
诊断包。默认生产 profile 下禁止自由 SQL、危险动作需 allowlist + 审批,触发层永远注入
deny-all 审批闸,不让模型自主执行写操作。

当前定位是 **T1/T2 之间的受控只读生产候选**:适合内部、单租户、人工复核的只读诊断;
不定位为无人值守变更系统或开放多租户 SaaS。

## 能力演进

| 阶段 | 核心能力 | 产出 | 引入的依赖 |
|---|---|---|---|
| **1 结构化诊断** ✅ | prompt + Pydantic schema | 日志 → `Diagnosis` | anthropic + pydantic |
| **2 只读取证工具** ✅ | function calling | `query_pg_template` / `read_logs` / metrics | psycopg + requests |
| **3 多步 agent** ✅ | 多步规划 + 循环 + trace | agent 自动取证到结论 | langgraph 可选 |
| **4 工程化门禁** ✅ | eval / adversarial / CI / coverage | 回归门禁 + 安全扫描 | pytest / ruff / mypy |
| **5 HITL 安全边界** ✅ | 危险动作 allowlist + 审批审计 | 只读触发层 + 审批记录 | exec_tools |
| **6 事件驱动运行** ✅ | ingress / queue / worker / DLQ | webhook 秒级 ACK + 后台诊断 | Redis 可选 |

## 起步

```bash
cd ops-agent
python3.12 -m venv .venv && source .venv/bin/activate   # 需 Python 3.11+
pip install -e ".[dev,graph]"     # 开发/测试推荐;仅运行核心也可 pip install -r requirements.txt
cp .env.example .env          # 填入 ANTHROPIC_API_KEY

# 预喂日志 → 结构化诊断
ops-agent diagnose data/sample-console.log
# 自然语言问题 → agent 调只读工具取证 → 结论
ops-agent investigate "console 最近有什么异常?"
# 多步 agent(多工具+循环+记忆),交互式多轮
ops-agent chat          # 多轮;或:ops-agent chat "sim 跑批为什么慢?"
# 同 agent 的 LangGraph 版
ops-agent graph
# eval(诊断判对没)+ 可选 Langfuse trace
ops-agent eval            # 确定性打分(需 key 跑诊断)
ops-agent eval --judge    # + LLM-as-judge
ops-agent eval --save base.json      # 存基线(改 prompt 后 --baseline base.json 对比升降)

ops-agent doctor          # 检查关键配置
ops-agent doctor --target file-batch-system
ops-agent services --target file-batch-system
ops-agent errors --target file-batch-system --max-lines 50
ops-agent compose --target file-batch-system
ops-agent app-config worker-import --target file-batch-system
ops-agent chat "现在系统哪里异常?" --target file-batch-system
ops-agent bundle "worker-import 最近为什么失败?" --target file-batch-system
pytest                    # 离线 mock 测试(无需 key)
ruff check . && ruff format --check .
```

原有 `python -m ops_agent.diagnose` / `python -m ops_agent.agent` 等模块入口仍可用。

## 目标系统上下文

当 `../file-batch-system` 存在时,ops-agent 会自动把它作为目标系统,默认读取
`../file-batch-system/logs`。也可以显式配置:

```bash
export OPS_TARGET_ROOT=../file-batch-system
export OPS_LOG_DIR=../file-batch-system/logs
export OPS_TRACE_DIR=.ops-agent/traces
```

多步 agent 现在有这些只读上下文工具:

- `list_services`:列出目标系统模块和日志文件。
- `tail_recent_errors`:扫描近期 WARN/ERROR/Exception/timeout 等关键行。
- `inspect_compose`:摘要 docker-compose 中的 PG/Kafka/Redis/Valkey/端口信息。
- `read_app_config`:读取 Spring application 配置摘要。
- `read_logs` / `query_pg`:继续用于精确日志和只读 SQL 取证。

`ops-agent bundle` 会生成诊断包目录,包含 `diagnosis.json`、`trace.jsonl`、
`evidence.log` 和 `summary.md`。

## 生产化安全开关

生产环境建议显式配置:

```bash
export OPS_PROFILE=prod
export OPS_SQL_ALLOW_FREE=false
export OPS_REDACT_ARTIFACTS=true
export OPS_APPROVAL_LOG=.ops-agent/approvals.jsonl
export OPS_AUDIT_ROTATE_KEEP=5
export OPS_ACTOR=ops-bot
export OPS_REDACTION_RULES_FILE=/etc/ops-agent/redaction-rules.json
# 如启用异步回调,prod 下必须 HTTPS + 主机 allowlist
export OPS_CALLBACK_ALLOW_HOSTS=ops-callback.example.com
```

生产 profile 下,`query_pg` 自由 SQL 默认禁用,agent 应使用 `query_pg_template`。
内置模板包括 `pg_lock_waits`、`active_queries`、`job_status_counts`、
`recent_failed_jobs`。如果需要新增查询,把 SQL 加到 `ops_agent/sql_templates/*.sql`
并同步 `manifest.json`,
不要让模型直接拼自由 SQL。

危险执行工具必须同时满足:

- `OPS_ALLOW_EXEC=true`
- `OPS_RESTART_CMD` 已配置
- `OPS_EXEC_ALLOWLIST` 命中完整命令或 argv[0]
- agent 审批闸批准

审批和执行结果会写入 `OPS_APPROVAL_LOG`,记录 `actor`、`prev_hash/hash` 哈希链,
并按 `OPS_AUDIT_MAX_BYTES` + `OPS_AUDIT_ROTATE_KEEP` 多档轮转;HTTP 触发层可用
`X-Ops-Actor` 传入调用主体,本地/worker 默认读取 `OPS_ACTOR`。trace 和 bundle 默认脱敏,
会遮蔽常见 token、DSN 密码、邮箱和手机号。`ops-agent doctor` 会检查生产 profile 下的
自由 SQL、执行 allowlist、PG 用户名、callback 出站策略和日志目录是否可写;生产环境应使用
最小权限只读 DB 用户,并把日志目录以只读方式挂载。

`diagnose.py` 已实现:读日志 → 用 Anthropic function calling 逼模型按 `models.Diagnosis`
schema 返回 → Pydantic 校验成对象。**概念详解见 [`docs/phase1-concepts.md`](docs/phase1-concepts.md)**
(function calling 怎么工作、为什么 Field description 影响输出)。

## T1 触发服务(serve / Docker / 多目标)

把 CLI 变成能接告警/被调度的**只读诊断服务**(就绪清单见
[`docs/production-readiness-checklist.md`](docs/production-readiness-checklist.md))。

```bash
export OPS_WEBHOOK_TOKEN=$(openssl rand -hex 16)   # /diagnose 鉴权;未配则 fail-closed
ops-agent serve --port 8080          # GET /healthz、GET /metrics、POST /diagnose

curl localhost:8080/healthz                          # {"status":"ok",...}
curl -H "Authorization: Bearer $OPS_WEBHOOK_TOKEN" \
     -H "Idempotency-Key: alertmanager/fingerprint-123" \
     -d '{"question":"worker-import 为什么失败?","target":"file-batch-system"}' \
     localhost:8080/diagnose          # 只读诊断(模型即便想 restart 也被全拒)
```

- **多目标**:`cp targets.toml.example targets.toml` 配 `root/log_dir/pg_dsn/metrics_url`,
  `--target <name>` 或 webhook `{"target": "..."}` 选用;不配则回退单目标 env。
- **指标**:`query_metrics` 只读查 Prometheus;agent 自身指标走 `/metrics` 或 `OPS_METRICS_FILE`。
- **预算闸**:`OPS_MAX_RUN_SECONDS` / `OPS_MAX_RUN_TOKENS` 防绕圈烧钱。
- **事件幂等**:异步模式支持 `event_id` / `idempotency_key` / `fingerprint` 字段,
  也支持 `Idempotency-Key` 或 `X-Ops-Event-Id` header;重复告警会返回同一个 job。
- **⚠️ webhook `question` 是未围栏输入(设计边界)**:它作为"用户问题"直达模型,不像工具输出
  那样包不可信围栏 —— **告警模板禁止内嵌原始日志内容**(日志属不可信数据,应由 agent 经
  `read_logs` 等工具自行取证,取证内容才会被围栏+脱敏)。服务端对含围栏定界符的 question
  直接 400 拒绝(纵深防御),但语义级注入仍取决于模板纪律。
- **密钥**:支持 `<NAME>_FILE`(docker/k8s secret)注入。
- **Docker**:`docker build -t ops-agent . && docker run -p 8080:8080 --env-file .env ops-agent`
  (非 root + HEALTHCHECK;基础镜像按 digest 固定)。
- **Kubernetes**:默认清单不使用 `latest`;生产 overlay 应使用发布镜像 digest。

## 模型来源

默认用 **Anthropic API**(模型强,学概念时不被"是我错还是模型笨"干扰)。
想零成本全本地:换 **Ollama**(M1 跑 7-8B 量化),把 `diagnose.py` 的 client 换成 OpenAI 兼容端点即可——留到后期做对比。

## 目录

```
ops_agent/
  models.py        # Pydantic 输出 schema(诊断结果的"目标形状")
  config.py        # Settings:profile/安全闸/路径,统一从 env 读(含 profile 校验)
  llm.py           # 共享 Anthropic 客户端工厂(集中重试配置)
  diagnose.py      # 阶段 1:日志→结构化诊断(function calling + 结构化解析)
  investigate.py   # 阶段 2:单工具回合(模型自取 read_logs 再下结论)
  agent.py         # 阶段 3:多步 agent(手写循环 + 多工具 + 记忆 + HITL)
  graph_agent.py   # 阶段 3b:同 agent 的 LangGraph 版(对照学)
  prompts.py       # 版本化系统 prompt + 不可信围栏(prompt 注入纵深)
  tools.py         # read_logs / query_pg / query_pg_template(只读取证)
  system_tools.py  # list_services / tail_recent_errors / inspect_compose / read_app_config
  metrics_tools.py # query_metrics(只读 Prometheus instant 查询)
  exec_tools.py    # 阶段 5:restart_service(危险写操作,白名单 + 审批闸 + dry-run)
  sql_templates.py # prod 下允许的只读 SQL 模板加载器
  sql_templates/   # 文件化只读 SQL 模板 + manifest
  redaction.py     # 脱敏(token/DSN 口令/AWS key/JWT/邮箱/手机号)+可配置规则
  audit.py         # 审批 / 执行审计记录(JSONL,按大小滚动留存 + hash chain)
  trace_io.py      # agent trace 落盘(JSONL,含 prompt_version)
  bundle.py        # 诊断包(diagnosis.json + trace.jsonl + evidence.log + summary.md)
  obs.py           # 可选 Langfuse 接线(未配则 no-op)
  # ── T1 生产化 ──
  targets.py       # 多目标注册表(name → root/log_dir/pg_dsn/metrics_url)
  budget.py        # RunBudget 预算闸(墙钟 + token,防绕圈烧钱)
  metrics.py       # agent 自身指标(Prometheus textfile / /metrics)
  server.py        # ops-agent serve:/healthz /metrics /diagnose(只读 webhook)
  cli.py           # ops-agent 命令行入口
docs/              # 概念笔记(phase1~5)+ 生产就绪清单(production-readiness-checklist.md)
tests/             # 离线 mock 测试(无需 key)
data/              # 样本日志
evals/             # 阶段 4:测试集 + 确定性/LLM-judge 评测(已实现:ops-agent eval)
```
