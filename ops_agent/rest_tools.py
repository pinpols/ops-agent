"""只读 REST 工具的共享底座 —— GET-only + 路径白名单 + scheme 校验 + 截断/超时。

flink_tools / kafka_tools 复用,避免两份相同的出网/校验逻辑。**只读是结构性的**:只发 GET,
被诊断系统的写操作(均为 POST/PATCH/DELETE)在协议层就到不了;再叠路径白名单做纵深防御。
"""

import re
import urllib.error
import urllib.request

from ops_agent.tool_result import ToolResult

DEFAULT_CAP = 12000  # 响应字符上限,防大 JSON 撑爆上下文
DEFAULT_TIMEOUT_S = 10


def path_allowed(path: str, allowed: list[re.Pattern[str]]) -> bool:
    if ".." in path or "://" in path or any(c.isspace() for c in path):
        return False
    return any(rx.match(path) for rx in allowed)


def readonly_rest_get(
    base: str | None,
    path: str,
    allowed: list[re.Pattern[str]],
    *,
    tool: str,
    whitelist_hint: str,
    not_configured_msg: str,
    cap: int = DEFAULT_CAP,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> ToolResult:
    """GET 一个白名单内的只读 REST 路径。校验顺序:path 合法 → 白名单 → base 已配 → scheme → GET。"""
    if not isinstance(path, str) or not path.strip():
        return ToolResult.failure(f"[{tool}] path 必须是非空字符串")
    path = path.strip()
    if not path.startswith("/"):
        return ToolResult.failure(f"[{tool}] path 必须以 / 开头(REST 路径,不是完整 URL)")
    if not path_allowed(path, allowed):
        return ToolResult.failure(f"[{tool}] 路径不在只读白名单内({whitelist_hint})", path=path)
    if not base:
        return ToolResult.failure(not_configured_msg)
    if not base.startswith(("http://", "https://")):
        return ToolResult.failure(f"[{tool}] base URL 必须是 http(s)")
    url = base.rstrip("/") + path
    # method 显式 GET:结构性只读(被诊断系统的写操作均非 GET)。
    req = urllib.request.Request(  # noqa: S310  # nosec B310
        url, headers={"Accept": "application/json"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as e:
        return ToolResult.failure(f"[{tool}] 请求失败:{e}", path=path)
    truncated = len(raw) > cap
    return ToolResult.success(raw[:cap], path=path, bytes=len(raw), truncated=truncated)
