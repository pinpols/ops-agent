"""Command line entry point for ops-agent."""

import argparse
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from ops_agent.config import get_settings

log = logging.getLogger("ops_agent")


def _configure_logging(verbose: bool) -> None:
    """日志默认 WARNING(可经 OPS_LOG_LEVEL 调),-v 则 DEBUG。

    日志统一走 stderr,带时间戳/级别;stdout 留给机器可读的诊断 JSON,二者不混。
    """
    level_name = "DEBUG" if verbose else os.environ.get("OPS_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _resolve_target(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.exists():
        return candidate.resolve()
    sibling = Path.cwd().parent / value
    if sibling.exists():
        return sibling.resolve()
    return candidate.resolve()


def _apply_target_arg(args: argparse.Namespace) -> None:
    target = getattr(args, "target", None)
    if target:
        os.environ["OPS_TARGET_ROOT"] = str(_resolve_target(target))


def _require_api_key() -> None:
    if not get_settings().anthropic_api_key:
        print("缺 ANTHROPIC_API_KEY,先 cp .env.example .env 并填 key", file=sys.stderr)
        raise SystemExit(2)


def _cmd_diagnose(args: argparse.Namespace) -> None:
    from ops_agent.diagnose import diagnose_log

    load_dotenv()
    _apply_target_arg(args)
    _require_api_key()
    log_text = Path(args.log_file).read_text(encoding="utf-8")
    print(diagnose_log(log_text).model_dump_json(indent=2))


def _cmd_investigate(args: argparse.Namespace) -> None:
    from ops_agent.investigate import investigate

    load_dotenv()
    _apply_target_arg(args)
    _require_api_key()
    print(investigate(args.question).model_dump_json(indent=2))


def _cmd_chat(args: argparse.Namespace) -> None:
    from ops_agent.agent import main as agent_main

    load_dotenv()
    _apply_target_arg(args)
    _require_api_key()
    sys.argv = [sys.argv[0], *args.question]
    agent_main()


def _cmd_graph(args: argparse.Namespace) -> None:
    from ops_agent.graph_agent import main as graph_main

    load_dotenv()
    _apply_target_arg(args)
    _require_api_key()
    sys.argv = [sys.argv[0], *args.question]
    graph_main()


def _cmd_eval(args: argparse.Namespace) -> None:
    from evals.run_eval import main as eval_main

    load_dotenv()
    sys.argv = [sys.argv[0]]
    if args.judge:
        sys.argv.append("--judge")
    if args.save:
        sys.argv.extend(["--save", args.save])
    if args.baseline:
        sys.argv.extend(["--baseline", args.baseline])
    eval_main()


def _cmd_bundle(args: argparse.Namespace) -> None:
    from ops_agent.bundle import create_bundle

    load_dotenv()
    _apply_target_arg(args)
    _require_api_key()
    bundle_dir = create_bundle(args.question)
    print(bundle_dir)


def _cmd_services(args: argparse.Namespace) -> None:
    from ops_agent.system_tools import list_services

    _apply_target_arg(args)
    print(list_services())


def _cmd_errors(args: argparse.Namespace) -> None:
    from ops_agent.system_tools import tail_recent_errors

    _apply_target_arg(args)
    print(tail_recent_errors(args.max_lines))


def _cmd_compose(args: argparse.Namespace) -> None:
    from ops_agent.system_tools import inspect_compose

    _apply_target_arg(args)
    print(inspect_compose(args.max_chars))


def _cmd_app_config(args: argparse.Namespace) -> None:
    from ops_agent.system_tools import read_app_config

    _apply_target_arg(args)
    print(read_app_config(args.service, args.max_chars))


def _cmd_doctor(args: argparse.Namespace) -> None:
    _apply_target_arg(args)
    settings = get_settings()
    compose_count = (
        len(list(settings.ops_target_root.glob("docker-compose*.yml")))
        if settings.ops_target_root and settings.ops_target_root.exists()
        else 0
    )
    module_count = (
        len(list(settings.ops_target_root.glob("batch-*")))
        if settings.ops_target_root and settings.ops_target_root.exists()
        else 0
    )
    db_user = urlparse(settings.ops_pg_dsn).username if settings.ops_pg_dsn else None
    db_user_minimal = bool(db_user and db_user.lower() not in {"postgres", "root", "admin"})
    log_dir_writable = (
        os.access(settings.ops_log_dir, os.W_OK) if settings.ops_log_dir.exists() else False
    )
    # exec 安全:要么彻底关(allow_exec=false),要么在 prod 下显式开且配了 allowlist。
    # 旧逻辑把"allowlist 非空"当 prod-ready 条件是反的(allowlist 非空≈允许执行)。
    exec_safe = (not settings.ops_allow_exec) or (
        settings.ops_prod_allow_exec and bool(settings.ops_exec_allowlist)
    )
    prod_ready = not settings.production or (
        not settings.ops_sql_allow_free
        and exec_safe
        and db_user_minimal
        and settings.ops_log_dir.exists()
        and not log_dir_writable
    )
    checks = {
        "OPS_PROFILE": settings.ops_profile,
        "ANTHROPIC_API_KEY": bool(settings.anthropic_api_key),
        "ANTHROPIC_MODEL": settings.anthropic_model,
        "ANTHROPIC_JUDGE_MODEL": settings.anthropic_judge_model,
        "OPS_TARGET_ROOT": settings.ops_target_root,
        "TARGET_ROOT_EXISTS": settings.has_target_root,
        "OPS_LOG_DIR": settings.ops_log_dir,
        "LOG_DIR_EXISTS": settings.ops_log_dir.exists(),
        "LOG_DIR_WRITABLE": log_dir_writable,
        "LOG_DIR_READ_ONLY_OK": settings.ops_log_dir.exists() and not log_dir_writable,
        "COMPOSE_FILES": compose_count,
        "BATCH_MODULES": module_count,
        "OPS_PG_DSN": settings.has_pg,
        "OPS_PG_USER": db_user,
        "OPS_PG_USER_MINIMAL_OK": db_user_minimal,
        "OPS_SQL_ALLOW_FREE": settings.ops_sql_allow_free,
        "OPS_ALLOW_EXEC": settings.ops_allow_exec,
        "OPS_PROD_ALLOW_EXEC": settings.ops_prod_allow_exec,
        "EXEC_SAFE": exec_safe,
        "OPS_RESTART_CMD": bool(settings.ops_restart_cmd),
        "OPS_EXEC_ALLOWLIST": settings.ops_exec_allowlist,
        "OPS_APPROVAL_LOG": settings.ops_approval_log,
        "OPS_REDACT_ARTIFACTS": settings.ops_redact_artifacts,
        "OPS_TRACE_DIR": settings.ops_trace_dir,
        "OPS_BUNDLE_DIR": settings.ops_bundle_dir,
        "LANGFUSE_ENABLED": settings.langfuse_enabled,
        "PROD_READY": prod_ready,
    }
    for key, value in checks.items():
        print(f"{key}: {value}")
    # 可做部署前置闸:prod 未就绪 → 非零退出(`ops-agent doctor && deploy`)。
    if settings.production and not prod_ready:
        print("PROD_READY=false:生产前置检查未通过(见上方各项)。", file=sys.stderr)
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ops-agent")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志(DEBUG,含异常堆栈)")
    sub = parser.add_subparsers(dest="command", required=True)

    diagnose = sub.add_parser("diagnose", help="诊断单个日志文件")
    diagnose.add_argument("log_file")
    diagnose.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    diagnose.set_defaults(func=_cmd_diagnose)

    investigate = sub.add_parser("investigate", help="自然语言问题 -> 工具取证 -> 结构化诊断")
    investigate.add_argument("question")
    investigate.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    investigate.set_defaults(func=_cmd_investigate)

    chat = sub.add_parser("chat", help="运行手写多步 agent，未传问题时进入交互模式")
    chat.add_argument("question", nargs="*")
    chat.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    chat.set_defaults(func=_cmd_chat)

    graph = sub.add_parser("graph", help="运行 LangGraph 版 agent")
    graph.add_argument("question", nargs="*")
    graph.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    graph.set_defaults(func=_cmd_graph)

    eval_cmd = sub.add_parser("eval", help="运行 evals.run_eval")
    eval_cmd.add_argument("--judge", action="store_true", help="启用 LLM-as-judge")
    eval_cmd.add_argument("--save", metavar="FILE", help="把本次结果存为基线 JSON")
    eval_cmd.add_argument("--baseline", metavar="FILE", help="与基线对比升降")
    eval_cmd.set_defaults(func=_cmd_eval)

    bundle = sub.add_parser("bundle", help="运行诊断并输出 diagnosis/trace/evidence/summary 包")
    bundle.add_argument("question")
    bundle.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    bundle.set_defaults(func=_cmd_bundle)

    services = sub.add_parser("services", help="列出目标系统服务、模块和日志")
    services.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    services.set_defaults(func=_cmd_services)

    errors = sub.add_parser("errors", help="扫描目标日志目录近期异常行")
    errors.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    errors.add_argument("--max-lines", type=int, default=200)
    errors.set_defaults(func=_cmd_errors)

    compose = sub.add_parser("compose", help="摘要目标系统 docker-compose 配置")
    compose.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    compose.add_argument("--max-chars", type=int, default=6000)
    compose.set_defaults(func=_cmd_compose)

    app_config = sub.add_parser("app-config", help="读取目标系统应用配置摘要")
    app_config.add_argument("service", nargs="?")
    app_config.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    app_config.add_argument("--max-chars", type=int, default=6000)
    app_config.set_defaults(func=_cmd_app_config)

    doctor = sub.add_parser("doctor", help="检查关键配置")
    doctor.add_argument("--target", help="目标系统根目录或名称,如 file-batch-system")
    doctor.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        raise SystemExit(130) from None
    except FileNotFoundError as e:
        # 缺输入文件:稳定退出码 3,便于脚本分支
        print(f"找不到输入文件:{e.filename or e}", file=sys.stderr)
        raise SystemExit(3) from None
    except ValueError as e:
        # 配置/用法错误(如非法 OPS_PROFILE):退出码 2
        print(f"配置错误:{e}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as e:  # noqa: BLE001 - CLI 顶层兜底:把 LLM/网络等错误转成干净提示,不抛裸堆栈
        log.debug("命令执行失败", exc_info=True)  # -v 时打印完整堆栈
        from anthropic import APIConnectionError, APIError

        if isinstance(e, (APIError, APIConnectionError)):
            # LLM / 网络:退出码 4
            print(
                f"LLM 调用失败({type(e).__name__}):{e}\n稍后重试,或检查 ANTHROPIC_API_KEY / 网络。",
                file=sys.stderr,
            )
            raise SystemExit(4) from None
        print(f"执行出错({type(e).__name__}):{e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
