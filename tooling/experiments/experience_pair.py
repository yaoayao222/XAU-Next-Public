"""Explicit local experiment commands. No scheduled work or implicit paid calls."""

import argparse
import json
import sys
from pathlib import Path

from xau_next.domains.analysis.public import ModelCallStore
from xau_next.domains.settings.public import ModelSettingsService
from xau_next.platform.credentials.public import WindowsCredentials
from xau_next.platform.db.public import Database
from xau_next.platform.files.public import AttachmentStore
from xau_next.platform.model_gateway.public import Gateway
from xau_next.shared.errors import DomainError
from xau_next.shared.values import utc_now
from xau_next.workflows.experience_experiment import (
    create_plan,
    freeze_pair,
    make_plan,
    read,
    run_pair,
)
from xau_next.workflows.experience_scoring import report, score_pair


def database(path):
    root = Path(path).resolve()
    if not (root / "db/xau-next.sqlite3").is_file():
        raise ValueError("必须指定已有 DATA_ROOT；不创建空数据库绕过用量记录。")
    return Database(root)


def main():
    parser = argparse.ArgumentParser(
        description="历史经验增量配对验证；init/freeze/report/score不调用模型，run只主动执行一对。"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="冻结计划及全部分母，不发送请求")
    init.add_argument("--data-root", required=True)
    init.add_argument("--root", required=True)
    init.add_argument("--pairs", type=int, default=10)
    init.add_argument("--horizons", type=int, nargs="+", required=True)
    init.add_argument("--cohort", choices=["forward", "historical_replay"], default="forward")
    init.add_argument("--start-at")
    init.add_argument("--interval-minutes", type=int, default=720)
    init.add_argument("--call-budget", type=int)
    init.add_argument("--cost-budget")
    init.add_argument("--accept-unknown-cost", action="store_true")
    freeze = commands.add_parser("freeze", help="只读已有新1.4请求，派生A/B冻结输入")
    freeze.add_argument("--data-root", required=True)
    freeze.add_argument("--root", required=True)
    freeze.add_argument("--sample", required=True)
    freeze.add_argument("--request-id", required=True)
    run = commands.add_parser("run", help="可能收费：人工启动且只执行指定的一对，不重试")
    run.add_argument("--data-root", required=True)
    run.add_argument("--root", required=True)
    run.add_argument("--sample", required=True)
    run.add_argument("--start-paid-pair", action="store_true", required=True)
    run.add_argument("--plan-sha256", required=True)
    score = commands.add_parser("score", help="独立离线评分，不写正式复盘")
    score.add_argument("--root", required=True)
    score.add_argument("--sample", required=True)
    score.add_argument("--actual-market", required=True, help="同源行情JSON，完整闭柱Bid M1及来源身份")
    score.add_argument("--as-of", required=True)
    show = commands.add_parser("report", help="报告所有预登记样本，包括未发送、失败和未知")
    show.add_argument("--root", required=True)
    args = parser.parse_args()
    db = None
    try:
        if args.command in {"init", "freeze", "run"}:
            db = database(args.data_root)
        if args.command == "init":
            with db.read() as tx:
                current = db.documents("settings").get(tx, "model_settings", "current")
            if not current:
                raise ValueError("该目录没有已配置的模型设置。")
            config = {"max_answer_bytes": 2000000, **current["values"]}
            plan = make_plan(
                config,
                pairs=args.pairs,
                horizons=args.horizons,
                cohort=args.cohort,
                start_at=args.start_at,
                interval_minutes=args.interval_minutes,
                call_budget=args.call_budget,
                cost_budget=args.cost_budget,
                accept_unknown_cost=args.accept_unknown_cost,
                source_root=args.data_root,
            )
            fingerprint = create_plan(args.root, plan)
            print(
                json.dumps(
                    {
                        "status": "PLAN_ONLY_NOT_STARTED",
                        "plan_sha256": fingerprint,
                        "planned_requests": plan["planned_requests"],
                        "call_budget": plan["call_budget"],
                        "cost_budget": plan["cost_budget"],
                        "cost_estimate": "unknown_until_inputs_frozen; price may remain unknown",
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "freeze":
            with db.read() as tx:
                request = db.documents("analysis").get(tx, "request", args.request_id)
            if request is None:
                raise ValueError("请求不存在。")
            frozen = freeze_pair(args.root, args.sample, request)
            print(
                json.dumps(
                    {
                        "status": "FROZEN_NOT_SENT",
                        "sample_id": args.sample,
                        "estimated_max_cost": frozen["estimated_max_cost"],
                        "files": frozen["files"],
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "run":
            settings = ModelSettingsService(db, WindowsCredentials(), utc_now)
            calls = ModelCallStore(db, AttachmentStore(db), utc_now)
            results = run_pair(
                args.root,
                args.sample,
                calls=calls,
                settings=settings,
                gateway=Gateway(),
                acknowledge_plan_sha256=args.plan_sha256,
            )
            print(
                json.dumps(
                    {
                        arm: {"state": result["state"], "error_code": result["error_code"]}
                        for arm, result in results.items()
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "score":
            result = score_pair(args.root, args.sample, read(args.actual_market), as_of=args.as_of)
            print(
                json.dumps(
                    {
                        "status": "SCORED_SEPARATELY",
                        "sample_id": result["sample_id"],
                        "cohort": result["cohort"],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            destination, result = report(args.root)
            print(
                json.dumps(
                    {
                        "report": str(destination),
                        "registered_pairs": result["registered_pairs"],
                        "conclusion": result["conclusion"],
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    except (DomainError, ValueError, OSError, KeyError) as error:
        print(
            json.dumps(
                {
                    "status": "NOT_COMPLETED",
                    "code": getattr(error, "code", type(error).__name__),
                    "detail": getattr(error, "detail", str(error)),
                },
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
