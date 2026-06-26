# 架构决策记录(ADR)

记录 ops-agent 里**承重的、不显然的、有取舍的**决策:为什么这么选、否决了什么、代价是什么。
新决策追加一条(序号递增),被推翻的标 `Superseded by`,不删历史。

格式(轻量 MADR):**Status / Context(问题与约束)/ Decision(选了什么)/ Alternatives(否决项)/ Consequences(收益与代价)**。

| # | 决策 | 状态 |
|---|---|---|
| [0001](0001-read-only-structural-gate.md) | 只读用**结构闸**强制,不靠 prompt | Accepted |
| [0002](0002-prompt-injection-defense.md) | prompt 注入**分层**防御(结构硬保证 + 围栏 best-effort) | Accepted |
| [0003](0003-queue-evolution.md) | 触发/并发:同步内联 → 进程内队列 → Redis + 独立 worker,且热路径原子 | Accepted |
| [0004](0004-needs-human-review.md) | `needs_human_review` **派生**(computed_field),不让模型填 | Accepted |
| [0005](0005-diagnosis-paths.md) | 四条诊断路径并存的取舍 + 共享内核的待治根因 | Accepted(部分待治) |

总览见 [../design/architecture.md](../design/architecture.md)。
