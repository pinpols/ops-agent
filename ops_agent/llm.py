"""共享 LLM 客户端工厂。

集中重试/韧性配置,替代 agent / diagnose / investigate / scorers 各自裸 ``Anthropic()``。
SDK 默认 ``max_retries=2``;运维诊断是多步工具链路,单次 429 / 超时不该中断整条链路,
默认调高到 4(可经 ``OPS_LLM_MAX_RETRIES`` 覆盖)。

可选经 **agentctl 网关**(``OPS_USE_GATEWAY=true``):统一路由/回退/成本/缓存 + 全量捕获。
网关默认关 → 原生直连,零回归。开启时返回一个 ``.messages.create(...)`` 兼容的 shim:
内部走 ``GatewayClient.messages(...)``,再把网关返回的结构化响应**还原成原生 SDK 形状**,
让 agent / diagnose 等调用方零改动(它们读 ``content[].input`` / ``.stop_reason`` / ``.usage``)。
"""

import os
from types import SimpleNamespace
from typing import Any, Protocol

from anthropic import Anthropic

from ops_agent.config import get_settings


class _Block(dict):
    """双访问内容块:既支持属性(``b.type`` / ``b.input``,ops-agent 消费用),
    又是 dict(``b.get('type')``,网关 openai 互译 tooltrans 用)。多轮工具对话两端都要。"""

    __getattr__ = dict.get


class LLMClient(Protocol):
    """``make_client()`` 返回契约:有 ``messages`` 且 ``.create(**kwargs)`` 返回原生形状响应。"""

    @property
    def messages(self) -> Any:  # 只读属性:兼容 Anthropic 的 property 与 shim 的实例属性
        ...


def make_client() -> LLMClient:
    """构造 LLM 客户端:默认原生 Anthropic;``OPS_USE_GATEWAY=true`` 时走 agentctl 网关 shim。"""
    if gateway_enabled():
        # 网关按**逻辑路由名**(routes 的键,如 default)选目标 + 回退,不是具体模型名;
        # ops-agent 调用方传的是具体模型(claude-sonnet-4-6),故 shim 统一改用路由名,
        # 由网关 config 的 routes 决定实际模型链(OPS_GATEWAY_ROUTE 可配,默认 default)。
        route = os.getenv("OPS_GATEWAY_ROUTE", "default")
        return _GatewayAnthropicShim(build_gateway_client(), route)
    return Anthropic(max_retries=get_settings().anthropic_max_retries)


def gateway_enabled() -> bool:
    return os.getenv("OPS_USE_GATEWAY", "").lower() == "true"


def build_gateway_client() -> Any:
    """OPS_USE_GATEWAY=true 时构造 agent_ctl 库形态 client。延迟 import,不开则不依赖 agent_ctl。

    **provider 可配置、不单依赖 Anthropic**:按目录(catalog)从环境里有 key 的 provider 构建
    (anthropic/openai/deepseek/qwen/glm);走哪家由 AGENT_CTL_CONFIG 的路由/别名决定(shim 用
    'default' 路由)。工具调用经 agent_ctl 的 Anthropic↔OpenAI 互译,在任意 provider 上都可用。
    没配任何 catalog key 时,退回原生 Anthropic(向后兼容)。
    """
    from agent_ctl.client.gateway_client import GatewayClient
    from agent_ctl.config import load_config
    from agent_ctl.providers.catalog import build_providers

    cfg = load_config(os.getenv("AGENT_CTL_CONFIG"))
    providers = build_providers()
    if not providers:
        import anthropic
        from agent_ctl.providers.anthropic_provider import AnthropicProvider

        native = anthropic.Anthropic(max_retries=get_settings().anthropic_max_retries)
        providers = {"anthropic": AnthropicProvider(native)}
    return GatewayClient.from_config(cfg, providers)


def reconstruct_response(normalized: Any) -> SimpleNamespace:
    """把 agentctl ``NormalizedResponse`` 还原成原生 Anthropic 响应形状(鸭子类型即可)。

    优先用 ``raw``(完整结构化响应,含 ``tool_use`` 块的 ``input``);provider 未带 raw 时退化成
    单 text 块(纯文本路由仍可用,但工具调用会拿不到 input —— 故 agentctl provider 必须回 raw)。
    """
    raw = getattr(normalized, "raw", None)
    if not raw:
        raw = {
            "content": [{"type": "text", "text": getattr(normalized, "text", "")}],
            "stop_reason": getattr(normalized, "finish_reason", None),
            "usage": {
                "input_tokens": getattr(normalized, "input_tokens", 0),
                "output_tokens": getattr(normalized, "output_tokens", 0),
            },
        }
    # 块用**双访问**对象:ops-agent 读 b.type/b.input(属性),而这些块会被原样塞进下一轮
    # 请求的 assistant 消息——网关 openai 互译(tooltrans)按 b.get('type') 读(字典)。
    # 二者都要支持,否则多轮(round2+)工具对话在 openai/deepseek provider 上炸。
    content = [_Block(block) for block in raw.get("content", [])]
    usage = raw.get("usage") or {}
    return SimpleNamespace(
        content=content,
        stop_reason=raw.get("stop_reason"),
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
        ),
    )


class _GatewayMessages:
    """``client.messages`` shim:原生 ``create(**kwargs)`` → 网关 ``messages(...)`` → 还原响应。"""

    def __init__(self, gateway: Any, route: str) -> None:
        self._gateway = gateway
        self._route = route

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int = 1024,
        system: str | None = None,
        tools: list | None = None,
        tool_choice: dict | None = None,
        temperature: float | None = None,
        **_ignored: Any,
    ) -> SimpleNamespace:
        # 用逻辑路由名(非传入的具体模型名 model)调网关:网关据 routes 解析实际模型链 + 回退。
        # cache_control 等原生特性经 **_ignored 吞掉(网关层不透传,缓存由网关自管),不报错。
        normalized = self._gateway.messages(
            model=self._route,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            system=system,
            tool_choice=tool_choice,
        )
        return reconstruct_response(normalized)


class _GatewayAnthropicShim:
    """``.messages.create(...)`` 兼容门面,内部走 agentctl 网关。"""

    def __init__(self, gateway: Any, route: str = "default") -> None:
        self.messages = _GatewayMessages(gateway, route)
