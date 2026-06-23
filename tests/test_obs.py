"""单测:@observe 在调用时(而非 import/装饰时)判定启用。

旧实现 import 期就绑定:若模块在 load_dotenv 之前被 import(如 evals 顶层 import),
Langfuse 即使后来配了也永久 no-op。改成调用时判定后,这条隐患消失。
"""

import ops_agent.obs as obs


def test_observe_decorates_without_checking_enablement(monkeypatch):
    calls = []
    monkeypatch.setattr(obs, "_enabled", lambda: calls.append(1) or False)

    @obs.observe
    def f():
        return 42

    # 装饰(import 期)不应判定启用
    assert calls == []
    # 调用时才判定,且原函数照常返回
    assert f() == 42
    assert calls == [1]


def test_observe_noop_preserves_return_when_disabled(monkeypatch):
    monkeypatch.setattr(obs, "_enabled", lambda: False)

    @obs.observe
    def add(a, b):
        return a + b

    assert add(2, 3) == 5
