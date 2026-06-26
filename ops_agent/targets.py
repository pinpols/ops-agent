"""多目标注册表 —— 把"被诊断的系统"从写死的 ../file-batch-system 抽象成具名 target。

生产里 ops-agent 要诊断多个系统;每个 target = {root, log_dir, pg_dsn, metrics_url}。
从 TOML 注册表(OPS_TARGETS_FILE,默认 ./targets.toml)加载;未配注册表时 resolve(None)
回退到 config.Settings 的单目标 env(完全向后兼容,旧用法不受影响)。

targets.toml 示例:
    [targets.file-batch-system]
    root = "../file-batch-system"
    log_dir = "../file-batch-system/logs"
    pg_dsn = "postgresql://ro@localhost:5432/batch"   # 只读用户
    metrics_url = "http://localhost:9090"
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ops_agent.config import get_settings


@dataclass(frozen=True)
class Target:
    name: str
    root: Path | None
    log_dir: Path
    pg_dsn: str | None = None
    metrics_url: str | None = None
    flink_url: str | None = None  # Flink JobManager REST base(只读诊断,GET-only)


def _targets_file() -> Path:
    return Path(os.environ.get("OPS_TARGETS_FILE", "targets.toml")).resolve()


def load_targets() -> dict[str, Target]:
    """读 TOML 注册表;文件不存在则空 dict(回退单目标 env)。"""
    path = _targets_file()
    if not path.exists():
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    out: dict[str, Target] = {}
    for name, cfg in (data.get("targets") or {}).items():
        if "log_dir" not in cfg:
            raise ValueError(f"target {name!r} 缺 log_dir")
        metrics_url = cfg.get("metrics_url")
        flink_url = cfg.get("flink_url")
        # 配置期就拒非 http(s) URL(防 file://、ftp:// 经只读 REST 工具触发 SSRF/本地读)。
        for label, value in (("metrics_url", metrics_url), ("flink_url", flink_url)):
            if value and not str(value).startswith(("http://", "https://")):
                raise ValueError(f"target {name!r} 的 {label} 必须是 http(s):{value!r}")
        out[name] = Target(
            name=name,
            root=Path(cfg["root"]).resolve() if cfg.get("root") else None,
            log_dir=Path(cfg["log_dir"]).resolve(),
            pg_dsn=cfg.get("pg_dsn"),
            metrics_url=metrics_url,
            flink_url=flink_url,
        )
    return out


def _default_target() -> Target:
    """无注册表时的隐式默认 target = 当前 Settings 的单目标 env(向后兼容)。"""
    s = get_settings()
    return Target(
        name="default",
        root=s.ops_target_root,
        log_dir=s.ops_log_dir,
        pg_dsn=s.ops_pg_dsn,
        metrics_url=os.environ.get("OPS_METRICS_URL"),
        flink_url=os.environ.get("OPS_FLINK_URL"),
    )


def resolve_target(name: str | None) -> Target:
    """按名解析 target;name 为空 → 默认。注册表里查无此名 → 明确报错(列出可选项)。"""
    if not name:
        return _default_target()
    registry = load_targets()
    if name in registry:
        return registry[name]
    # 注册表为空但用户传了名:容错回退默认(单目标用户用 --target 指自己也能跑)
    if not registry:
        return _default_target()
    raise ValueError(f"未知 target {name!r};已注册:{sorted(registry)}")
