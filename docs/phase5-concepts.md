# ②(进阶):执行类工具 + 人工审批(HITL)

只读 agent 再聪明也只是"看";要它"动手"(重启服务等),就进入**危险区**——LLM 可能误判、
日志里可能藏提示注入。原则:**写操作单独成类 + 白名单 + 默认 dry-run + 执行前人工审批闸**。
(这正是你 ADR-029 危险能力隔离思路在 agent 上的对应。)

## 1. 写工具 vs 读工具:严格分开

| | 只读(read_logs/query_pg)| 执行(restart_service)|
|---|---|---|
| 风险 | 低(最多泄信息,已白名单/只读) | **高**(改变系统状态) |
| 调用前 | 直接执行 | **必须过审批闸** |
| 默认 | — | **dry-run**(只说要做什么,不真做) |

## 2. 四层护栏(restart_service)

1. **白名单**:只允许已知服务集合,模型/注入内容越不出去。
2. **默认 dry-run**:`OPS_ALLOW_EXEC≠true` 时只回报"将重启 X",不真执行。
3. **真执行要显式配**:`OPS_ALLOW_EXEC=true` + `OPS_RESTART_CMD`(命令模板)。
4. **HITL 审批闸**:就算模型要调、就算允许执行,**agent 循环里仍先问人 y/N**,批准才执行。

## 3. 审批闸怎么进循环(手写,透明)

阶段 3 的循环里,执行工具前加一道判断:

```
for tu in tool_uses:
    if tu.name in DANGEROUS_TOOLS and not approver(tu.name, tu.input):
        result = "用户拒绝执行,未执行"     # 拒绝 → 把这个喂回模型,它会改走别的路
    else:
        result = 执行(tu)
```

`approver` 是可注入的回调:默认命令行 y/N;测试里注入"永远拒绝/批准"来验两条路。
**关键**:拒绝不是报错,而是把"被拒"作为 tool_result 喂回——模型据此调整(比如改成只给建议)。

## 4. 对照 LangGraph 的 HITL(框架版)

手写审批闸 = 让你看清机制。LangGraph 的等价物:
- `create_react_agent(..., interrupt_before=["tools"])`:执行工具前**暂停整个图**,等外部 resume。
- 或工具内 `interrupt({...})`:只在危险工具处暂停(更精细),用 `Command(resume=...)` 继续。
- 这些依赖 checkpointer(把暂停点存下),所以 LangGraph 的 HITL 天然可"暂停几小时等人批"再续——
  比我们这个"同进程 input() 阻塞"强(适合真异步审批 / 网页确认)。

先懂手写这道闸,再用 LangGraph interrupt 就知道它解决的是"跨时间/跨进程的暂停-恢复"。

## 5. 红线

- **永不**让 agent 无审批执行写操作(哪怕"看起来安全")。
- **永不**把 shell/SQL 拼接用户/模型原文执行而不过白名单+参数化(restart 用白名单服务名,不让模型传任意命令)。
- dry-run 是默认,真执行是例外且要双重显式(env + 审批)。
