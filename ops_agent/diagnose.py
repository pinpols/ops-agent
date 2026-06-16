"""阶段 1:日志 → 结构化诊断(一次 LLM 调用,无工具执行、无 agent)。

用 Anthropic 的 function calling 机制"逼"模型按 ``Diagnosis`` schema 返回结构化结果:
把 Diagnosis 的 JSON Schema 当成一个工具的 input_schema,用 tool_choice 强制模型调它,
模型把诊断结论填进 tool_use.input,我们再用 Pydantic 校验成对象。

概念详解见 docs/phase1-concepts.md。

运行:  python -m ops_agent.diagnose data/sample-console.log
"""

import os
import sys

from anthropic import Anthropic
from dotenv import load_dotenv

from ops_agent.models import Diagnosis

# 工具名:模型不会真执行它,只是按它的 input_schema 把"诊断结论"作为参数填好返回。
_TOOL_NAME = "report_diagnosis"

_SYSTEM_PROMPT = (
    "你是资深 SRE,基于给定的服务日志做**只读**诊断。"
    "规则:"
    "(1) 只依据日志里**实际出现**的内容下结论,不要编造日志中没有的证据;"
    "(2) 证据不足时,root_cause 明说'证据不足,需进一步查 X',confidence 给低分,不要硬编;"
    "(3) 这是只读诊断阶段,suggested_action 只给排查方向,不要建议重启/删除等危险操作;"
    "(4) 通过 report_diagnosis 工具返回结构化结论。"
)


def _build_tool() -> dict:
    """把 Diagnosis 的 JSON Schema 包成一个 Anthropic 工具定义。

    Diagnosis.model_json_schema() 自动生成 schema(含 Severity 枚举的 $defs/$ref、
    required 列表、各 Field 的 description)——这些 description 会随 schema 发给模型,
    直接影响填得准不准(见 docs/phase1-concepts.md §3)。
    """
    return {
        "name": _TOOL_NAME,
        "description": "把对这段日志的结构化诊断结论作为参数提交。",
        "input_schema": Diagnosis.model_json_schema(),
    }


def diagnose_log(log_text: str) -> Diagnosis:
    """把一段日志交给 LLM,返回结构化的 Diagnosis。"""
    client = Anthropic()  # 自动读环境变量 ANTHROPIC_API_KEY
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        tools=[_build_tool()],
        # 强制模型必须调用该工具(而不是自由回话)→ 保证拿到结构化参数
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": f"诊断以下日志,通过 {_TOOL_NAME} 返回结论:\n\n```\n{log_text}\n```",
            }
        ],
    )

    # 从返回内容里取出 tool_use 块;tool_choice 强制后正常必有一个。
    tool_input = next(
        (block.input for block in response.content if block.type == "tool_use"),
        None,
    )
    if tool_input is None:
        raise RuntimeError(
            f"模型未按预期调用工具,stop_reason={response.stop_reason};"
            f"内容块类型={[b.type for b in response.content]}"
        )

    # Pydantic 再校验一遍(类型/枚举/0~1 范围);校验失败说明 prompt/schema 还得调。
    return Diagnosis.model_validate(tool_input)


def main() -> None:
    load_dotenv()
    if len(sys.argv) < 2:
        print("用法: python -m ops_agent.diagnose <日志文件路径>", file=sys.stderr)
        raise SystemExit(2)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("缺 ANTHROPIC_API_KEY,先 cp .env.example .env 并填 key", file=sys.stderr)
        raise SystemExit(2)

    log_text = open(sys.argv[1], encoding="utf-8").read()
    result = diagnose_log(log_text)

    # 结构化结果 → 人读(用 Pydantic 序列化,顺带验证拿到的是合法 Diagnosis)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
