"""执行类(写)工具 —— 危险动作。与只读工具(read_logs/query_pg)严格分开:
**必须经人工审批(HITL)才执行**,且默认 dry-run。

设计沿用你 ADR-029 的隔离思路:危险能力单独成类、白名单、默认不真执行、留审批闸。
"""

import os
import subprocess

# 允许操作的服务白名单(模型/被注入内容都越不出这个集合)
_ALLOWED_SERVICES = {"orchestrator", "trigger", "console", "worker-import",
                     "worker-export", "worker-process", "worker-dispatch", "worker-atomic"}


def restart_service(service: str) -> str:
    """重启一个服务(危险/写操作)。默认 dry-run:只回报"将要做什么",不真执行。

    真执行需显式 OPS_ALLOW_EXEC=true + 配 OPS_RESTART_CMD(命令模板,{service} 占位)。
    本工具被列入 DANGEROUS_TOOLS,调用前由 agent 的审批闸拦截(见 agent.py)。
    """
    if service not in _ALLOWED_SERVICES:
        return f"[restart_service] 拒绝:{service!r} 不在白名单 {sorted(_ALLOWED_SERVICES)}"

    if os.environ.get("OPS_ALLOW_EXEC", "").lower() != "true":
        return f"[restart_service] DRY-RUN:将重启 {service}(未真执行;设 OPS_ALLOW_EXEC=true 才真跑)"

    cmd_tpl = os.environ.get("OPS_RESTART_CMD")
    if not cmd_tpl:
        return "[restart_service] 已允许执行但未配 OPS_RESTART_CMD(如 'bash scripts/local/restart.sh {service}')"
    cmd = cmd_tpl.format(service=service)
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return f"[restart_service] 执行 `{cmd}` rc={out.returncode}\n{out.stdout[-500:]}{out.stderr[-300:]}"
    except subprocess.TimeoutExpired:
        return f"[restart_service] `{cmd}` 超时(60s)"


RESTART_TOOL = {
    "name": "restart_service",
    "description": "重启指定服务(危险操作,需人工审批)。仅在确认是该服务卡死/需重启时使用。",
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "服务名(白名单内,如 orchestrator/worker-import)"}
        },
        "required": ["service"],
    },
}

# 危险工具集合:agent 调它们前必须过审批闸
DANGEROUS_TOOLS = {"restart_service"}
EXEC_TOOL_IMPLS = {"restart_service": restart_service}
