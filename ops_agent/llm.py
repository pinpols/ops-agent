"""共享 LLM 客户端工厂。

集中重试/韧性配置,替代 agent / diagnose / investigate / scorers 各自裸 ``Anthropic()``。
SDK 默认 ``max_retries=2``;运维诊断是多步工具链路,单次 429 / 超时不该中断整条链路,
默认调高到 4(可经 ``OPS_LLM_MAX_RETRIES`` 覆盖)。集中一处便于后续接入缓存 / thinking。
"""

from anthropic import Anthropic

from ops_agent.config import get_settings


def make_client() -> Anthropic:
    """构造带重试配置的 Anthropic 客户端(自动读 ``ANTHROPIC_API_KEY``)。"""
    return Anthropic(max_retries=get_settings().anthropic_max_retries)
