# ③ 接 Langfuse:看每次 agent 跑的 trace / token / 成本

代码侧已就绪(`ops_agent/obs.py` 的 `@observe` 已挂在 `diagnose_log` / `run_agent`),
**没配 LANGFUSE_* 时是 no-op**。要真看 trace,三步:

## 1. 拿一个 Langfuse(选一种)

- **最省事:云免费档** —— 注册 cloud.langfuse.com,建项目,拿到 public/secret key。host 用 `https://cloud.langfuse.com`(或 EU/US 区域域名)。
- **自托管**:按 langfuse 官方 docker-compose(v3 需 postgres+clickhouse+redis+minio,较重)起一套,host 指向你的实例。学习阶段建议先用云免费档。

## 2. 装 + 配

```bash
pip install langfuse          # 已在 requirements 里(注释,放开即可)
# .env 追加:
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_HOST=https://cloud.langfuse.com
```

配上后 `@observe` 自动启用,每次 `python -m ops_agent.agent ...` / `python -m evals.run_eval`
都会在 Langfuse 里生成一条 trace(函数入参/出参/耗时/嵌套步骤)。

## 3. 想看 token / $ 成本(关键)

`@observe` 默认记的是**函数 span**(耗时/I-O),不含 token。要采 token/成本,把 Anthropic client 换成
langfuse 的包装版(**一行 import 改动**):

```python
# ops_agent/diagnose.py / agent.py 里:
# from anthropic import Anthropic
from langfuse.anthropic import Anthropic     # ← 自动捕获每次调用的 token 数 + 按模型价算成本
```

换完,trace 里每次 LLM 调用就带 input/output token + 估算 $。**多步 agent 很烧 token,这是优化的依据**
(看哪步贵 → 裁上下文 / 换小模型 / 缓存)。

> 为什么不默认就用包装版?避免逼你装/配 langfuse 才能跑 agent。学习期先 no-op 跑通,
> 要观测时再放开这行 + 配 key。生产则固定用包装版 + 自托管 Langfuse。

## 4. trace 能帮你排查什么

- 某次诊断很差 → 点开 trace 看它调了哪些工具、读到什么、最后怎么推的。
- 某次特别慢/贵 → 看是哪一步(哪个工具调用 / 哪次 LLM)耗时长 token 多。
- eval 批量跑 → 每条 case 一条 trace,失败的能下钻看原因(配合阶段4 的 scorecard)。
