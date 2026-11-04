"""Set XAU_NEXT_TEST_CANDIDATE to validate a built artifact, rather than a source checkout."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.operations.test_operations import request_json


def post_json(runtime, path, value, key):
    request = urllib.request.Request(runtime["api_url"] + path, data=json.dumps(value).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + runtime["token"],
                 "Idempotency-Key": key}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def wait_job(runtime, job):
    for _ in range(200):
        job = request_json(runtime["api_url"] + "/v1/jobs/" + job["job_id"], runtime["token"])
        if job["state"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert job["state"] == "succeeded", job.get("error")
    return job


def files(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.skipif(not os.environ.get("XAU_NEXT_TEST_CANDIDATE"), reason="需要显式指定已构建候选目录")
def test_ac01_24_candidate_wheel_install_start_backup_restore_keeps_app_unchanged(tmp_path):
    candidate = Path(os.environ["XAU_NEXT_TEST_CANDIDATE"]).resolve()
    before = files(candidate)
    assert not (candidate / "apps/api/src").exists()
    data_root = tmp_path / "candidate-data"
    launcher = candidate / "tooling/launch/manage.py"
    result = subprocess.run([sys.executable, str(launcher), "setup", "--data-root", str(data_root)],
                            capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    installation = json.loads((data_root / "runtime/install.json").read_text(encoding="utf-8"))
    python = installation["api_python"]
    origin = subprocess.run([python, "-c", "import xau_next; print(xau_next.__file__)"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    assert str(data_root) in origin
    assert (Path(origin).parent / "_migrations/env.py").is_file()
    spec = importlib.util.spec_from_file_location("candidate_launcher", launcher)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = module.launch(app_root=candidate, data_root=data_root, source="synthetic", open_browser=False)
    try:
        assert request_json(runtime["api_url"] + "/v1/health")["database_ready"]
        assert "event_cursor" in request_json(runtime["api_url"] + "/v1/workspace", runtime["token"])
        wait_job(runtime, post_json(runtime, "/v1/market/refresh", {
            "include_history": False, "history_from": None, "history_to": None}, "candidate-capture"))
        market = request_json(runtime["api_url"] + "/v1/workspace", runtime["token"])["market"]
        analysis = post_json(runtime, "/v1/analysis/requests", {"kind": "new", "market_snapshot_id": market["snapshot_id"],
            "forecast_id": None, "base_version_id": None, "horizon_minutes": [180, 360, 720]}, "candidate-analysis")
        answer = json.loads((ROOT / "fixtures/analysis-answer.experience.json").read_text(encoding="utf-8"))
        answer["schema_version"] = analysis["answer_schema_version"]
        if analysis["answer_schema_version"] in {"1.5.0", "1.6.0", "1.7.0"}:
            for plan in answer["plans"]:
                plan["holding_time_basis"] = None
        if analysis["answer_schema_version"] in {"1.6.0", "1.7.0"}:
            for horizon in answer["horizons"]:
                horizon["forecast_basis"] = {
                    "direction_basis": "SYNTHETIC frozen endpoint assessment",
                    "range_basis": "SYNTHETIC full-window bounds",
                    "supporting_evidence": ["SYNTHETIC current structure"],
                    "opposing_evidence": ["SYNTHETIC reverse path"],
                    "evidence_refs": [market["snapshot_id"]],
                    "limitations": ["Synthetic acceptance only; no forecast-quality claim."],
                }
            for plan in answer["plans"]:
                for check in plan["entry_checks"]:
                    check.update(observation="new_since_available", purpose="SYNTHETIC independent entry check")
        if analysis["answer_schema_version"] == "1.7.0":
            for plan in answer["plans"]:
                plan["holding_exit"] = {
                    "condition": None,
                    "reason": "SYNTHETIC no additional structure exit is defined.",
                }
        for key in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
            answer[key] = analysis[key]
        start = datetime.fromisoformat(analysis["market_cutoff"])
        stamp = lambda value: value.isoformat().replace("+00:00", "Z")
        for horizon in answer["horizons"]:
            horizon["end_at"] = stamp(start + timedelta(minutes=horizon["horizon_minutes"]))
            horizon["phases"][0].update(start_at=analysis["market_cutoff"], end_at=horizon["end_at"])
        answer["plans"][0].update(valid_from=analysis["market_cutoff"], entry_deadline=stamp(start + timedelta(hours=1)),
                                   exit_deadline=stamp(start + timedelta(hours=2)))
        answer["evidence_refs"] = [market["snapshot_id"]]
        raw = "  " + json.dumps(answer, ensure_ascii=False) + "\n"
        wait_job(runtime, post_json(runtime, "/v1/analysis/imports", {"request_id": analysis["request_id"],
            "raw_text": raw, "source": "manual_paste", "provider": None, "model_id": None}, "candidate-import"))
        forecast = request_json(runtime["api_url"] + "/v1/forecasts/" + analysis["forecast_id"], runtime["token"])
        assert forecast["current_version"]["raw_text"] == raw
    finally:
        module.stop(app_root=candidate, data_root=data_root)
    backup_root, restored_root = tmp_path / "backup", tmp_path / "restored"
    for action, destination, extra in (("backup", backup_root, []),
                                        ("restore", restored_root, ["--backup-root", str(backup_root)])):
        result = subprocess.run([python, str(launcher), action, "--data-root", str(data_root),
                                 "--destination", str(destination), *extra],
                                capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        assert result.returncode == 0, result.stderr
    assert (restored_root / "db/xau-next.sqlite3").is_file()
    inspect_script = ("import json,sys,hashlib; from xau_next.platform.db.public import Database; "
        "from xau_next.platform.files.public import AttachmentStore; "
        "from xau_next.domains.analysis.public import AnalysisService; db=Database(sys.argv[1]); "
        "s=AnalysisService(db,AttachmentStore(db),json.load(open(sys.argv[2],encoding='utf-8'))); "
        "v=s.get_forecast(sys.argv[3]).to_dict()['current_version']; "
        "print(hashlib.sha256(v['raw_text'].encode('utf-8')).hexdigest()); db.close()")
    restored = subprocess.run([python, "-c", inspect_script, str(restored_root),
                               str(candidate / "contracts/analysis-answer.schema.json"), analysis["forecast_id"]],
                              capture_output=True, text=True, timeout=10, check=True)
    assert restored.stdout.strip() == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert files(candidate) == before
