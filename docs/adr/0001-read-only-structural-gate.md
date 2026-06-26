# ADR-0001:只读用结构闸强制,不靠 prompt

**Status**: Accepted

## Context
ops-agent 能触达生产系统(读日志、查 PG、甚至 restart_service)。LLM 不可靠 + 日志是攻击者可写的不可信输入,
一旦"按 prompt 说的别乱动"被绕过(注入 / 幻觉 / 越狱),写操作的爆炸半径无法承受。
约束:既要让模型有能力建议/在特定场景执行,又要让"webhook 触发的诊断绝不写"成为**不可绕过**的保证。

## Decision
把只读做成**结构不变量**,不是 prompt 约束:
- 危险工具登记进 `DANGEROUS_TOOLS`(exec_tools.py),执行前必过审批闸 `approve(name, input)`。
- webhook/worker 路径注入 `_deny_all_approver`(server.py)—— 对任何危险工具任何入参恒返 `False`。
- agent 循环里 `approved is False` 时**根本不查/不调** impl(agent.py),只回"审批拒绝"文案 + 落审计。

即:模型即便被注入完全劫持、真的发出 `restart_service` 工具调用,impl 也**永不运行**。

## Alternatives(否决)
- **纯 prompt 约束**("你是只读,不要重启"):被注入/幻觉一击即破,否决。
- **执行后回滚**:写已经发生,不可接受。
- **去掉 restart_service**:丧失未来 HITL 受控执行能力;保留工具但用闸控制更灵活。

## Consequences
- ✅ 只读是确定性保证(100%),与模型行为解耦;有对照测试防假阳(`test_prompt_injection.py`:放行审批时 impl 确会被调,证明 deny 时"没被调"非假阳)。
- ✅ 同一机制支持未来 T3 受控执行(换非 deny-all 审批闸 + HITL)。
- ⚠️ `DANGEROUS_TOOLS` 被清空则闸形同虚设 → 有"非空"回归测试守。
- ⚠️ 新增危险工具必须记得登记进 `DANGEROUS_TOOLS`(三处注册面之一,见 [ADR-0005](0005-diagnosis-paths.md) 的待治)。
