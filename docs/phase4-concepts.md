# 阶段 4 概念:eval + 可观测(把 LLM app 做扎实)

前面在"让它能跑";这一步在"**怎么知道它跑得好不好、花了多少**"。这是大多数人 agent 的短板,
也是你工程强项的发挥点——多数 demo 没 eval、没 trace,上线即裸奔。

## 1. 为什么必须 eval

LLM 输出**不确定**:同一输入两次结果可能不同;改一句 prompt 可能让 A 变好、B 变坏。
没有 eval,你"调 prompt"全靠感觉,改完不知道整体是变好还是变差(**回归看不见**)。
eval = 一组**固定测试用例 + 自动打分**,让"改一处→整体好坏"可量化。这正是你后端做单测/回归的思路搬到 LLM。

## 2. 测试集(golden set)怎么建

每条用例:**输入(日志)+ 期望(该判什么)**。期望不必是逐字答案(LLM 不会逐字复现),而是:
- `expected_severity`:该给的严重级别(强约束,能精确比)
- `expected_keywords`:根因/证据里**应出现**的关键词(如 redis / 16379 / hikari),做"召回"判断
- 也要有**反例**:正常日志 → 期望 INFO + "未发现异常"(防 agent 草木皆兵乱报 CRITICAL)

用例从**真实日志**来(本项目从 file-batch-system 抓);先 5~10 条覆盖典型故障 + 1~2 条正常。
阶段 1 我就说"把明显对/错的样本记下来"——那就是这里的雏形。

## 3. 两种打分:确定性 vs LLM-as-judge

| | 确定性打分 | LLM-as-judge |
|---|---|---|
| 怎么打 | 代码断言:severity 是否相等、关键词是否命中 | 另一个 LLM 调用,读"日志+期望+实际诊断"给 0~1 分 + 理由 |
| 优点 | 快、免费、可复现、CI 友好 | 能判"语义对不对"(根因表述不同但意思对) |
| 缺点 | 死板(同义/换表述会漏判) | 慢、要钱、judge 本身可能错(要校准) |
| 用在哪 | severity / 关键事实 / 反例不误报 | 根因合理性这种"软"判断 |

**实践:两者都用**。确定性兜硬指标(severity 必须对、正常日志不许报 CRITICAL),LLM-judge 评根因质量。
judge 要给它**明确评分标准**(rubric),否则它也乱给分——和你写工具 description 一个道理。

## 4. 跑出来看什么(scorecard)

- **通过率**(确定性):severity 命中率、关键词召回率、反例误报率
- **judge 平均分** + 每条的分 + 理由(失败的要能点开看为什么)
- 这套数字就是你"改 prompt / 换模型"的**回归基线**:改完重跑,数字升了才算改对。

## 5. 可观测:trace / token / 成本(Langfuse)

eval 是"离线批量评质量";trace 是"线上每次跑发生了啥":
- 每次调用的**延迟、输入输出、token 数、$ 成本**、调了哪些工具、几步收口。
- 排查"为什么这次诊断差""为什么这次特别慢/贵"靠它。
- **成本**:agent 多步 + 长上下文很烧 token,trace 让你看见每步花多少,才好优化(裁上下文/换小模型/缓存)。

本项目用 **Langfuse**(自托管 docker 或云免费档):
- 裸 SDK:`from langfuse.anthropic import Anthropic`(替换 import)即自动采集 token/成本;或用 `@observe()` 装饰函数串成 trace。
- LangGraph:挂 Langfuse 的 LangChain CallbackHandler。
- 本项目把它做成**可选**(配了 LANGFUSE_* 才启用,没配是 no-op),避免逼你先搭 Langfuse 才能用 agent。

## 6. 这阶段的产物

```
evals/
  cases.py       # golden 测试集(真实日志 + 期望)
  scorers.py     # 确定性打分 + LLM-as-judge
  run_eval.py    # 跑全集 → scorecard
ops_agent/obs.py # 可选 Langfuse trace(env 没配则 no-op)
```

判据:**先有 eval 基线,再谈优化**。没有基线的"我感觉这版更好"= 又一个 vecstream。
