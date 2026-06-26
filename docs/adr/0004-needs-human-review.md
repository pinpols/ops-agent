# ADR-0004:needs_human_review 派生,不让模型填

**Status**: Accepted

## Context
诊断有 `confidence`,但此前**产出却从不消费**:下游告警系统拿到看似确定的 severity,无法区分"模型很确定"和"模型猜的"。叠加 eval 实测——相邻 severity 会 run-to-run 摆动——over-confident 误诊会被自动当真。需要一个"该不该转人工"的路由信号。

## Decision
在 `Diagnosis` 上加 **pydantic `computed_field`** `needs_human_review`(models.py):
- 规则:`confidence < 0.6` 一律转人工;`CRITICAL` 误报代价高,`< 0.8` 也转人工(阈值为模块常量 `REVIEW_CONFIDENCE_FLOOR`/`CRITICAL_REVIEW_FLOOR`)。
- **派生而非模型填**:它进 `model_dump(mode=json)` → webhook/bundle/history 自动带上;但**不进** `report_diagnosis` 的 validation schema(computed_field 是序列化-only)→ 模型看不到、改不了、注入也篡改不了。
- 加 `diagnose_needs_review_total` 指标观测转人工率。

## Alternatives(否决)
- **让模型自评"要不要人看"**:confidence 已是模型自评,再自评是双重乐观,且可被注入篡改这个路由信号,否决。
- **double-run 跑两遍比对分歧**:更准但翻倍成本,推迟(可作为后续高阶信号)。
- **硬编码只看 confidence**:漏掉"高代价 CRITICAL 即便中等把握也该复核",故加 severity 维度。

## Consequences
- ✅ webhook 契约零改动(已 `model_dump(mode=json)`),信号自动出现在响应里。
- ✅ 关键不变量有测试锁:在 model_dump 内、**不在** validation schema 内。
- ✅ 结构上杜绝模型/注入篡改路由信号。
- ⚠️ 阈值是经验常量,非校准值;真实 precision/recall 需生产数据 + 反馈闭环(尚无)来调。
- 📌 这是把"有用工具"推向"可信产品"的一步,但**反馈闭环**(标错→回流 eval)才是信任复利的分水岭,仍待做。
