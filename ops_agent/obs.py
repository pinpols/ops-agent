"""可选可观测:配了 Langfuse 就把函数纳入 trace(token/成本/延迟),没配则 no-op。

故意做成零摩擦:不装 langfuse、或没设 LANGFUSE_* 环境变量时,@observe 退化为原函数,
agent/eval 照常跑。要看 trace 时:pip install langfuse + 设 LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST。
"""

import functools
from collections.abc import Callable
from typing import TypeVar

from ops_agent.config import get_settings

F = TypeVar("F", bound=Callable)


def _enabled() -> bool:
    return get_settings().langfuse_enabled


def observe(fn: F) -> F:
    """把 fn 纳入 Langfuse trace;未配置/未安装则原样执行。

    启用判定放在**调用时**(而非装饰/import 时):否则若本模块在 load_dotenv 之前被 import
    (如 evals 顶层 import diagnose),会永久绑定为 no-op,后来配了 Langfuse 也不生效。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _enabled():
            return fn(*args, **kwargs)
        try:
            from langfuse import observe as _lf_observe
        except ImportError:
            return fn(*args, **kwargs)
        return _lf_observe()(fn)(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
