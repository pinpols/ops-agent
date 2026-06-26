"""执行类(写)工具 —— 危险动作。与只读工具(read_logs/query_pg)严格分开:
**必须经人工审批(HITL)才执行**,且默认 dry-run。

设计沿用你 ADR-029 的隔离思路:危险能力单独成类、白名单、默认不真执行、留审批闸。
"""

import os
import shlex
import subprocess  # nosec B404

from ops_agent.config import Settings, get_settings
from ops_agent.tool_result import ToolResult

# 允许操作的服务白名单(模型/被注入内容都越不出这个集合)
_ALLOWED_SERVICES = {
    "orchestrator",
    "trigger",
    "console",
    "worker-import",
    "worker-export",
    "worker-process",
    "worker-dispatch",
    "worker-atomic",
}


def exec_gate(
    settings: Settings, *, dry_run_desc: str, tool: str, **meta: object
) -> ToolResult | None:
    """写操作执行侧统一闸(审批闸 HITL 在更上层 agent.DANGEROUS_TOOLS):

    - 未开 `OPS_ALLOW_EXEC` → 返回 DRY-RUN 结果(只报"将做什么",不真执行)。
    - prod profile 未显式 `OPS_PROD_ALLOW_EXEC` → 硬拒(防 dev/CI 的 OPS_ALLOW_EXEC 泄漏继承)。
    - 放行 → 返回 None(调用方继续真执行)。

    restart_service 与 Flink 写工具共用此闸,避免各自复制 dry-run/prod 双开关逻辑。
    """
    if not settings.ops_allow_exec:
        return ToolResult.success(
            f"[{tool}] DRY-RUN:{dry_run_desc}(未真执行;设 OPS_ALLOW_EXEC=true 才真跑)",
            dry_run=True,
            **meta,
        )
    if settings.production and not settings.ops_prod_allow_exec:
        return ToolResult.failure(
            f"[{tool}] 拒绝:生产 profile 下执行需显式 OPS_PROD_ALLOW_EXEC=true"
            "(OPS_ALLOW_EXEC 单独不足以在 prod 放行)",
            **meta,
        )
    return None


def _command_allowed(command_args: list[str], allowlist: tuple[str, ...]) -> bool:
    if not command_args or not allowlist:
        return False
    executable = command_args[0]
    # 按完整 argv[0] 或其 basename 匹配:'/bin/echo' 命中 allowlist 里的 'echo',
    # 避免旧实现 path 形态 vs 名字形态对不上导致的误判/误拒。
    return executable in allowlist or os.path.basename(executable) in allowlist


def restart_service_result(service: str) -> ToolResult:
    """重启一个服务(危险/写操作),返回结构化工具结果。"""
    if service not in _ALLOWED_SERVICES:
        return ToolResult.failure(
            f"[restart_service] 拒绝:{service!r} 不在白名单 {sorted(_ALLOWED_SERVICES)}",
            service=service,
        )

    settings = get_settings()
    # 执行侧默认安全闸(dry-run / prod 双开关),与 Flink 写工具共用。
    gate = exec_gate(
        settings, dry_run_desc=f"将重启 {service}", tool="restart_service", service=service
    )
    if gate is not None:
        return gate

    cmd_tpl = settings.ops_restart_cmd
    if not cmd_tpl:
        return ToolResult.failure(
            "[restart_service] 已允许执行但未配 OPS_RESTART_CMD"
            "(如 'bash scripts/local/restart.sh {service}')",
            service=service,
        )
    # 先分词模板,再把 {service} 仅替换进单个 token(service 已经过白名单校验;
    # 这样即便将来放宽校验,service 值也无法拆出额外 argv)。非 shell 执行(argv 列表)。
    cmd_args = [token.format(service=service) for token in shlex.split(cmd_tpl)]
    if not cmd_args:
        return ToolResult.failure("[restart_service] OPS_RESTART_CMD 解析后为空", service=service)
    cmd = shlex.join(cmd_args)
    if not _command_allowed(cmd_args, settings.ops_exec_allowlist):
        return ToolResult.failure(
            "[restart_service] 命令不在 OPS_EXEC_ALLOWLIST 中,拒绝执行",
            service=service,
            command=cmd,
            command_args=cmd_args,
            allowlist=settings.ops_exec_allowlist,
        )
    try:
        out = subprocess.run(  # nosec B603
            cmd_args, capture_output=True, text=True, timeout=60
        )
        content = (
            f"[restart_service] 执行 `{cmd}` rc={out.returncode}\n"
            f"{out.stdout[-500:]}{out.stderr[-300:]}"
        )
        return ToolResult(
            ok=out.returncode == 0,
            content=content,
            error=None if out.returncode == 0 else content,
            metadata={
                "service": service,
                "command": cmd,
                "command_args": cmd_args,
                "returncode": out.returncode,
            },
        )
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"[restart_service] `{cmd}` 超时(60s)", service=service)


def restart_service(service: str) -> str:
    """重启一个服务(危险/写操作)。默认 dry-run:只回报"将要做什么",不真执行。

    真执行需显式 OPS_ALLOW_EXEC=true + 配 OPS_RESTART_CMD(命令模板,{service} 占位)。
    本工具被列入 DANGEROUS_TOOLS,调用前由 agent 的审批闸拦截(见 agent.py)。
    """
    return restart_service_result(service).to_text()


RESTART_TOOL = {
    "name": "restart_service",
    "description": "重启指定服务(危险操作,需人工审批)。仅在确认是该服务卡死/需重启时使用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "服务名(白名单内,如 orchestrator/worker-import)",
            }
        },
        "required": ["service"],
    },
}

# 危险标记现由 tool_registry 单一声明(Tool(..., dangerous=True)),不再在此另立一份,防双源漂移。
EXEC_TOOL_IMPLS = {"restart_service": restart_service}
EXEC_TOOL_RESULT_IMPLS = {"restart_service": restart_service_result}
