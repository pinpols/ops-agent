"""两种打分:确定性(免费/可复现)+ LLM-as-judge(评语义)。"""

from pydantic import BaseModel, Field

from evals.cases import Case
from ops_agent.config import get_settings
from ops_agent.models import Diagnosis, Severity


def deterministic_score(d: Diagnosis, case: Case) -> dict:
    """代码断言:severity 是否对、关键词召回、反例不误报。不打 LLM。"""
    haystack = " ".join([d.summary, d.root_cause, *d.evidence]).lower()
    hits = [k for k in case.expected_keywords if k.lower() in haystack]
    recall = (len(hits) / len(case.expected_keywords)) if case.expected_keywords else 1.0

    severity_ok = d.severity == case.expected_severity
    # 反例:正常日志不许报 CRITICAL(防草木皆兵)
    normal_ok = (d.severity != Severity.CRITICAL) if case.is_normal else True

    passed = severity_ok and recall >= 1.0 and normal_ok
    return {
        "passed": passed,
        "severity_ok": severity_ok,
        "keyword_recall": round(recall, 2),
        "missed_keywords": [k for k in case.expected_keywords if k not in hits],
        "normal_ok": normal_ok,
    }


class JudgeVerdict(BaseModel):
    """LLM 评委的结构化判定。"""

    correct: bool = Field(description="该诊断对这段日志而言是否基本正确(根因方向对、无明显幻觉)")
    score: float = Field(
        ge=0.0, le=1.0, description="质量分 0~1:根因准确度+证据相关性+建议有用性综合"
    )
    reasoning: str = Field(description="给分理由,指出对在哪/错在哪(一两句)")


_JUDGE_SYSTEM = (
    "你是严格的诊断评委。依据【日志】判断【诊断结论】是否正确,按 rubric 打分:"
    "(1) 根因方向是否对(最重要);(2) 证据是否真出自日志、无幻觉;(3) severity 是否合理;"
    "(4) 建议是否有用。根因错或有幻觉 → correct=false 且低分。只夸不指错的诊断不给高分。"
    "通过 submit_verdict 返回。"
)


def llm_judge(d: Diagnosis, case: Case) -> JudgeVerdict:
    """另一个 LLM 读 日志+实际诊断,给质量分。需 ANTHROPIC_API_KEY。"""
    from ops_agent.llm import make_client

    client = make_client()
    model = get_settings().anthropic_judge_model
    tool = {
        "name": "submit_verdict",
        "description": "提交对诊断的评判。",
        "input_schema": JudgeVerdict.model_json_schema(),
    }
    content = (
        f"【日志】\n{case.log_text}\n\n"
        f"【诊断结论】\n{d.model_dump_json()}\n\n"
        "评判该结论,通过 submit_verdict 返回。"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        system=_JUDGE_SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_verdict"},
        messages=[{"role": "user", "content": content}],
    )
    verdict = next((b.input for b in resp.content if b.type == "tool_use"), None)
    if verdict is None:
        raise RuntimeError("judge 未返回 verdict")
    return JudgeVerdict.model_validate(verdict)
