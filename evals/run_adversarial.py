"""跑对抗注入样本(真模型,本地)。打真 diagnose_log,统计抗注入 pass rate。

  set -a; . ./.env; set +a
  python -m evals.run_adversarial

CI 无 key → 跳过(同 run_eval)。这是行为抗性的本地证据,
不替代 test_prompt_injection 的结构闸 CI 测试。
"""

import sys

from dotenv import load_dotenv

from evals.adversarial import CASES, score_adversarial
from ops_agent.diagnose import diagnose_log


def main() -> int:
    load_dotenv()
    passed = 0
    rows = []
    for c in CASES:
        try:
            d = diagnose_log(c.log_text)
            r = score_adversarial(d, c)
        except Exception as e:  # noqa: BLE001 - 单条异常不中断整轮
            r = {"passed": False, "got_severity": f"ERROR:{type(e).__name__}", "leaks": [str(e)]}
        rows.append((c, r))
        passed += 1 if r["passed"] else 0
        tag = "PASS" if r["passed"] else "FAIL"
        detail = ""
        if not r.get("severity_held", True):
            detail += f" 被翻转→{r['got_severity']}(应 {c.true_severity.value})"
        if r.get("leaks"):
            detail += f" 泄露={r['leaks']}"
        print(f"[{tag}] {c.id:30s} attack={c.attack}{detail}")

    n = len(CASES)
    rate = round(100 * passed / n)
    print(f"\n──── 对抗注入 scorecard ────\n抗注入通过率: {passed}/{n} = {rate}%")
    # 任何一条被注入攻破(翻转或泄露)都退非零码,便于当门禁
    return 0 if passed == n else 1


if __name__ == "__main__":
    sys.exit(main())
