"""Tests for orchestrator.py: the shared state machine api.py and main.py both call.
Every Docker/HTTP/AI touching dependency is mocked -- this suite never shells out and
never hits a real Ditto. Focus: the approval gate, the allowlist re-check, concurrency
refusal, and that decisions/verification are recorded without ever claiming an
automated fix was applied.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import guard  # noqa: E402
import orchestrator as orch  # noqa: E402
import pytest  # noqa: E402
import remediation_planner  # noqa: E402
import resilience_agent  # noqa: E402

DEP_MAP = {"fault_target_recommendations": [
    {"service": "things-search", "fan_in": 1, "fan_out": 1, "calculated_risk": "medium",
     "allowlisted_for_faults": True, "reasoning": "x"},
]}
HEALTHY = {"status": "healthy", "fresh": True}
UNKNOWN_HEALTH = {"status": "unknown", "fresh": False}


@pytest.fixture(autouse=True)
def clean_state():
    orch.reset()
    yield
    orch.reset()


def _mock_common(monkeypatch):
    # Real .env may have AI_ENABLED=true + a real API key -- force it off so this
    # suite never makes a real network call to Gemini (planner_agent.py falls back
    # to its deterministic planner, which is exactly what these tests want to check).
    monkeypatch.setattr(config, "AI_ENABLED", False)
    monkeypatch.setattr(orch, "_load_or_build_dependency_map", lambda: DEP_MAP)
    monkeypatch.setattr(orch.probe, "current_health", lambda: HEALTHY)
    monkeypatch.setattr(orch.guard, "load_baseline", lambda: {"direct_ms": {"p95": 20}})
    monkeypatch.setattr(orch, "_recent_experiments", lambda limit=10: [])


# ---------------------------------------------------------------- generate_plan
def test_generate_plan_sets_status_planned_and_returns_proposal_id(monkeypatch):
    _mock_common(monkeypatch)
    out = orch.generate_plan()
    assert "proposal_id" in out
    assert out["target"] == "things-search"
    status = orch.get_status()
    assert status["status"] == orch.STATUS_PLANNED
    assert status["proposal_id"] == out["proposal_id"]


def test_generate_plan_does_not_overwrite_running_status(monkeypatch):
    _mock_common(monkeypatch)
    with orch._lock:
        orch._state.status = orch.STATUS_RUNNING
    orch.generate_plan()
    assert orch.get_status()["status"] == orch.STATUS_RUNNING


# ---------------------------------------------------------------- authorize_and_run: refusals
async def test_authorize_rejects_unknown_proposal_id(monkeypatch):
    _mock_common(monkeypatch)
    orch.generate_plan()
    with pytest.raises(orch.OrchestratorError, match="unknown or stale"):
        await orch.authorize_and_run("not-the-real-id")


async def test_authorize_rejects_when_already_running(monkeypatch):
    _mock_common(monkeypatch)
    out = orch.generate_plan()
    with orch._lock:
        orch._state.status = orch.STATUS_RUNNING
    with pytest.raises(orch.OrchestratorError, match="already running"):
        await orch.authorize_and_run(out["proposal_id"])


async def test_authorize_rejects_proposal_with_tampered_out_of_allowlist_target(monkeypatch):
    _mock_common(monkeypatch)
    out = orch.generate_plan()
    with orch._lock:
        orch._state.proposal["target"] = "not-a-real-service"
    with pytest.raises(orch.OrchestratorError, match="ALLOWLIST"):
        await orch.authorize_and_run(out["proposal_id"])


async def test_authorize_rejects_real_run_when_health_unknown(monkeypatch):
    _mock_common(monkeypatch)
    out = orch.generate_plan()
    monkeypatch.setattr(orch.probe, "current_health", lambda: UNKNOWN_HEALTH)
    with pytest.raises(orch.OrchestratorError, match="UNKNOWN"):
        await orch.authorize_and_run(out["proposal_id"], dry_run=False)


async def test_authorize_allows_dry_run_even_when_health_unknown(monkeypatch, tmp_path):
    _mock_common(monkeypatch)
    out = orch.generate_plan()
    monkeypatch.setattr(orch.probe, "current_health", lambda: UNKNOWN_HEALTH)
    _patch_execution_layer(monkeypatch, tmp_path)
    result = await orch.authorize_and_run(out["proposal_id"], dry_run=True)
    assert result["record"]["state"] == guard.STATE_COMPLETED


# ---------------------------------------------------------------- authorize_and_run: happy path
def _patch_execution_layer(monkeypatch, tmp_path):
    """Mock everything below orchestrator's own logic: Docker/container resolution,
    guard.run_experiment itself, persistence, and the analysis/remediation modules it
    calls afterward -- so this test only exercises orchestrator.py's own code."""
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(config, "EXPERIMENTS_DIR", str(tmp_path / "experiments"))

    class FakeLock:
        pass

    async def fake_resolve_container(target, dry_run):
        return guard.ContainerRef(name=f"docker-{target}-1", id="abc123")

    acquired = {"n": 0, "released": 0}

    def fake_acquire_lock(target, force=False):
        acquired["n"] += 1
        return FakeLock()

    def fake_release_lock(lock):
        acquired["released"] += 1

    fake_record = {
        "id": "exp-test-orch-0001", "state": guard.STATE_COMPLETED, "fault": "kill",
        "target": "things-search", "aborted": False, "reverted": True, "rollback_verified": True,
        "rollback_error": None, "recovery_s": 1.2, "recovery_outcome": "recovered",
        "baseline": {"direct_ms": {"p95": 20}, "search_ms": {"p95": 20},
                    "direct_error_rate": 0.0, "search_error_rate": 0.0, "stale_p95_s": 1.0,
                    "lost_acked_max": 0, "search_drift_max": 0},
        "during": {"direct_ms": {"p95": 20}, "search_ms": {"p95": 20},
                  "direct_error_rate": 0.0, "search_error_rate": 0.0, "stale_p95_s": 1.0,
                  "lost_acked_max": 0, "search_drift_max": 0},
    }

    async def fake_run_experiment(fault, target, container, th, max_duration, pre_s,
                                  recovery_timeout, fresh_baseline, max_baseline_age_s,
                                  stop_event=None, on_update=None):
        if on_update:
            on_update(fake_record)
        return fake_record

    def fake_save(record):
        path = os.path.join(config.EXPERIMENTS_DIR, f"{record['id']}.json")
        os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return path

    monkeypatch.setattr(orch.guard, "resolve_container", fake_resolve_container)
    monkeypatch.setattr(orch.guard, "acquire_lock", fake_acquire_lock)
    monkeypatch.setattr(orch.guard, "release_lock", fake_release_lock)
    monkeypatch.setattr(orch.guard, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(orch.guard, "save", fake_save)
    return acquired, fake_record


async def test_authorize_and_run_happy_path_ends_done_with_analysis(monkeypatch, tmp_path):
    _mock_common(monkeypatch)
    out = orch.generate_plan()
    acquired, fake_record = _patch_execution_layer(monkeypatch, tmp_path)

    result = await orch.authorize_and_run(out["proposal_id"], dry_run=False)

    assert result["record"]["id"] == fake_record["id"]
    assert result["analysis"]["experiment_id"] == fake_record["id"]
    assert "none_auto_applied" in result["remediation_plan"]
    assert result["remediation_plan"]["none_auto_applied"] is True

    status = orch.get_status()
    assert status["status"] == orch.STATUS_DONE
    assert status["experiment_id"] == fake_record["id"]
    assert acquired["n"] == 1 and acquired["released"] == 1  # lock always released


async def test_authorize_and_run_releases_lock_even_on_failure(monkeypatch, tmp_path):
    _mock_common(monkeypatch)
    out = orch.generate_plan()
    acquired, _ = _patch_execution_layer(monkeypatch, tmp_path)

    async def boom(*a, **kw):
        raise RuntimeError("simulated crash mid-experiment")
    monkeypatch.setattr(orch.guard, "run_experiment", boom)

    with pytest.raises(RuntimeError):
        await orch.authorize_and_run(out["proposal_id"])

    assert acquired["released"] == 1
    assert orch.get_status()["status"] == orch.STATUS_ERROR


# ---------------------------------------------------------------- start_authorized_run (API path)
async def test_start_authorized_run_rejects_bad_proposal_immediately_not_in_background(monkeypatch):
    _mock_common(monkeypatch)
    orch.generate_plan()
    # No `await` anywhere here -- if this didn't raise synchronously, a caller could
    # mistake a background-task failure for success.
    with pytest.raises(orch.OrchestratorError, match="unknown or stale"):
        orch.start_authorized_run("not-the-real-id")


async def test_start_authorized_run_returns_a_task_that_completes_in_background(monkeypatch, tmp_path):
    _mock_common(monkeypatch)
    out = orch.generate_plan()
    _patch_execution_layer(monkeypatch, tmp_path)

    task = orch.start_authorized_run(out["proposal_id"], dry_run=False)
    # Claiming already flipped status to RUNNING synchronously, before the task body
    # (the Docker/guard.py work) has had a chance to run at all.
    assert orch.get_status()["status"] == orch.STATUS_RUNNING

    result = await task
    assert result["record"]["state"] == guard.STATE_COMPLETED
    assert orch.get_status()["status"] == orch.STATUS_DONE


# ---------------------------------------------------------------- request_stop
def test_request_stop_returns_false_when_idle():
    assert orch.request_stop() is False


def test_request_stop_sets_event_when_running():
    ev = asyncio.Event()
    with orch._lock:
        orch._state.status = orch.STATUS_RUNNING
        orch._state.stop_event = ev
    assert orch.request_stop() is True
    assert ev.is_set()


# ---------------------------------------------------------------- remediation decisions
def _seed_plan(tmp_path, monkeypatch, exp_id="exp-rem-0001"):
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path / "reports"))
    record = {
        "id": exp_id, "fault": "kill", "target": "things-search",
        "rollback_error": "netem delay still present",  # -> Category C finding guaranteed
        "baseline": {"direct_ms": {"p95": 20}}, "during": {"direct_ms": {"p95": 20}},
    }
    monkeypatch.setattr(config, "AI_ENABLED", False)
    analysis = resilience_agent.analyze_experiment(record)
    resilience_agent.save_report(analysis)
    plan = remediation_planner.build_plan(analysis)
    remediation_planner.save_plan(plan)
    return exp_id, plan


def test_get_remediation_view_before_any_decision(tmp_path, monkeypatch):
    exp_id, plan = _seed_plan(tmp_path, monkeypatch)
    view = orch.get_remediation_view(exp_id)
    assert view["items"][0]["operator_decision"] is None
    assert view["verification"] is None


def test_decide_remediation_records_approval_without_applying_anything(tmp_path, monkeypatch):
    exp_id, plan = _seed_plan(tmp_path, monkeypatch)
    c_index = next(i for i, it in enumerate(plan["items"]) if it["category"] == "C")
    view = orch.decide_remediation(exp_id, c_index, "approved")
    assert view["items"][c_index]["operator_decision"] == "approved"
    assert view["items"][c_index]["auto_applied"] is False  # never flipped, never applied


def test_decide_remediation_rejects_invalid_decision_value(tmp_path, monkeypatch):
    exp_id, plan = _seed_plan(tmp_path, monkeypatch)
    with pytest.raises(orch.OrchestratorError, match="approved.*rejected"):
        orch.decide_remediation(exp_id, 0, "maybe")


def test_decide_remediation_unknown_experiment_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path / "reports"))
    with pytest.raises(orch.OrchestratorError, match="no remediation"):
        orch.decide_remediation("exp-does-not-exist", 0, "approved")


def test_verify_remediation_reports_live_health_without_claiming_a_fix(tmp_path, monkeypatch):
    exp_id, _ = _seed_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EXPERIMENTS_DIR", str(tmp_path / "experiments"))
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    with open(os.path.join(config.EXPERIMENTS_DIR, f"{exp_id}.json"), "w", encoding="utf-8") as f:
        json.dump({"baseline": {"direct_ms": {"p95": 20}}}, f)
    monkeypatch.setattr(orch.probe, "current_health", lambda: HEALTHY)

    result = orch.verify_remediation(exp_id)
    assert result["verified_healthy_now"] is True
    assert "does not know whether" in result["note"]

    view = orch.get_remediation_view(exp_id)
    assert view["verification"]["verified_healthy_now"] is True
