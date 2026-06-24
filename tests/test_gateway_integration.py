"""agentctl 网关集成单测:开关 + 响应还原 + shim 转发(不需安装 agentctl,用注入假网关)。"""

from types import SimpleNamespace

from anthropic import Anthropic

from ops_agent import llm


def test_gateway_disabled_returns_native(monkeypatch):
    monkeypatch.delenv("OPS_USE_GATEWAY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert llm.gateway_enabled() is False
    assert isinstance(llm.make_client(), Anthropic)  # 关闭=原生路径


def test_gateway_enabled_flag(monkeypatch):
    monkeypatch.setenv("OPS_USE_GATEWAY", "true")
    assert llm.gateway_enabled() is True


def test_reconstruct_rebuilds_tool_use_from_raw():
    # 网关带 raw(完整结构化响应)→ 还原出 tool_use 块,input 可取(诊断/agent 靠它拿结论)
    normalized = SimpleNamespace(
        raw={
            "content": [
                {"type": "text", "text": "thinking"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "report",
                    "input": {"severity": "WARNING"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
    )
    resp = llm.reconstruct_response(normalized)
    assert resp.stop_reason == "tool_use"
    assert resp.usage.input_tokens == 11
    tu = resp.content[1]
    assert tu.type == "tool_use"
    assert tu.name == "report"
    assert tu.input == {"severity": "WARNING"}


def test_reconstruct_falls_back_to_text_when_no_raw():
    normalized = SimpleNamespace(
        raw=None, text="hello", finish_reason="end_turn", input_tokens=3, output_tokens=1
    )
    resp = llm.reconstruct_response(normalized)
    assert resp.content[0].type == "text"
    assert resp.content[0].text == "hello"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.output_tokens == 1


def test_shim_create_forwards_system_tool_choice_and_ignores_native_extras():
    captured = {}

    class FakeGateway:
        def messages(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                raw={
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {},
                }
            )

    shim = llm._GatewayAnthropicShim(FakeGateway())
    resp = shim.messages.create(
        model="claude-x",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        system="你是 SRE",
        tools=[{"name": "report"}],
        tool_choice={"type": "tool", "name": "report"},
        cache_control={"type": "ephemeral"},  # 原生专属,应被 shim 吞掉不报错
    )
    assert captured["system"] == "你是 SRE"
    assert captured["tool_choice"] == {"type": "tool", "name": "report"}
    assert captured["tools"] == [{"name": "report"}]
    assert "cache_control" not in captured
    assert resp.content[0].text == "ok"
