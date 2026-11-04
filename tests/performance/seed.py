"""Build clearly synthetic persistent data through owner-bound repositories.

This loads validated fixture shapes, rather than claiming 10,000 user imports
were performed. The one-million-bar archive uses immutable compressed blobs.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from common import ROOT, sha256, write_json

sys.path.insert(0, str(ROOT / "apps/mt5_bridge/src"))

from xau_mt5_bridge.synthetic import SyntheticSource, bars
from xau_next.domains.analysis.public import AnalysisService, market_scope_key
from xau_next.domains.plans.public import PlansService
from xau_next.domains.portfolio.public import PortfolioService
from xau_next.domains.review.public import ReviewService, compute_prediction
from xau_next.domains.settings.public import SettingsService
from xau_next.http.contracts import Contracts
from xau_next.platform.db.public import Database
from xau_next.platform.files.public import AttachmentStore
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import utc_now

START, IMPORTED, REVIEWED = "2026-10-11T02:00:00Z", "2026-10-11T02:07:00Z", "2026-10-11T15:00:00Z"
SCOPE = "synthetic-account"


def _stamp(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def archive_bars(db, files, contracts, count: int):
    store, archive = db.documents("market"), []
    first = datetime.fromisoformat(START) - timedelta(minutes=count)
    bar_template = {
        "open": "100.00",
        "high": "101.00",
        "low": "99.00",
        "close": "100.05",
        "tick_volume": 10,
        "closed": True,
    }
    for offset in range(0, count, 5000):
        rows = []
        for index in range(offset, min(count, offset + 5000)):
            at = first + timedelta(minutes=index)
            rows.append(
                {"open_at": _stamp(at), "close_at": _stamp(at + timedelta(minutes=1)), **bar_template}
            )
        contracts.validate("Bar", rows[0])
        contracts.validate("Bar", rows[-1])
        content = b"".join((json.dumps(row, separators=(",", ":")) + "\n").encode() for row in rows)
        prepared = files.prepare(
            gzip.compress(content, compresslevel=6, mtime=0), media_type="application/gzip"
        )
        metadata = {
            "data_kind": "synthetic",
            "account_scope_id": SCOPE,
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "price_basis": "bid",
            "count": len(rows),
            "from": rows[0]["open_at"],
            "to": rows[-1]["close_at"],
            "format": "bar-json-lines-gzip/1",
            "attachment_id": prepared.id,
            "sha256": prepared.sha256,
        }
        with db.write() as tx:
            files.register(tx, prepared)
            store.insert(
                tx, "bar_archive", f"perf-bars-{offset:07d}", metadata, parent_id=SCOPE, available_at=START
            )
        # Verify every archived line count and hash; endpoint samples validate the invariant row schema.
        path = db.data_root / prepared.relative_path
        with gzip.open(path, "rb") as handle:
            verified_count = sum(1 for _ in handle)
        if verified_count != len(rows) or sha256(path) != prepared.sha256:
            raise AssertionError("压缩 K 线数量或哈希验证失败")
        archive.append(
            {**metadata, "relative_path": prepared.relative_path, "verified_count": verified_count}
        )
    return archive


def seed(
    data_root: Path, *, versions=10_000, review_revisions=10, bar_count=1_000_000, progress=True
) -> dict:
    if data_root.exists() and any(data_root.iterdir()):
        raise ValueError("合成基准 DATA_ROOT 必须为新的空目录")
    started = time.perf_counter()
    db = Database(data_root)
    db.initialize()
    files, contracts = AttachmentStore(db), Contracts(ROOT / "contracts")
    settings = SettingsService(db)
    settings.initialize()
    source = SyntheticSource()
    synthetic = source.handle("snapshot", {"captured_at": START, "symbol": "XAUUSD"})
    # Historical import fixture is deliberately compact; current workspace uses the full default four series.
    historical_market = copy.deepcopy(
        next(
            item["value"]
            for item in json.loads((ROOT / "fixtures/api-examples.json").read_text(encoding="utf-8"))
            if item["schema"] == "MarketSnapshot"
        )
    )
    historical_market = json.loads(json.dumps(historical_market).replace("2026-10-12", "2026-10-11"))
    historical_market["account_scope_id"] = SCOPE
    with db.write() as tx:
        db.documents("market").insert(
            tx,
            "snapshot",
            historical_market["snapshot_id"],
            historical_market,
            parent_id=SCOPE,
            available_at=START,
        )
    portfolio = PortfolioService(db)
    portfolio.sync_facts(Snapshot.of(synthetic["portfolio"]))
    analysis = AnalysisService(db, files, contracts.answer, clock=lambda: START)
    prototype = analysis.create_request(
        {
            "kind": "new",
            "market_snapshot_id": historical_market["snapshot_id"],
            "forecast_id": None,
            "base_version_id": None,
            "horizon_minutes": [180, 360, 720],
        },
        market=Snapshot.of(historical_market),
        portfolio=Snapshot.of(synthetic["portfolio"]),
        settings=settings.get_snapshot(),
    ).to_dict()
    with db.read() as scope:
        prototype_record = db.documents("analysis").get(scope, "request", prototype["request_id"])
    answer_template = json.loads(
        (ROOT / "fixtures/analysis-answer.valid.json")
        .read_text(encoding="utf-8")
        .replace("2026-10-12", "2026-10-11")
    )
    analysis_store, plans = db.documents("analysis"), PlansService(db)
    forecast_ids, version_ids = [], []
    packet_lengths = []
    for begin in range(0, versions, 100):
        prepared_rows = []
        for index in range(begin, min(versions, begin + 100)):
            forecast_id, version_id = f"perf-forecast-{index:05d}", f"perf-version-{index:05d}"
            request_id = prototype["request_id"] if index == 0 else f"perf-request-{index:05d}"
            request = {
                key: value
                for key, value in prototype_record.items()
                if key not in {"id", "revision", "recorded_at"}
            }
            request = copy.deepcopy(request)
            request.update(request_id=request_id, forecast_id=forecast_id, state="imported")
            request["packet_text"] = (
                prototype["packet_text"]
                .replace(prototype["request_id"], request_id)
                .replace(prototype["forecast_id"], forecast_id)
            )
            contracts.validate("AnalysisRequest", {key: request[key] for key in prototype})
            answer = copy.deepcopy(answer_template)
            answer.update(
                request_id=request_id,
                forecast_id=forecast_id,
                market_snapshot_id=historical_market["snapshot_id"],
            )
            raw_text = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
            attachment = files.prepare(raw_text.encode(), media_type="application/json")
            origin = {
                "source": "manual_paste",
                "provider": "synthetic-performance-fixture",
                "model_id": None,
                "prompt_version": prototype["prompt_version"],
                "answer_schema_version": "1.0.0",
                "raw_sha256": attachment.sha256,
            }
            drafts = [
                {
                    "plan_id": f"perf-plan-{index:05d}",
                    "version_id": version_id,
                    "forecast_id": forecast_id,
                    "available_at": IMPORTED,
                    "definition": answer["plans"][0],
                }
            ]
            version = {
                "version_id": version_id,
                "forecast_id": forecast_id,
                "sequence": 1,
                "request_id": request_id,
                "previous_version_id": None,
                "available_at": IMPORTED,
                "answer": answer,
                "origin": origin,
                "raw_attachment_id": attachment.id,
                "plan_drafts": drafts,
            }
            forecast = {
                "forecast_id": forecast_id,
                "current_version_id": version_id,
                "window_start": request["window_start"],
                "window_end": request["window_end"],
                "horizon_minutes": request["horizon_minutes"],
                "summary": answer["summary"],
                "available_at": IMPORTED,
                "account_scope_id": SCOPE,
                "symbol": "XAUUSD",
                "source_kind": "synthetic",
            }
            prepared_rows.append((request, attachment, version, forecast, drafts))
            forecast_ids.append(forecast_id)
            version_ids.append(version_id)
            packet_lengths.append(len(request["packet_text"]))
        with db.write() as tx:
            for request, attachment, version, forecast, drafts in prepared_rows:
                if request["request_id"] == prototype["request_id"]:
                    analysis_store.update(tx, "request", request["request_id"], request, 1, state="imported")
                else:
                    analysis_store.insert(
                        tx,
                        "request",
                        request["request_id"],
                        request,
                        parent_id=forecast["forecast_id"],
                        state="imported",
                    )
                files.register(tx, attachment)
                analysis_store.insert(
                    tx,
                    "version",
                    version["version_id"],
                    version,
                    parent_id=forecast["forecast_id"],
                    available_at=IMPORTED,
                )
                # Source key is the domain service's public stable index helper; no private formula is duplicated.
                parent = market_scope_key(historical_market)
                analysis_store.insert(
                    tx, "forecast", forecast["forecast_id"], forecast, parent_id=parent, available_at=IMPORTED
                )
                analysis_store.insert(
                    tx,
                    "request_result",
                    request["request_id"],
                    {"raw_sha256": attachment.sha256, "version_id": version["version_id"]},
                )
                plans.register(
                    drafts, uow=tx, market_identity={"account_scope_id": SCOPE, "symbol": "XAUUSD"}
                )
                contracts.validate(
                    "ForecastSummary",
                    {
                        key: saved_value
                        for key, saved_value in {**forecast, "revision": 1}.items()
                        if key in contracts.api["components"]["schemas"]["ForecastSummary"]["properties"]
                    },
                )
        if progress and (begin % 1000 == 0 or begin + 100 >= versions):
            print(
                json.dumps(
                    {
                        "seed_stage": "versions",
                        "completed": min(versions, begin + 100),
                        "elapsed_seconds": round(time.perf_counter() - started, 1),
                    }
                ),
                flush=True,
            )

    review_market = {
        "snapshot_id": "perf-review-market",
        "source_kind": "synthetic",
        "account_scope_id": SCOPE,
        "symbol": "XAUUSD",
        "from": START,
        "to": "2026-10-11T14:02:00Z",
        "price_basis": "bid",
        "bar_status": {"state": "available"},
        "bars": bars("2026-10-11T14:02:00Z", "M1", 722),
        "ticks": [],
        "tick_status": "unavailable",
    }
    template_version = {
        "version_id": "template-version",
        "forecast_id": "template-forecast",
        "available_at": IMPORTED,
        "answer": answer_template,
    }
    review_snapshot = source.handle("snapshot", {"captured_at": REVIEWED, "symbol": "XAUUSD"})["market"]
    review_snapshot["snapshot_id"] = review_market["snapshot_id"]
    next(series for series in review_snapshot["series"] if series["timeframe"] == "M1")["bars"] = (
        review_market["bars"]
    )
    contracts.validate("MarketSnapshot", review_snapshot)
    with db.write() as tx:
        db.documents("market").insert(
            tx,
            "snapshot",
            review_snapshot["snapshot_id"],
            review_snapshot,
            parent_id=SCOPE,
            available_at=REVIEWED,
        )
    templates = [
        compute_prediction(
            template_version,
            review_market,
            horizon_minutes=180,
            as_of=REVIEWED,
            neutral_band=f"{revision // 100}.{revision % 100:02d}",
        ).to_dict()
        for revision in range(review_revisions)
    ]
    for template in templates:
        contracts.validate("Review", template["review"])
    review_service = ReviewService(db, clock=lambda: REVIEWED)
    for begin in range(0, versions, 50):
        with db.write() as tx:
            for index in range(begin, min(versions, begin + 50)):
                for revision, template in enumerate(templates):
                    prepared = copy.deepcopy(template)
                    review = prepared["review"]
                    review.update(review_id=f"perf-review-{index:05d}", target_id=forecast_ids[index])
                    review["inputs"]["forecast_version_id"] = version_ids[index]
                    review["source_refs"] = [review_market["snapshot_id"], version_ids[index]]
                    prepared["input_fingerprint"] = hashlib.sha256(
                        f"synthetic:{index}:{revision}".encode()
                    ).hexdigest()
                    review_service.commit_review(prepared, uow=tx)
        if progress and (begin % 500 == 0 or begin + 50 >= versions):
            print(
                json.dumps(
                    {
                        "seed_stage": "review_revisions",
                        "completed": min(versions, begin + 50) * review_revisions,
                        "elapsed_seconds": round(time.perf_counter() - started, 1),
                    }
                ),
                flush=True,
            )

    archive = archive_bars(db, files, contracts, bar_count)
    current = source.handle("snapshot", {"captured_at": utc_now(), "symbol": "XAUUSD"})
    with db.write() as tx:
        db.documents("market").insert(
            tx,
            "snapshot",
            current["market"]["snapshot_id"],
            current["market"],
            parent_id=SCOPE,
            available_at=current["market"]["captured_at"],
        )
        portfolio.sync_facts(Snapshot.of(current["portfolio"]), uow=tx)
    contracts.validate("MarketSnapshot", current["market"])
    cpu_input = {
        "version": template_version,
        "market": review_market,
        "horizon_minutes": 720,
        "as_of": REVIEWED,
    }
    write_json(data_root / "cpu-input.json", cpu_input)
    with db.read() as scope:
        example_version = analysis.get_version(version_ids[-1], scope=scope).to_dict()
        contracts.validate("ForecastVersion", example_version)
        example_review = review_service.get_review(f"perf-review-{versions - 1:05d}", scope=scope).to_dict()
        contracts.validate("Review", example_review)
    import sqlite3

    with sqlite3.connect(db.path) as connection:
        counts = {
            f"{owner}.{kind}": count
            for owner, kind, count in connection.execute(
                "SELECT owner,kind,COUNT(*) FROM documents GROUP BY owner,kind"
            )
        }
        if counts["analysis.version"] != versions or counts["review.revision"] != versions * review_revisions:
            raise AssertionError("合成基准记录数不符")
    manifest = {
        "kind": "synthetic-performance-fixture",
        "data_root": str(data_root),
        "created_at": utc_now(),
        "expected_versions": versions,
        "review_heads": versions,
        "review_revisions": versions * review_revisions,
        "archived_m1_bars": sum(item["verified_count"] for item in archive),
        "archive_chunks": archive,
        "database_counts": counts,
        "current_workspace_series_counts": {
            row["timeframe"]: len(row["bars"]) for row in current["market"]["series"]
        },
        "historical_request_packet_characters": {"min": min(packet_lengths), "max": max(packet_lengths)},
        "database_integrity": db.check_integrity(),
        "seed_elapsed_seconds": round(time.perf_counter() - started, 3),
        "schema_checks": {
            "every_analysis_request": versions,
            "every_forecast_summary": versions,
            "review_templates": len(templates),
            "full_forecast_version_sample": 1,
            "current_review_sample": 1,
            "current_market_snapshot": 1,
            "bar_schema_endpoint_samples": len(archive) * 2,
            "every_archive_line_count_and_sha256": True,
        },
        "limitations": [
            "100,000 means review revisions with 10,000 current review heads, not 100,000 independent targets.",
            "Historical requests use the compact provided market fixture; current workspace uses full default four-period synthetic snapshots.",
            "Records are fixture-loaded through public owner-bound persistence; this does not claim 10,000 UI imports were exercised.",
        ],
    }
    write_json(data_root / "dataset-manifest.json", manifest)
    db.close()
    return manifest
