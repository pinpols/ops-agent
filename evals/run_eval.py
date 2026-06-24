"""跑全集 → scorecard。改 prompt / 换模型后重跑,对比分数=量化回归(不靠感觉)。

python -m evals.run_eval                      # 仅确定性打分
python -m evals.run_eval --judge              # + LLM-as-judge
python -m evals.run_eval --save base.json     # 把本次结果存为基线
python -m evals.run_eval --baseline base.json # 与基线对比,显示每条/总分的升降
"""

import argparse
import json
import sys

from dotenv import load_dotenv

from evals.cases import CASES
from evals.scorers import deterministic_score, llm_judge
from ops_agent.diagnose import diagnose_log  # 已 @observe:配了 Langfuse 则每条进 trace


def run(use_judge: bool) -> dict:
    results = {}
    for c in CASES:
        d = diagnose_log(c.log_text)
        det = deterministic_score(d, c)
        entry = {
            "passed": det["passed"],
            "severity": d.severity.value,
            "keyword_recall": det["keyword_recall"],
            "missed_keywords": det["missed_keywords"],
            "normal_ok": det["normal_ok"],
        }
        if use_judge:
            v = llm_judge(d, c)
            entry["judge_score"] = v.score
            entry["judge_correct"] = v.correct
            entry["judge_reasoning"] = v.reasoning
        results[c.id] = entry
    return results


def _aggregate(results: dict) -> dict:
    n = len(results)
    passed = sum(1 for r in results.values() if r["passed"])
    js = [r["judge_score"] for r in results.values() if "judge_score" in r]
    return {
        "pass_rate": round(passed / n, 3) if n else 0.0,
        "passed": passed,
        "total": n,
        "judge_avg": round(sum(js) / len(js), 3) if js else None,
    }


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true", help="启用 LLM-as-judge")
    ap.add_argument("--save", metavar="FILE", help="把本次结果存为基线 JSON")
    ap.add_argument("--baseline", metavar="FILE", help="与基线对比升降")
    ap.add_argument(
        "--fail-under",
        type=float,
        metavar="RATE",
        help="确定性通过率低于该阈值(0~1)则退出码 2 —— 供 CI 当硬闸卡回归",
    )
    ap.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="配合 --baseline:任一 case PASS→FAIL 则退出码 3",
    )
    args = ap.parse_args()

    results = run(args.judge)
    agg = _aggregate(results)
    regressed: list[str] = []

    print(f"\n跑 {agg['total']} 条 (judge={'on' if args.judge else 'off'})\n")
    for cid, r in results.items():
        status = "PASS" if r["passed"] else "FAIL"
        line = f"[{status}] {cid:18} severity={r['severity']} 召回={r['keyword_recall']}"
        if r["missed_keywords"]:
            line += f" 漏词={r['missed_keywords']}"
        if not r["normal_ok"]:
            line += " [反例误报CRITICAL!]"
        if "judge_score" in r:
            line += f" | judge={r['judge_score']:.2f}{'✓' if r['judge_correct'] else '✗'}"
        print(line)

    print("\n──── scorecard ────")
    print(f"确定性通过率: {agg['passed']}/{agg['total']} = {agg['pass_rate']:.0%}")
    if agg["judge_avg"] is not None:
        print(f"judge 平均分:  {agg['judge_avg']:.2f}")

    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            base = json.load(f)
        base_agg = _aggregate(base)
        print("\n──── vs 基线 ────")
        print(f"通过率: {base_agg['pass_rate']:.0%} → {agg['pass_rate']:.0%}")
        for cid, r in results.items():
            was = base.get(cid, {}).get("passed")
            now = r["passed"]
            if was is not None and was != now:
                if was:
                    regressed.append(cid)
                print(f"  {cid}: {'PASS→FAIL ⚠️ 回归!' if was else 'FAIL→PASS ✅'}")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n已存基线 → {args.save}")

    # CI 硬闸:阈值/回归不达标 → 非零退出(让 pipeline 红)。
    if args.fail_on_regression and regressed:
        print(f"\n❌ 回归闸:{len(regressed)} 条 PASS→FAIL:{regressed}", file=sys.stderr)
        raise SystemExit(3)
    if args.fail_under is not None and agg["pass_rate"] < args.fail_under:
        print(
            f"\n❌ 通过率闸:{agg['pass_rate']:.0%} < 阈值 {args.fail_under:.0%}",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
