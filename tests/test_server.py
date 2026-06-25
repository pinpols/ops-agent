"""HTTP 触发层单测:Bearer 鉴权(fail-closed)+ /diagnose 业务(只读、降级)。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from ops_agent import server
from ops_agent.budget import BudgetExceeded
from ops_agent.models import Diagnosis, Severity


def _fake_diagnosis() -> Diagnosis:
    return Diagnosis(
        severity=Severity.WARNING,
        summary="s",
        root_cause="r",
        evidence=["e"],
        suggested_action="a",
        confidence=0.5,
    )


class AuthorizeTest(unittest.TestCase):
    def test_no_token_configured_is_fail_closed(self):
        self.assertFalse(server.authorize("Bearer x", None))
        self.assertFalse(server.authorize("Bearer x", ""))

    def test_correct_bearer(self):
        self.assertTrue(server.authorize("Bearer secret123", "secret123"))

    def test_wrong_or_malformed(self):
        self.assertFalse(server.authorize("Bearer nope", "secret123"))
        self.assertFalse(server.authorize("secret123", "secret123"))  # 缺 Bearer 前缀
        self.assertFalse(server.authorize(None, "secret123"))


class HandleDiagnoseTest(unittest.TestCase):
    def test_missing_question_400(self):
        status, body = server.handle_diagnose({})
        self.assertEqual(status, 400)
        self.assertIn("question", body["error"])

    def test_success_returns_diagnosis_and_is_read_only(self):
        captured = {}

        def fake_run(q, approver=None):
            captured["q"] = q
            captured["approver"] = approver
            return _fake_diagnosis(), []

        with patch("ops_agent.agent.run_agent", side_effect=fake_run):
            status, body = server.handle_diagnose({"question": "为什么慢", "target": "fbs"})
        self.assertEqual(status, 200)
        self.assertEqual(body["diagnosis"]["severity"], "WARNING")
        self.assertIn("[target=fbs]", captured["q"])
        # 只读保证:注入的审批闸对任何危险工具都返回 False
        self.assertIs(captured["approver"], server._deny_all_approver)
        self.assertFalse(server._deny_all_approver("restart_service", {}))

    def test_budget_exceeded_returns_503(self):
        with patch("ops_agent.agent.run_agent", side_effect=BudgetExceeded("token 预算耗尽")):
            status, body = server.handle_diagnose({"question": "x"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "budget_exceeded")

    def test_internal_error_returns_500(self):
        with patch("ops_agent.agent.run_agent", side_effect=RuntimeError("boom")):
            status, body = server.handle_diagnose({"question": "x"})
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "internal_error")


class HttpEndToEndTest(unittest.TestCase):
    """真起一个 HTTP server,验证路由/鉴权/健康探针端到端通(不打 LLM)。"""

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read().decode("utf-8")

    def test_healthz_ok(self):
        status, body = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_metrics_text(self):
        status, body = self._get("/metrics")
        self.assertEqual(status, 200)

    def _post(self, body: bytes, token: str | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/diagnose", data=body, method="POST", headers=headers
        )
        return urllib.request.urlopen(req, timeout=5)

    def test_diagnose_unauthorized_when_no_token(self):
        # server 未设 expected_token(setUp 直接构造)→ fail-closed,任何请求 401
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post(b'{"question":"x"}', token="anything")
        self.assertEqual(ctx.exception.code, 401)

    def test_diagnose_authorized_read_only_path(self):
        self.httpd.expected_token = "secret"  # 模拟 serve() 启动快照
        diag = _fake_diagnosis()
        with patch("ops_agent.agent.run_agent", return_value=(diag, [])):
            resp = self._post(b'{"question":"\xe6\x85\xa2"}', token="secret")
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read())["diagnosis"]["severity"], "WARNING")


class AsyncDiagnoseHttpTest(unittest.TestCase):
    """事件驱动 Step 1:/diagnose 入队 → 202 + job_id,/jobs/{id} 轮询到结果。"""

    def setUp(self):
        from ops_agent.jobqueue import JobQueue

        # 假 handler:不打 LLM,直接回结果(验证 HTTP 异步管道:202 + 入队 + 查询)
        self.jq = JobQueue(lambda job: {"diagnosis": {"severity": job.question}}, workers=1)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
        self.httpd.expected_token = "tok"
        self.httpd.job_queue = self.jq
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.jq.shutdown()

    def test_async_returns_202_then_pollable_to_succeeded(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/diagnose",
            data=b'{"question":"WARNING","target":"fbs"}',
            method="POST",
            headers={"Authorization": "Bearer tok"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        self.assertEqual(resp.status, 202)
        job_id = json.loads(resp.read())["job_id"]

        # 轮询 /jobs/{id} 直到 succeeded
        import time

        deadline = time.time() + 3
        final = None
        while time.time() < deadline:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/jobs/{job_id}", timeout=5
            ) as r:
                final = json.loads(r.read())
            if final["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.02)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(final["result"]["diagnosis"]["severity"], "WARNING")

    def test_unknown_job_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/jobs/nope", timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
