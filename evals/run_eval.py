"""跑全集 → scorecard。确定性打分默认开;LLM-judge 加 --judge(要 API 调用)。

  python -m evals.run_eval            # 仅确定性
  python -m evals.run_eval --judge    # + LLM-as-judge
"""

import sys

from dotenv import load_dotenv

from evals.cases import CASES
from evals.scorers import deterministic_score, llm_judge
from ops_agent.diagnose import diagnose_log  # 已 @observe:配了 Langfuse 则每条进 trace


def main() -> None:
    load_dotenv()
    use_judge = "--judge" in sys.argv

    passed = 0
    judge_scores: list[float] = []
    print(f"跑 {len(CASES)} 条用例  (judge={'on' if use_judge else 'off'})\n")

    for c in CASES:
        d = diagnose_log(c.log_text)
        det = deterministic_score(d, c)
        passed += int(det["passed"])
        line = (
            f"[{'PASS' if det['passed'] else 'FAIL'}] {c.id:18} "
            f"severity={d.severity.value}(期望 {c.expected_severity.value}) "
            f"召回={det['keyword_recall']} "
        )
        if det["missed_keywords"]:
            line += f"漏词={det['missed_keywords']} "
        if not det["normal_ok"]:
            line += "[反例误报CRITICAL!] "
        if use_judge:
            v = llm_judge(d, c)
            judge_scores.append(v.score)
            line += f"| judge={v.score:.2f}{'✓' if v.correct else '✗'} {v.reasoning[:40]}"
        print(line)

    print("\n──── scorecard ────")
    print(f"确定性通过率: {passed}/{len(CASES)} = {passed / len(CASES):.0%}")
    if judge_scores:
        print(f"judge 平均分:  {sum(judge_scores) / len(judge_scores):.2f}")


if __name__ == "__main__":
    main()
