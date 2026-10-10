"""Backend API for the dashboard (frontend/). Thin HTTP layer only -- every endpoint
here either reads existing files/in-memory state directly, or calls orchestrator.py
for anything that touches Docker/Ditto. This file must never reimplement a safety
check, an allowlist, or a kill switch; if a rule needs to exist, it belongs in
guard.py or orchestrator.py so main.py's CLI enforces the exact same rule.

Local-development tool, not a public service: destructive endpoints
(/api/experiments/run, /stop) have no authentication of their own beyond "can reach
this port". config.API_HOST defaults to 127.0.0.1 for that reason -- don't change it
to 0.0.0.0 on an untrusted network without adding real auth first.

Run:  python main.py serve           (preferred -- same env/config loading as the CLI)
  or: uvicorn api:app --reload       (dev convenience; same app object)
"""
from __future__ import annotations

import glob
import json
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import dependency_mapper
import guard
import orchestrator as orch
import probe
import remediation_planner
import resilience_agent

app = FastAPI(title="ditto-chaos API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=config.FRONTEND_ORIGINS,
                   allow_methods=["GET", "POST"], allow_headers=["*"])


@app.exception_handler(orch.OrchestratorError)
async def _orch_error_handler(request, exc: orch.OrchestratorError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(guard.GuardError)
async def _guard_error_handler(request, exc: guard.GuardError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------------------------------------------------------------- health / metrics / baseline
@app.get("/api/health")
def get_health(window_s: float = 10):
    return {**probe.current_health(window_s=window_s), "num_cars": config.NUM_CARS,
           "ditto_url": config.DITTO_URL}


def _read_metrics_history(window_s: float, max_points: int = 400) -> list[dict]:
    try:
        files = sorted(f for f in os.listdir(config.METRICS_DIR)
                       if f.startswith("probe-") and f.endswith(".jsonl"))
    except OSError:
        return []
    if not files:
        return []
    path = os.path.join(config.METRICS_DIR, files[-1])
    max_lines = min(20000, max(50, int(window_s / max(config.PROBE_INTERVAL_S, 0.1)) + 20))
    try:
        lines = probe._tail_lines(path, max_lines)
    except OSError:
        return []
    cutoff = time.time() - window_s
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = r.get("ts")
        if isinstance(ts, (int, float)) and ts >= cutoff:
            rows.append(r)
    if len(rows) > max_points:
        step = len(rows) / max_points
        rows = [rows[int(i * step)] for i in range(max_points)]
    return rows


@app.get("/api/metrics/history")
def get_metrics_history(window_s: float = 600):
    rows = _read_metrics_history(window_s)
    return {"window_s": window_s, "sample_count": len(rows), "rows": rows,
           "stale_note": None if rows else "no recent probe data -- is `python probe.py` running?"}


@app.get("/api/config/thresholds")
def get_thresholds():
    from dataclasses import asdict
    return asdict(guard.Thresholds.from_env())


@app.get("/api/baseline")
def get_baseline():
    baseline = probe.load_baseline()
    if baseline is None:
        return {"present": False, "baseline": None}
    return {"present": True, "baseline": baseline}


# ---------------------------------------------------------------- dependencies
@app.get("/api/dependencies")
def get_dependencies():
    if os.path.isfile("dependency_map.json"):
        with open("dependency_map.json", encoding="utf-8") as f:
            return json.load(f)
    raise HTTPException(404, "no dependency_map.json yet -- POST /api/dependencies/refresh "
                             "or run `python main.py map`")


@app.post("/api/dependencies/refresh")
def refresh_dependencies():
    dep_map = dependency_mapper.build_dependency_map()
    dependency_mapper.save(dep_map)
    return dep_map


# ---------------------------------------------------------------- orchestrator / agents
@app.get("/api/orchestrator/status")
def get_orchestrator_status():
    return orch.get_status()


@app.post("/api/agents/plan")
def post_plan():
    return orch.generate_plan()


class RunRequest(BaseModel):
    proposal_id: str
    authorize: bool = False
    dry_run: bool = False


@app.post("/api/experiments/run")
async def run_experiment(req: RunRequest):
    """Starts a REAL (or, with dry_run, fully synthetic) experiment in the
    background and returns immediately -- poll GET /api/orchestrator/status or
    GET /api/experiments/{id} for progress. `authorize` must be explicitly true:
    this is the one human-in-the-loop checkbox an AI proposal can never tick for
    itself.

    MUST stay `async def`, not `def` -- orchestrator.start_authorized_run() calls
    asyncio.create_task(), which needs an event loop running in THIS thread.
    FastAPI runs plain `def` endpoints in a worker thread pool instead, where
    create_task() raises "no running event loop" (caught the hard way once already;
    see test_api.py's test_run_must_be_callable_from_the_event_loop_thread)."""
    if not req.authorize:
        raise HTTPException(400, "authorize must be true -- a human must explicitly approve "
                                 "running this experiment; an AI proposal cannot self-approve")
    orch.start_authorized_run(req.proposal_id, dry_run=req.dry_run)
    return {"status": "started", "proposal_id": req.proposal_id, "dry_run": req.dry_run}


@app.post("/api/experiments/{experiment_id}/stop")
def stop_experiment(experiment_id: str):
    status = orch.get_status()
    if status["status"] != orch.STATUS_RUNNING:
        raise HTTPException(409, "no experiment is currently running")
    if status.get("experiment_id") not in (experiment_id, None):
        raise HTTPException(409, f"the running experiment is '{status.get('experiment_id')}', not "
                                 f"'{experiment_id}'")
    stopped = orch.request_stop()
    if not stopped:
        raise HTTPException(409, "no experiment is currently running")
    return {"status": "stop_requested"}


@app.post("/api/orchestrator/reset")
def post_reset():
    """Clear the dashboard's in-memory view back to idle after inspecting a
    DONE/ERROR run. Touches nothing real."""
    orch.reset()
    return orch.get_status()


# ---------------------------------------------------------------- experiments (history)
def _experiment_files() -> list[str]:
    return sorted(glob.glob(os.path.join(config.EXPERIMENTS_DIR, "*.json")), reverse=True)


@app.get("/api/experiments")
def list_experiments():
    out = []
    for path in _experiment_files():
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        analysis_path = os.path.join(config.REPORTS_DIR, f"experiment_{rec.get('id')}_analysis.json")
        out.append({
            "id": rec.get("id"), "started_at_utc": rec.get("started_at_utc"),
            "ended_at_utc": rec.get("ended_at_utc"), "fault": rec.get("fault"),
            "target": rec.get("target"), "state": rec.get("state"), "aborted": rec.get("aborted"),
            "injected": rec.get("injected"),
            "detected_s": rec.get("detected_s"), "reverted": rec.get("reverted"),
            "rollback_verified": rec.get("rollback_verified"), "recovery_s": rec.get("recovery_s"),
            "recovery_outcome": rec.get("recovery_outcome"), "error": rec.get("error"),
            "abort_reason": rec.get("abort_reason"),
            "analyzed": os.path.isfile(analysis_path),
        })
    return out


def _load_experiment_or_404(experiment_id: str) -> dict:
    path = os.path.join(config.EXPERIMENTS_DIR, f"{experiment_id}.json")
    if not os.path.isfile(path):
        raise HTTPException(404, f"no saved experiment '{experiment_id}'")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/experiments/{experiment_id}")
def get_experiment(experiment_id: str):
    record = _load_experiment_or_404(experiment_id)
    analysis_path = os.path.join(config.REPORTS_DIR, f"experiment_{experiment_id}_analysis.json")
    analysis = None
    if os.path.isfile(analysis_path):
        with open(analysis_path, encoding="utf-8") as f:
            analysis = json.load(f)
    try:
        remediation = orch.get_remediation_view(experiment_id)
    except orch.OrchestratorError:
        remediation = None
    return {"record": record, "analysis": analysis, "remediation_plan": remediation}


@app.get("/api/experiments/{experiment_id}/report")
def get_experiment_report(experiment_id: str):
    return get_experiment(experiment_id)


# ---------------------------------------------------------------- insights / AI analysis
@app.post("/api/insights/analyze")
def post_analyze(experiment_id: str):
    record = _load_experiment_or_404(experiment_id)
    dep_map = None
    if os.path.isfile("dependency_map.json"):
        with open("dependency_map.json", encoding="utf-8") as f:
            dep_map = json.load(f)
    analysis = resilience_agent.analyze_experiment(record, dep_map)
    resilience_agent.save_report(analysis)
    plan = remediation_planner.build_plan(analysis)
    remediation_planner.save_plan(plan)
    return {"analysis": analysis, "remediation_plan": plan}


@app.get("/api/insights/latest")
def get_latest_insights():
    pattern = os.path.join(config.REPORTS_DIR, "experiment_*_analysis.json")
    files = glob.glob(pattern)
    if not files:
        raise HTTPException(404, "no analysis reports yet")
    latest = max(files, key=os.path.getmtime)
    with open(latest, encoding="utf-8") as f:
        analysis = json.load(f)
    try:
        remediation = orch.get_remediation_view(analysis["experiment_id"])
    except orch.OrchestratorError:
        remediation = None
    return {"analysis": analysis, "remediation_plan": remediation}


# ---------------------------------------------------------------- remediation decisions
@app.post("/api/remediations/{experiment_id}/{item_index}/approve")
def approve_remediation(experiment_id: str, item_index: int):
    return orch.decide_remediation(experiment_id, item_index, "approved")


@app.post("/api/remediations/{experiment_id}/{item_index}/reject")
def reject_remediation(experiment_id: str, item_index: int):
    return orch.decide_remediation(experiment_id, item_index, "rejected")


@app.post("/api/remediations/{experiment_id}/verify")
def verify_remediation(experiment_id: str):
    return orch.verify_remediation(experiment_id)
