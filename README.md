# ops-agent — 运维诊断智能体(学习项目)

一个从"基本功 → 单工具 → 多步 agent → 工程化"逐层长起来的学习项目。
被诊断对象 = 隔壁 `../file-batch-system`(日志 / PG / 指标都现成)。

> **学习原则**:前期**不用框架**,先用裸 SDK 把 LLM 的输入输出/工具调用机制搞懂,
> 需要"状态+循环"了再上 LangGraph。每个阶段**只啃一个新东西**,踩实再加下一层。

## 分阶段路线图

| 阶段 | 只学这一件新事 | 产出 | 引入的依赖 |
|---|---|---|---|
| **1 基本功** | prompt + **结构化输出**(Pydantic 逼模型守 JSON 格式) | 日志 → 结构化诊断,**一次 LLM 调用,无工具** | anthropic + pydantic |
| **2 单工具** | **一次 function calling**(LLM 决定调一个工具) | LLM 自己决定调 `query_pg` / `read_logs` 一次 | (同上) |
| **3 多步 agent** | **多步规划 + 循环**(自己连着调几个工具到结论) | 真 agent | + langgraph |
| **4 工程化** | **eval**(根因判对没)+ **trace/成本** | 测试集 + 可观测 | + langfuse |

## 起步(阶段 1)

```bash
cd ops-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 填入 API key
python -m ops_agent.diagnose data/sample-console.log
```

目标:`diagnose.py` 读一段日志 → 调一次 LLM → 按 `models.Diagnosis` 的 schema 返回结构化诊断。
**核心逻辑(LLM 调用 + 结构化解析)留你自己写**(`diagnose.py` 里是 TODO 脚手架);写完跑通,卡住或想被 review 就贴出来。

## 模型来源

默认用 **Anthropic API**(模型强,学概念时不被"是我错还是模型笨"干扰)。
想零成本全本地:换 **Ollama**(M1 跑 7-8B 量化),把 `diagnose.py` 的 client 换成 OpenAI 兼容端点即可——留到后期做对比。

## 目录

```
src/ops_agent/
  models.py     # Pydantic 输出 schema(诊断结果的"目标形状")
  diagnose.py   # 阶段 1:日志→结构化诊断(你来填核心)
data/           # 样本日志
evals/          # 阶段 4:测试集 + 评测
```
