# 阶段 1 概念:结构化输出 / function calling

目标:让 LLM 不要返回一段自由文本,而是返回**严格符合 `Diagnosis` schema 的结构化数据**,
你的程序能直接 `model_validate` 拿到对象。这是把 LLM 接进工程系统的第一块基本功。

## 1. 为什么不能"让模型输出 JSON 就行"

最朴素的做法:prompt 里说"请输出 JSON"。问题:
- 模型可能加 ```json 代码块、加解释性前后缀、漏字段、把枚举写成同义词、数字给成字符串。
- 你得写一堆容错解析,且不稳定。

更稳的做法是**用模型原生的 function calling(工具调用)机制**来"逼格式"。

## 2. function calling 到底怎么工作(核心)

把一个"工具"的**入参 schema** 交给模型,模型不会真去执行什么,它只是**按那个 schema 把参数填好**返回给你。我们正是利用这一点:

1. 定义一个工具 `report_diagnosis`,它的 `input_schema` = `Diagnosis` 的 JSON Schema。
2. 调用时用 `tool_choice` **强制**模型必须调这个工具(而不是自由回话)。
3. 模型返回里会有一个 `tool_use` 块,其 `.input` 就是**已经按 schema 填好的 dict**。
4. 你 `Diagnosis.model_validate(tool_use.input)` —— Pydantic 再校验一遍(类型/枚举/0~1 范围)。

```
你的程序                         Claude
   │  tools=[report_diagnosis(schema)]   │
   │  tool_choice=强制调它               │
   ├────────────────────────────────────▶│  读日志 + 按 schema 填参数
   │   tool_use.input = {severity:..,    │
   │◀────────────────────────────────────┤   root_cause:.., evidence:[..], ..}
   │  Diagnosis.model_validate(input)    │
```

**关键认知**:LLM 这里没"执行工具",它只是被 schema 约束着输出结构化参数。
真正的"工具执行"是阶段 2 的事(那时模型让你调 `query_pg`,你执行后把结果喂回去)。

## 3. 为什么 Field 的 description 影响输出质量

`Diagnosis.model_json_schema()` 生成的 JSON Schema 里,**每个字段的 description 会原样进 schema 发给模型**。
模型填参数时就读这些 description。所以:
- `confidence: Field(description="证据弱就给低分,别一律 0.9")` → 模型真的会按这个收敛。
- `evidence: Field(description="不要编造日志里没有的内容")` → 降低幻觉。

**description 不是注释,是 prompt 的一部分。** 写得越具体,输出越准。这是结构化输出最省力的提质手段。

## 4. schema 从哪来

不用手写 JSON Schema —— Pydantic 直接生成:

```python
Diagnosis.model_json_schema()
# → {"type":"object","properties":{"severity":{"$ref":"#/$defs/Severity"},...},
#    "$defs":{"Severity":{"enum":["INFO","WARNING","CRITICAL"],...}}, "required":[...]}
```

枚举走 `$defs`/`$ref`,Claude 能正确处理。`required` 自动包含没有默认值的字段。

## 5. system prompt 的作用

工具机制管"格式",system prompt 管"行为/角色":
- 角色:"你是 SRE,基于日志做**只读**诊断"
- 约束:"证据不足就说不足,不要硬编根因;不要编造日志里没有的证据"
- 边界:"只读阶段,不要建议危险操作(重启/删除)"

格式靠工具 schema 兜底,质量靠 system prompt + Field description 共同塑造。

## 6. 怎么判断做得好不好(为阶段 4 埋点)

阶段 1 先人工看:拿 5-10 段不同日志,看输出的 root_cause / severity / confidence 合不合理。
把"明显对/明显错"的样本记下来 —— 这就是阶段 4 eval 测试集的雏形。
