"""可选可观测:配了 Langfuse 就把函数纳入 trace(token/成本/延迟),没配则 no-op。

故意做成零摩擦:不装 langfuse、或没设 LANGFUSE_* 环境变量时,@observe 退化为原函数,
agent/eval 照常跑。要看 trace 时:pip install langfuse + 设 LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST。
"""

import os
from collections.abc import Callable
from typing import TypeVar

F = TypeVar("F", bound=Callable)


def _enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def observe(fn: F) -> F:
    """把 fn 纳入 Langfuse trace;未配置/未安装则原样返回。"""
    if not _enabled():
        return fn
    try:
        from langfuse import observe as _lf_observe  # type: ignore
    except ImportError:
        return fn
    return _lf_observe()(fn)  # type: ignore[return-value]
