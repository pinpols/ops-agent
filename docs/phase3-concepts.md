# 阶段 3 概念:多步 agent(循环 + 多工具 + 记忆)

阶段 2 是**一个工具、一回合**。阶段 3 把它推广成真 agent:
- **多工具**:`read_logs` + `query_pg`(只读 SQL),模型按需挑。
- **循环**:`while 模型还在调工具: 执行 + 喂回`,直到它给出结论。模型可以**连着调好几次**(先 read_logs 看错误,再 query_pg 看状态,再下结论)——这就是"规划"。
- **记忆**:把 messages 累积下来,支持**连续追问**("那 orchestrator 呢?"接着上文)。

## 1. agent 循环(核心,就这几行)

```
messages = history + [user 问题]
loop:
    resp = create(tools=[read_logs, query_pg, report_diagnosis], tool_choice=auto)
    把 resp(assistant)追加进 messages
    tool_uses = resp 里所有 tool_use 块
    若没有 tool_use → 模型在讲话,结束
    若有 report_diagnosis → 解析成 Diagnosis,结束
    否则:逐个执行工具,把每个 tool_result 追加进 messages(user 角色),继续循环
```

**和阶段 2 的唯一区别**:阶段 2 写死"读一次就下结论";阶段 3 是 `while`,模型自己决定调几次、调哪些、什么时候停。**这就是 agent 和"一次工具调用"的本质分界**。

必须有**步数上限**(`max_steps`):模型可能绕圈/反复调,封顶防失控(也是成本护栏)。

## 2. 多工具:模型怎么"规划"

你只是把 N 个工具的 schema 都给它,模型自己决定**先调哪个、根据结果再调哪个**。没有显式的"规划器"——规划能力来自模型本身 + 你工具 description 写得清不清楚。例如:

> 问:"sim 跑批为什么慢?"
> → query_pg 查 pg_stat_activity 看锁等待 → read_logs 看 orchestrator ERROR → query_pg 看 job 积压 → report_diagnosis 综合

这条链不是你编排的,是模型读着每步结果**临场决定**的。

## 3. 记忆 / 多轮

把整个 `messages` 列表(含历次 tool_use / tool_result / 结论)**带到下一个问题前面**,模型就有了上下文,能接着追问。这是最朴素的"记忆"=对话历史累积。
代价:历史越长 token 越多。真实系统要做**上下文管理**(摘要旧轮、只保留相关片段)——阶段 4 再碰。

## 4. query_pg:强工具 = 强护栏(多层)

`query_pg` 能执行 SQL,危险远大于 read_logs。**纵深防御**:
1. **字符串闸**:只允许单条 `SELECT`/`WITH` 开头;禁 `;` 多语句;禁 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/COPY 等关键词。
2. **连接级只读**(最硬的一层):连接开 `default_transaction_read_only=on`——**就算字符串闸被绕过,DB 也拒绝任何写**。
3. **statement_timeout**:封顶单条查询耗时,防慢查询拖垮库 / 烧 token。
4. **结果限量**:截断返回行数,别把百万行喂进上下文。
> "字符串黑名单"会被绕,所以**真正承重的是连接级只读**;黑名单只是第一道、给清晰报错。

## 5. 为什么先手写循环,之后才上 LangGraph

你现在看到的循环就十几行——**完全透明**。LangGraph 不是让你"会写 agent",而是当循环长大后帮你管:
- **持久化 / checkpointer**:agent 跑一半崩了能续(你做过 orchestrator,懂这价值)
- **streaming**:把中间步骤实时吐给前端
- **human-in-the-loop**:危险动作前暂停等人确认(将来加"执行类"工具时用)
- **图可视化 / 分支**:复杂流程

**先懂这十几行循环,再看 LangGraph 替你做了哪些——才不会把它当黑盒。** 阶段 3b 就做这个 port。
