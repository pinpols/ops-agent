# Changelog

本项目遵循语义化版本。日期倒序。

## 0.2.0 — T1 生产化(只读诊断副驾)

跨 10 个维度把"成熟学习项目"提升到 **T1 生产级只读诊断**。所有新能力默认安全、向后兼容。

### 新增
- **触发层** `ops-agent serve`(stdlib HTTP):`GET /healthz`、`GET /metrics`、`POST /diagnose`(告警 webhook,Bearer 鉴权,**未配 token 即 fail-closed**,只读=注入全拒审批闸)。
- **Dockerfile**:非 root + HEALTHCHECK + prod 默认。
- **多目标注册表** `targets.toml`(`ops_agent/targets.py`),回退单目标 env。
- **指标源工具** `query_metrics`(只读 Prometheus instant query)。
- **运行预算闸** `RunBudget`(墙钟 + token,越界 `BudgetExceeded`)。
- **自身指标** `Metrics`(Prometheus textfile + `/metrics`):诊断成败/超预算、累计 token、各工具调用数。
- **prompt 版本化** `PROMPT_VERSION` + 不可信围栏 `fence_untrusted`(注入纵深),随 run 落 trace。
- **密钥文件注入** `<NAME>_FILE`(docker/k8s secret)。
- **审计留存滚动**(`OPS_AUDIT_MAX_BYTES`)。
- **eval CI 硬闸**:`ops-agent eval --fail-under / --fail-on-regression`。
- `docs/production-readiness-checklist.md`(T1 十维度就绪清单 + 升 T2/T3 边界)。

### 变更
- 工具输出喂回 LLM 前包进不可信围栏。
- `ANTHROPIC_API_KEY` / `OPS_PG_DSN` / `OPS_WEBHOOK_TOKEN` 支持 `_FILE` 注入。
- CI eval 由非阻塞改为 `--fail-under 0.6` 硬闸。
- 包版本 0.1.0 → 0.2.0。

### 测试
- 145 测试(+38),覆盖率 80%(gate 70%);新模块全覆盖,serve 端到端起真 HTTP。

## 0.1.0 — 学习项目成熟化

阶段 1–5(结构化输出 → function calling → 多步 agent → LangGraph → 执行+HITL)+ 工程化骨架(CI/类型/测试/安全闸/脱敏/审批/eval/trace)。
