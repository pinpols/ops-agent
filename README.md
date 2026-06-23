# ops-agent — 运维诊断智能体(学习项目)

一个从"基本功 → 单工具 → 多步 agent → 工程化"逐层长起来的学习项目。
被诊断对象 = 隔壁 `../file-batch-system`(日志 / PG / 指标都现成)。

> **学习原则**:前期**不用框架**,先用裸 SDK 把 LLM 的输入输出/工具调用机制搞懂,
> 需要"状态+循环"了再上 LangGraph。每个阶段**只啃一个新东西**,踩实再加下一层。

## 分阶段路线图

| 阶段 | 只学这一件新事 | 产出 | 引入的依赖 |
|---|---|---|---|
| **1 基本功** ✅ | prompt + **结构化输出**(Pydantic 逼模型守 JSON 格式) | 日志 → 结构化诊断,**一次 LLM 调用,无工具** | anthropic + pydantic |
| **2 单工具** ✅ | **一次 function calling**(LLM 决定调一个工具) | LLM 自己决定调 `query_pg` / `read_logs` 一次 | (同上) |
| **3 多步 agent** ✅(手写循环)| **多步规划 + 循环**(自己连着调几个工具到结论) | 真 agent | + langgraph |
| **3b LangGraph** ✅(对照:框架替你做了啥)| port 手写循环→LangGraph(checkpointer 记忆/可续/HITL)| 同左 | + langgraph/langchain-anthropic |
| **4 工程化** ✅ | **eval**(根因判对没)+ **trace/成本** | 测试集 + 可观测 | + langfuse |
| **5 执行+HITL** ✅ | 危险动作工具 + **人工审批闸**(白名单/dry-run/批准才执行) | restart_service + approver | exec_tools |

## 起步(阶段 1)

```bash
cd ops-agent
python3.12 -m venv .venv && source .venv/bin/activate   # 需 Python 3.11+
pip install -e ".[dev,graph]"     # 开发/测试推荐;仅运行核心也可 pip install -r requirements.txt
cp .env.example .env          # 填入 ANTHROPIC_API_KEY

# 阶段 1:预喂日志 → 结构化诊断
ops-agent diagnose data/sample-console.log
# 阶段 2:自然语言问题 → 模型自己调 read_logs 取数据 → 结论
ops-agent investigate "console 最近有什么异常?"
# 阶段 3:多步 agent(多工具+循环+记忆),交互式多轮
ops-agent chat          # 多轮;或:ops-agent chat "sim 跑批为什么慢?"
# 阶段 3b:同 agent,但用 LangGraph(循环/记忆/结构化都框架代劳)
ops-agent graph
# 阶段 4:eval(诊断判对没)+ 可选 Langfuse trace
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
```

生产 profile 下,`query_pg` 自由 SQL 默认禁用,agent 应使用 `query_pg_template`。
内置模板包括 `pg_lock_waits`、`active_queries`、`job_status_counts`、
`recent_failed_jobs`。如果需要新增查询,把 SQL 加到 `ops_agent/sql_templates.py`,
不要让模型直接拼自由 SQL。

危险执行工具必须同时满足:

- `OPS_ALLOW_EXEC=true`
- `OPS_RESTART_CMD` 已配置
- `OPS_EXEC_ALLOWLIST` 命中完整命令或 argv[0]
- agent 审批闸批准

审批结果会写入 `OPS_APPROVAL_LOG`。trace 和 bundle 默认脱敏,会遮蔽常见 token、
DSN 密码、邮箱和手机号。`ops-agent doctor` 会检查生产 profile 下的自由 SQL、
执行 allowlist、PG 用户名和日志目录是否可写;生产环境应使用最小权限只读 DB 用户,
并把日志目录以只读方式挂载。

`diagnose.py` 已实现:读日志 → 用 Anthropic function calling 逼模型按 `models.Diagnosis`
schema 返回 → Pydantic 校验成对象。**概念详解见 [`docs/phase1-concepts.md`](docs/phase1-concepts.md)**
(function calling 怎么工作、为什么 Field description 影响输出)。

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
  tools.py         # read_logs / query_pg / query_pg_template(只读取证)
  system_tools.py  # list_services / tail_recent_errors / inspect_compose / read_app_config
  exec_tools.py    # 阶段 5:restart_service(危险写操作,白名单 + 审批闸 + dry-run)
  sql_templates.py # prod 下允许的只读 SQL 模板
  redaction.py     # 脱敏(token/DSN 口令/AWS key/JWT/邮箱/手机号)
  audit.py         # 审批 / 执行审计记录(JSONL)
  trace_io.py      # agent trace 落盘(JSONL)
  bundle.py        # 诊断包(diagnosis.json + trace.jsonl + evidence.log + summary.md)
  obs.py           # 可选 Langfuse 接线(未配则 no-op)
  cli.py           # ops-agent 命令行入口
docs/              # 概念笔记(phase1~5-concepts.md)
tests/             # 离线 mock 测试(无需 key)
data/              # 样本日志
evals/             # 阶段 4:测试集 + 确定性/LLM-judge 评测(已实现:ops-agent eval)
```
