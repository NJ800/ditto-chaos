"""Backend API for the dashboard (frontend/). Thin HTTP layer only -- every endpoint
either reads state, or calls orchestrator.py / controller.py for anything that touches
Docker or Ditto. No safety rule lives here: allowlists, limits, locks and rollback are
scenario.py / guard.py, so the CLI enforces exactly the same ones.

Authentication: when API_TOKEN is set, every state-changing route requires the
`X-API-Token` header. When it is not set the API relies on being bound to loopback
(`main.py serve` refuses a non-loopback host without a token).

Liveness (/api/livez) only says the process answers. Readiness (/api/readyz) says whether
it can safely be used: knowledge store reachable, monitor loop ticking, no recovery
lockout. Neither reports Ditto's health -- that is /api/health.

Run:  python main.py serve
"""
from __future__ import annotations

import asyncio
import glob
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import activity
import agents
import config
import dependency_mapper
import faults
import guard
import learning
import orchestrator as orch
import probe
import remediation_planner
import resilience_agent
import scenario
from scenario import Scenario as ScenarioModel
import store
from controller import ModeError, controller
from events import bus
from monitor import monitor, read_rows

API_VERSION = "2.0"
_started = {"lifespan": False, "recovery": None, "store_sync": None, "at": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _started.update(lifespan=True, at=time.time())
    try:
        _started["store_sync"] = await asyncio.to_thread(store.sync_from_files)
    except store.StoreError as e:
        _started["store_sync"] = {"error": str(e)}
        print(f"[api] knowledge store unavailable at startup: {e}", flush=True)
    # A previous process may have died with faults applied: replay its recovery journal
    # before anything else can run. Fails closed into the lockout.
    if scenario.list_journals():
        _started["recovery"] = await scenario.recover_orphans()
        r = _started["recovery"]
        print(f"[api] startup recovery: {len(r['recovered'])} fault(s) cleaned up, "
              f"{len(r['failed'])} could not be verified", flush=True)
    monitor.start()
    try:
        yield
    finally:
        await controller.shutdown()
        await orch.shutdown()
        await monitor.stop()


app = FastAPI(title="ditto-chaos API", version=API_VERSION, lifespan=lifespan,
              description="Chaos-engineering control plane for a local Eclipse Ditto sandbox.")

OPEN_PATHS = {"/api/livez", "/api/readyz"}      # probes for a process supervisor: no data in them
STREAM_TICKET_TTL_S = 30.0
_stream_tickets: dict[str, float] = {}


def _token_ok(supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(supplied, config.API_TOKEN)


@app.middleware("http")
async def _require_token(request: Request, call_next):
    """With API_TOKEN set, EVERY /api route needs it -- reads included, since experiment
    records, commands and the audit log are not public. POST routes also check it through
    the `operator` dependency. The event stream is the one exception: a browser EventSource
    cannot send headers, so it presents a single-use ticket instead (see /api/stream/ticket)."""
    path = request.url.path
    if (config.API_TOKEN and request.method == "GET" and path.startswith("/api/")
            and path not in OPEN_PATHS and path != "/api/stream"):
        if not _token_ok(request.headers.get("x-api-token")):
            return _error(401, "missing or invalid X-API-Token", "unauthorized")
    return await call_next(request)


# Added after the token check so CORS is the OUTERMOST layer: a 401 then still carries the
# CORS headers and the dashboard can show "not authorized" instead of an opaque network error.
app.add_middleware(CORSMiddleware, allow_origins=config.FRONTEND_ORIGINS,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-API-Token", "X-Operator"])


def _error(status: int, detail: str, code: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code, **extra})


@app.exception_handler(orch.OrchestratorError)
async def _orch_error_handler(request, exc: orch.OrchestratorError):
    if isinstance(exc, orch.OrchestratorConflict):
        return _error(409, str(exc), "conflict")
    return _error(400, str(exc), "refused")


@app.exception_handler(guard.GuardError)
async def _guard_error_handler(request, exc: guard.GuardError):
    return _error(400, str(exc), "guard_error")


@app.exception_handler(scenario.ScenarioRejected)
async def _rejected_handler(request, exc: scenario.ScenarioRejected):
    status = {"recovery_required": 423, "locked": 409, "unauthorized": 403}.get(exc.code, 422)
    return _error(status, str(exc), exc.code, errors=exc.errors)


@app.exception_handler(ModeError)
async def _mode_handler(request, exc: ModeError):
    return _error(403, str(exc), "mode_forbids")


@app.exception_handler(store.StoreError)
async def _store_handler(request, exc: store.StoreError):
    return _error(503, str(exc), "store_unavailable")


def operator(x_api_token: str | None = Header(default=None),
             x_operator: str | None = Header(default=None)) -> str:
    """Dependency for every state-changing route. Returns the actor name for the audit log."""
    if config.API_TOKEN:
        if not x_api_token or not hmac.compare_digest(x_api_token, config.API_TOKEN):
            raise HTTPException(401, "missing or invalid X-API-Token")
    return (x_operator or "operator")[:60]


# ---------------------------------------------------------------- liveness / readiness
@app.get("/api/livez", tags=["system"])
def livez():
    return {"status": "alive", "version": API_VERSION, "software_version": config.SOFTWARE_VERSION}


@app.get("/api/readyz", tags=["system"])
def readyz():
    st = store.ping()
    mon = monitor.status()
    lock = scenario.read_lockout()
    checks = {
        "knowledge_store": {"ok": st["available"], "detail": st["error"] or f"{st['experiments']} experiments indexed"},
        "monitor_loop": {"ok": mon["loop_running"] or not _started["lifespan"],
                         "detail": f"last tick {mon['loop_age_s']}s ago" if mon["loop_age_s"] is not None
                         else "not started"},
        "no_recovery_lockout": {"ok": lock is None, "detail": "clear" if lock is None else lock.get("reason")},
    }
    ready = all(c["ok"] for c in checks.values())
    body = {"ready": ready, "checks": checks, "monitoring": mon,
            "startup_recovery": _started["recovery"], "store_sync": _started["store_sync"]}
    return JSONResponse(status_code=200 if ready else 503, content=body)


# ---------------------------------------------------------------- health / metrics / baseline
@app.get("/api/health", tags=["monitoring"])
def get_health(window_s: float = 10):
    return {**probe.current_health(window_s=window_s), "num_cars": config.NUM_CARS,
            "ditto_url": config.DITTO_URL}


def _read_metrics_history(window_s: float, max_points: int = 400) -> list[dict]:
    return read_rows(window_s, max_points)


@app.get("/api/metrics/history", tags=["monitoring"])
def get_metrics_history(window_s: float = 600):
    window_s = max(10.0, min(float(window_s), 86400.0))
    rows = _read_metrics_history(window_s)
    return {"window_s": window_s, "sample_count": len(rows), "rows": rows,
            "stale_note": None if rows else "no recent probe data -- is `python probe.py` running?"}


@app.get("/api/config/thresholds", tags=["monitoring"])
def get_thresholds():
    return asdict(guard.Thresholds.from_env())


@app.get("/api/baseline", tags=["monitoring"])
def get_baseline():
    baseline = probe.load_baseline()
    if baseline is None:
        return {"present": False, "baseline": None}
    return {"present": True, "baseline": baseline}


def _status_view() -> dict:
    return {**orch.get_status(), "recovery_state": agents.recovery_state(
        orch.get_status().get("record"), orch.get_status().get("stop_requested", False))}


@app.get("/api/snapshot", tags=["monitoring"])
def get_snapshot(window_s: float = 600):
    """Everything the dashboard needs before it attaches to /api/stream. `seq`/`boot`
    are read FIRST, so replaying the stream from `seq` cannot miss an event that the
    rest of this snapshot does not already reflect."""
    seq, boot = bus.seq, bus.boot
    return {"seq": seq, "boot": boot, "server_time": time.time(),
            **monitor.snapshot(max(10.0, min(float(window_s), 3600.0))),
            "baseline": probe.load_baseline(), "thresholds": asdict(guard.Thresholds.from_env()),
            "baseline_max_age_s": guard.MAX_BASELINE_AGE_S_DEFAULT,
            "orchestrator": _status_view(), "controller": controller.status(),
            "agents": agents.coordinator.status()}


@app.post("/api/stream/ticket", tags=["monitoring"])
def stream_ticket(actor: str = Depends(operator)):
    """A single-use, 30-second ticket for opening /api/stream. Exists so the long-lived
    API token never has to be put in a URL (where it would end up in access logs)."""
    now = time.time()
    for t in [t for t, exp in _stream_tickets.items() if exp < now]:
        _stream_tickets.pop(t, None)
    ticket = secrets.token_urlsafe(24)
    _stream_tickets[ticket] = now + STREAM_TICKET_TTL_S
    return {"ticket": ticket, "expires_in_s": STREAM_TICKET_TTL_S}


@app.get("/api/stream", tags=["monitoring"])
async def stream(request: Request, last_seq: int | None = None, boot: str | None = None,
                 max_events: int | None = None, ticket: str | None = None):
    """Server-Sent Events. Each message is one JSON event {v, boot, seq, ts, type, data}.
    Reconnect with the last seq (and boot) you saw to replay what you missed; a `resync`
    event means: re-fetch /api/snapshot. With API_TOKEN set, pass `ticket` from
    POST /api/stream/ticket (or the X-API-Token header)."""
    if config.API_TOKEN and not _token_ok(request.headers.get("x-api-token")):
        expires = _stream_tickets.pop(ticket, None) if ticket else None      # single use
        if expires is None or expires < time.time():
            raise HTTPException(401, "missing, used or expired stream ticket")
    header = request.headers.get("last-event-id")
    if last_seq is None and header and header.isdigit():
        last_seq = int(header)

    async def gen():
        sent = 0
        async for event in bus.stream(last_seq, boot):
            yield f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n"
            sent += 1
            if max_events is not None and sent >= max_events:
                return
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- dependencies
@app.get("/api/dependencies", tags=["dependencies"])
def get_dependencies():
    if os.path.isfile("dependency_map.json"):
        try:
            with open("dependency_map.json", encoding="utf-8") as f:
                return json.load(f)
        except ValueError as e:
            raise HTTPException(500, f"dependency_map.json is not valid JSON: {e}")
    raise HTTPException(404, "no dependency_map.json yet -- POST /api/dependencies/refresh "
                             "or run `python main.py map`")


@app.post("/api/dependencies/refresh", tags=["dependencies"])
def refresh_dependencies(actor: str = Depends(operator)):
    dep_map = dependency_mapper.build_dependency_map()
    dependency_mapper.save(dep_map)
    return dep_map


@app.get("/api/risk", tags=["dependencies"])
def get_risk():
    """The risk agent's view (blast radius, untested services). Deterministic, read-only."""
    ctx = orch.gather_context()
    return agents.RiskAgent().run(ctx)


# ---------------------------------------------------------------- faults / scenarios
_env_cache: dict = {"at": 0.0, "report": None}


@app.get("/api/faults", tags=["scenarios"])
async def get_faults():
    if time.time() - _env_cache["at"] > 30:
        _env_cache.update(at=time.time(), report=await faults.environment_report())
    return {"faults": faults.catalog(), "environment": _env_cache["report"],
            "targets": sorted(guard.ALLOWLIST), "limits": scenario.limits_from_config(),
            "tested_cross_target_category_pairs": sorted(sorted(p) for p in scenario.TESTED_CATEGORY_PAIRS),
            "same_target_group_pairs": sorted(sorted(p) for p in scenario.SAME_TARGET_GROUP_PAIRS)}


@app.post("/api/scenarios/validate", tags=["scenarios"])
def validate_scenario(scn: scenario.Scenario):
    """Preview. Pure policy check plus the commands that WOULD run. Touches nothing."""
    ctx_dep = orch._load_or_build_dependency_map() if os.path.isfile("dependency_map.json") else None
    report = scenario.validate(scn, ctx_dep)
    try:
        history = store.history()
    except store.StoreError:
        history = []
    report["prediction"] = learning.predict([a.model_dump() for a in scn.actions], history) \
        if report["ok"] else None
    return report


@app.post("/api/scenarios/preflight", tags=["scenarios"])
async def preflight_scenario(scn: scenario.Scenario):
    """Read-only check of the real containers. Injects nothing."""
    dep = orch._load_or_build_dependency_map() if os.path.isfile("dependency_map.json") else None
    return await scenario.preflight_check(scn, dep)


@app.post("/api/plan", tags=["scenarios"])
async def post_scenario_plan(actor: str = Depends(operator)):
    """Run the planning agents (risk -> fault injection). Read-only; proposes, never runs."""
    policy = controller.policy() if controller.mode == "autonomous_sandbox" else None
    return await orch.generate_scenario_plan(policy=policy, mode=controller.mode)


class SelectRequest(BaseModel):
    signature: str = Field(max_length=300)


@app.get("/api/plan/candidates", tags=["scenarios"])
def get_plan_candidates():
    """Ranked candidate experiments with risk, hypothesis, reason, expected impact and the
    stored evidence behind each. Read-only; every candidate is validated against the fault
    registry, the allowlist, parameter bounds and the policy."""
    policy = controller.policy() if controller.mode == "autonomous_sandbox" else None
    return orch.rank_candidates_view(policy)


@app.post("/api/plan/select", tags=["scenarios"])
def select_plan_candidate(req: SelectRequest, actor: str = Depends(operator)):
    """Make one ranked candidate the current proposal. Selecting authorizes nothing and
    starts nothing; running it still needs POST /api/scenarios/run with authorize: true."""
    return orch.select_candidate(req.signature, controller.mode)


@app.get("/api/plan", tags=["scenarios"])
def get_scenario_plan():
    proposal = orch.get_scenario_proposal()
    if proposal is None:
        raise HTTPException(404, "no proposal yet -- POST /api/plan")
    return proposal


class ScenarioRunRequest(BaseModel):
    authorize: bool = False
    dry_run: bool = False
    proposal_id: str | None = None
    scenario: ScenarioModel | None = None
    request_id: str | None = Field(default=None, max_length=80)


@app.post("/api/scenarios/run", tags=["scenarios"])
async def run_scenario(req: ScenarioRunRequest, actor: str = Depends(operator)):
    """Start a REAL scenario in the background. `authorize` must be true and comes from
    an operator action; a proposal, a preview or a model response never sets it."""
    if not req.authorize and not req.dry_run:
        raise HTTPException(400, "authorize must be true -- an operator must explicitly approve this run")
    proposal = None
    if req.proposal_id:
        proposal = orch.get_scenario_proposal()
        if proposal is None or proposal.get("proposal_id") != req.proposal_id:
            raise HTTPException(409, "unknown or stale proposal_id -- generate a new plan")
        scn = scenario.Scenario(**proposal["scenario"])
    elif req.scenario is not None:
        scn = req.scenario
    else:
        raise HTTPException(400, "provide proposal_id or scenario")
    if req.dry_run:
        # No authorization object is created: a dry run cannot mutate, in any mode.
        started = orch.start_scenario(scn, None, proposal=proposal, mode=controller.mode,
                                      request_id=req.request_id, dry_run=True)
    else:
        auth = controller.operator_authorization(actor, custom=proposal is None)
        started = orch.start_scenario(scn, auth, proposal=proposal, mode=controller.mode,
                                      source="proposal" if proposal else "operator", request_id=req.request_id)
    return {"status": "started", "experiment_id": started["experiment_id"], "dry_run": req.dry_run,
            "signature": started["signature"], "duplicate": started["duplicate"]}


@app.post("/api/stop", tags=["scenarios"])
def emergency_stop(actor: str = Depends(operator)):
    """Global emergency stop: the running experiment rolls back every active fault."""
    if not orch.request_stop():
        raise HTTPException(409, "no experiment is currently running")
    try:
        store.audit(actor, "emergency_stop", {"experiment_id": orch.get_status().get("experiment_id")})
    except store.StoreError:
        pass
    return {"status": "stop_requested", "experiment_id": orch.get_status().get("experiment_id")}


# ---------------------------------------------------------------- controller / agents
class ModeRequest(BaseModel):
    mode: Literal["observe", "recommend", "manual", "autonomous_sandbox"]


class StopLoopRequest(BaseModel):
    stop_experiment: bool = False


@app.get("/api/controller", tags=["controller"])
def get_controller():
    return controller.status()


@app.get("/api/controller/sandbox", tags=["controller"])
async def get_sandbox():
    checks = await controller.sandbox_checks()
    return {"ready": all(c["ok"] for c in checks), "checks": checks}


@app.post("/api/controller/mode", tags=["controller"])
async def set_mode(req: ModeRequest, actor: str = Depends(operator)):
    return await controller.set_mode(req.mode, actor)


@app.post("/api/controller/autonomous/start", tags=["controller"])
async def start_autonomous(actor: str = Depends(operator)):
    return await controller.start_autonomous(actor)


@app.post("/api/controller/autonomous/stop", tags=["controller"])
async def stop_autonomous(req: StopLoopRequest, actor: str = Depends(operator)):
    return await controller.stop_autonomous(f"stopped by {actor}", req.stop_experiment)


@app.get("/api/agents", tags=["agents"])
def get_agents():
    return {"agents": agents.coordinator.status(),
            "pipelines": {"plan": list(agents.Coordinator.PLAN), "post_run": list(agents.Coordinator.POST_RUN)}}


@app.get("/api/agents/runs", tags=["agents"])
def get_agent_runs(experiment_id: str | None = None, agent: str | None = None, limit: int = 50):
    return store.agent_runs(experiment_id, agent, max(1, min(limit, 200)))


# ---------------------------------------------------------------- recovery
class ResolveRequest(BaseModel):
    force: bool = False
    note: str = Field(default="", max_length=500)


@app.get("/api/recovery", tags=["recovery"])
def get_recovery():
    st = orch.get_status()
    return {"lockout": scenario.read_lockout(), "journals": scenario.list_journals(),
            "recovery_state": agents.recovery_state(st.get("record"), st.get("stop_requested", False)),
            "states": list(agents.RECOVERY_STATES)}


@app.post("/api/recovery/resolve", tags=["recovery"])
async def resolve_recovery(req: ResolveRequest, actor: str = Depends(operator)):
    return await orch.resolve_lockout(actor, req.force, req.note)


# ---------------------------------------------------------------- legacy single-fault path
@app.get("/api/orchestrator/status", tags=["orchestrator"])
def get_orchestrator_status():
    return _status_view()


@app.post("/api/agents/plan", tags=["orchestrator"])
def post_plan(actor: str = Depends(operator)):
    return orch.generate_plan()


class RunRequest(BaseModel):
    proposal_id: str
    authorize: bool = False
    dry_run: bool = False


@app.post("/api/experiments/run", tags=["orchestrator"])
async def run_experiment(req: RunRequest, actor: str = Depends(operator)):
    """Legacy single-fault run (schema 1.0 proposal from /api/agents/plan).

    MUST stay `async def`: orchestrator.start_authorized_run() calls
    asyncio.create_task(), which needs the event loop of THIS thread."""
    if not req.authorize:
        raise HTTPException(400, "authorize must be true -- a human must explicitly approve "
                                 "running this experiment; an AI proposal cannot self-approve")
    if not req.dry_run:
        controller.operator_authorization(actor, custom=False)   # observe mode refuses here
        lock = scenario.read_lockout()
        if lock is not None:
            raise scenario.ScenarioRejected(f"mutation is disabled: recovery is required "
                                            f"({lock.get('reason')})", code="recovery_required")
    orch.start_authorized_run(req.proposal_id, dry_run=req.dry_run)
    return {"status": "started", "proposal_id": req.proposal_id, "dry_run": req.dry_run}


@app.post("/api/experiments/{experiment_id}/stop", tags=["orchestrator"])
def stop_experiment(experiment_id: str, actor: str = Depends(operator)):
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


@app.post("/api/orchestrator/reset", tags=["orchestrator"])
def post_reset(actor: str = Depends(operator)):
    """Clear the in-memory view back to idle after inspecting a finished run."""
    if orch.get_status()["status"] in (orch.STATUS_RUNNING, orch.STATUS_ANALYZING):
        raise HTTPException(409, "an experiment is still running; stop it first")
    orch.reset()
    return _status_view()


# ---------------------------------------------------------------- experiments (history)
def _experiment_files() -> list[str]:
    return sorted(glob.glob(os.path.join(config.EXPERIMENTS_DIR, "*.json")), reverse=True)


@app.get("/api/experiments", tags=["history"])
def list_experiments():
    """Every saved experiment of either schema. A file that cannot be read is listed in
    `unreadable` (via the X-Unreadable-Count header) instead of crashing the listing."""
    out, bad = [], 0
    for path in _experiment_files():
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
            if not isinstance(rec, dict):
                raise ValueError("not an object")
            case = learning.to_case(rec)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            bad += 1
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
            "kind": case["kind"], "signature": case["signature"], "faults": case["faults"],
            "targets": case["targets"], "simulated": case["simulated"], "evidence": case["evidence"],
            "outcome_class": case["outcome_class"], "symptoms": case["symptoms"],
            "mode": rec.get("mode"), "source": rec.get("source"),
            "schema_version": rec.get("schema_version"),
            "validation_verdict": (rec.get("validation") or {}).get("verdict"),
            "recovery_state": rec.get("recovery_state") or agents.recovery_state(rec),
        })
    return JSONResponse(content=out, headers={"X-Unreadable-Count": str(bad)})


def _load_experiment_or_404(experiment_id: str) -> dict:
    if not experiment_id or any(c in experiment_id for c in "/\\") or ".." in experiment_id:
        raise HTTPException(400, "invalid experiment id")
    path = os.path.join(config.EXPERIMENTS_DIR, f"{experiment_id}.json")
    if not os.path.isfile(path):
        raise HTTPException(404, f"no saved experiment '{experiment_id}'")
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as e:
        raise HTTPException(422, f"experiment '{experiment_id}' exists but cannot be read: {e}")
    if not isinstance(rec, dict):
        raise HTTPException(422, f"experiment '{experiment_id}' is not a valid record")
    return rec


def _optional(fn, default=None):
    try:
        return fn()
    except store.StoreError:
        return default


@app.get("/api/experiments/{experiment_id}", tags=["history"])
def get_experiment(experiment_id: str):
    record = _load_experiment_or_404(experiment_id)
    analysis_path = os.path.join(config.REPORTS_DIR, f"experiment_{experiment_id}_analysis.json")
    analysis = None
    if os.path.isfile(analysis_path):
        try:
            with open(analysis_path, encoding="utf-8") as f:
                analysis = json.load(f)
        except (OSError, ValueError):
            analysis = None
    try:
        remediation = orch.get_remediation_view(experiment_id)
    except (orch.OrchestratorError, OSError, ValueError, KeyError):
        remediation = None
    return {"record": record, "analysis": analysis, "remediation_plan": remediation,
            "effects": learning.observe(record),
            "learning": _optional(lambda: store.get_learning(experiment_id)),
            "recommendations": _optional(
                lambda: [r for r in store.recommendations() if r["experiment_id"] == experiment_id], []),
            "agent_runs": _optional(lambda: [{k: v for k, v in r.items() if k != "output"}
                                             for r in store.agent_runs(experiment_id)], []),
            "timeline": _optional(lambda: store.timeline(experiment_id), []),
            "followups": _optional(lambda: store.followups(experiment_id), [])}


@app.get("/api/experiments/{experiment_id}/report", tags=["history"])
def get_experiment_report(experiment_id: str):
    return get_experiment(experiment_id)


class ExperimentEvent(BaseModel):
    seq: int
    ts: str
    t_s: float
    state: str | None = None
    kind: Literal["info", "command", "health", "decision", "warning", "error"]
    message: str
    detail: dict = Field(default_factory=dict)


class ExperimentEventsResponse(BaseModel):
    experiment_id: str
    state: str | None
    running: bool
    terminal: bool
    dry_run: bool
    last_seq: int
    events: list[ExperimentEvent]


TERMINAL_STATES = {"COMPLETED", "ABORTED", "FAILED", "RECOVERY_TIMEOUT", "RECOVERY_REQUIRED"}


@app.get("/api/experiments/{experiment_id}/events", tags=["history"], response_model=ExperimentEventsResponse)
def get_experiment_events(experiment_id: str,
                          after_seq: int = Query(0, ge=0, description="return only events with seq greater than this")):
    """Activity log, incrementally. Works for the running experiment (from memory) and for
    saved records. A record written before the activity log existed returns no events."""
    st = orch.get_status()
    live = st.get("record") if st.get("experiment_id") == experiment_id else None
    running = live is not None and st["status"] == orch.STATUS_RUNNING
    record = live if running else None
    if record is None:
        try:
            record = _load_experiment_or_404(experiment_id)
        except HTTPException:
            if live is None:
                raise
            record = live          # finished in memory, not yet on disk
    events = activity.events_of(record)
    state = record.get("state")
    return {"experiment_id": experiment_id, "state": state, "running": running,
            "terminal": (not running) and (state in TERMINAL_STATES or live is None),
            "dry_run": bool(record.get("dry_run") or record.get("simulated")),
            "last_seq": events[-1]["seq"] if events else 0,
            "events": activity.events_after(record, after_seq)}


class RerunRequest(BaseModel):
    recommendation_index: int = Field(ge=0)
    authorize: bool = False
    confirm_applied: bool = False
    note: str = Field(default="", max_length=500)
    request_id: str | None = Field(default=None, max_length=80)


@app.get("/api/experiments/{experiment_id}/rerun-plan", tags=["insights"])
def get_rerun_plan(experiment_id: str, recommendation_index: int = Query(ge=0)):
    """Read-only: whether the original scenario can be repeated as it was, and if not, why."""
    _load_experiment_or_404(experiment_id)
    return orch.plan_rerun(experiment_id, recommendation_index)


@app.post("/api/experiments/{experiment_id}/rerun", tags=["insights"])
async def rerun_experiment(experiment_id: str, req: RerunRequest, actor: str = Depends(operator)):
    """Re-run an experiment after a remediation was applied by hand. Needs BOTH an explicit
    authorization and the operator's confirmation that the change was really made. The
    follow-up is linked to the original and to the recommendation, and compared with it."""
    _load_experiment_or_404(experiment_id)
    if not req.authorize:
        raise HTTPException(400, "authorize must be true -- an operator must explicitly approve this run")
    if not req.confirm_applied:
        raise HTTPException(400, "confirm_applied must be true -- confirm that the remediation was actually "
                                 "applied; an accepted recommendation is not evidence that anything changed")
    # A re-run repeats a scenario the platform already ran, so it is allowed wherever a
    # proposed scenario is (not in observe mode).
    auth = controller.operator_authorization(actor, custom=False)
    started = orch.start_rerun(experiment_id, req.recommendation_index, auth, confirmed_applied=True,
                               actor=actor, note=req.note, mode=controller.mode, request_id=req.request_id)
    return {"status": "started", "experiment_id": started["experiment_id"], "original_id": experiment_id,
            "recommendation_index": req.recommendation_index, "duplicate": started["duplicate"]}


@app.get("/api/followups", tags=["insights"])
def get_followups(experiment_id: str | None = None):
    """Persisted links between original experiments, remediations and follow-up runs,
    with each before/after comparison and verification result."""
    return store.followups(experiment_id)


@app.get("/api/experiments/{experiment_id}/timeline", tags=["history"])
def get_experiment_timeline(experiment_id: str):
    return store.timeline(experiment_id)


# ---------------------------------------------------------------- insights / learning
@app.post("/api/insights/analyze", tags=["insights"])
def post_analyze(experiment_id: str, actor: str = Depends(operator)):
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


@app.get("/api/insights/latest", tags=["insights"])
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


@app.get("/api/learning", tags=["insights"])
def get_learning():
    """What has been learned, as counts: patterns per scenario, the current candidate
    ranking with its score components, and the per-experiment learning records."""
    store.sync_from_files()
    history = store.history()
    policy = controller.policy() if controller.mode == "autonomous_sandbox" else None
    ranked = learning.rank_candidates(
        orch._load_or_build_dependency_map() if os.path.isfile("dependency_map.json") else None, history,
        (policy or {}).get("allowed_faults"), (policy or {}).get("allowed_targets"), policy,
        store.pending_validation_signatures())
    records = store.learning_records(40)
    evaluated = [r for r in records if r.get("comparison", {}).get("evaluated")]
    return {"summary": learning.summarize(history), "ranking": ranked[:25], "records": records,
            "prediction_accuracy": {
                "evaluated": len(evaluated),
                "outcome_matched": sum(1 for r in evaluated if r["comparison"].get("outcome_match")),
                "note": "counts only; shown as n of m, never as a percentage of a tiny sample"},
            "policy_scope": policy}


class RecStatusRequest(BaseModel):
    status: Literal["accepted", "implemented", "rejected", "proposed"]
    note: str = Field(default="", max_length=500)


@app.get("/api/recommendations", tags=["insights"])
def get_recommendations(status: str | None = None):
    return store.recommendations(status)


@app.post("/api/recommendations/{experiment_id}/{item_index}/status", tags=["insights"])
def set_recommendation_status(experiment_id: str, item_index: int, req: RecStatusRequest,
                              actor: str = Depends(operator)):
    """Record an operator decision. 'implemented' means the operator applied the change
    OUTSIDE this tool; the next run of the same scenario then verifies it. Nothing here
    applies a fix."""
    if store.get_recommendation(experiment_id, item_index) is None:
        raise HTTPException(404, f"no recommendation {item_index} for experiment '{experiment_id}'")
    store.set_recommendation_status(experiment_id, item_index, req.status, None,
                                    req.note or f"set by {actor}")
    store.audit(actor, "recommendation_status", {"experiment_id": experiment_id, "index": item_index,
                                                 "status": req.status, "note": req.note})
    return store.get_recommendation(experiment_id, item_index)


@app.get("/api/audit", tags=["system"])
def get_audit(limit: int = 100):
    return store.audit_log(max(1, min(limit, 500)))


# ---------------------------------------------------------------- remediation decisions (legacy)
def _mirror_decision(experiment_id: str, item_index: int, status: str, actor: str) -> None:
    try:
        if store.get_recommendation(experiment_id, item_index) is not None:
            store.set_recommendation_status(experiment_id, item_index, status, None, f"set by {actor}")
    except store.StoreError:
        pass


@app.post("/api/remediations/{experiment_id}/{item_index}/approve", tags=["insights"])
def approve_remediation(experiment_id: str, item_index: int, actor: str = Depends(operator)):
    view = orch.decide_remediation(experiment_id, item_index, "approved")
    _mirror_decision(experiment_id, item_index, "accepted", actor)
    return view


@app.post("/api/remediations/{experiment_id}/{item_index}/reject", tags=["insights"])
def reject_remediation(experiment_id: str, item_index: int, actor: str = Depends(operator)):
    view = orch.decide_remediation(experiment_id, item_index, "rejected")
    _mirror_decision(experiment_id, item_index, "rejected", actor)
    return view


@app.post("/api/remediations/{experiment_id}/verify", tags=["insights"])
def verify_remediation(experiment_id: str, actor: str = Depends(operator)):
    return orch.verify_remediation(experiment_id)


# ---------------------------------------------------------------- built dashboard (production)
# `npm run build` writes frontend/dist. When it exists, this process serves it, so one
# port carries both the dashboard and the API and no CORS or second server is needed.
# Registered LAST: every /api route above wins, and unknown /api paths stay JSON 404s.
FRONTEND_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")


def dashboard_built() -> bool:
    return os.path.isfile(os.path.join(FRONTEND_DIST, "index.html"))


@app.get("/{path:path}", include_in_schema=False)
def serve_dashboard(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, f"no such API route: /{path}")
    if not dashboard_built():
        raise HTTPException(404, "the dashboard has not been built: run `npm run build` in frontend/ "
                                 "(or use the dev server on http://localhost:5173)")
    root = os.path.realpath(FRONTEND_DIST)
    candidate = os.path.realpath(os.path.join(root, path))
    inside = candidate == root or candidate.startswith(root + os.sep)
    if path and inside and os.path.isfile(candidate):
        # Hashed asset names never change content, so they can be cached hard.
        cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
        return FileResponse(candidate, headers={"Cache-Control": cache})
    if path.startswith("assets/"):
        raise HTTPException(404, f"no such asset: /{path}")
    # Any other path is a client-side route: the single-page app handles it.
    return FileResponse(os.path.join(root, "index.html"), headers={"Cache-Control": "no-cache"})
