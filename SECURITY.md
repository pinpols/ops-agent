# 安全策略 / Security Policy

## 支持的版本

本项目处于活跃开发,安全修复只针对 `main` 与最近一个发布版本。

## 报告漏洞 / Reporting a Vulnerability

**请勿在公开 issue 中披露安全漏洞。**

通过 GitHub 的 **Security Advisories**(仓库 → Security → *Report a vulnerability*)私下提交,
或邮件联系维护者。请尽量包含:

- 受影响组件 / 版本(`ops_agent` 模块、`serve` 端点、网关集成等)
- 复现步骤或 PoC
- 影响评估(信息泄露 / RCE / 越权 / 拒绝服务等)

我们会在 **3 个工作日**内确认收到,并在评估后给出修复时间线。

## 本项目的安全基线(已落地)

诊断智能体涉敏感运维数据,已内建多重控制(详见 `docs/production-readiness-checklist.md`):

- **只读优先**:触发层(`serve /diagnose`)注入"全拒"审批闸,危险执行需显式多重开关 + HITL。
- **fail-closed 鉴权**:webhook 未配 token 即拒;生产 profile 强制最小权限只读 DB、日志只读挂载。
- **脱敏**:出网内容与落库历史统一过 `redact_text`(token / DSN 口令 / JWT / 邮箱 / 手机号)。
- **防注入**:工具输出包不可信围栏喂回 LLM;PromQL/SQL 走参数化 + scheme 校验。
- **供应链门禁**(CI):`bandit` SAST、`pip-audit` 依赖漏洞、CycloneDX SBOM、gitleaks 秘钥扫描、
  Dependabot 自动依赖更新。

## 安全相关配置开关

生产部署请对照 `docs/production-readiness-checklist.md` 的"上线前置",并跑 `ops-agent doctor` 自检。
