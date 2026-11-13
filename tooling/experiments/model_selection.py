"""Isolated selection CLI. Prepare, credential entry, seal, freeze and score never call a model."""

import argparse
import copy
import getpass
import gzip
import hashlib
import json
import sqlite3
import sys
import zlib
from datetime import timedelta

import httpx
from pathlib import Path

from xau_next.platform.credentials.public import WindowsCredentials
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc, utc_now
from xau_next.workflows.model_selection import (
    digest,
    freeze_sample,
    load_plan,
    make_plan,
    read,
    report,
    run_sample,
    save,
    score_sample,
    seal_followup,
    seal_plan,
    slot_for,
)


def decoded(db, payload):
    if payload.startswith("@xau-payload-v1:"):
        expected = payload.split(":", 1)[1]
        blob = db.execute(
            "SELECT compressed,uncompressed_size FROM payload_blobs WHERE sha256=?", (expected,)
        ).fetchone()
        if blob is None:
            raise ValueError("payload missing")
        decoder = zlib.decompressobj()
        raw = decoder.decompress(blob[0], blob[1] + 1)
        if (
            len(raw) != blob[1]
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
            or hashlib.sha256(raw).hexdigest() != expected
        ):
            raise ValueError("payload integrity")
        payload = raw
    return json.loads(payload)


def read_saved_attachment(db, data_root, attachment_id, *, expected_sha256=None):
    """Read one registered file through the caller's read-only SQLite connection."""
    row = db.execute(
        "SELECT sha256,relative_path,size,media_type FROM attachments WHERE id=?", (attachment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("source attachment missing")
    data_root = Path(data_root).resolve()
    path = (data_root / row[1]).resolve()
    if not path.is_relative_to((data_root / "attachments").resolve()):
        raise ValueError("attachment path")
    content = path.read_bytes()
    if (
        len(content) != row[2]
        or digest(content) != row[0]
        or expected_sha256 is not None and row[0] != expected_sha256
    ):
        raise ValueError("source integrity")
    return content, row[3]


def read_cpa_profile(data_root, profile_id):
    """Exact read-only document, including a reference, never the referenced credential."""
    path = Path(data_root).resolve() / "db/xau-next.sqlite3"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        row = db.execute(
            "SELECT payload FROM documents WHERE owner='settings' AND kind='model_profile' AND id=?",
            (profile_id,),
        ).fetchone()
        if row is None:
            raise DomainError("SELECTION_PROFILE_MISSING", "未找到指定已保存的CPA配置。", 409)
        value = decoded(db, row[0])
        return {
            "profile_id": profile_id,
            "values": value["values"],
            "credential_ref": value["credential_ref"],
        }
    finally:
        db.close()


def capture_saved_snapshot(root, sample_id, schema_path, *, clock=utc_now):
    """Reads already-saved exact source once. No MT5 connection or formal request creation."""
    from xau_next.domains.analysis.public import AnalysisService, build_packet, frozen_answer_schema, bind_shared_research_refs
    from xau_next.domains.market.public import decode_market_attachment
    from xau_next.platform.db.public import Database
    from xau_next.platform.files.public import AttachmentStore
    from xau_next.shared.entry_timing import timing_reference
    from xau_next.shared.snapshots import Snapshot

    root = Path(root).resolve()
    plan = load_plan(root)
    slot_for(plan, sample_id, root)
    data = Path(plan["formal_root"])
    db = sqlite3.connect((data / "db/xau-next.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        row = db.execute(
            "SELECT id,payload FROM documents WHERE owner='market' AND kind='snapshot' "
            "ORDER BY recorded_at DESC,id DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise DomainError("SELECTION_SOURCE_MISSING", "未保存行情，不自动采集。", 409)
        identity, envelope = row[0], decoded(db, row[1])
        header = envelope["header"]
        cutoff = header["market_cutoff"]
        if (
            not 0
            <= (parse_utc(clock()) - parse_utc(cutoff)).total_seconds()
            <= plan["max_freeze_age_seconds"]
        ):
            raise DomainError("SELECTION_SOURCE_STALE", "没有足够新的已保存快照，未采集或重试。", 409)
        ev = db.execute(
            "SELECT payload FROM documents WHERE owner='market' AND kind='evidence' AND id=?", (identity,)
        ).fetchone()
        evidence = decoded(db, ev[0])
        raw, media_type = read_saved_attachment(
            db, data, evidence["attachment_id"], expected_sha256=evidence["sha256"]
        )
        payload = decode_market_attachment(
            raw,
            media_type=media_type,
            expected=evidence.get("storage_format", "bridge-response"),
            read_many=lambda ids: {
                part_id: read_saved_attachment(db, data, part_id)[0] for part_id in dict.fromkeys(ids)
            },
        )
        if payload.get("error") is not None:
            raise ValueError("source error")
        market, portfolio = payload["result"]["market"], payload["result"]["portfolio"]
        if (
            market["snapshot_id"] != identity
            or portfolio["snapshot_id"] != evidence["portfolio_snapshot_id"]
            or any(market[k] != value for k, value in header.items() if k in market)
        ):
            raise ValueError("source binding")
        sr = db.execute(
            "SELECT id,revision,recorded_at,payload FROM documents WHERE owner='settings' "
            "AND kind='settings' ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
        settings = {**decoded(db, sr[3]), "id": sr[0], "revision": sr[1], "recorded_at": sr[2]}
        if sr[1] != evidence["settings_revision"] or parse_utc(sr[2]) > parse_utc(cutoff):
            raise ValueError("settings binding")
        # Historical request context is reused only if saved before this cutoff and same source.
        experience = None
        for (stored,) in db.execute(
            "SELECT payload FROM documents WHERE owner='analysis' AND kind='request' "
            "AND recorded_at<=? ORDER BY recorded_at DESC LIMIT 100",
            (cutoff,),
        ):
            q = decoded(db, stored)
            source = q.get("context", {}).get("market", {})
            if all(source.get(k) == market.get(k) for k in ("account_scope_id", "symbol", "source_kind")):
                experience = q.get("context", {}).get("experience")
                break
        previous, timing_samples = None, []
        for (stored,) in db.execute(
            "SELECT payload FROM documents WHERE owner='analysis' AND kind='version' "
            "AND available_at<=? ORDER BY available_at DESC LIMIT 100",
            (cutoff,),
        ):
            version = decoded(db, stored)
            qr = db.execute(
                "SELECT payload FROM documents WHERE owner='analysis' AND kind='request' AND id=?",
                (version["request_id"],),
            ).fetchone()
            q = decoded(db, qr[0])
            source = q["context"]["market"]
            if not all(source.get(k) == market.get(k) for k in ("account_scope_id", "symbol", "source_kind")):
                continue
            if previous is None:
                previous = version
            timing_samples.append(
                {
                    "version_id": version["version_id"],
                    "schema_version": version["answer"]["schema_version"],
                    "market_cutoff": q["market_cutoff"],
                    "created_at": q["created_at"],
                    "available_at": version["available_at"],
                }
            )
        timing = timing_reference(timing_samples[:5])
    finally:
        db.close()

    class DetachedAnalysis(AnalysisService):
        def _timing_reference(self, *_args):
            return copy.deepcopy(timing)

        def latest_for_market(self, *_args, **_kwargs):
            return Snapshot.of(previous) if previous else None

        def version_at(self, *_args, **_kwargs):
            return Snapshot.of(previous) if previous else None

    isolated = root / ("input-" + sample_id)
    if isolated.exists():
        raise DomainError("SELECTION_SAMPLE_EXISTS", "本组已有准备痕迹，不覆盖或自动重试。", 409)
    isolated.mkdir()
    target = Database(isolated)
    target.initialize()
    try:
        service = DetachedAnalysis(target, AttachmentStore(target), read(schema_path), clock)
        request = service.create_request(
            {
                "kind": "new",
                "forecast_id": None,
                "base_version_id": None,
                "market_snapshot_id": identity,
                "horizon_minutes": [30, 60, 180],
            },
            market=Snapshot.of(market),
            portfolio=Snapshot.of(portfolio),
            settings=Snapshot.of(settings),
            experience=Snapshot.of(experience) if experience else None,
            capture_binding={"capture_settings_revision": sr[1]},
        ).to_dict()
        with target.read() as tx:
            request = service._docs.get(tx, "request", request["request_id"])
    finally:
        target.close()
    request = {k: v for k, v in request.items() if k not in ("id", "revision", "recorded_at")}
    # A newly created isolated request deliberately selects the existing 27b builder branch.
    # No historical request is relabelled or modified.
    request["prompt_version"] = plan.get("prompt_version", "manual-analysis/14")
    if request["prompt_version"] in {"manual-analysis/20", "manual-analysis/21", "manual-analysis/22", "manual-analysis/23"}:
        research = plan["external_research"]
        calendar = read(research["context_path"])
        if digest(calendar) != research["context_sha256"]:
            raise DomainError("SELECTION_RESEARCH_CHANGED", "经济资料哈希变化，不重新取数或补齐。", 409)
        request["context"]["economic_context"] = calendar
        request["context"]["research_policy"] = {"mode": research["mode"]}
        if request["prompt_version"] in {"manual-analysis/21", "manual-analysis/22", "manual-analysis/23"}:
            request["context"] = bind_shared_research_refs(request, request["context"])
    request["packet_text"] = build_packet(
        {k: v for k, v in request.items() if k not in ("context", "packet_text")},
        request["context"],
        frozen_answer_schema(request),
    )
    freeze_sample(root, sample_id, request, clock=clock)
    save(
        root / sample_id / "source-provenance.json",
        {
            "source_attachment_sha256": evidence["sha256"],
            "market_snapshot_id": identity,
            "settings_revision": sr[1],
            "market_cutoff": cutoff,
            "new_market_reads": 0,
        },
    )
    return {"state": "FROZEN_FROM_SAVED_SOURCE", "sample_id": sample_id, "new_model_calls": 0}


def public_plan(plan):
    """Reports expose credential presence, never Windows targets or key values."""
    return {
        "experiment_id": plan["experiment_id"],
        "status": plan["status"],
        "source_cpa_profile": plan["cpa_profile_id"],
        "arms": {
            arm: {
                "configuration": item["configuration"],
                "credential_reference_present": bool(item["credential_ref"]),
                "credential_available": "NOT_READ",
                "real_compatibility": "NOT_TESTED",
            }
            for arm, item in plan["arms"].items()
        },
        "new_model_calls": 0,
        **({"prompt_version": plan["prompt_version"], "input_revision": plan["input_revision"],
            "external_research_mode": plan["external_research"]["mode"],
            "economic_context_sha256": plan["external_research"]["context_sha256"],
            "native_search_compatibility": "NOT_TESTED"} if plan.get("external_research") else {}),
    }


def credential_window(write):
    """User-local masked entry; the callback saves only, never tests a provider."""
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title("XAU-Next · DeepSeek 官方 API 密钥")
    window.geometry("580x260")
    window.resizable(False, False)
    saved = False
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="粘贴 DeepSeek 官方 API 密钥（Ctrl+V）").pack(anchor="w")
    value = tk.StringVar(master=window)
    entry = ttk.Entry(frame, textvariable=value, show="*", width=65)
    entry.pack(fill="x", pady=(10, 8))
    status = ttk.Label(frame, text="尚未输入；输入后将显示 *")
    status.pack(anchor="w")
    ttk.Label(frame, text="只保存到本机 Windows 凭据存储，不发送测试请求。\n不要把密钥发到聊天中。").pack(
        anchor="w", pady=12
    )

    def changed(*_):
        status.configure(text=f"已输入 {len(value.get())} 个字符（内容已遮挡）")

    value.trace_add("write", changed)

    def close():
        value.set("")
        window.destroy()

    def submit(*_):
        nonlocal saved
        if saved:
            return
        secret = value.get().strip()
        if not secret:
            status.configure(text="尚未输入密钥，请先点击输入框再粘贴。")
            entry.focus_set()
            return
        try:
            write(secret)
        except (DomainError, OSError, ValueError):
            # Credential providers can include sensitive values in errors. Never echo them.
            status.configure(text="本机保存失败，请关闭后重试；未发送模型请求。")
            return
        finally:
            secret = None
        saved = True
        value.set("")
        entry.configure(state="disabled")
        status.configure(text="已在本机保存成功；未发送模型请求。可以关闭窗口。")
        button.configure(text="关闭", command=close)

    button = ttk.Button(frame, text="仅保存到本机", command=submit)
    button.pack(anchor="e")
    window.protocol("WM_DELETE_WINDOW", close)
    window.bind("<Escape>", lambda _: close())
    entry.bind("<Return>", submit)
    window.after(100, entry.focus_force)
    window.mainloop()
    return saved


def main(argv=None):
    parser = argparse.ArgumentParser(description="四模型选模；只有run且有当前计划收费授权文件才发送。")
    commands = parser.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare", help="只读复用CPA，生成未授权草案")
    prepare.add_argument("--data-root", required=True)
    prepare.add_argument("--cpa-profile", required=True)
    prepare.add_argument("--root", required=True)
    prepare.add_argument(
        "--schema", default=str(Path(__file__).resolve().parents[2] / "contracts/analysis-answer.schema.json")
    )
    prepare.add_argument("--economic-context", help="统一经济日历文件；启用独立20版三模型草案")
    prepare.add_argument("--native-search", action="store_true", help="独立搜索接入验证，不计入同输入准确率")
    prepare.add_argument("--umich-actuals", action="store_true", help="显式23版官方实际值补充；不授予收费额度")
    calendar = commands.add_parser("calendar", help="只读获取一次公开经济日历；无模型、无密钥、无自动重试")
    calendar.add_argument("--root", required=True)
    actuals = commands.add_parser("actuals", help="一次读取密歇根官网实际值，生成独立日历副本；无模型或密钥")
    actuals.add_argument("--root", required=True)
    actuals.add_argument("--calendar", required=True, help="已有经济日历文件，只读")
    actuals.add_argument("--reference-period", required=True, help="明确月份，如2026-10")
    actuals.add_argument("--release-kind", required=True, choices=["preliminary", "final"])
    key = commands.add_parser("credential", help="用户本机不回显输入DeepSeek密钥；无探针、无模型请求")
    key.add_argument("--root", required=True)
    key.add_argument("--window", action="store_true", help="打开显示遮挡字符的本机输入框")
    seal = commands.add_parser("seal", help="冻结含日程的计划，仍不发送")
    seal.add_argument("--plan", required=True)
    seal.add_argument("--root", required=True)
    freeze = commands.add_parser("freeze", help="从完整27b请求文件冻结四臂同资料")
    freeze.add_argument("--root", required=True)
    freeze.add_argument("--sample", required=True)
    freeze.add_argument("--request", required=True)
    capture = commands.add_parser("capture", help="只读复用已保存新快照，在隔离库准备27b；不调用MT5")
    capture.add_argument("--root", required=True)
    capture.add_argument("--sample", required=True)
    capture.add_argument("--schema", required=True)
    followup = commands.add_parser("followup", help="筛选后追加第二阶段；原计划和账本不重置")
    followup.add_argument("--root", required=True)
    followup.add_argument("--samples", required=True)
    followup.add_argument("--challenger", choices=list("BCD"), required=True)
    followup.add_argument("--evidence", action="append", required=True)
    run = commands.add_parser("run", help="显式执行一组；最多4个请求，无自动下一组")
    run.add_argument("--root", required=True)
    run.add_argument("--sample", required=True)
    run.add_argument("--authorization", required=True)
    score = commands.add_parser("score", help="仅评价已保存同源资料，不读取MT5")
    score.add_argument("--root", required=True)
    score.add_argument("--sample", required=True)
    score.add_argument("--market", required=True)
    score.add_argument("--as-of", required=True)
    summary = commands.add_parser("report", help="报告全分母，不自动选赢家或切换模型")
    summary.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.action == "prepare":
            if args.umich_actuals and (not args.economic_context or args.native_search):
                raise DomainError("SELECTION_RESEARCH_INVALID", "23版须提供补充日历且不能启用独立搜索。", 409)
            profile = read_cpa_profile(args.data_root, args.cpa_profile)
            plan = make_plan(profile, args.data_root, answer_schema=read(args.schema))
            if args.native_search and not args.economic_context:
                raise DomainError("SELECTION_RESEARCH_INVALID", "搜索验证也须先提供同一份基础日历。", 409)
            if args.economic_context:
                path = Path(args.economic_context).resolve()
                context = read(path)
                plan.update(prompt_version="manual-analysis/20", input_revision="economic-context/1",
                    purpose="three_model_selection", external_research={
                        "mode": "native_search_probe" if args.native_search else "shared_calendar",
                        "context_path": str(path), "context_sha256": digest(context)})
                if args.umich_actuals:
                    if context.get("actual_supplement", {}).get("version") != "umich-actuals/1":
                        raise DomainError("SELECTION_RESEARCH_INVALID", "23版缺少独立官方实际值资料。", 409)
                    plan.update(prompt_version="manual-analysis/23", input_revision="economic-actuals/1", purpose="model_selection")
                    plan["external_research"]["mode"] = "shared_search" if context.get("shared_research") else "shared_calendar"
            formal = Path(args.data_root).resolve()
            if root == formal or root.is_relative_to(formal) or formal.is_relative_to(root):
                raise DomainError("SELECTION_ROOT_INVALID", "草案必须与正式目录分离。", 409)
            save(root / "plan.draft.json", plan)
            save(root / "CONFIGURATIONS.json", public_plan(plan))
            result = {"status": "DRAFT_ONLY", "root": str(root), "new_model_calls": 0}
        elif args.action == "calendar":
            from xau_next.platform.economic_calendar.public import fetch_calendar
            if root.exists() and any(root.iterdir()):
                raise DomainError("CALENDAR_CAPTURE_EXISTS", "保留已有日历原件，请为新取数使用新目录。", 409)
            context, raw = fetch_calendar()
            save(root / "calendar-source.raw.json", raw)
            save(root / "economic-context.json", context)
            result = {"status": "PUBLIC_CALENDAR_SAVED", "context_path": str(root / "economic-context.json"),
                "context_sha256": digest(context), "events": len(context["events"]), "new_model_calls": 0,
                "actual_value_feed": "NOT_CONNECTED", "source": context["sources"][0]}
            save(root / "CAPTURE.json", result)
        elif args.action == "actuals":
            from xau_next.platform.economic_calendar.umich import URL, fetch_umich_raw, umich_document
            from xau_next.domains.analysis.public import build_economic_context
            if root.exists() and any(root.iterdir()):
                raise DomainError("ACTUALS_CAPTURE_EXISTS", "保留已有实际值原件，请使用新目录。", 409)
            calendar_path = Path(args.calendar).resolve()
            context = read(calendar_path)
            if context.get("version") != "economic-context/1" or context.get("data_kind") != "REAL" or "actual_supplement" in context:
                raise DomainError("ECONOMIC_ACTUALS_INVALID", "只允许从真实原始日历生成新副本，不覆盖已有补数。", 409)
            # Persist raw data before extraction, including layout/version failures.
            save(root / "ATTEMPT.json", {"url": URL, "started_at": utc_now(), "model_calls": 0})
            try:
                raw, captured = fetch_umich_raw()
                save(root / "umich-source.raw.html", raw)
                document = umich_document(raw, captured_at=captured,
                    reference_period=args.reference_period, release_kind=args.release_kind)
                save(root / "umich-actuals.json", document)
                combined = copy.deepcopy(context)
                combined["actual_supplement"] = document
                # Validate as observed now, never against a historical prediction cutoff.
                request = {"prompt_version": "manual-analysis/23", "market_cutoff": captured,
                    "window_end": (parse_utc(captured) + timedelta(minutes=180)).isoformat().replace("+00:00", "Z"),
                    "horizon_minutes": [30, 60, 180]}
                mode = "shared_search" if combined.get("shared_research") else "shared_calendar"
                # Search material is not refreshed or paid for by this command. Its
                # separate future-cutoff freshness gate still applies when frozen.
                validation_input = copy.deepcopy(combined)
                validation_input.pop("shared_research", None)
                checked = build_economic_context(request, {"economic_context": validation_input,
                    "research_policy": {"mode": "shared_calendar"}})
                save(root / "economic-context.json", combined)
                save(root / "AS-OF-VALIDATION.json", checked)
                result = {"status": "OFFICIAL_ACTUALS_SAVED_NOT_A_PREDICTION", "context_path": str(root / "economic-context.json"),
                    "context_sha256": digest(combined), "calendar_path": str(calendar_path),
                    "calendar_sha256": digest(context), "source": document["source"], "source_state": document["state"],
                    "matched_event_actuals": checked["matched_event_actuals"], "calendar_state": checked["state"],
                    "actual_completion_state": "matched" if checked["matched_event_actuals"] else "unmatched",
                    "future_research_mode": mode, "new_model_calls": 0, "paid_search_requests": 0,
                    "new_mt5_calls": 0, "formal_activation": False}
                save(root / "CAPTURE.json", result)
            except Exception as error:
                save(root / "FAILURE.json", {"status": "ACTUALS_UNAVAILABLE_NOT_RETRIED", "at": utc_now(),
                    "code": getattr(error, "code", type(error).__name__), "message": str(error), "new_model_calls": 0})
                raise
        elif args.action == "credential":
            plan = read(root / "plan.draft.json")
            expected = "XAU-Next/model/" + plan["experiment_id"] + "-deepseek"
            if any(plan["arms"][arm]["credential_ref"] != expected for arm in "CD"):
                raise DomainError("SELECTION_KEY_REFERENCE_INVALID", "凭据目标不属于此实验。", 409)
            if args.window:
                if not credential_window(lambda value: WindowsCredentials().write(expected, value)):
                    raise DomainError("SELECTION_KEY_ENTRY_CANCELLED", "已取消录入，未发送请求。", 409)
            elif not sys.stdin.isatty():
                raise DomainError("SELECTION_KEY_INTERACTIVE_REQUIRED", "密钥只能由用户在本机交互输入。", 409)
            else:
                value = getpass.getpass("仅在本机录入DeepSeek官方API密钥（输入字符不显示，粘贴后按回车；不发送测试）：")
                try:
                    if not value.strip():
                        raise DomainError("MODEL_KEY_REQUIRED", "未输入密钥。", 409)
                    WindowsCredentials().write(expected, value)
                finally:
                    value = None
            result = {
                "status": "CREDENTIAL_SAVED_LOCALLY",
                "new_model_calls": 0,
                "real_compatibility": "NOT_TESTED",
            }
        elif args.action == "seal":
            plan = seal_plan(root, read(args.plan))
            result = {"plan_sha256": digest(plan), "new_model_calls": 0}
        elif args.action == "freeze":
            result = {
                "sample_sha256": freeze_sample(root, args.sample, read(args.request)),
                "new_model_calls": 0,
            }
        elif args.action == "capture":
            result = capture_saved_snapshot(root, args.sample, args.schema)
        elif args.action == "followup":
            result = seal_followup(root, read(args.samples), args.challenger, args.evidence)
        elif args.action == "run":
            # No app/settings constructor, migrations, profile activation, or bridge initialization.
            from xau_next.domains.analysis.public import ModelCallStore
            from xau_next.platform.db.public import Database
            from xau_next.platform.files.public import AttachmentStore
            from xau_next.platform.model_gateway.public import Gateway

            plan = load_plan(root)
            if plan["cohort"] != "forward":
                raise DomainError("SELECTION_SYNTHETIC_NETWORK", "CLI不允许合成计划访问真实渠道。", 409)

            def check():
                profile = read_cpa_profile(plan["formal_root"], plan["cpa_profile_id"])
                if digest(profile) != plan["cpa_profile_fingerprint"]:
                    raise DomainError("SELECTION_CPA_CHANGED", "保存的CPA配置已经变化，未发送。", 409)

            db = Database(Path(plan["formal_root"]))
            try:
                calls = ModelCallStore(db, AttachmentStore(db), utc_now)
                result = run_sample(
                    root,
                    args.sample,
                    authorization=read(args.authorization),
                    gateway=Gateway(),
                    credentials=WindowsCredentials(),
                    calls=calls,
                    profile_check=check,
                )
            finally:
                db.close()
        elif args.action == "score":
            result = score_sample(root, args.sample, read(args.market), as_of=args.as_of)
        else:
            result = report(root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (DomainError, OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as error:
        # Never expose exception text, connection objects, payloads or credentials.
        print(
            json.dumps(
                {
                    "status": "STOPPED",
                    "error_code": error.code
                    if isinstance(error, DomainError)
                    else "SELECTION_LOCAL_INPUT_ERROR",
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
