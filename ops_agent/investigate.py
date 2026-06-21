"""阶段 2:模型自己决定调 read_logs 取数据,我们执行喂回,再给结构化结论。

单工具 + 单回合(不是多步 agent;循环留阶段 3)。概念见 docs/phase2-concepts.md。

运行:  python -m ops_agent.investigate "console 最近有什么异常?"
"""

import sys

from anthropic import Anthropic
from dotenv import load_dotenv

from ops_agent.config import get_settings
from ops_agent.diagnose import _TOOL_NAME as REPORT_TOOL_NAME
from ops_agent.diagnose import _build_tool as build_report_tool
from ops_agent.models import Diagnosis
from ops_agent.tools import READ_LOGS_TOOL, TOOL_IMPLS

_SYSTEM_PROMPT = (
    "你是资深 SRE。先用 read_logs 取相关日志,再基于**真实日志内容**做只读诊断,"
    "最后用 report_diagnosis 提交结构化结论。"
    "不要编造日志里没有的证据;证据不足就在 root_cause 说明并给低 confidence。"
)


def investigate(question: str, *, max_tokens: int = 1024) -> Diagnosis:
    client = Anthropic()
    model = get_settings().anthropic_model
    report_tool = build_report_tool()
    tools = [READ_LOGS_TOOL, report_tool]

    messages: list[dict] = [{"role": "user", "content": question}]

    # 回合 1:模型 auto 决定调哪个工具(预期先 read_logs)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        tools=tools,
        messages=messages,
    )

    # 处理工具回合:执行 read_logs 并喂回,直到模型给出 report_diagnosis(单工具场景通常 1~2 轮)
    for _ in range(4):  # 防御性上限,避免异常情况下死循环(真正的多步循环是阶段 3)
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            break

        # 原样回放 assistant 的 tool_use 请求
        messages.append({"role": "assistant", "content": resp.content})

        results = []
        report_input = None
        for tu in tool_uses:
            if tu.name == REPORT_TOOL_NAME:
                report_input = tu.input  # 模型下结论了
                # 结论工具也要回一个 tool_result,协议才完整
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "ok"})
            elif tu.name in TOOL_IMPLS:
                output = TOOL_IMPLS[tu.name](**tu.input)  # 真执行(read_logs)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output})
            else:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": f"unknown tool {tu.name}",
                        "is_error": True,
                    }
                )

        if report_input is not None:
            return Diagnosis.model_validate(report_input)

        # 把工具结果喂回,继续下一回合
        messages.append({"role": "user", "content": results})
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

    # 兜底:模型读了数据但没主动调 report → 强制它给结构化结论
    messages.append({"role": "assistant", "content": resp.content})
    messages.append({"role": "user", "content": "基于以上日志,用 report_diagnosis 给出结论。"})
    final = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        tools=[report_tool],
        tool_choice={"type": "tool", "name": REPORT_TOOL_NAME},
        messages=messages,
    )
    report_input = next((b.input for b in final.content if b.type == "tool_use"), None)
    if report_input is None:
        raise RuntimeError(f"模型未给出结论,stop_reason={final.stop_reason}")
    return Diagnosis.model_validate(report_input)


def main() -> None:
    load_dotenv()
    if len(sys.argv) < 2:
        print('用法: python -m ops_agent.investigate "你的运维问题"', file=sys.stderr)
        raise SystemExit(2)
    if not get_settings().anthropic_api_key:
        print("缺 ANTHROPIC_API_KEY,先 cp .env.example .env 并填 key", file=sys.stderr)
        raise SystemExit(2)

    result = investigate(sys.argv[1])
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
