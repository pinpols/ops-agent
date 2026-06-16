"""阶段 1:日志 → 结构化诊断(一次 LLM 调用,无工具无 agent)。

脚手架已给好(读文件 / 加载 env / 打印)。**核心 `diagnose_log` 留你自己写**——
这正是阶段 1 要学的:怎么调 LLM 并逼它按 Pydantic schema 返回结构化结果。

运行:  python -m ops_agent.diagnose data/sample-console.log
"""

import os
import sys

from dotenv import load_dotenv

from ops_agent.models import Diagnosis


def diagnose_log(log_text: str) -> Diagnosis:
    """把一段日志交给 LLM,返回结构化的 Diagnosis。

    TODO(你来写)—— 阶段 1 的全部学习点都在这个函数里:

    1) 初始化 client:
         from anthropic import Anthropic
         client = Anthropic()              # 自动读 ANTHROPIC_API_KEY
         model = os.environ["ANTHROPIC_MODEL"]

    2) 逼模型按 Diagnosis schema 返回结构化结果。两条主流路子,任选一条先跑通:
       (A) tool/function calling:把 Diagnosis 当成一个"工具"的入参 schema 交给模型,
           用 client.messages.create(..., tools=[{...}], tool_choice={"type":"tool","name":...}),
           模型会把结论填进 tool_use.input —— 拿到的就是一个 dict。
           schema 怎么来?Diagnosis.model_json_schema() 直接生成 JSON Schema。
       (B) 让模型直接输出 JSON:system 里说"只输出符合该 schema 的 JSON",
           把 model_json_schema() 贴进 prompt,再自己 json.loads。
       建议先试 (A),它把"守格式"交给了模型的工具机制,比纯 prompt 稳。

    3) 把模型返回的 dict 解析成 Diagnosis:
         return Diagnosis.model_validate(tool_input)
       —— Pydantic 会校验类型/枚举/0~1 范围;校验失败说明 prompt/schema 还得调。

    提示:
    - system prompt 写清角色("你是 SRE,基于日志做只读诊断")+ 约束("不要编造日志里没有的证据")。
    - 先让它跑出**任意**合法 Diagnosis,再逐步调 prompt 让结论更准 —— 这就是"基本功"。
    """
    raise NotImplementedError("阶段 1:在这里实现 LLM 调用 + 结构化解析(见上方 TODO)")


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

    # 结构化结果 → 人读(用 Pydantic 序列化,顺带验证你拿到的是合法 Diagnosis)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
