import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_ROOT))

from common import ROOT, APIServer, process_resources, summarize
from seed import seed


def test_deadline_observation_starts_once_and_never_labels_short_run_as_eight_hours(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import live_soak

    elapsed, launches = [0.0], []
    epoch = datetime(2026, 10, 13, 10, 49, 4, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return epoch + timedelta(seconds=elapsed[0])

    def sleep(seconds):
        elapsed[0] += seconds

    def launch(**kwargs):
        launches.append(kwargs)
        return {"test_only": True}

    def unexpected_stop(**kwargs):
        raise AssertionError("The deadline-only observation must leave the application running")

    workspace = {"current_forecast": {"forecast_id": "synthetic-forecast", "current_version_id": "v1"},
                 "market": {"snapshot_id": "synthetic-market", "quote": {"event_time": epoch.isoformat()},
                            "quote_status": {"state": "available"}},
                 "plans": [], "active_jobs": [], "fact_refresh": {}}

    def request(runtime, path, body=None, **kwargs):
        assert body is None, "This mode must not create model requests or change settings"
        return {"/v1/workspace": workspace, "/v1/settings": {"revision": 11, "values": {}},
                "/v1/reviews?limit=50": {"items": []}}[path]

    monkeypatch.setattr(live_soak, "datetime", Clock)
    monkeypatch.setattr(live_soak.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(live_soak.time, "sleep", sleep)
    monkeypatch.setattr(live_soak, "launch", launch)
    monkeypatch.setattr(live_soak, "stop", unexpected_stop)
    monkeypatch.setattr(live_soak, "fresh_workspace", lambda *a, **k: workspace)
    monkeypatch.setattr(live_soak, "request", request)
    monkeypatch.setattr(live_soak, "storage", lambda *a: {"db_bytes": 1, "attachment_bytes": 1})
    monkeypatch.setattr(live_soak, "probe_web", lambda *a: {"state": "available"})
    monkeypatch.setattr(live_soak, "sample_resources", lambda *a: [])
    app = tmp_path / "app"
    app.mkdir()
    (app / "release-manifest.json").write_text(json.dumps({"build_id": "synthetic", "source_commit": "test"}))
    output = tmp_path / "observation"
    live_soak.run(SimpleNamespace(
        app_root=app, data_root=tmp_path / "data", output_root=output,
        duration_seconds=28800, interval_seconds=60, end_at_utc="2026-10-13T10:51:04Z",
        skip_restart_check=True, recovery_check=False, refresh_due_reviews=False,
        skip_update_packet=True, daily_risk_cap_usd=None, minimum_net_rr=None,
    ))
    proof = json.loads((output / "live-smoke.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "soak-summary.json").read_text(encoding="utf-8"))
    assert len(launches) == 1 and launches[0]["open_browser"] is True
    assert proof["restart_check_performed"] is False and proof["restart_preserved_current_forecast"] is None
    assert summary["elapsed_seconds"] == summary["requested_seconds"] == 120
    assert summary["samples"] == 2 and summary["observation_state"] == "completed"
    assert summary["eight_hour_observation_completed"] is False


def test_live_probe_covers_static_web_interpreter_and_never_records_command_lines(monkeypatch):
    import live_soak

    roots = [{"pid": 1, "command_line": "uvicorn"}, {"pid": 2, "command_line": "static_server"}]
    children = {
        1: [{"pid": 3, "command_line": "xau_mt5_bridge"}],
        2: [{"pid": 4, "command_line": "static_server"}],
    }
    monkeypatch.setattr(live_soak, "owned_python_children", lambda pid: children[pid])
    monkeypatch.setattr(live_soak, "process_identity", lambda pid: next(r for r in roots if r["pid"] == pid))
    monkeypatch.setattr(live_soak, "process_resources", lambda pid: {"pid": pid, "private_bytes": pid * 100})
    monkeypatch.setattr(
        live_soak, "connection_resources", lambda pids: {pid: {"tcp_connections": 1} for pid in pids}
    )
    samples = live_soak.sample_resources({"processes": roots})
    assert {s["pid"] for s in samples} == {1, 2, 3, 4}
    assert next(s for s in samples if s["pid"] == 4)["component"] == "static_web"
    assert all("command_line" not in s and "tcp_connections" in s for s in samples)


def test_windows_connection_probe_observes_open_and_closed_sockets():
    import os
    import socket

    import pytest
    from common import connection_resources

    if os.name != "nt":
        pytest.skip("Windows TCP/UDP probe")
    pid = os.getpid()
    before = connection_resources([pid])[pid]
    with socket.socket() as listener, socket.socket() as client, socket.socket(type=socket.SOCK_DGRAM) as udp:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        accepted, _ = listener.accept()
        with accepted:
            udp.bind(("127.0.0.1", 0))
            during = connection_resources([pid])[pid]
            assert (
                during["tcp_states"].get("Established", 0) >= before["tcp_states"].get("Established", 0) + 2
            )
            assert during["tcp_states"].get("Listen", 0) >= before["tcp_states"].get("Listen", 0) + 1
            assert during["udp_endpoints"] == before["udp_endpoints"] + 1
    after = connection_resources([pid])[pid]
    assert after["udp_endpoints"] == before["udp_endpoints"]
    assert after["tcp_states"].get("Listen", 0) == before["tcp_states"].get("Listen", 0)
    assert after["tcp_states"].get("Established", 0) <= before["tcp_states"].get("Established", 0)
    assert after["tcp_states"].get("CloseWait", 0) <= before["tcp_states"].get("CloseWait", 0)


def test_nearest_rank_percentiles_preserve_tail():
    stats = summarize(list(range(1, 101)))
    assert stats["p50_ms"] == 50
    assert stats["p95_ms"] == 95
    assert stats["max_ms"] == 100


def test_small_dataset_keeps_revision_denominator_and_compressed_count(tmp_path):
    result = seed(
        tmp_path / "performance-data", versions=3, review_revisions=2, bar_count=100, progress=False
    )
    assert result["database_counts"]["analysis.version"] == 3
    assert result["database_counts"]["analysis.request"] == 3
    assert result["database_counts"]["review.review"] == 3
    assert result["database_counts"]["review.revision"] == 6
    assert result["archived_m1_bars"] == 100
    assert result["archive_chunks"][0]["verified_count"] == 100
    assert result["current_workspace_series_counts"] == {"H1": 240, "M15": 400, "M5": 400, "M1": 1440}


def test_loopback_server_reads_real_schema_and_reports_own_resources(tmp_path):
    data_root = tmp_path / "data"
    seed(data_root, versions=3, review_revisions=2, bar_count=10, progress=False)
    with APIServer(data_root, ROOT, tmp_path / "server") as server:
        workspace = server.client.get("/v1/workspace")
        assert workspace.status_code == 200
        assert workspace.json()["market"]["source_kind"] == "synthetic"
        assert workspace.json()["current_forecast"]["forecast_id"] == "perf-forecast-00002"
        revisions = server.client.get("/v1/reviews?revisions=all&limit=50")
        assert revisions.status_code == 200 and len(revisions.json()["items"]) == 6
        resources = process_resources(server.api_pid)
        assert resources["pid"] == server.api_pid
        assert resources.get("working_set_bytes", 100_000_000) > 10_000_000
