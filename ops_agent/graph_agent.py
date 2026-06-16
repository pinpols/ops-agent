"""阶段 3b:把阶段 3 的手写循环 port 到 LangGraph。

对照看框架替你做了什么:阶段 3 你手写 `while 有 tool_use: 执行+喂回` + 手动传 messages 做记忆;
这里 `create_react_agent` 把**整个循环**封掉,`checkpointer` 把**记忆**变成"传个 thread_id"。
你不再写循环——但因为阶段 3 你已亲手写过,所以这不是黑盒,你清楚里面就是那个循环。

概念/对照见 docs/phase3b-concepts.md。

交互多轮:  python -m ops_agent.graph_agent
单次:      python -m ops_agent.graph_agent "sim 跑批为什么慢?"
"""

import os
import sys

from dotenv import load_dotenv
from langchain_core.tools import tool

from ops_agent.models import Diagnosis
from ops_agent.tools import query_pg as _query_pg
from ops_agent.tools import read_logs as _read_logs

_SYSTEM_PROMPT = (
    "你是资深 SRE。用 read_logs / query_pg 按需多次取证(日志看错误、SQL 看锁/积压),"
    "证据够了给出结构化诊断。只依据真实取到的数据,不编造;证据不足给低 confidence;只读,不建议危险操作。"
)


# LangChain 工具 = 给我们已有的纯函数套一层(docstring 会作为 description 发给模型,和裸 SDK 一样)
@tool
def read_logs(service: str, pattern: str | None = None, max_lines: int = 200) -> str:
    """读取指定服务日志做排查。先取数据再下结论;pattern 是可选正则,过滤关键行。"""
    return _read_logs(service, pattern, max_lines)


@tool
def query_pg(sql: str, max_rows: int = 50) -> str:
    """对平台库执行只读 SQL(单条 SELECT/WITH)查日志看不到的运行态,如 pg_stat_activity、状态计数。"""
    return _query_pg(sql, max_rows)


def build_agent():
    """构建 LangGraph react agent:模型 + 工具 + 系统提示 + 结构化输出 + 记忆(checkpointer)。"""
    from langchain_anthropic import ChatAnthropic
    from langgraph.checkpoint.memory import MemorySaver

    # 注:LangGraph V1.0 起 create_react_agent 已迁到 langchain.agents.create_agent
    # (需装 langchain 元包)。当前依赖下 langgraph.prebuilt 仍可用,故沿用;
    # 升级 langchain 后可平滑切到 create_agent(签名兼容 model/tools/prompt/response_format)。
    from langgraph.prebuilt import create_react_agent

    model = ChatAnthropic(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"), max_tokens=1024
    )
    return create_react_agent(
        model,
        tools=[read_logs, query_pg],
        prompt=_SYSTEM_PROMPT,
        response_format=Diagnosis,   # 框架替你做"最后一步结构化输出"
        checkpointer=MemorySaver(),  # 框架替你做"记忆":同 thread_id 自动接上文
    )


def run(agent, question: str, thread_id: str = "default") -> Diagnosis:
    """跑一轮。记忆靠 thread_id —— 同一个 thread_id 的多次调用自动共享历史(不用手传 messages)。"""
    result = agent.invoke(
        {"messages": [("user", question)]},
        config={"configurable": {"thread_id": thread_id}},
    )
    return result["structured_response"]


def main() -> None:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("缺 ANTHROPIC_API_KEY,先 cp .env.example .env 并填 key", file=sys.stderr)
        raise SystemExit(2)

    agent = build_agent()
    if len(sys.argv) >= 2:
        print(run(agent, sys.argv[1]).model_dump_json(indent=2))
        return

    print("LangGraph 多轮诊断 agent(空行/exit 退出)。记忆走 checkpointer。")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q in {"exit", "quit"}:
            break
        # 固定 thread_id="repl" → 多轮共享记忆(对比阶段3:那里要手传 history)
        print(run(agent, q, thread_id="repl").model_dump_json(indent=2))


if __name__ == "__main__":
    main()
