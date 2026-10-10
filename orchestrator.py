"""Orchestrator: the ONE place that turns an AI proposal into a real, running
experiment. api.py (the dashboard's backend) and main.py (the CLI) both call this
module -- never guard.py directly for anything beyond read-only inspection -- so
there is exactly one kill switch, one allowlist, one approval gate, no matter which
front door was used.

This module does NOT add new safety rules of its own invention. It re-checks the
SAME things guard.py already checks (allowlist membership, baseline validity,
current health) immediately before injecting, because time may have passed since a
proposal was generated. The one new rule it adds is procedural, not technical: it
refuses to start a second experiment while one is already running (`is_running()`),
so the dashboard's "Stop" button always means "stop THE experiment", unambiguously.

Authorization model: `authorize_and_run()` performs a REAL fault injection the
moment it's called -- it does not ask for confirmation itself. Callers are
responsible for only calling it once a human has explicitly authorized THIS
proposal_id (api.py's POST /api/experiments/run requires `authorize: true` in the
request body from an operator action, not an AI decision; main.py's `run` subcommand
reuses guard.py's own --yes/interactive prompt). An AI can call generate_plan() all
day; it can never reach authorize_and_run() on its own.
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass

import asyncio

import config
import dependency_mapper
import guard
import planner_agent
import probe
import remediation_planner
import resilience_agent

STATUS_IDLE = "idle"
STATUS_PLANNED = "planned"
STATUS_RUNNING = "running"
STATUS_ANALYZING = "analyzing"
STATUS_DONE = "done"
STATUS_ERROR = "error"


class OrchestratorError(Exception):
    """A requested action was refused -- bad proposal_id, already running, unhealthy
    right now, etc. Always a reason a human did something wrong or something changed,
    never a sign a fault was left in an inconsistent state."""


@dataclass
class _RunState:
    proposal: dict | None = None
    proposal_id: str | None = None
    status: str = STATUS_IDLE
    experiment_id: str | None = None
    record_snapshot: dict | None = None
    analysis: dict | None = None
    remediation_plan: dict | None = None
    error: str | None = None
    stop_event: "asyncio.Event | None" = None
    started_at: float | None = None
    # ---- scenario (v2) path ----
    kind: str = "single"
    scenario_proposal: dict | None = None
    plan_ctx: dict | None = None
    learning: dict | None = None
    agent_runs: list | None = None
    task: "asyncio.Task | None" = None
    stop_requested: bool = False
    candidates: dict | None = None
    candidates_ctx: dict | None = None


_state = _RunState()
_lock = threading.Lock()  # guards _state; cheap and short-held, never across an await


def get_status() -> dict:
    with _lock:
        s = _state
        return {
            "status": s.status, "proposal": s.proposal, "proposal_id": s.proposal_id,
            "experiment_id": s.experiment_id, "record": s.record_snapshot,
            "analysis": s.analysis, "remediation_plan": s.remediation_plan,
            "error": s.error, "started_at": s.started_at,
            "kind": s.kind, "scenario_proposal": s.scenario_proposal, "learning": s.learning,
            "agent_runs": s.agent_runs, "stop_requested": s.stop_requested,
        }


def is_running() -> bool:
    with _lock:
        return _state.status == STATUS_RUNNING


def _load_or_build_dependency_map() -> dict:
    if os.path.isfile("dependency_map.json"):
        try:
            with open("dependency_map.json", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return dependency_mapper.build_dependency_map()


def _recent_experiments(limit: int = 10) -> list[dict]:
    files = sorted(glob.glob(os.path.join(config.EXPERIMENTS_DIR, "*.json")), reverse=True)[:limit]
    out = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("dry_run") or rec.get("simulated"):
            continue  # a dry run touched nothing: it is not a target that has "been tried"
        out.append({"id": rec.get("id"), "fault": rec.get("fault"), "target": rec.get("target"),
                    "state": rec.get("state"), "aborted": rec.get("aborted"),
                    "recovery_outcome": rec.get("recovery_outcome")})
    return out


def generate_plan() -> dict:
    """Ask the planner agent for one proposal. Entirely read-only: no Docker, no
    lock, safe to call repeatedly (e.g. a "Refresh proposal" button), even while an
    experiment is running -- it just won't be authorizable until that one finishes."""
    dep_map = _load_or_build_dependency_map()
    health = probe.current_health()
    baseline = guard.load_baseline()
    past = _recent_experiments(limit=10)
    proposal = planner_agent.propose_experiment(dep_map, health, baseline, past)
    proposal_id = uuid.uuid4().hex[:12]
    with _lock:
        _state.proposal, _state.proposal_id = proposal, proposal_id
        if _state.status != STATUS_RUNNING:
            _state.status = STATUS_PLANNED
    return {"proposal_id": proposal_id, **proposal}


def get_current_proposal() -> dict | None:
    with _lock:
        if _state.proposal is None:
            return None
        return {"proposal_id": _state.proposal_id, **_state.proposal}


class _FaultArgs:
    """Minimal stand-in for argparse's Namespace -- guard.build_fault() only reads
    .fault/.ms/.ttl off whatever object it's given."""
    def __init__(self, fault: str, ms: int, ttl: int):
        self.fault, self.ms, self.ttl = fault, ms, ttl


def _claim(proposal_id: str, dry_run: bool) -> tuple[dict, "asyncio.Event"]:
    """Synchronous validation + claim -- deliberately has no `await` in it, so it
    either raises OrchestratorError IMMEDIATELY (the caller gets a real error, not a
    background task that silently fails) or atomically flips status to RUNNING
    before returning. Re-validates the proposal against guard.py's live ALLOWLIST and
    current health RIGHT NOW -- a proposal generated five minutes ago might no longer
    be safe to run."""
    with _lock:
        if _state.status == STATUS_RUNNING:
            raise OrchestratorError("an experiment is already running -- stop it first or wait")
        if _state.proposal is None or _state.proposal_id != proposal_id:
            raise OrchestratorError("unknown or stale proposal_id -- call generate_plan() again")
        proposal = dict(_state.proposal)

    target, fault_name = proposal.get("target"), proposal.get("fault")
    if target not in guard.ALLOWLIST:
        raise OrchestratorError(f"target '{target}' is not in guard.py's ALLOWLIST -- refusing")
    if fault_name not in ("kill", "delay"):
        raise OrchestratorError(f"unknown fault type '{fault_name}'")

    if not dry_run:
        health = probe.current_health()
        if health.get("status") == "unknown":
            raise OrchestratorError(
                "current health is UNKNOWN (probe.py may not be running) -- refusing to authorize "
                "a real experiment with no live monitoring. Start `python probe.py` first.")

    th = guard.Thresholds.from_env()
    try:
        th.validate()
    except guard.GuardError as e:
        raise OrchestratorError(f"invalid threshold configuration: {e}") from e

    stop_event = asyncio.Event()
    with _lock:
        _state.status = STATUS_RUNNING
        _state.stop_event = stop_event
        _state.started_at = time.time()
        _state.error = None
        _state.analysis = None
        _state.remediation_plan = None
        _state.experiment_id = None
        _state.record_snapshot = None
    return proposal, stop_event


async def _execute(proposal: dict, dry_run: bool, stop_event: "asyncio.Event") -> dict:
    """The actual async work: Docker/container resolution, the full guard.py
    lifecycle, then analysis + remediation planning. Assumes _claim() already
    validated everything and flipped status to RUNNING."""
    target, fault_name = proposal["target"], proposal["fault"]
    th = guard.Thresholds.from_env()

    def _on_update(record: dict) -> None:
        with _lock:
            _state.experiment_id = record.get("id")
            _state.record_snapshot = dict(record)

    lock = None
    try:
        lock = guard.acquire_lock(target)
        container = await guard.resolve_container(target, dry_run=dry_run)
        params = proposal.get("params", {}) or {}
        args = _FaultArgs(fault_name, int(params.get("delay_ms", 1000)), int(params.get("ttl_s", 60)))
        fault = guard.build_fault(container, dry_run, args)

        record = await guard.run_experiment(
            fault, target, container, th,
            max_duration=proposal.get("max_duration_s", 90),
            pre_s=proposal.get("pre_s", 10),
            recovery_timeout=proposal.get("recovery_timeout_s", 180),
            fresh_baseline=False, max_baseline_age_s=guard.MAX_BASELINE_AGE_S_DEFAULT,
            stop_event=stop_event, on_update=_on_update,
        )
    except Exception as e:
        with _lock:
            _state.status = STATUS_ERROR
            _state.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        guard.release_lock(lock)

    path = guard.save(record)
    with _lock:
        _state.record_snapshot = record
        _state.status = STATUS_ANALYZING

    try:
        dep_map = _load_or_build_dependency_map()
        analysis = resilience_agent.analyze_experiment(record, dep_map)
        resilience_agent.save_report(analysis)
        plan = remediation_planner.build_plan(analysis)
        remediation_planner.save_plan(plan)
    except Exception as e:
        # The experiment itself finished and is saved; only the analysis failed. Without
        # this the status stayed "analyzing" forever.
        with _lock:
            _state.status = STATUS_ERROR
            _state.error = f"experiment saved to {path}, but analysis failed: {type(e).__name__}: {e}"
        raise

    with _lock:
        _state.analysis, _state.remediation_plan = analysis, plan
        _state.status = STATUS_DONE

    return {"experiment_path": path, "record": record, "analysis": analysis, "remediation_plan": plan}


async def authorize_and_run(proposal_id: str, dry_run: bool = False) -> dict:
    """Validate, claim, and run ONE experiment to completion, awaiting the whole
    thing. Used by main.py's CLI, where blocking until the experiment finishes is
    exactly what a terminal command should do."""
    proposal, stop_event = _claim(proposal_id, dry_run)
    return await _execute(proposal, dry_run, stop_event)


def start_authorized_run(proposal_id: str, dry_run: bool = False) -> "asyncio.Task":
    """Validate and claim SYNCHRONOUSLY (raises OrchestratorError immediately for any
    bad request), then schedule the experiment to run in the background and return
    right away. Used by api.py so a dashboard click gets an immediate yes/no, and the
    (possibly minutes-long) experiment itself never blocks the HTTP response -- the
    dashboard polls get_status() for progress instead.

    MUST be called from a coroutine running on an active asyncio event loop (an
    `async def` FastAPI route, not a plain `def` one -- FastAPI runs those in a
    worker thread with no event loop of its own). If scheduling the task fails for
    any reason, status is rolled back from RUNNING rather than left stuck -- a
    caller mistake here must not permanently block every future experiment."""
    proposal, stop_event = _claim(proposal_id, dry_run)
    try:
        return asyncio.create_task(_execute(proposal, dry_run, stop_event))
    except RuntimeError as e:
        with _lock:
            _state.status = STATUS_ERROR
            _state.error = f"could not schedule the experiment: {e}"
        raise


def request_stop() -> bool:
    """Signal the running experiment to stop at its next check point. Returns False
    if nothing is running. guard.py still performs its own revert + recovery
    verification -- this is a graceful early-stop request, not a kill -9."""
    with _lock:
        if _state.status != STATUS_RUNNING or _state.stop_event is None:
            return False
        _state.stop_event.set()
        _state.stop_requested = True
        return True


def reset() -> None:
    """Clear this process's in-memory view back to idle (e.g. after inspecting a
    DONE/ERROR run). Touches nothing real -- no Docker, no files."""
    with _lock:
        _state.__init__()


# ---------------------------------------------------------------- remediation decisions
# Category B's auto-apply allowlist is intentionally empty (see remediation_planner.py's
# module docstring for why) -- there is nothing safe to auto-apply yet. "Approving" a
# remediation item here only records operator intent for the dashboard/audit trail; it
# NEVER mutates a running container, config file, or Docker Compose service. This keeps
# the dashboard honest: a decision is tracked, but nothing is silently automated beyond
# what guard.py's own rollback already does.
def _decisions_path(experiment_id: str) -> str:
    return os.path.join(config.REPORTS_DIR, f"remediation_decisions_{experiment_id}.json")


def _load_decisions(experiment_id: str) -> dict:
    path = _decisions_path(experiment_id)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_decisions(experiment_id: str, decisions: dict) -> str:
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    path = _decisions_path(experiment_id)
    tmp = path + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(decisions, f, indent=2)
    os.replace(tmp, path)
    return path


def _load_plan(experiment_id: str) -> dict | None:
    path = os.path.join(config.REPORTS_DIR, f"remediation_plan_{experiment_id}.md")
    if not os.path.isfile(path):
        return None
    analysis_path = os.path.join(config.REPORTS_DIR, f"experiment_{experiment_id}_analysis.json")
    if not os.path.isfile(analysis_path):
        return None
    with open(analysis_path, encoding="utf-8") as f:
        analysis = json.load(f)
    return remediation_planner.build_plan(analysis)


def decide_remediation(experiment_id: str, item_index: int, decision: str) -> dict:
    """decision is "approved" or "rejected". Category C items (the overwhelming
    majority, by design) always require this human step; this function only ever
    records that step -- it never applies the underlying fix."""
    if decision not in ("approved", "rejected"):
        raise OrchestratorError("decision must be 'approved' or 'rejected'")
    plan = _load_plan(experiment_id)
    if plan is None or not (0 <= item_index < len(plan["items"])):
        raise OrchestratorError(f"no remediation item {item_index} for experiment '{experiment_id}'")
    if plan["items"][item_index]["category"] == "A":
        raise OrchestratorError("this item is already automated by guard.py's own rollback -- "
                                "there is nothing separate to approve or reject")
    decisions = _load_decisions(experiment_id)
    decisions[str(item_index)] = {
        "decision": decision, "decided_at_utc": guard.utc_now_iso(),
        "finding_title": plan["items"][item_index]["finding_title"],
        "note": "recorded for audit trail only -- no automated action was taken "
                "(Category B/C fixes in this project always require a human to apply them manually)",
    }
    _save_decisions(experiment_id, decisions)
    return get_remediation_view(experiment_id)


def verify_remediation(experiment_id: str) -> dict:
    """Re-checks CURRENT live health against the experiment's own baseline so the
    dashboard can show "has this gotten better since the fix was applied manually?"
    without ever claiming a fix it can't observe actually happened."""
    exp_path = os.path.join(config.EXPERIMENTS_DIR, f"{experiment_id}.json")
    if not os.path.isfile(exp_path):
        raise OrchestratorError(f"no saved experiment '{experiment_id}'")
    with open(exp_path, encoding="utf-8") as f:
        record = json.load(f)
    baseline = record.get("baseline") or {}
    current = probe.current_health()
    verified = (current.get("status") == "healthy" and current.get("fresh") is True)
    result = {
        "experiment_id": experiment_id, "checked_at_utc": guard.utc_now_iso(),
        "current_health": current, "baseline_direct_p95_ms": (baseline.get("direct_ms") or {}).get("p95"),
        "verified_healthy_now": verified,
        "note": "This compares LIVE current health to the saved baseline. It does not know whether a "
                "specific remediation was actually applied outside this tool -- it only answers "
                "'is the system healthy right now'.",
    }
    decisions = _load_decisions(experiment_id)
    decisions["_verification"] = result
    _save_decisions(experiment_id, decisions)
    return result


def get_remediation_view(experiment_id: str) -> dict:
    plan = _load_plan(experiment_id)
    if plan is None:
        raise OrchestratorError(f"no remediation plan for experiment '{experiment_id}' "
                                "-- run `main.py analyze` or let the orchestrator analyze it first")
    decisions = _load_decisions(experiment_id)
    items = []
    for i, item in enumerate(plan["items"]):
        items.append({**item, "operator_decision": decisions.get(str(i), {}).get("decision")
                      if item["category"] != "A" else "not_applicable_already_automated"})
    return {**plan, "items": items, "verification": decisions.get("_verification")}


# ================================================================ scenario path (schema 2.0)
# Multi-fault scenarios, the agent pipeline, and the knowledge store. Shares `_state` with
# the legacy single-fault path above, so there is still exactly ONE run at a time in this
# process no matter which entry point started it.
import agents  # noqa: E402
import learning  # noqa: E402
import scenario  # noqa: E402
import store  # noqa: E402
from events import bus  # noqa: E402


class OrchestratorConflict(OrchestratorError):
    """The request is valid but cannot be honoured in the current state (HTTP 409)."""


_recent_requests: dict[str, dict] = {}   # request_id -> response, for duplicate submissions
_REQUEST_MEMORY = 50


def save_record(record: dict) -> str:
    """Atomic write into config.EXPERIMENTS_DIR (guard.save() uses its own import-time copy)."""
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    path = os.path.join(config.EXPERIMENTS_DIR, f"{record['id']}.json")
    tmp = path + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(guard._sanitize(record), f, indent=2, allow_nan=False, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def _history() -> tuple[list[dict], str | None]:
    """Stored history, falling back to reading the JSON files directly when the
    knowledge store is unavailable. Returns (cases, degraded_reason)."""
    try:
        store.sync_from_files()
        return store.history(), None
    except store.StoreError as e:
        cases = []
        for path in sorted(glob.glob(os.path.join(config.EXPERIMENTS_DIR, "*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict) and rec.get("id"):
                    cases.append(learning.to_case(rec))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return cases, str(e)


def gather_context(policy: dict | None = None) -> dict:
    history, degraded = _history()
    try:
        pending = store.pending_validation_signatures()
    except store.StoreError:
        pending = set()
    return {"dep_map": _load_or_build_dependency_map(), "health": probe.current_health(),
            "baseline": guard.load_baseline(), "history": history, "store_degraded": degraded,
            "pending_validation": pending, "policy": policy, "outputs": {}}


def _timeline(type_: str, data: dict, experiment_id: str | None = None, proposal_id: str | None = None) -> None:
    try:
        store.add_timeline(type_, data, experiment_id, proposal_id)
    except store.StoreError as e:
        print(f"[orchestrator] timeline not persisted ({type_}): {e}", flush=True)


async def generate_scenario_plan(policy: dict | None = None, mode: str | None = None) -> dict:
    """Run the plan pipeline (risk -> fault_injection). Read-only."""
    proposal_id = uuid.uuid4().hex[:12]
    ctx = await asyncio.to_thread(gather_context, policy)
    result = await agents.coordinator.plan(ctx, proposal_id)
    proposal = result["proposal"]
    if proposal is None:
        failed = next((r for r in result["runs"] if r["agent"] == "fault_injection"), {})
        raise OrchestratorError(f"no proposal could be produced: {failed.get('error') or 'unknown error'}")
    proposal = {"proposal_id": proposal_id, "mode": mode, "store_degraded": ctx["store_degraded"], **proposal}
    with _lock:
        _state.scenario_proposal, _state.plan_ctx = proposal, ctx
        if _state.status not in (STATUS_RUNNING, STATUS_ANALYZING):
            _state.status = STATUS_PLANNED
    _timeline("proposed", {"signature": proposal["validation"]["signature"], "method": proposal["method"],
                           "hypothesis": proposal["hypothesis"]}, proposal_id=proposal_id)
    bus.publish("proposal", {"proposal_id": proposal_id, "signature": proposal["validation"]["signature"],
                             "method": proposal["method"]})
    return proposal


def get_scenario_proposal() -> dict | None:
    with _lock:
        return _state.scenario_proposal


def _on_event(kind: str, data: dict) -> None:
    bus.publish(kind, data)
    if kind in ("experiment", "alert"):
        _timeline(kind, data, data.get("experiment_id"))


def _on_update(record: dict) -> None:
    with _lock:
        record["recovery_state"] = agents.recovery_state(record, _state.stop_requested)
        _state.experiment_id = record.get("id")
        _state.record_snapshot = dict(record)


def start_scenario(scn: "scenario.Scenario", authorization: "scenario.Authorization | None", *,
                   policy: dict | None = None, proposal: dict | None = None, mode: str | None = None,
                   source: str = "operator", request_id: str | None = None, dry_run: bool = False,
                   fresh_baseline: bool = False, extra_meta: dict | None = None) -> dict:
    """Validate and claim SYNCHRONOUSLY, then run in the background. Raises
    OrchestratorConflict / scenario.ScenarioRejected immediately for anything refused.
    Must be called on the event loop thread. Returns {experiment_id, task, duplicate}.

    dry_run=True walks the same lifecycle with real health probes but executes no Docker
    command; it needs no authorization and is never counted as evidence."""
    with _lock:
        if request_id and request_id in _recent_requests:
            return {**_recent_requests[request_id], "duplicate": True, "task": None}
        if _state.status in (STATUS_RUNNING, STATUS_ANALYZING):
            raise OrchestratorConflict("an experiment is already running or being analysed -- "
                                       "stop it or wait for it to finish")
        previous = _state.status
        _state.status = STATUS_RUNNING     # claimed: a second caller is refused from here on
    try:
        health = probe.current_health()
        if not dry_run and health.get("status") == "unknown":
            raise OrchestratorConflict("current health is UNKNOWN (probe.py is not writing fresh data) -- "
                                       "refusing to inject with no independent monitoring. Start `python probe.py`.")
        ctx = None
        if proposal is not None:
            with _lock:
                same = (_state.scenario_proposal or {}).get("proposal_id") == proposal.get("proposal_id")
                ctx = _state.plan_ctx if same else None
        if ctx is None:
            ctx = gather_context(policy)
        ctx["policy"] = policy
        prediction = (proposal or {}).get("prediction") or learning.predict(
            [a.model_dump() for a in scn.actions], ctx["history"])
        stop_event = asyncio.Event()
        keep = ("proposal_id", "method", "objective", "hypothesis", "ranking", "confidence")
        run = scenario.ScenarioRun(
            scn, authorization, dep_map=ctx["dep_map"], policy=policy, stop_event=stop_event,
            on_event=_on_event, on_update=_on_update, dry_run=dry_run, fresh_baseline=fresh_baseline,
            meta={"mode": mode, "source": "dry_run" if dry_run else source, "prediction": prediction,
                  "proposal": {k: proposal.get(k) for k in keep} if proposal else None,
                  **(extra_meta or {})})
        report = run.prepare()
    except BaseException:
        with _lock:
            _state.status = previous
        raise

    proposal_id = (proposal or {}).get("proposal_id")
    with _lock:
        _state.kind, _state.stop_event, _state.stop_requested = "scenario", stop_event, False
        _state.started_at, _state.error = time.time(), None
        _state.analysis = _state.remediation_plan = _state.learning = _state.agent_runs = None
        _state.experiment_id, _state.record_snapshot = run.id, dict(run.record)
    if proposal_id:
        try:
            store.link_proposal(proposal_id, run.id)
        except store.StoreError:
            pass
    _timeline("validated", {"signature": report["signature"], "warnings": len(report["warnings"]),
                            "cumulative_intensity": report["cumulative_intensity"]}, run.id)
    if not dry_run:
        _timeline("authorized", authorization.model_dump(), run.id)
        try:
            store.audit(authorization.actor, "scenario_authorized",
                        {"experiment_id": run.id, "kind": authorization.kind, "signature": report["signature"]})
        except store.StoreError:
            pass
    try:
        task = asyncio.create_task(_execute_scenario(run, ctx), name=f"scenario-{run.id}")
    except RuntimeError as e:
        run.release()
        with _lock:
            _state.status, _state.error = STATUS_ERROR, f"could not schedule the scenario: {e}"
        raise
    response = {"experiment_id": run.id, "signature": report["signature"], "status": "started"}
    with _lock:
        _state.task = task
        if request_id:
            _recent_requests[request_id] = response
            while len(_recent_requests) > _REQUEST_MEMORY:
                _recent_requests.pop(next(iter(_recent_requests)))
    return {**response, "duplicate": False, "task": task}


async def _execute_scenario(run: "scenario.ScenarioRun", ctx: dict) -> dict:
    """Run to completion, save, then the post-run agent pipeline. Always leaves the
    status in DONE or ERROR -- never RUNNING/ANALYZING."""
    agents.coordinator.set_state("recovery", "monitoring", run.id)
    try:
        record = await run.run()
    except BaseException as e:
        record = run.record
        record["error"] = guard._join(record.get("error"), f"{type(e).__name__}: {e}")
        try:
            save_record(record)
        finally:
            with _lock:
                _state.status, _state.error = STATUS_ERROR, record["error"]
        raise
    finally:
        agents.coordinator.set_state("recovery", "idle")

    path = None
    try:
        path = save_record(record)
        if run.dry_run:
            # Nothing was injected: there is no outcome to analyse, learn from or remediate.
            try:
                store.upsert_experiment(record, None, path)
            except store.StoreError:
                pass
            with _lock:
                _state.record_snapshot, _state.status = record, STATUS_DONE
            bus.publish("pipeline", {"experiment_id": record["id"], "state": record["state"], "agents": {}})
            return {"experiment_path": path, "record": record, "outputs": {}, "runs": []}
        with _lock:
            _state.record_snapshot, _state.status = record, STATUS_ANALYZING

        def after_agent(name: str, agent_run: dict) -> None:
            if name in ("recovery", "validation"):
                save_record(record)     # these two write their verdict into the record itself
        ctx.update(record=record, record_path=path, after_agent=after_agent)
        result = await agents.coordinator.post_run(ctx)
        outputs = result["outputs"]
        if record.get("followup_of"):
            await asyncio.to_thread(_complete_followup, record, outputs)
        save_record(record)
        failed = [r["agent"] for r in result["runs"] if r["status"] in ("failed", "timeout")]
        with _lock:
            _state.record_snapshot = record
            _state.analysis, _state.learning = outputs.get("insights"), outputs.get("learning")
            _state.remediation_plan = outputs.get("remediation")
            _state.agent_runs = [{k: v for k, v in r.items() if k != "output"} for r in result["runs"]]
            _state.error = f"agent(s) failed: {', '.join(failed)}" if failed else None
            _state.status = STATUS_DONE
        bus.publish("pipeline", {"experiment_id": record["id"], "state": record["state"],
                                 "agents": {r["agent"]: r["status"] for r in result["runs"]}})
        return {"experiment_path": path, "record": record, "outputs": outputs, "runs": result["runs"]}
    except BaseException as e:
        where = f"saved to {path}" if path else "NOT saved"
        with _lock:
            _state.status = STATUS_ERROR
            _state.error = f"experiment {where}; post-run processing failed: {type(e).__name__}: {e}"
        raise


async def shutdown(timeout: float | None = None) -> None:
    """Called on API shutdown: ask the running experiment to stop and wait for its
    rollback to finish, so a restart never leaves an untracked fault behind."""
    with _lock:
        task, ev = _state.task, _state.stop_event
        running = _state.status in (STATUS_RUNNING, STATUS_ANALYZING)
    if not running or task is None or task.done():
        return
    if ev is not None:
        ev.set()
    print("[orchestrator] shutdown: waiting for the running experiment to roll back...", flush=True)
    limit = timeout or config.ROLLBACK_STEP_TIMEOUT_S * config.MAX_ACTIVE_FAULTS + 60
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=limit)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception) as e:  # noqa: BLE001
        print(f"[orchestrator] shutdown did not complete cleanly ({type(e).__name__}); the recovery "
              f"journal in {config.JOURNAL_DIR} will be replayed on next start.", flush=True)


async def resolve_lockout(actor: str, force: bool = False, note: str = "") -> dict:
    """Re-run the verifying rollback for everything in the recovery journal. The lockout
    clears only when every item is observed clean -- or when an operator forces it with a
    note after fixing things by hand (audited)."""
    if is_running():
        raise OrchestratorConflict("an experiment is running; stop it before resolving recovery")
    result = await scenario.recover_orphans()
    if result["lockout"] is not None and force:
        if not note.strip():
            raise OrchestratorError("a note describing the manual recovery is required to force-clear the lockout")
        for j in scenario.list_journals():
            try:
                os.remove(os.path.join(config.JOURNAL_DIR, f"{j.get('id')}.json"))
            except OSError:
                pass
        scenario.clear_lockout()
        result = {**result, "lockout": None, "forced": True}
    try:
        store.audit(actor, "lockout_resolve", {"forced": bool(result.get("forced")), "note": note,
                                               "recovered": len(result["recovered"]),
                                               "failed": len(result["failed"])})
    except store.StoreError:
        pass
    cleared = result["lockout"] is None
    bus.publish("alert", {"level": "info" if cleared else "critical", "kind": "lockout",
                          "message": "recovery lockout cleared" if cleared else "recovery is still required"})
    return result


# ================================================================ ranked candidates
import faults  # noqa: E402
import scenario_planner  # noqa: E402
from planner_agent import PlannerError  # noqa: E402


def rank_candidates_view(policy: dict | None = None) -> dict:
    """The ranked candidate list for the dashboard. Read-only: no Docker, no lock, nothing
    is authorized. The ranking context is kept so a later select_candidate() refers to
    exactly the list the operator saw."""
    ctx = gather_context(policy)
    ctx["outputs"]["risk"] = agents.RiskAgent().run(ctx)
    ranked = learning.rank_candidates(ctx["dep_map"], ctx["history"], (policy or {}).get("allowed_faults"),
                                      (policy or {}).get("allowed_targets"), policy, ctx["pending_validation"])
    ctx["ranked"] = ranked
    view = {"generated_at_utc": guard.utc_now_iso(), "policy": policy,
            "store_degraded": ctx["store_degraded"], "weights": learning.WEIGHTS,
            "evidence_runs": sum(1 for c in ctx["history"] if c.get("evidence")),
            "candidates": scenario_planner.propose_candidates(ranked, ctx["dep_map"], ctx["history"],
                                                              ctx["outputs"]["risk"], policy)}
    with _lock:
        _state.candidates, _state.candidates_ctx = view, ctx
    return view


def select_candidate(signature: str, mode: str | None = None) -> dict:
    """Turn a ranked candidate into the current proposal. This is NOT an authorization and
    starts nothing: the proposal still has to be run through start_scenario() with an
    Authorization, exactly like a planner-generated one."""
    with _lock:
        ctx = _state.candidates_ctx
    if ctx is None:
        raise OrchestratorConflict("no ranked candidate list exists yet -- rank candidates first")
    try:
        body = scenario_planner.proposal_for(signature, ctx["ranked"], ctx["dep_map"], ctx["history"],
                                             ctx["outputs"].get("risk"), ctx.get("policy"))
    except PlannerError as e:
        raise OrchestratorError(str(e)) from e
    proposal_id = uuid.uuid4().hex[:12]
    proposal = {"proposal_id": proposal_id, "mode": mode, "store_degraded": ctx["store_degraded"], **body}
    with _lock:
        _state.scenario_proposal, _state.plan_ctx = proposal, ctx
        if _state.status not in (STATUS_RUNNING, STATUS_ANALYZING):
            _state.status = STATUS_PLANNED
    _timeline("proposed", {"signature": signature, "method": "operator_selected",
                           "hypothesis": proposal["hypothesis"]}, proposal_id=proposal_id)
    bus.publish("proposal", {"proposal_id": proposal_id, "signature": signature, "method": "operator_selected"})
    return proposal


# ================================================================ re-run after a fix
RERUN_PRE_S = 30.0      # a follow-up re-measures its own healthy baseline over this long


def _load_record(experiment_id: str) -> dict:
    path = os.path.join(config.EXPERIMENTS_DIR, f"{experiment_id}.json")
    if not os.path.isfile(path):
        raise OrchestratorError(f"no saved experiment '{experiment_id}'")
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as e:
        raise OrchestratorError(f"experiment '{experiment_id}' cannot be read: {e}") from e
    if not isinstance(rec, dict):
        raise OrchestratorError(f"experiment '{experiment_id}' is not a valid record")
    return rec


def _original_scenario(record: dict) -> tuple["scenario.Scenario | None", list[str]]:
    """The scenario to repeat, rebuilt from a schema-2.0 or schema-1.x record."""
    reasons: list[str] = []
    if record.get("dry_run") or record.get("simulated"):
        reasons.append("the original was a dry run: nothing was injected, so there is no result to compare with")
    elif not record.get("injected"):
        reasons.append("no fault was injected in the original run, so there is no result to compare with")
    try:
        if isinstance(record.get("scenario"), dict):
            base = scenario.Scenario(**record["scenario"])
        elif record.get("fault") and record.get("target") and record.get("fault") != "multi":
            spec = faults.get_spec(record["fault"])
            known = {p.name for p in spec.params} if spec else set()
            params = {k: v for k, v in (record.get("params") or {}).items()
                      if k in known and isinstance(v, (int, float)) and not isinstance(v, bool)}
            base = scenario.Scenario(name=f"{record['fault']}@{record['target']}", max_duration_s=60,
                                     actions=[{"fault": record["fault"], "target": record["target"],
                                               "params": params}])
        else:
            return None, reasons + ["the original record does not contain a fault scenario"]
    except Exception as e:  # noqa: BLE001 -- pydantic: the stored scenario no longer fits the schema
        return None, reasons + [f"the original scenario is no longer valid: {str(e).splitlines()[0][:200]}"]
    scn = base.model_copy(update={"pre_s": RERUN_PRE_S,
                                  "name": f"re-run of {record.get('id')}"[:120],
                                  "hypothesis": (f"After the remediation, repeating '{learning.signature_of(record)}' "
                                                 f"should no longer show the original finding.")})
    return scn, reasons


def plan_rerun(experiment_id: str, rec_index: int) -> dict:
    """Read-only. Can the original scenario be repeated safely as it was? If not, says why
    and that a revised plan is required -- it never substitutes a different scenario."""
    record = _load_record(experiment_id)
    rec = store.get_recommendation(experiment_id, rec_index)
    if rec is None:
        raise OrchestratorError(f"no recommendation {rec_index} for experiment '{experiment_id}'")
    scn, reasons = _original_scenario(record)
    report = None
    if scn is not None:
        report = scenario.validate(scn, _load_or_build_dependency_map())
        reasons += [e["message"] for e in report["errors"]]
    if rec["status"] == "rejected":
        reasons.append("this recommendation was rejected; reopen it before testing it")
    ok = scn is not None and not reasons
    return {"ok": ok, "requires_revised_plan": not ok, "reasons": reasons,
            "scenario": scn.model_dump() if scn is not None else None, "validation": report,
            "original": {"id": record.get("id"), "signature": learning.signature_of(record),
                         "state": record.get("state"), "outcome_class": learning.observe(record)["outcome_class"],
                         "started_at_utc": record.get("started_at_utc")},
            "recommendation": rec,
            "baseline_note": (f"The follow-up ignores baseline.json and measures a fresh baseline over "
                              f"{RERUN_PRE_S:g}s immediately before injecting; it is refused if Ditto is not "
                              f"healthy during that window."),
            "caveat": learning.CORRELATION_CAVEAT}


def start_rerun(experiment_id: str, rec_index: int, authorization: "scenario.Authorization", *,
                confirmed_applied: bool, actor: str, note: str = "", mode: str | None = None,
                request_id: str | None = None) -> dict:
    """Start a follow-up experiment linked to the original and to the remediation. Goes
    through start_scenario(): same authorization, locks, kill switch and rollback as any run."""
    if not confirmed_applied:
        raise OrchestratorError("confirm that the change was actually applied before re-running: an accepted "
                                "recommendation is not evidence that anything changed")
    plan = plan_rerun(experiment_id, rec_index)
    if not plan["ok"]:
        raise scenario.ScenarioRejected(
            "the original scenario cannot be re-run as it was; a revised plan is required",
            errors=[{"code": "rerun_not_possible", "message": r, "action": None} for r in plan["reasons"]],
            code="policy_violation")
    rec = plan["recommendation"]
    link = {"experiment_id": experiment_id, "recommendation_index": rec_index,
            "recommendation_id": rec["item"].get("id"), "recommendation_title": rec["title"],
            "finding_title": rec["item"].get("finding_title"),
            "confirmed_applied_by": actor, "confirmation_note": note,
            "confirmed_at_utc": guard.utc_now_iso()}
    started = start_scenario(scenario.Scenario(**plan["scenario"]), authorization, mode=mode, source="rerun",
                             request_id=request_id, fresh_baseline=True, extra_meta={"followup_of": link})
    if not started["duplicate"]:
        try:
            store.set_recommendation_status(experiment_id, rec_index, "implemented", None,
                                            f"{actor} confirmed the change was applied"
                                            + (f": {note}" if note else ""))
            store.create_followup(started["experiment_id"], experiment_id, rec_index, link["recommendation_id"],
                                  actor, note)
            store.audit(actor, "rerun_started", {"original": experiment_id, "followup": started["experiment_id"],
                                                 "recommendation_index": rec_index, "note": note})
        except store.StoreError as e:
            print(f"[orchestrator] follow-up link not persisted: {e}", flush=True)
        _timeline("rerun", {"original": experiment_id, "recommendation": rec["title"], "confirmed_by": actor},
                  started["experiment_id"])
    return {**started, "original_id": experiment_id, "recommendation_index": rec_index}


def _complete_followup(record: dict, outputs: dict) -> None:
    """Compare the follow-up with its original and persist the verification result."""
    link = record.get("followup_of") or {}
    try:
        original = _load_record(link["experiment_id"])
        comparison = learning.compare_runs(original, record)
        analysis = outputs.get("insights")
        titles = [f.get("title") for f in analysis["findings"]] if analysis else None
        rec = store.get_recommendation(link["experiment_id"], link["recommendation_index"])
        verification = remediation_planner.verification_result(
            comparison, link.get("finding_title"), titles, (record.get("validation") or {}).get("verdict"),
            ((rec or {}).get("item") or {}).get("expected_benefit"))
        result = {**comparison, "verification": verification, "link": link,
                  "recommendation_status": (rec or {}).get("status")}
        record["followup_comparison"] = result
        store.complete_followup(record["id"], verification["demonstrated"], result)
    except Exception as e:  # noqa: BLE001 -- the run itself is already saved and rolled back
        record["followup_comparison"] = {"error": f"comparison could not be produced: {type(e).__name__}: {e}"[:400],
                                         "link": link}
        print(f"[orchestrator] follow-up comparison failed: {e}", flush=True)
        try:
            store.complete_followup(record["id"], "inconclusive", record["followup_comparison"])
        except store.StoreError:
            pass
