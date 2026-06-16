"""诊断结果的结构化 schema。

学习点:每个 Field 的 description 不是给人看的注释——结构化输出 / function calling 时,
这些 description 会进 JSON Schema 一起发给模型,**直接影响模型填得准不准**。写清楚 = 输出质量高。
"""

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """严重级别。用枚举 = 约束模型只能从固定集合里选,避免它自由发挥乱写。"""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Diagnosis(BaseModel):
    """对一段运维日志的诊断结论。"""

    severity: Severity = Field(description="整体严重级别:INFO 正常/可忽略,WARNING 需关注,CRITICAL 影响可用性")
    summary: str = Field(description="一句话结论:这段日志反映的核心问题(没问题就说'未发现异常')")
    root_cause: str = Field(description="推断的根因;证据不足时明说'证据不足,需进一步查 X',不要硬编")
    evidence: list[str] = Field(
        default_factory=list,
        description="支撑结论的具体日志证据(引用关键行/关键词),每条一项;不要编造日志里没有的内容",
    )
    suggested_action: str = Field(description="下一步建议动作(查什么 / 改什么);只读诊断阶段不要建议危险操作")
    confidence: float = Field(
        ge=0.0, le=1.0, description="对该诊断的置信度 0~1;证据弱就给低分,别一律 0.9"
    )
