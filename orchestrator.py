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

    dep_map = _load_or_build_dependency_map()
    analysis = resilience_agent.analyze_experiment(record, dep_map)
    resilience_agent.save_report(analysis)
    plan = remediation_planner.build_plan(analysis)
    remediation_planner.save_plan(plan)

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
