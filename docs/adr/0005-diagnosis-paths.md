# ADR-0005:四条诊断路径并存的取舍 + 共享内核待治

**Status**: Accepted(部分待治)

## Context
项目按分阶段教学演进,留下了四条做同一件事(日志/问题 → 结构化 Diagnosis)的路径:
- `diagnose_log`(单发,无工具)— 阶段1,CLI 单文件 / eval 用。
- `investigate`(单工具单回合)— 阶段2,教学。
- `run_agent`(多步 ReAct + 全工具)— 阶段3,**生产 webhook/worker 路径**。
- `graph_agent`(LangGraph 版)— 阶段3b,框架对照。

它们各有独立的 system prompt 和 tool-dispatch 循环,曾**安全姿态漂移**:run_agent 有围栏+脱敏+反注入,而 diagnose_log/investigate/graph 一度缺其中一项,造成真实注入敞口(审计发现并补齐)。

## Decision
**短期**:接受四路并存(教学价值 + 渐进复杂度对照),但**强制四路安全姿态一致**(围栏 + 脱敏 + 反注入条款),并加测试 `AllPathsConsistentDefenseTest` 锁住,防再漂移。
**长期(待治)**:抽一个共享"诊断回合"内核(tool-dispatch + 围栏 + 脱敏 + 报告校验一处),四路收成"1 内核 + 薄壳",根除复制。同样待治的是工具的三处注册面(`_ALL_IMPLS` / `tools=[...]` schema / `DANGEROUS_TOOLS`)合并为单一 `Tool` 声明。

## Alternatives(否决/推迟)
- **立即删除 investigate/graph**:省维护,但丢教学对照价值;且 graph 验证了"框架替你做了什么"。推迟到内核重构时再定去留。
- **现在就抽共享内核**:价值高但是中等规模重构,且 run_agent/server 在并行演进中,择期单独做。

## Consequences
- ✅ 当前四路安全一致,有测试守门,注入敞口已闭。
- ⚠️ **每次安全/工具改动仍要在多处复制** —— 这是当前最大维护负债,根因未除,只是被测试守住不再漂移。
- ⚠️ 三处工具注册面不同步会"静默不可用 / unknown_tool",随工具增多会腐化。
- 📌 共享内核重构 + 工具单一声明是下一步架构治理的首选项;`worker_main → server` 反向耦合(job handler 放错层)宜一并迁出到独立 `jobs` 模块。
