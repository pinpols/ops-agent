"""阶段 3:多步 agent —— 循环 + 多工具(read_logs / query_pg)+ 记忆(多轮)。

模型自己决定调几次、调哪些工具,直到给出结论;我们逐步执行并喂回。手写循环=完全透明,
之后(阶段 3b)再 port 到 LangGraph 体会框架价值。概念见 docs/phase3-concepts.md。

交互式多轮:  python -m ops_agent.agent
单次:        python -m ops_agent.agent "sim 跑批为什么慢?"
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from ops_agent.config import get_settings
from ops_agent.diagnose import _TOOL_NAME as REPORT_TOOL_NAME
from ops_agent.diagnose import _build_tool as build_report_tool
from ops_agent.exec_tools import DANGEROUS_TOOLS, EXEC_TOOL_RESULT_IMPLS, RESTART_TOOL
from ops_agent.models import Diagnosis
from ops_agent.obs import observe
from ops_agent.system_tools import SYSTEM_TOOL_RESULT_IMPLS, SYSTEM_TOOLS
from ops_agent.tool_result import ToolResult
from ops_agent.tools import QUERY_PG_TOOL, READ_LOGS_TOOL, TOOL_RESULT_IMPLS
from ops_agent.trace_io import write_agent_trace

_SYSTEM_PROMPT = (
    "你是资深 SRE。工具:list_services(列服务)、tail_recent_errors(扫近期异常)、"
    "inspect_compose(看依赖/端口)、read_app_config(看配置)、read_logs(读日志)、"
    "query_pg(只读 SQL)、restart_service(重启服务,危险)。"
    "用户没给明确服务名时,先用 list_services/tail_recent_errors 建立上下文。"
    "先用只读工具按需多次取证,证据够了用 report_diagnosis 给结论。"
    "只在确实定位到某服务卡死、且诊断已说明理由后,才考虑 restart_service(它会要人工审批)。"
    "只依据真实取到的数据,不编造;证据不足给低 confidence。"
)

# 所有工具实现的派发表(只读 + 执行)
_ALL_IMPLS = {**SYSTEM_TOOL_RESULT_IMPLS, **TOOL_RESULT_IMPLS, **EXEC_TOOL_RESULT_IMPLS}


@dataclass(frozen=True)
class AgentStepTrace:
    step: int
    tool_name: str
    tool_input: dict[str, Any]
    ok: bool
    output: str
    error: str | None = None
    approved: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _console_approver(tool_name: str, tool_input: dict) -> bool:
    """默认审批闸:命令行问 y/N。可注入替换(测试 / 自动化)。"""
    ans = input(f"\n⚠️  agent 要执行危险操作 {tool_name}({tool_input})。批准?[y/N] ").strip().lower()
    return ans == "y"


@observe
def run_agent(
    question: str,
    history: list[dict] | None = None,
    *,
    max_steps: int = 8,
    max_tokens: int = 1024,
    approver: Callable[[str, dict], bool] | None = None,
    include_trace: bool = False,
) -> tuple[Diagnosis, list[dict]] | tuple[Diagnosis, list[dict], list[AgentStepTrace]]:
    """跑一轮多步诊断。返回 (结论, 更新后的 messages)。把 messages 传回即可多轮追问。

    approver:危险工具(DANGEROUS_TOOLS)执行前的审批闸,返回 True 才执行;
    默认命令行 y/N。测试/自动化可注入。
    """
    client = Anthropic()
    settings = get_settings()
    model = settings.anthropic_model
    approve = approver or _console_approver
    tools = [*SYSTEM_TOOLS, READ_LOGS_TOOL, QUERY_PG_TOOL, RESTART_TOOL, build_report_tool()]

    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": question})
    trace: list[AgentStepTrace] = []

    for step in range(1, max_steps + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            # 模型只讲话没调工具:逼它用 report_diagnosis 收口
            messages.append({"role": "user", "content": "请用 report_diagnosis 给出结构化结论。"})
            continue

        results = []
        for tu in tool_uses:
            if tu.name == REPORT_TOOL_NAME:
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "ok"})
                messages.append({"role": "user", "content": results})
                diagnosis = Diagnosis.model_validate(tu.input)
                if settings.ops_trace_dir:
                    write_agent_trace(
                        settings.ops_trace_dir,
                        question=question,
                        model=model,
                        diagnosis=diagnosis,
                        steps=trace,
                    )
                if include_trace:
                    return diagnosis, messages, trace
                return diagnosis, messages
            # 危险工具:执行前过审批闸(HITL)
            approved: bool | None = None
            if tu.name in DANGEROUS_TOOLS:
                approved = approve(tu.name, tu.input)
            if approved is False:
                result = ToolResult.failure(
                    f"[审批] 用户拒绝执行 {tu.name}({tu.input}),未执行。",
                    error_type="approval_denied",
                )
            else:
                impl = _ALL_IMPLS.get(tu.name)
                if impl is None:
                    result = ToolResult.failure(
                        f"unknown tool {tu.name}", error_type="unknown_tool"
                    )
                else:
                    try:
                        result = impl(**tu.input)
                    except Exception as e:  # noqa: BLE001 - tool boundary
                        result = ToolResult.failure(
                            f"[{tu.name}] 工具异常:{type(e).__name__}: {e}",
                            error_type="tool_exception",
                        )
            output = result.to_text()
            trace.append(
                AgentStepTrace(
                    step=step,
                    tool_name=tu.name,
                    tool_input=dict(tu.input),
                    ok=result.ok,
                    output=output,
                    error=result.error,
                    approved=approved,
                    metadata=result.metadata,
                )
            )
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(output)})
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"达到 max_steps={max_steps} 仍未得出结论(可能在绕圈,检查工具/prompt)")


def _print(d: Diagnosis) -> None:
    print(d.model_dump_json(indent=2))


def main() -> None:
    load_dotenv()
    if not get_settings().anthropic_api_key:
        print("缺 ANTHROPIC_API_KEY,先 cp .env.example .env 并填 key", file=sys.stderr)
        raise SystemExit(2)

    if len(sys.argv) >= 2:  # 单次模式
        d, _ = run_agent(sys.argv[1])
        _print(d)
        return

    # 多轮交互:history 累积 = 记忆
    print("多轮运维诊断 agent(空行/exit 退出)。例:sim 跑批为什么慢?")
    history: list[dict] = []
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q in {"exit", "quit"}:
            break
        d, history = run_agent(q, history)
        _print(d)


if __name__ == "__main__":
    main()
