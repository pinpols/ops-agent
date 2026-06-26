"""阶段 3:多步 agent —— 循环 + 多工具(read_logs / query_pg)+ 记忆(多轮)。

模型自己决定调几次、调哪些工具,直到给出结论;我们逐步执行并喂回。手写循环=完全透明,
之后(阶段 3b)再 port 到 LangGraph 体会框架价值。概念见 docs/phase3-concepts.md。

交互式多轮:  python -m ops_agent.agent
单次:        python -m ops_agent.agent "sim 跑批为什么慢?"
"""

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

from ops_agent.audit import append_approval_record, append_execution_record
from ops_agent.budget import BudgetExceeded, RunBudget
from ops_agent.config import Settings, get_settings
from ops_agent.diagnose import _TOOL_NAME as REPORT_TOOL_NAME
from ops_agent.diagnose import _build_tool as build_report_tool
from ops_agent.exec_tools import DANGEROUS_TOOLS, EXEC_TOOL_RESULT_IMPLS, RESTART_TOOL
from ops_agent.llm import make_client
from ops_agent.metrics import METRICS
from ops_agent.metrics_tools import QUERY_METRICS_TOOL, QUERY_METRICS_TOOL_RESULT_IMPLS
from ops_agent.models import Diagnosis
from ops_agent.obs import observe
from ops_agent.prompts import AGENT_SYSTEM as _SYSTEM_PROMPT
from ops_agent.prompts import PROMPT_VERSION, fence_untrusted
from ops_agent.redaction import redact_text
from ops_agent.system_tools import SYSTEM_TOOL_RESULT_IMPLS, SYSTEM_TOOLS
from ops_agent.tool_result import ToolResult
from ops_agent.tools import QUERY_PG_TEMPLATE_TOOL, QUERY_PG_TOOL, READ_LOGS_TOOL, TOOL_RESULT_IMPLS
from ops_agent.trace_io import write_agent_trace

# 所有工具实现的派发表(只读 + 执行 + 指标)
_ALL_IMPLS = {
    **SYSTEM_TOOL_RESULT_IMPLS,
    **TOOL_RESULT_IMPLS,
    **QUERY_METRICS_TOOL_RESULT_IMPLS,
    **EXEC_TOOL_RESULT_IMPLS,
}


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


def _safe_tool_input(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {"_raw": value}


@observe
def run_agent(
    question: str,
    history: list[dict] | None = None,
    *,
    max_steps: int = 8,
    max_tokens: int = 4096,
    approver: Callable[[str, dict], bool] | None = None,
    include_trace: bool = False,
    budget: RunBudget | None = None,
    trace_id: str | None = None,
) -> tuple[Diagnosis, list[dict]] | tuple[Diagnosis, list[dict], list[AgentStepTrace]]:
    """跑一轮多步诊断。返回 (结论, 更新后的 messages)。把 messages 传回即可多轮追问。

    approver:危险工具(DANGEROUS_TOOLS)执行前的审批闸,返回 True 才执行;
    默认命令行 y/N。测试/自动化可注入。
    budget:墙钟/token 预算闸;默认取 Settings(OPS_MAX_RUN_SECONDS/TOKENS)。越界抛 BudgetExceeded。
    """
    client = make_client()
    settings = get_settings()
    model = settings.anthropic_model
    approve = approver or _console_approver
    run_budget = budget or RunBudget(
        max_seconds=settings.ops_max_run_seconds,
        max_total_tokens=settings.ops_max_run_tokens,
    )
    METRICS.inc("diagnose_started_total")
    # 在最后一个稳定工具上打 ephemeral 缓存断点:tools→system 这段固定前缀在多步循环里
    # 跨轮重发,命中缓存可大幅省输入 token(工具列表确定且有序,前缀稳定)。
    report_tool = {**build_report_tool(), "cache_control": {"type": "ephemeral"}}
    tools = [
        *SYSTEM_TOOLS,
        READ_LOGS_TOOL,
        QUERY_METRICS_TOOL,
        QUERY_PG_TEMPLATE_TOOL,
        QUERY_PG_TOOL,
        RESTART_TOOL,
        report_tool,
    ]

    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": question})
    trace: list[AgentStepTrace] = []
    total_in = 0  # 跨步累计 token(成本观测:看哪步贵)
    total_out = 0

    for step in range(1, max_steps + 1):
        # 预算闸:墙钟/token 越界即抛 BudgetExceeded(由调用方降级展示),防绕圈烧钱。
        try:
            run_budget.check(total_in + total_out)
        except BudgetExceeded:
            METRICS.inc("diagnose_budget_exceeded_total")
            _flush_metrics(settings)
            raise
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        # max_tokens 截断 → 本轮 tool_use/结论可能不完整,继续会喂回部分块或撞 ValidationError。
        # 显式识别并给可操作报错,而不是静默绕圈。
        if resp.stop_reason == "max_tokens":
            raise RuntimeError(
                f"LLM 响应被 max_tokens={max_tokens} 截断(stop_reason=max_tokens),结果可能不完整;"
                "请调高 max_tokens 或缩小工具输出(如 read_logs 的 max_lines)。"
            )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            total_in += in_tok
            total_out += out_tok
            # 每次调用即记累计 token,而非仅成功路径 —— 否则预算击杀/截断/max_steps 这些
            # 最烧钱的失控 run 完全不计入花费看板(审计发现的成本盲区)。
            METRICS.inc("llm_input_tokens_total", in_tok)
            METRICS.inc("llm_output_tokens_total", out_tok)
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            # 模型只讲话没调工具:逼它用 report_diagnosis 收口
            messages.append({"role": "user", "content": "请用 report_diagnosis 给出结构化结论。"})
            continue

        results = []
        diagnosis: Diagnosis | None = None  # 本轮是否产出合规结论
        report_pending = False  # 本轮调了 report 但结构不合规,需回喂重报
        for tu in tool_uses:
            tool_input = _safe_tool_input(tu.input)
            if tu.name == REPORT_TOOL_NAME:
                try:
                    diagnosis = Diagnosis.model_validate(tool_input)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "ok"})
                except ValidationError as e:
                    # 结论结构不合规(越界/截断):不崩,回喂让模型修正后重报。
                    report_pending = True
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": f"诊断结构不合规,请修正后用 report_diagnosis 重报:{e}",
                            "is_error": True,
                        }
                    )
                continue
            # 本轮已下结论(或结论待修正)→ 不再执行后续工具,但仍回 tool_result 保持消息合法
            # (否则遗留 tool_use 无对应 tool_result,下一轮 messages.create 会 400)。
            if diagnosis is not None or report_pending:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": "本轮已提交诊断结论,跳过该工具调用。",
                    }
                )
                continue
            # 危险工具:执行前过审批闸(HITL)
            approved: bool | None = None
            if tu.name in DANGEROUS_TOOLS:
                approved = approve(tu.name, tool_input)
                append_approval_record(
                    settings.ops_approval_log,
                    tool_name=tu.name,
                    tool_input=tool_input,
                    approved=approved,
                )
            if approved is False:
                result = ToolResult.failure(
                    f"[审批] 用户拒绝执行 {tu.name}({tool_input}),未执行。",
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
                        result = impl(**tool_input)
                    except Exception as e:  # noqa: BLE001 - tool boundary
                        result = ToolResult.failure(
                            f"[{tu.name}] 工具异常:{type(e).__name__}: {e}",
                            error_type="tool_exception",
                        )
            # 危险动作批准后留执行结果痕(成败 / dry-run),补"只记批没批"的审计盲区
            if tu.name in DANGEROUS_TOOLS and approved:
                append_execution_record(
                    settings.ops_approval_log,
                    tool_name=tu.name,
                    ok=result.ok,
                    dry_run=bool(result.metadata.get("dry_run", False)),
                    detail=result.error,
                )
            METRICS.inc("tool_calls_total", tool=tu.name, ok=str(result.ok).lower())
            # 工具输出在喂回 LLM(出网到 Anthropic)+ 落 trace 前统一脱敏,
            # 防 Spring 配置 / SQL 结果 / 日志里的明文凭据外泄。
            output = result.to_text()
            if settings.ops_redact_artifacts:
                output = redact_text(output)
            trace.append(
                AgentStepTrace(
                    step=step,
                    tool_name=tu.name,
                    tool_input=tool_input,
                    ok=result.ok,
                    output=output,
                    error=result.error,
                    approved=approved,
                    metadata=result.metadata,
                )
            )
            # 喂回 LLM 前包进不可信围栏:结构上把"工具数据"与"指令"隔开,纵深防 prompt 注入。
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": fence_untrusted(str(output)),
                }
            )
        # 每轮只 append 一次(修正旧实现 report 分支 + 循环尾的双 append)。
        messages.append({"role": "user", "content": results})
        if diagnosis is not None:
            METRICS.inc("diagnose_succeeded_total")  # token 计量已在每步累加,不在此重复 inc
            _flush_metrics(settings)
            if settings.ops_trace_dir:
                write_agent_trace(
                    settings.ops_trace_dir,
                    question=question,
                    model=model,
                    diagnosis=diagnosis,
                    steps=trace,
                    usage={"input_tokens": total_in, "output_tokens": total_out},
                    prompt_version=PROMPT_VERSION,
                    trace_id=trace_id,
                )
            _record_history(settings, question, model, diagnosis, total_in, total_out, trace_id)
            if include_trace:
                return diagnosis, messages, trace
            return diagnosis, messages

    METRICS.inc("diagnose_max_steps_total")
    _flush_metrics(settings)
    raise RuntimeError(f"达到 max_steps={max_steps} 仍未得出结论(可能在绕圈,检查工具/prompt)")


def _flush_metrics(settings: Settings) -> None:
    """配了 OPS_METRICS_FILE 就把累计指标原子写成 Prometheus textfile(否则只留进程内)。"""
    if settings.ops_metrics_file:
        METRICS.write_textfile(settings.ops_metrics_file)


_TARGET_PREFIX_RE = re.compile(r"^\s*\[target=([^\]]+)\]")


def _extract_target(question: str) -> str | None:
    """从 server 注入的 `[target=X] ...` 前缀解析 target,供历史库按 target 过滤。"""
    m = _TARGET_PREFIX_RE.match(question)
    return m.group(1).strip() if m else None


def _record_history(
    settings: Settings,
    question: str,
    model: str,
    diagnosis: Diagnosis,
    total_in: int,
    total_out: int,
    trace_id: str | None,
) -> None:
    """配了 OPS_HISTORY_DB 就把本次诊断落历史库(可查询 + 留存);未配=no-op,零回归。

    落库失败绝不冒泡打断诊断主链路(历史是旁路观测,坏了不该影响出结论)。
    """
    if not settings.ops_history_db:
        return
    try:
        from ops_agent.history import DiagnosisRun, DiagnosisStore

        store = DiagnosisStore(settings.ops_history_db)
        try:
            store.record(
                DiagnosisRun(
                    severity=diagnosis.severity.value,
                    summary=diagnosis.summary,
                    root_cause=diagnosis.root_cause,
                    confidence=diagnosis.confidence,
                    question=question,
                    target=_extract_target(question),  # 否则 history --target 永远查不到(列恒 NULL)
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    trace_id=trace_id,
                    input_tokens=total_in,
                    output_tokens=total_out,
                )
            )
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - 历史落库是旁路,失败只 warn 不打断诊断
        import logging

        logging.getLogger("ops_agent.history").warning("诊断历史落库失败(忽略): %s", exc)


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
