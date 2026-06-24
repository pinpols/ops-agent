"""redaction 直接回归守卫。

背景:此前 redaction 只有经 trace_io 的间接测试,且样本都是"已经能过"的,
导致 YAML 冒号写法的明文口令长期漏脱。本测试用真实泄漏样本驱动,确保改坏正则即变红。
每条样本断言:① 敏感原值不再出现;② 脱敏标记出现(或值被替换)。
"""

import json

import pytest

from ops_agent.redaction import redact, redact_text

# (说明, 原文, 不应再出现的敏感子串)
LEAK_SAMPLES = [
    ("YAML 冒号口令", "spring.datasource.password: mySecretP@ss123", "mySecretP@ss123"),
    ("properties 等号口令", "spring.datasource.password=mySecretP@ss123", "mySecretP@ss123"),
    ("client-secret 冒号", "oauth.client-secret: abcdef123456ZZ", "abcdef123456ZZ"),
    ("token 冒号", "auth.token: eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
    ("api_key 等号", "ANTHROPIC_API_KEY=sk-xyz-secret-000", "sk-xyz-secret-000"),
    (
        "AWS secret key",
        "aws_secret_access_key: wJalrXUtnFEMIK7MDENGbPxRfiCY",
        "wJalrXUtnFEMIK7MDENGbPxRfiCY",
    ),
    ("sk-ant key", "key=sk-ant-api03-AbCdEf012345", "sk-ant-api03-AbCdEf012345"),
    ("github token", "token gho_ABCDEFghijkl0123456789", "gho_ABCDEFghijkl0123456789"),
    ("bearer header", "Authorization: Bearer abc.def.ghi-123", "abc.def.ghi-123"),
    ("postgres dsn 密码", "postgres://batch_user:p%40ss@db:5432/x", "p%40ss"),
    (
        "redis 无 user URL 密码",
        "spring.redis.url: redis://:redisPass1@localhost:6379",
        "redisPass1",
    ),
    ("mysql URL 密码", "jdbc=mysql://root:rootPw99@127.0.0.1:3306/db", "rootPw99"),
    (
        "RSA 私钥块",
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890\n"
        "-----END RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA1234567890",
    ),
    (
        "裸 AWS Access Key ID(无关键词)",
        "auth failed for AKIAIOSFODNN7EXAMPLE",
        "AKIAIOSFODNN7EXAMPLE",
    ),
    (
        "裸 JWT(无关键词)",
        "X-Auth eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U end",
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
    ),
]


@pytest.mark.parametrize("desc,raw,secret", LEAK_SAMPLES, ids=[s[0] for s in LEAK_SAMPLES])
def test_secret_is_redacted(desc, raw, secret):
    out = redact_text(raw)
    assert secret not in out, f"{desc}: 敏感值未脱敏 -> {out!r}"
    assert "***" in out, f"{desc}: 未见脱敏标记 -> {out!r}"


def test_redact_recurses_structures():
    # 注:redact() 对结构化数据按字符串"内容"脱敏(不按 dict key),所以值需自带内联模式。
    payload = {
        "config": "password: topSecret123",
        "rows": ["postgres://u:pw1234@h/db", "ok line"],
        "nested": {"detail": "Authorization: Bearer abc.def.ghi"},
    }
    out = redact(payload)
    flat = repr(out)
    assert "topSecret123" not in flat
    assert "pw1234" not in flat
    assert "abc.def.ghi" not in flat


def test_redact_keeps_nonsensitive_text():
    # 普通文本不应被误伤(确保不是"全删")
    raw = "worker-import 在 02:00 启动,处理了 10000 行,耗时 240s,无错误。"
    assert redact_text(raw) == raw


def test_password_word_without_assignment_not_touched():
    # "password is unknown" 没有 :/= 赋值,不应被改(避免过度脱敏吞正常叙述)
    raw = "the password rotation policy is unclear"
    assert redact_text(raw) == raw


def test_external_redaction_rules_file(tmp_path, monkeypatch):
    rules_file = tmp_path / "redaction-rules.json"
    rules_file.write_text(
        json.dumps(
            [
                {
                    "name": "tenant-ticket",
                    "pattern": "TENANT-[0-9]{6}",
                    "replacement": "TENANT-***",
                },
                {
                    "name": "case-insensitive-field",
                    "pattern": "internal-ref:[A-Z0-9]+",
                    "replacement": "internal-ref:***",
                    "flags": ["ignorecase"],
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPS_REDACTION_RULES_FILE", str(rules_file))

    out = redact_text("tenant TENANT-123456 internal-ref:ABC123")

    assert "TENANT-123456" not in out
    assert "internal-ref:ABC123" not in out
    assert "TENANT-***" in out
    assert "internal-ref:***" in out
