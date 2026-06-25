#!/usr/bin/env python3
"""ops-agent 基础负载发生器(stdlib,零依赖)。

并发向 `serve` 的 /diagnose POST 告警 webhook,统计 202(入队)/429(背压)/其他、
端到端延迟分位,以及可选轮询 /jobs/{id} 到终态测全链路时延。用于:
- 容量摸底:多少 QPS 开始触发背压(429)。
- 回归:改动后吞吐/延迟是否退化。

示例:
    # 200 个请求,并发 20,打本地 serve
    python scripts/loadtest.py --url http://127.0.0.1:8080 --token "$OPS_WEBHOOK_TOKEN" \
        --total 200 --concurrency 20

    # 加 --poll:每个 202 再轮询 /jobs/{id} 到 succeeded/failed,测全链路
    python scripts/loadtest.py --url ... --token ... --total 50 --concurrency 10 --poll

注意:这是只读诊断,worker 会真打 LLM。压测请指向测试环境 / 用假 gateway,别打爆真模型额度。
"""

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def _post_diagnose(url: str, token: str, payload: dict, timeout: float) -> tuple[int, dict, float]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/diagnose",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), time.monotonic() - start
    except urllib.error.HTTPError as e:
        return e.code, {}, time.monotonic() - start
    except Exception as e:  # noqa: BLE001 - 负载工具:任何错误记为 0 码,不中断整轮
        return 0, {"error": str(e)}, time.monotonic() - start


def _poll_job(url: str, token: str, job_id: str, timeout: float, deadline_s: float) -> str:
    end = time.monotonic() + deadline_s
    req = urllib.request.Request(
        f"{url}/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"}
    )
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = json.loads(r.read()).get("status")
                if status in ("succeeded", "failed"):
                    return status
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return "timeout"


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(p / 100 * len(s)))
    return s[idx]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ops-agent /diagnose 负载发生器")
    ap.add_argument("--url", default="http://127.0.0.1:8080", help="serve 基础地址")
    ap.add_argument("--token", required=True, help="OPS_WEBHOOK_TOKEN")
    ap.add_argument("--total", type=int, default=100, help="总请求数")
    ap.add_argument("--concurrency", type=int, default=10, help="并发数")
    ap.add_argument("--question", default="为什么服务变慢", help="诊断问题")
    ap.add_argument("--target", default="fbs", help="目标系统名")
    ap.add_argument("--timeout", type=float, default=15.0, help="单请求超时秒")
    ap.add_argument("--poll", action="store_true", help="对 202 轮询 /jobs 到终态测全链路")
    ap.add_argument("--poll-deadline", type=float, default=120.0, help="单任务轮询上限秒")
    args = ap.parse_args(argv)

    codes: dict[int, int] = {}
    submit_latencies: list[float] = []
    e2e: list[tuple[str, float]] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        payload = {"question": args.question, "target": args.target, "trace_id": f"load-{i}"}
        code, body, dt = _post_diagnose(args.url, args.token, payload, args.timeout)
        with lock:
            codes[code] = codes.get(code, 0) + 1
            submit_latencies.append(dt)
        if args.poll and code == 202 and body.get("job_id"):
            t0 = time.monotonic()
            status = _poll_job(
                args.url, args.token, body["job_id"], args.timeout, args.poll_deadline
            )
            with lock:
                e2e.append((status, time.monotonic() - t0))

    wall_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(one, range(args.total)))
    wall = time.monotonic() - wall_start

    print(f"\n== 负载结果:{args.total} 请求 / 并发 {args.concurrency} / 墙钟 {wall:.2f}s ==")
    print(f"吞吐(提交): {args.total / wall:.1f} req/s")
    print("HTTP 码分布:")
    for code in sorted(codes):
        label = {202: "入队", 429: "背压", 401: "鉴权失败", 0: "连接错误"}.get(code, "")
        print(f"  {code} {label}: {codes[code]}")
    if submit_latencies:
        print(
            f"提交延迟(s): p50={_pct(submit_latencies, 50):.3f} "
            f"p95={_pct(submit_latencies, 95):.3f} "
            f"max={max(submit_latencies):.3f} "
            f"avg={statistics.mean(submit_latencies):.3f}"
        )
    if args.poll and e2e:
        ok = [d for s, d in e2e if s == "succeeded"]
        statuses: dict[str, int] = {}
        for s, _ in e2e:
            statuses[s] = statuses.get(s, 0) + 1
        print(f"全链路终态: {statuses}")
        if ok:
            print(f"全链路时延(succeeded,s): p50={_pct(ok, 50):.2f} p95={_pct(ok, 95):.2f}")

    # 退出码:有连接错误或全是非 2xx 视为失败,便于 CI/脚本判定
    accepted = codes.get(202, 0)
    return 0 if accepted > 0 and codes.get(0, 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
