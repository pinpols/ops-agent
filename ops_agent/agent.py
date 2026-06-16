"""阶段 3:多步 agent —— 循环 + 多工具(read_logs / query_pg)+ 记忆(多轮)。

模型自己决定调几次、调哪些工具,直到给出结论;我们逐步执行并喂回。手写循环=完全透明,
之后(阶段 3b)再 port 到 LangGraph 体会框架价值。概念见 docs/phase3-concepts.md。

交互式多轮:  python -m ops_agent.agent
单次:        python -m ops_agent.agent "sim 跑批为什么慢?"
"""

import os
import sys

from anthropic import Anthropic
from dotenv import load_dotenv

from ops_agent.diagnose import _TOOL_NAME as REPORT_TOOL_NAME
from ops_agent.diagnose import _build_tool as build_report_tool
from ops_agent.models import Diagnosis
from ops_agent.obs import observe
from ops_agent.tools import QUERY_PG_TOOL, READ_LOGS_TOOL, TOOL_IMPLS

_SYSTEM_PROMPT = (
    "你是资深 SRE。你有工具:read_logs(读服务日志)、query_pg(只读 SQL 查运行态)。"
    "按需**多次**调用它们收集证据(日志看错误、SQL 看锁/积压等),证据够了再用 "
    "report_diagnosis 提交结构化结论。规则:只依据真实取到的数据,不编造;"
    "证据不足就在 root_cause 说明并给低 confidence;只读诊断,不建议危险操作。"
)


@observe
def run_agent(
    question: str,
    history: list[dict] | None = None,
    *,
    max_steps: int = 8,
    max_tokens: int = 1024,
) -> tuple[Diagnosis, list[dict]]:
    """跑一轮多步诊断。返回 (结论, 更新后的 messages)。把 messages 传回即可多轮追问。"""
    client = Anthropic()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    tools = [READ_LOGS_TOOL, QUERY_PG_TOOL, build_report_tool()]

    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": question})

    for _ in range(max_steps):
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=_SYSTEM_PROMPT,
            tools=tools, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            # 模型只讲话没调工具:逼它用 report_diagnosis 收口
            messages.append(
                {"role": "user", "content": "请用 report_diagnosis 给出结构化结论。"}
            )
            continue

        results = []
        for tu in tool_uses:
            if tu.name == REPORT_TOOL_NAME:
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "ok"})
                messages.append({"role": "user", "content": results})
                return Diagnosis.model_validate(tu.input), messages
            impl = TOOL_IMPLS.get(tu.name)
            output = impl(**tu.input) if impl else f"unknown tool {tu.name}"
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(output)})
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"达到 max_steps={max_steps} 仍未得出结论(可能在绕圈,检查工具/prompt)")


def _print(d: Diagnosis) -> None:
    print(d.model_dump_json(indent=2))


def main() -> None:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
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
