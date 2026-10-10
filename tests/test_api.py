"""Tests for api.py using FastAPI's TestClient. Every Docker/AI-touching call is
mocked via monkeypatching the SAME module objects api.py imports (api.py does
`import orchestrator as orch`, so patching the `orchestrator` module's attributes is
visible through `api.orch` too -- they're the same module object)."""
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api  # noqa: E402
import config  # noqa: E402
import orchestrator  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def clean_state():
    orchestrator.reset()
    yield
    orchestrator.reset()


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPERIMENTS_DIR", str(tmp_path / "experiments"))
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(config, "METRICS_DIR", str(tmp_path / "metrics"))
    monkeypatch.setattr(config, "AI_ENABLED", False)
    return tmp_path


# ---------------------------------------------------------------- health / baseline / metrics
def test_get_health_returns_probe_output_plus_fleet_context(monkeypatch):
    monkeypatch.setattr(api.probe, "current_health", lambda window_s=10: {"status": "healthy", "fresh": True})
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["num_cars"] == config.NUM_CARS
    assert body["ditto_url"] == config.DITTO_URL


def test_get_thresholds_matches_guard_configuration():
    r = client.get("/api/config/thresholds")
    assert r.status_code == 200
    body = r.json()
    th = api.guard.Thresholds.from_env()
    assert body["max_error_rate"] == th.max_error_rate
    assert body["breach_window"] == th.breach_window


def test_get_baseline_absent_by_default(isolated_dirs, monkeypatch):
    monkeypatch.setattr(config, "BASELINE_FILE", str(isolated_dirs / "no-such-baseline.json"))
    r = client.get("/api/baseline")
    assert r.status_code == 200
    assert r.json() == {"present": False, "baseline": None}


def test_get_metrics_history_empty_when_no_metrics_dir(isolated_dirs):
    r = client.get("/api/metrics/history?window_s=60")
    assert r.status_code == 200
    body = r.json()
    assert body["sample_count"] == 0
    assert body["stale_note"] is not None


def test_get_metrics_history_reads_real_jsonl(isolated_dirs):
    os.makedirs(config.METRICS_DIR, exist_ok=True)
    import time
    now = time.time()
    with open(os.path.join(config.METRICS_DIR, "probe-1.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "direct_ok": True}) + "\n")
    r = client.get("/api/metrics/history?window_s=60")
    assert r.status_code == 200
    assert r.json()["sample_count"] == 1


# ---------------------------------------------------------------- dependencies
def test_get_dependencies_404_when_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    r = client.get("/api/dependencies")
    assert r.status_code == 404


def test_get_dependencies_200_when_present(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with open(tmp_path / "dependency_map.json", "w", encoding="utf-8") as f:
        json.dump({"services": [], "edges": []}, f)
    r = client.get("/api/dependencies")
    assert r.status_code == 200
    assert r.json() == {"services": [], "edges": []}


def test_post_dependencies_refresh_calls_mapper(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    fake_map = {"services": ["things"], "edges": [], "fault_target_recommendations": []}
    monkeypatch.setattr(api.dependency_mapper, "build_dependency_map", lambda: fake_map)
    monkeypatch.setattr(api.dependency_mapper, "save", lambda m: ("dependency_map.json", "dependency_map.md"))
    r = client.post("/api/dependencies/refresh")
    assert r.status_code == 200
    assert r.json() == fake_map


# ---------------------------------------------------------------- orchestrator / agents / run
def test_orchestrator_status_reflects_idle_by_default():
    r = client.get("/api/orchestrator/status")
    assert r.status_code == 200
    assert r.json()["status"] == orchestrator.STATUS_IDLE


def test_post_plan_calls_generate_plan(monkeypatch):
    monkeypatch.setattr(orchestrator, "generate_plan",
                        lambda: {"proposal_id": "abc123", "target": "mongodb", "fault": "kill"})
    r = client.post("/api/agents/plan")
    assert r.status_code == 200
    assert r.json()["proposal_id"] == "abc123"


def test_run_endpoint_must_stay_async_or_create_task_has_no_event_loop():
    """Regression test for a real bug found while building this: this endpoint was
    briefly a plain `def`. FastAPI dispatches a plain `def` route to a worker thread
    with no event loop of its own, so orchestrator.start_authorized_run()'s
    asyncio.create_task() raised "no running event loop" -- AFTER _claim() had
    already flipped the orchestrator to STATUS_RUNNING, wedging it there permanently
    (every future experiment refused with "already running"). Caught via a live
    curl test against a real `uvicorn` process, not by the mocked tests below, which
    is exactly why this cheap structural check exists: those tests patch
    start_authorized_run itself and can't see which thread it was called from."""
    assert inspect.iscoroutinefunction(api.run_experiment)


def test_run_requires_explicit_authorize():
    r = client.post("/api/experiments/run", json={"proposal_id": "x", "authorize": False})
    assert r.status_code == 400
    assert "authorize" in r.json()["detail"]


def test_run_starts_background_task_when_authorized(monkeypatch):
    called = {}

    def fake_start(proposal_id, dry_run=False):
        called["proposal_id"], called["dry_run"] = proposal_id, dry_run

        class _FakeTask:
            pass
        return _FakeTask()
    monkeypatch.setattr(orchestrator, "start_authorized_run", fake_start)
    r = client.post("/api/experiments/run", json={"proposal_id": "abc123", "authorize": True})
    assert r.status_code == 200
    assert r.json() == {"status": "started", "proposal_id": "abc123", "dry_run": False}
    assert called == {"proposal_id": "abc123", "dry_run": False}


def test_run_surfaces_orchestrator_error_as_400(monkeypatch):
    def boom(proposal_id, dry_run=False):
        raise orchestrator.OrchestratorError("unknown or stale proposal_id")
    monkeypatch.setattr(orchestrator, "start_authorized_run", boom)
    r = client.post("/api/experiments/run", json={"proposal_id": "nope", "authorize": True})
    assert r.status_code == 400
    assert "unknown or stale" in r.json()["detail"]


def test_stop_returns_409_when_nothing_running():
    r = client.post("/api/experiments/exp-123/stop")
    assert r.status_code == 409


def test_stop_succeeds_when_matching_experiment_running(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_status",
                        lambda: {"status": orchestrator.STATUS_RUNNING, "experiment_id": "exp-123"})
    monkeypatch.setattr(orchestrator, "request_stop", lambda: True)
    r = client.post("/api/experiments/exp-123/stop")
    assert r.status_code == 200
    assert r.json() == {"status": "stop_requested"}


def test_stop_rejects_mismatched_experiment_id(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_status",
                        lambda: {"status": orchestrator.STATUS_RUNNING, "experiment_id": "exp-OTHER"})
    r = client.post("/api/experiments/exp-123/stop")
    assert r.status_code == 409


# ---------------------------------------------------------------- experiments history
def test_list_experiments_empty(isolated_dirs):
    r = client.get("/api/experiments")
    assert r.status_code == 200
    assert r.json() == []


def test_list_and_get_experiment(isolated_dirs):
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    rec = {"id": "exp-abc", "fault": "kill", "target": "things", "state": "COMPLETED"}
    with open(os.path.join(config.EXPERIMENTS_DIR, "exp-abc.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)

    r = client.get("/api/experiments")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "exp-abc"
    assert r.json()[0]["analyzed"] is False

    r2 = client.get("/api/experiments/exp-abc")
    assert r2.status_code == 200
    body = r2.json()
    assert body["record"]["id"] == "exp-abc"
    assert body["analysis"] is None


def test_list_experiments_distinguishes_never_injected_from_unverified_rollback(isolated_dirs):
    """A pre-injection abort (e.g. a stale baseline refused before anything touched
    Docker) must be distinguishable from a real fault whose rollback couldn't be
    verified -- the frontend renders these very differently (muted "n/a" vs. an
    alarming red "unverified"), so the API must expose `injected` and the reason."""
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    safe_abort = {"id": "exp-safe", "fault": "kill", "target": "things", "state": "ABORTED",
                 "injected": False, "reverted": False, "rollback_verified": False,
                 "error": "baseline.json is stale or missing a timestamp (age=70 min, max allowed=60 min)"}
    real_failure = {"id": "exp-real", "fault": "kill", "target": "things", "state": "FAILED",
                    "injected": True, "reverted": False, "rollback_verified": False,
                    "error": "REVERT FAILED: container did not reach running state"}
    for rec in (safe_abort, real_failure):
        with open(os.path.join(config.EXPERIMENTS_DIR, f"{rec['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)

    by_id = {e["id"]: e for e in client.get("/api/experiments").json()}
    assert by_id["exp-safe"]["injected"] is False
    assert "stale" in by_id["exp-safe"]["error"]
    assert by_id["exp-real"]["injected"] is True
    assert "REVERT FAILED" in by_id["exp-real"]["error"]


def test_get_experiment_404_when_missing(isolated_dirs):
    r = client.get("/api/experiments/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------- insights
def test_analyze_endpoint_builds_and_saves_reports(isolated_dirs):
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    rec = {"id": "exp-xyz", "fault": "kill", "target": "things",
          "baseline": {"direct_ms": {"p95": 20}}, "during": {"direct_ms": {"p95": 20}}}
    with open(os.path.join(config.EXPERIMENTS_DIR, "exp-xyz.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)

    r = client.post("/api/insights/analyze", params={"experiment_id": "exp-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["analysis"]["experiment_id"] == "exp-xyz"
    assert body["remediation_plan"]["none_auto_applied"] is True
    assert os.path.isfile(os.path.join(config.REPORTS_DIR, "experiment_exp-xyz_analysis.json"))


def test_latest_insights_404_when_none(isolated_dirs):
    r = client.get("/api/insights/latest")
    assert r.status_code == 404


def test_latest_insights_returns_most_recent(isolated_dirs):
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    rec = {"id": "exp-latest", "fault": "kill", "target": "things",
          "baseline": {"direct_ms": {"p95": 20}}, "during": {"direct_ms": {"p95": 20}}}
    with open(os.path.join(config.EXPERIMENTS_DIR, "exp-latest.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)
    client.post("/api/insights/analyze", params={"experiment_id": "exp-latest"})

    r = client.get("/api/insights/latest")
    assert r.status_code == 200
    assert r.json()["analysis"]["experiment_id"] == "exp-latest"


# ---------------------------------------------------------------- remediation decisions
def test_approve_and_reject_and_verify_delegate_to_orchestrator(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator, "decide_remediation",
                        lambda eid, idx, decision: calls.append((eid, idx, decision)) or {"ok": True})
    monkeypatch.setattr(orchestrator, "verify_remediation",
                        lambda eid: calls.append(("verify", eid)) or {"ok": True})

    r1 = client.post("/api/remediations/exp-1/0/approve")
    r2 = client.post("/api/remediations/exp-1/1/reject")
    r3 = client.post("/api/remediations/exp-1/verify")

    assert r1.status_code == 200 and r2.status_code == 200 and r3.status_code == 200
    assert ("exp-1", 0, "approved") in calls
    assert ("exp-1", 1, "rejected") in calls
    assert ("verify", "exp-1") in calls


def test_approve_surfaces_orchestrator_error_as_400(monkeypatch):
    def boom(eid, idx, decision):
        raise orchestrator.OrchestratorError("no remediation item")
    monkeypatch.setattr(orchestrator, "decide_remediation", boom)
    r = client.post("/api/remediations/exp-1/99/approve")
    assert r.status_code == 400
