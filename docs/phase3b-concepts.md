# 阶段 3b 概念:手写循环 → LangGraph(对照学,不黑盒)

你阶段 3 已亲手写过那个循环。现在用 LangGraph 重做**同一件事**,目的是**对照**看框架替你做了什么。

## 1. 一一对应:你的手写循环 ←→ LangGraph

`create_react_agent` 编译出的图,节点是:

```
__start__ → agent ⇄ tools → generate_structured_response → __end__
```

| 你阶段 3 手写的 | LangGraph 里的 | 说明 |
|---|---|---|
| `resp = client.messages.create(tools=...)` | **`agent` 节点** | 调模型,决定调工具还是收口 |
| `for tu in tool_uses: 执行 + 喂回` | **`tools` 节点**(ToolNode) | 执行工具、把 tool_result 塞回 messages |
| `while ...:`(agent↔tools 来回) | **`agent ⇄ tools` 条件边** | "还有 tool_use 吗?有→tools,没有→收口"——就是你的 if/continue |
| 最后强制 `report_diagnosis` | **`generate_structured_response` 节点** | `response_format=Diagnosis` 触发的最后一步结构化 |
| 手动传 `history` / 累积 `messages` | **`checkpointer` + `thread_id`** | 记忆:不用手传,同 thread_id 自动接上文 |
| `max_steps` 防失控 | recursion_limit(图递归上限) | 同一回事 |

**看懂这张表 = LangGraph 对你不再是黑盒**:它就是你那十几行,被拆成图的节点 + 边。

## 2. 框架到底买到了什么(你手写时没有的)

1. **记忆零成本**:`MemorySaver` checkpointer + `thread_id`。多轮不再手传 messages——框架把每步状态存下,下次同 thread 自动续。阶段 3 你是手动 `return messages` 再传回。
2. **可恢复 / 持久化**:checkpointer 可换成 SqliteSaver/PostgresSaver → agent 跑一半崩了能**从断点续**(你做过 orchestrator,懂这价值)。手写循环崩了就没了。
3. **streaming**:`agent.stream(...)` 实时吐每一步(调了哪个工具、结果),给前端做"思考过程"很顺。
4. **human-in-the-loop**:`interrupt_before=["tools"]` 能在执行工具前**暂停等人确认**——将来加"重启服务"这类危险动作时,这就是审批闸(对应你 ADR-029 的隔离/审批思路)。
5. **结构化输出内建**:`response_format=Diagnosis` 自动加一个结构化收口节点,不用你手写"强制 report_diagnosis"。
6. **可视化**:`agent.get_graph().draw_mermaid()` 直接出流程图。

## 3. 代价 / 取舍(诚实)

- **抽象成本**:StateGraph / 节点 / 边 / checkpointer / reducer 一堆概念;不懂底层就容易"能跑但不知所以然"(正是为什么先手写)。
- **可控性**:细粒度控制(自定义每步逻辑)要下沉到底层 StateGraph 手搭节点,prebuilt 的 `create_react_agent` 是"开箱"档。
- **依赖重**:langgraph + langchain-anthropic + 一票传递依赖。

## 4. 什么时候用哪个

- **学 / 小工具 / 想完全掌控** → 手写循环(阶段 3),十几行、透明。
- **要记忆持久化 / 断点续跑 / streaming / 审批闸 / 多 agent 协作** → LangGraph。

判据和你后端一样:**没到那个复杂度别上框架**;到了(尤其要 checkpointer 续跑、HITL 审批),框架省的是真功夫。

## 5. 代码量对比(直观)

- 阶段 3 `agent.py`:手写循环 ~40 行。
- 阶段 3b `graph_agent.py`:`create_react_agent(...)` 一行装配 + 工具 wrapper —— 循环/记忆/结构化**都没写**。

省了代码,但**前提是你已经懂那 40 行在干嘛**。这就是先手写再上框架的意义。
