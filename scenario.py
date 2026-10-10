"""Scenario engine: several coordinated faults run as ONE experiment.

Three layers, deliberately separate:

  validate()         pure policy check of a whole scenario -- allowlist, parameter bounds,
                     conflicts, the tested-combination allowlist, hard limits. No Docker.
  preflight_check()  read-only look at the real containers. Mutates nothing.
  ScenarioRun        the only thing here that mutates. prepare() is synchronous and either
                     refuses immediately or takes the locks; run() then always returns a
                     record and always attempts to roll back every action it touched.

Rollback rules (see ScenarioRun._rollback_all): every action whose status is anything
but "pending" is rolled back, in dependency-aware order, each one independently -- a
failure on one never stops the others. An action counts as reverted only after its
clean state has been OBSERVED. If any cannot be confirmed, the run ends
RECOVERY_REQUIRED, a lockout file is written, and nothing in this process will mutate
again until an operator resolves it.

Recovery information is journaled to disk BEFORE the first mutation and before every
apply, so a crash of this process leaves enough behind for recover_orphans() to clean up.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import asdict
from typing import Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

import activity
import config
import faults
import guard
import fleet
import probe
import profiles

SCHEMA_VERSION = "2.0"
clock = time.monotonic   # indirection so tests can drive scenario time deterministically

STATE_PREFLIGHT = "PREFLIGHT"
STATE_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
TERMINAL_STATES = {guard.STATE_COMPLETED, guard.STATE_ABORTED, guard.STATE_FAILED,
                   guard.STATE_RECOVERY_TIMEOUT, STATE_RECOVERY_REQUIRED}

SCENARIO_LOCK = "_scenario"
FRESH_BASELINE_MIN_S = 30.0     # a real run that cannot use baseline.json measures its own over this long
APPLY_TIMEOUT_S = guard.DOCKER_CMD_TIMEOUT_S * 2 + 30

# Cross-target combinations that are allowed to run together, by fault category. This is
# an allowlist: anything not listed is refused. Expand it only alongside a test.
TESTED_CATEGORY_PAIRS = {
    frozenset({"network"}),                # network fault on A + network fault on B
    frozenset({"network", "process"}),     # one service down/frozen while another is slow or lossy
    frozenset({"network", "resource"}),
    frozenset({"process", "resource"}),
}
# Fault groups that may share ONE target. Everything else on the same target is refused.
SAME_TARGET_GROUP_PAIRS = {frozenset({"netem", "cpu"})}


class ScenarioRejected(Exception):
    """Refused before anything was touched. `errors` is a list of {code, message}."""

    def __init__(self, message: str, errors: list[dict] | None = None, code: str = "rejected"):
        super().__init__(message)
        self.code = code
        self.errors = errors or [{"code": code, "message": message}]


class FaultAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault: str = Field(max_length=40)
    target: str = Field(max_length=60)
    params: dict[str, float] = Field(default_factory=dict)
    start_offset_s: float = Field(0, ge=0, le=300)
    duration_s: float | None = Field(None, ge=2, le=600)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field("ad-hoc scenario", max_length=120)
    hypothesis: str = Field("", max_length=1000)
    actions: list[FaultAction] = Field(min_length=1, max_length=8)
    max_duration_s: float = Field(60, ge=10, le=600)
    pre_s: float = Field(10, ge=2, le=60)
    recovery_timeout_s: float = Field(180, ge=20, le=600)
    # Product profiles the operator wants to watch. Observation only: it never narrows what
    # a fault can affect, and the record still reports the impact on every profile.
    observe_profiles: list[str] | None = Field(None, max_length=20)


class Authorization(BaseModel):
    """Who allowed this run to mutate. Constructed only by api.py (operator action) or
    controller.py (autonomous sandbox policy) -- never from AI output."""
    kind: Literal["operator", "autonomous_policy"]
    actor: str
    granted_at_utc: str = Field(default_factory=guard.utc_now_iso)
    note: str = ""


def limits_from_config() -> dict:
    return {"max_active_faults": config.MAX_ACTIVE_FAULTS,
            "max_affected_services": config.MAX_AFFECTED_SERVICES,
            "max_duration_s": config.MAX_SCENARIO_DURATION_S,
            "max_cumulative_intensity": config.MAX_CUMULATIVE_INTENSITY}


def signature(actions: list[dict]) -> str:
    """Stable identity of 'what was done to what', ignoring parameters."""
    return "+".join(sorted(f"{a['fault']}@{a['target']}" for a in actions))


def _fan_in(dep_map: dict | None) -> dict:
    return (dep_map or {}).get("fan_in") or {}


# ---------------------------------------------------------------- validation (pure)
def validate(scn: Scenario, dep_map: dict | None = None, policy: dict | None = None) -> dict:
    """Whole-plan policy check. Never touches Docker. `policy` optionally narrows what is
    allowed (the autonomous sandbox passes allowed_faults/allowed_targets/max_actions/
    max_duration_s); it can only ever make the check stricter."""
    limits = limits_from_config()
    errors: list[dict] = []
    warnings: list[dict] = []
    actions: list[dict] = []

    def err(code: str, message: str, index: int | None = None) -> None:
        errors.append({"code": code, "message": message, "action": index})

    def warn(code: str, message: str, index: int | None = None) -> None:
        warnings.append({"code": code, "message": message, "action": index})

    risk = {r.get("service"): r for r in (dep_map or {}).get("fault_target_recommendations", [])}

    for i, a in enumerate(scn.actions):
        spec = faults.get_spec(a.fault)
        item = {"index": i, "id": f"a{i + 1}", "fault": a.fault, "target": a.target,
                "params": dict(a.params), "start_offset_s": a.start_offset_s, "duration_s": a.duration_s,
                "end_s": None, "intensity": None, "category": None, "conflict_group": None,
                "commands": [], "live_verified": None}
        actions.append(item)
        if spec is None:
            err("unknown_fault", f"unknown fault type '{a.fault}'", i)
            continue
        if not spec.supported:
            err("unsupported_fault", f"'{a.fault}' is not supported: {spec.unsupported_reason}", i)
            continue
        item.update(category=spec.category, conflict_group=spec.conflict_group, live_verified=spec.live_verified)
        if a.target not in guard.ALLOWLIST:
            err("target_not_allowlisted", f"target '{a.target}' is not in the allowlist "
                                          f"{sorted(guard.ALLOWLIST)}", i)
            continue
        if a.start_offset_s >= scn.max_duration_s:
            err("offset_past_end", f"start_offset_s={a.start_offset_s:g} is not before the scenario's "
                                   f"max_duration_s={scn.max_duration_s:g}", i)
            continue
        end = scn.max_duration_s if a.duration_s is None else min(scn.max_duration_s, a.start_offset_s + a.duration_s)
        item["end_s"] = end

        params = dict(a.params)
        has_ttl = any(p.name == "ttl_s" for p in spec.params)
        if has_ttl and "ttl_s" not in params:
            # Dead-man's switch outlives the planned window by a margin, never by default 60s.
            params["ttl_s"] = int(min(600, max(5, math.ceil(end - a.start_offset_s) + 30)))
        try:
            fault = faults.build(a.fault, a.target,
                                 guard.ContainerRef(f"{guard.COMPOSE_PROJECT}-{a.target}-1", "preview"), params)
        except guard.GuardError as e:
            err("invalid_params", str(e), i)
            continue
        item.update(params=fault.params, intensity=round(fault.intensity(), 3),
                    commands=fault.planned_commands())
        if has_ttl and fault.params["ttl_s"] < end - a.start_offset_s:
            warn("ttl_shorter_than_window", f"ttl_s={fault.params['ttl_s']} is shorter than this action's "
                                            f"{end - a.start_offset_s:g}s window; the rule removes itself early", i)
        if not spec.live_verified:
            warn("not_live_verified", f"'{a.fault}' has only been tested against mocked Docker", i)
        if (risk.get(a.target) or {}).get("calculated_risk") == "high":
            warn("high_risk_target", f"'{a.target}' is rated high risk: "
                                     f"{risk[a.target].get('reasoning', 'many services depend on it')}", i)

    valid = [a for a in actions if a["intensity"] is not None]

    # ---- pairwise: duplicates, same-target conflicts, tested combinations ----
    for x in range(len(valid)):
        for y in range(x + 1, len(valid)):
            a, b = valid[x], valid[y]
            if a["target"] == b["target"]:
                if a["fault"] == b["fault"]:
                    err("duplicate_action", f"'{a['fault']}' on '{a['target']}' is listed twice", b["index"])
                elif a["conflict_group"] == b["conflict_group"]:
                    err("conflicting_faults", f"'{a['fault']}' and '{b['fault']}' both act on the "
                                              f"{a['conflict_group']} state of '{a['target']}' and cannot be "
                                              f"combined on one target", b["index"])
                elif frozenset({a["conflict_group"], b["conflict_group"]}) not in SAME_TARGET_GROUP_PAIRS:
                    err("conflicting_faults", f"'{a['fault']}' and '{b['fault']}' on the same target "
                                              f"'{a['target']}' is not a tested combination", b["index"])
            else:
                pair = frozenset({a["category"], b["category"]})
                if pair not in TESTED_CATEGORY_PAIRS:
                    err("untested_combination",
                        f"'{a['fault']}' on '{a['target']}' together with '{b['fault']}' on '{b['target']}' "
                        f"({' + '.join(sorted([a['category'], b['category']]))}) is not in the tested "
                        f"combination list", b["index"])

    # ---- limits ----
    targets = sorted({a["target"] for a in valid})
    intensity = round(sum(a["intensity"] for a in valid), 3)
    edges = sorted({a["start_offset_s"] for a in valid})
    concurrent = max((sum(1 for a in valid if a["start_offset_s"] <= t < a["end_s"]) for t in edges), default=0)

    if scn.max_duration_s > limits["max_duration_s"]:
        err("limit_duration", f"max_duration_s={scn.max_duration_s:g} exceeds the configured ceiling "
                              f"{limits['max_duration_s']:g}")
    if len(targets) > limits["max_affected_services"]:
        err("limit_services", f"{len(targets)} services affected, limit is {limits['max_affected_services']}")
    if concurrent > limits["max_active_faults"]:
        err("limit_active_faults", f"{concurrent} faults active at once, limit is {limits['max_active_faults']}")
    if intensity > limits["max_cumulative_intensity"]:
        err("limit_intensity", f"cumulative intensity {intensity:g} exceeds the budget "
                               f"{limits['max_cumulative_intensity']:g}")

    if policy:
        for a in valid:
            if "allowed_faults" in policy and a["fault"] not in policy["allowed_faults"]:
                err("policy_fault", f"'{a['fault']}' is not in this policy's allowed faults "
                                    f"{sorted(policy['allowed_faults'])}", a["index"])
            if "allowed_targets" in policy and a["target"] not in policy["allowed_targets"]:
                err("policy_target", f"'{a['target']}' is not in this policy's allowed targets "
                                     f"{sorted(policy['allowed_targets'])}", a["index"])
        if "max_actions" in policy and len(scn.actions) > policy["max_actions"]:
            err("policy_actions", f"{len(scn.actions)} actions, this policy allows {policy['max_actions']}")
        if "max_duration_s" in policy and scn.max_duration_s > policy["max_duration_s"]:
            err("policy_duration", f"max_duration_s={scn.max_duration_s:g}, this policy allows "
                                   f"{policy['max_duration_s']:g}")

    scope = None
    if scn.observe_profiles is not None:
        scope = []
        try:
            enabled = set(fleet.current().prefixes)
        except fleet.FleetConfigError as e:
            enabled = None
            warn("fleet_unavailable", f"observation scope not checked against the fleet: {e}")
        for name in scn.observe_profiles:
            try:
                key = profiles.get(name).key
            except KeyError as e:
                err("unknown_profile", str(e).strip("'\""))
                continue
            if enabled is not None and key not in enabled:
                err("profile_not_in_fleet", f"profile '{key}' has no twins in the running fleet "
                                            f"(enabled: {', '.join(sorted(enabled))})")
                continue
            if key not in scope:
                scope.append(key)
        if scope:
            warn("observation_scope", "the observation scope only selects what is highlighted: a fault on a "
                                      "shared service can affect every product profile, and all of them are "
                                      "measured and reported")

    fan_in = _fan_in(dep_map)
    apply_order = [a["id"] for a in sorted(valid, key=lambda a: (a["start_offset_s"], a["index"]))]
    rank = {aid: n for n, aid in enumerate(apply_order)}
    # Services that others depend on come back first; ties undo in reverse order of application.
    rollback_order = [a["id"] for a in sorted(valid, key=lambda a: (-fan_in.get(a["target"], 0), -rank[a["id"]]))]

    return {"ok": not errors, "errors": errors, "warnings": warnings, "actions": actions,
            "affected_services": targets, "cumulative_intensity": intensity,
            "max_concurrent_faults": concurrent, "apply_order": apply_order,
            "rollback_order": rollback_order, "limits": limits, "policy": policy,
            "observe_profiles": scope, "signature": signature(valid) if valid else ""}


async def preflight_check(scn: Scenario, dep_map: dict | None = None) -> dict:
    """Read-only check of the real containers for every action. Mutates nothing: the only
    commands issued are docker version/ps/inspect, plus a short-lived helper container that
    READS tc state for network faults."""
    report = validate(scn, dep_map)
    results: list[dict] = []
    if not report["ok"]:
        return {"ok": False, "validation": report, "results": results, "error": "scenario failed validation"}
    try:
        await guard.docker_preflight()
    except guard.GuardError as e:
        return {"ok": False, "validation": report, "results": results, "error": str(e)}
    ok = True
    for a in report["actions"]:
        entry = {"id": a["id"], "fault": a["fault"], "target": a["target"], "ok": False,
                 "container": None, "detail": ""}
        try:
            container = await guard.resolve_container(a["target"], dry_run=False)
            entry["container"] = {"name": container.name, "id": container.id}
            await faults.build(a["fault"], a["target"], container, a["params"]).preflight()
            entry.update(ok=True, detail="ready")
        except guard.GuardError as e:
            ok = False
            entry["detail"] = str(e)
        results.append(entry)
    return {"ok": ok, "validation": report, "results": results, "error": None}


# ---------------------------------------------------------------- lockout / journal
def _atomic_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_lockout() -> dict | None:
    """None when mutation is allowed. A lockout file that cannot be parsed still locks."""
    if not os.path.isfile(config.LOCKOUT_FILE):
        return None
    try:
        with open(config.LOCKOUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        return {"reason": f"lockout file exists but could not be read ({e})", "items": [],
                "created_at_utc": None}


def write_lockout(experiment_id: str, reason: str, items: list[dict]) -> dict:
    existing = read_lockout() or {}
    info = {"experiment_id": experiment_id, "reason": reason, "created_at_utc": guard.utc_now_iso(),
            "items": (existing.get("items") or []) + items}
    _atomic_write(config.LOCKOUT_FILE, info)
    return info


def clear_lockout() -> None:
    try:
        os.remove(config.LOCKOUT_FILE)
    except FileNotFoundError:
        pass


def _journal_path(experiment_id: str) -> str:
    return os.path.join(config.JOURNAL_DIR, f"{experiment_id}.json")


def list_journals() -> list[dict]:
    try:
        names = sorted(n for n in os.listdir(config.JOURNAL_DIR) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        try:
            with open(os.path.join(config.JOURNAL_DIR, n), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            out.append({"id": n[:-5], "unreadable": True, "actions": []})
    return out


_NEEDS_ROLLBACK = {"applying", "applied", "uncertain", "rolling_back", "rollback_failed"}


async def recover_orphans(busy_ids: set[str] | None = None) -> dict:
    """Roll back whatever a previous process left behind. Called on startup and when an
    operator asks to resolve a lockout. Idempotent. Fails closed: anything that cannot be
    verified clean (including because Docker is unreachable) leaves the lockout in place."""
    recovered, failed = [], []
    for journal in list_journals():
        jid = journal.get("id")
        if busy_ids and jid in busy_ids:
            continue
        if journal.get("unreadable"):
            failed.append({"experiment_id": jid, "action": None, "error": "journal file is unreadable",
                           "manual": ["inspect the Ditto containers with `docker ps -a` and "
                                      "`tc qdisc show` and restore them by hand"]})
            continue
        clean = True
        for a in journal.get("actions", []):
            # Everything that was ever touched is re-checked, including actions already
            # marked rolled_back: a journal only survives a run that ended unverified, and
            # rollback is idempotent, so re-running it is how the target itself is verified.
            if a.get("status") == "pending":
                continue
            try:
                fault = faults.from_recovery_info(a["recovery"])
                await asyncio.wait_for(fault.rollback(), timeout=config.ROLLBACK_STEP_TIMEOUT_S)
                verified, detail = await fault.verify_clean()
                if not verified:
                    raise guard.RollbackError(f"still not clean after rollback: {detail}")
                a["status"] = "rolled_back"
                recovered.append({"experiment_id": jid, "action": a.get("id"), "fault": a["recovery"]["fault"],
                                  "target": a["recovery"]["service"], "detail": detail})
            except Exception as e:  # noqa: BLE001 -- every failure must be reported, none may stop the loop
                clean = False
                a["status"] = "rollback_failed"
                manual = []
                try:
                    manual = faults.from_recovery_info(a["recovery"]).manual_recovery()
                except Exception:  # noqa: BLE001
                    pass
                failed.append({"experiment_id": jid, "action": a.get("id"),
                               "fault": (a.get("recovery") or {}).get("fault"),
                               "target": (a.get("recovery") or {}).get("service"),
                               "error": f"{type(e).__name__}: {e}"[:600], "manual": manual})
        if clean:
            try:
                os.remove(_journal_path(jid))
            except OSError:
                pass
        else:
            _atomic_write(_journal_path(jid), journal)
    if failed:
        info = {"experiment_id": failed[0]["experiment_id"], "created_at_utc": guard.utc_now_iso(),
                "reason": "faults left by an earlier run could not be verified as removed", "items": failed}
        _atomic_write(config.LOCKOUT_FILE, info)
    elif read_lockout() is not None and not list_journals():
        clear_lockout()
    return {"recovered": recovered, "failed": failed, "lockout": read_lockout()}


# ---------------------------------------------------------------- the run
async def _uninterruptible(coro):
    """Run `coro` to completion even if the surrounding task is cancelled meanwhile.
    Returns (result, was_cancelled)."""
    task = asyncio.ensure_future(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


class _Action:
    def __init__(self, plan: dict):
        self.id, self.fault_name, self.target = plan["id"], plan["fault"], plan["target"]
        self.params, self.offset, self.end = plan["params"], plan["start_offset_s"], plan["end_s"]
        self.duration, self.intensity = plan["duration_s"], plan["intensity"]
        self.fault: faults.Fault | None = None
        self.status = "pending"
        self.applied_at_s: float | None = None
        self.rolled_back_at_s: float | None = None
        self.rollback_verified = False
        self.error: str | None = None
        self.rollback_error: str | None = None
        self.observed: dict | None = None

    def to_dict(self) -> dict:
        c = self.fault.container if self.fault else None
        return {"id": self.id, "fault": self.fault_name, "target": self.target, "params": self.params,
                "start_offset_s": self.offset, "duration_s": self.duration, "intensity": self.intensity,
                "status": self.status, "applied_at_s": self.applied_at_s,
                "rolled_back_at_s": self.rolled_back_at_s, "rollback_verified": self.rollback_verified,
                "error": self.error, "rollback_error": self.rollback_error, "observed": self.observed,
                "container": {"name": c.name, "id": c.id} if c else None,
                "recovery_state": dict(self.fault.state) if self.fault else None}


class ScenarioRun:
    def __init__(self, scn: Scenario, authorization: Authorization | None, *, dep_map: dict | None = None,
                 policy: dict | None = None, stop_event: asyncio.Event | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 on_update: Callable[[dict], None] | None = None, meta: dict | None = None,
                 dry_run: bool = False, fresh_baseline: bool = False):
        self.scn, self.authorization, self.dep_map, self.policy = scn, authorization, dep_map, policy
        # True = ignore baseline.json and measure the baseline from this run's own pre-check
        # ticks (used by follow-up runs, which must re-capture a healthy baseline).
        self.fresh_baseline = fresh_baseline
        # A dry run walks the same lifecycle with real health probes but executes NO Docker
        # command at all: targets are synthetic and every command is only recorded.
        self.dry_run = dry_run
        self.stop_event = stop_event or asyncio.Event()
        self._on_event, self._on_update = on_event, on_update
        self.meta = meta or {}
        self.id = f"exp-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.th = guard.Thresholds.from_env()
        self.actions: list[_Action] = []
        self.report: dict = {}
        self._locks: list[guard.Lock] = []
        self._prepared = False
        self._t0: float | None = None
        self.record: dict = {}

    # ---- synchronous gate: refuse now, or hold the locks ----
    def prepare(self) -> dict:
        if not self.dry_run:
            if not isinstance(self.authorization, Authorization):
                raise ScenarioRejected("no authorization: a scenario runs only with an explicit operator "
                                       "authorization or an autonomous-sandbox policy grant", code="unauthorized")
            lock = read_lockout()
            if lock is not None:
                raise ScenarioRejected(f"mutation is disabled: recovery is required for experiment "
                                       f"{lock.get('experiment_id')} ({lock.get('reason')})",
                                       code="recovery_required")
        try:
            self.th.validate()
        except guard.GuardError as e:
            raise ScenarioRejected(str(e), code="invalid_thresholds") from e
        self.report = validate(self.scn, self.dep_map, self.policy)
        if not self.report["ok"]:
            raise ScenarioRejected("scenario failed policy validation", errors=self.report["errors"],
                                   code="policy_violation")
        if not self.dry_run:    # a dry run mutates nothing, so it holds no lock
            try:
                self._locks.append(guard.acquire_lock(SCENARIO_LOCK))
                for target in self.report["affected_services"]:
                    self._locks.append(guard.acquire_lock(target))
            except guard.GuardError as e:
                self.release()
                raise ScenarioRejected(str(e), code="locked") from e
        by_id = {a["id"]: a for a in self.report["actions"]}
        self.actions = [_Action(by_id[i]) for i in self.report["apply_order"]]
        self._prepared = True
        self._init_record()
        rep_ = self.report
        what = " + ".join(f"{a.fault_name} on {a.target}" for a in self.actions)
        if self.dry_run:
            self._log("info", f"Dry run {self.id} started: {what}. No Docker command will be executed and "
                              f"nothing will be changed.", dry_run=True)
            self._log("info", "Dry run needs no authorization, lock or recovery check because it mutates nothing.")
        else:
            self._log("info", f"Experiment {self.id} started (REAL fault injection): {what}.", dry_run=False)
            self._log("info", f"Authorized by {self.authorization.actor} "
                              f"({self.authorization.kind.replace('_', ' ')}).", authorization=self.authorization.model_dump())
            self._log("info", "No recovery lockout is active.")
            self._log("info", f"Acquired the scenario lock and a lock for each target "
                              f"({', '.join(rep_['affected_services'])}): no other experiment can touch them.")
        self._log("decision",
                  f"Policy check passed: every target is allowlisted, every parameter is within bounds, "
                  f"{len(rep_['affected_services'])} service(s) (limit {rep_['limits']['max_affected_services']}), "
                  f"{rep_['max_concurrent_faults']} concurrent fault(s) (limit {rep_['limits']['max_active_faults']}), "
                  f"intensity {rep_['cumulative_intensity']:g} (budget {rep_['limits']['max_cumulative_intensity']:g}).",
                  apply_order=rep_["apply_order"], rollback_order=rep_["rollback_order"],
                  warnings=[w["message"] for w in rep_["warnings"]])
        return self.report

    def release(self) -> None:
        for lock in reversed(self._locks):
            guard.release_lock(lock)
        self._locks = []

    # ---- record / events ----
    def _init_record(self) -> None:
        acts = self.report["actions"]
        single = acts[0] if len(acts) == 1 else None
        targets = self.report["affected_services"]
        edges = [f"{e['from']}->{e['to']}" for e in (self.dep_map or {}).get("edges", [])
                 if e.get("from") in targets or e.get("to") in targets]
        self.record = {
            "schema_version": SCHEMA_VERSION, "software_version": config.SOFTWARE_VERSION,
            "guard_version": guard.GUARD_VERSION, "kind": "scenario",
            "id": self.id, "scenario_id": self.meta.get("scenario_id") or self.id,
            "state": guard.STATE_PRECHECK,
            "fault": single["fault"] if single else "multi",
            "target": single["target"] if single else ", ".join(targets),
            "params": single["params"] if single else {},
            "signature": self.report["signature"],
            "scenario": self.scn.model_dump(), "plan": {k: self.report[k] for k in (
                "apply_order", "rollback_order", "cumulative_intensity", "max_concurrent_faults", "limits",
                "warnings")},
            "actions": [], "targets": {},
            "authorization": self.authorization.model_dump() if isinstance(self.authorization, Authorization)
            else None,
            "mode": self.meta.get("mode"), "source": self.meta.get("source", "operator"),
            "proposal": self.meta.get("proposal"), "prediction": self.meta.get("prediction"),
            "followup_of": self.meta.get("followup_of"), "followup_comparison": None,
            "hypothesis": self.scn.hypothesis, "dependency_context": edges,
            "dry_run": self.dry_run, "simulated": self.dry_run,
            "events": [], "event_seq": 0,
            "thresholds": asdict(self.th), "baseline": None, "baseline_source": None,
            "started_at_utc": guard.utc_now_iso(), "ended_at_utc": None, "started": None,
            "injected": False, "injection_error": None,
            "aborted": False, "abort_reason": None, "detected_s": None,
            "reverted": False, "reverted_at": None, "rollback_verified": False, "rollback_error": None,
            "recovery_s": None, "recovery_outcome": None, "error": None,
            "safety_events": [], "timeline": [], "during": None,
            "fleet": fleet.record_metadata(), "observe_profiles": self.report.get("observe_profiles"),
            "profile_impact": None,
        }
        self._sync()

    def _elapsed(self) -> float | None:
        return None if self._t0 is None else round(clock() - self._t0, 2)

    def _sync(self) -> None:
        self.record["actions"] = [a.to_dict() for a in self.actions]
        if self._on_update is not None:
            self._on_update(self.record)

    def _emit(self, kind: str, data: dict) -> None:
        if self._on_event is not None:
            self._on_event(kind, {"experiment_id": self.id, **data})

    def _set_state(self, state: str) -> None:
        self.record["state"] = state
        self._sync()
        self._emit("experiment", {"state": state, "phase": state, "t_s": self._elapsed(),
                                  "active_faults": self._active_count()})

    def _log(self, kind: str, message: str, **detail) -> None:
        """Activity-log entry. Recording only: never raises, never affects a decision."""
        activity.emit(self.record, kind, message, **detail)

    def _safety(self, kind: str, detail: str, level: str = "warning") -> None:
        ev = {"t_s": self._elapsed(), "ts": round(time.time(), 3), "kind": kind, "detail": detail}
        self.record["safety_events"].append(ev)
        self._emit("alert", {"level": level, "kind": kind, "message": detail})

    def _active_count(self) -> int:
        return sum(1 for a in self.actions if a.status in ("applying", "applied", "uncertain"))

    def _journal(self) -> None:
        _atomic_write(_journal_path(self.id), {
            "id": self.id, "written_at_utc": guard.utc_now_iso(), "pid": os.getpid(),
            "actions": [{"id": a.id, "status": a.status, "recovery": a.fault.recovery_info()}
                        for a in self.actions if a.fault is not None]})

    # ---- mutation steps ----
    async def _apply(self, a: _Action) -> None:
        a.status, a.applied_at_s = "applying", self._elapsed()
        self.record["injected"] = True
        self._journal()   # on disk BEFORE the command runs
        self._log("info", f"Applying {a.fault_name} to {a.target} ({a.id}); recovery information was saved first.",
                  action=a.id, fault=a.fault_name, target=a.target, params=a.params)
        self._sync()
        try:
            await asyncio.wait_for(a.fault.apply(), timeout=APPLY_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 -- includes TimeoutError; state is now uncertain
            a.status = "uncertain"
            a.error = f"{type(e).__name__}: {e}"[:800]
            # A failed or timed-out command may still have taken effect: look, never assume.
            a.observed = await a.fault.safe_inspect()
            self._journal()
            self._sync()
            self._safety("apply_failed", f"{a.fault_name} on {a.target}: {a.error} "
                                         f"(observed: {a.observed['detail']})", "error")
            self._log("error", f"Applying {a.fault_name} to {a.target} failed or timed out: {a.error}. Its real "
                               f"state was inspected ({a.observed['detail']}); it will be rolled back, not retried.",
                      action=a.id, observed=a.observed)
            raise
        a.status = "applied"
        self._journal()
        self._log("info", f"{a.fault_name} is active on {a.target} ({self._active_count()} fault(s) active).",
                  action=a.id, active_faults=self._active_count())
        self._sync()
        self._emit("experiment", {"state": self.record["state"], "phase": "fault_applied", "action": a.id,
                                  "fault": a.fault_name, "target": a.target, "t_s": self._elapsed(),
                                  "active_faults": self._active_count()})
        print(f"[scenario] >>> {a.fault_name} applied to {a.target} ({a.id})", flush=True)

    async def _rollback_one(self, a: _Action) -> bool:
        """Never raises. True only when the clean state was observed."""
        a.status = "rolling_back"
        self._log("info", f"Removing {a.fault_name} from {a.target} ({a.id}) and verifying the result.", action=a.id)
        self._sync()
        try:
            await asyncio.wait_for(a.fault.rollback(), timeout=config.ROLLBACK_STEP_TIMEOUT_S)
            verified, detail = await a.fault.verify_clean()
            a.observed = {"active": not verified, "detail": detail}
            if not verified:
                raise guard.RollbackError(f"rollback ran but the fault is not confirmed removed: {detail}")
            a.status, a.rollback_verified, a.rollback_error = "rolled_back", True, None
            a.rolled_back_at_s = self._elapsed()
            self._log("info", f"Removal verified: {a.fault_name} is confirmed gone from {a.target} ({detail}).",
                      action=a.id, verified=True, observed=detail)
            print(f"[scenario] <<< {a.fault_name} on {a.target} reverted (verified)", flush=True)
        except Exception as e:  # noqa: BLE001 -- one failed rollback must never stop the others
            a.status = "rollback_failed"
            a.rollback_error = f"{type(e).__name__}: {e}"[:800]
            self._safety("rollback_failed", f"{a.fault_name} on {a.target}: {a.rollback_error}", "critical")
            self._log("error", f"Removal of {a.fault_name} from {a.target} could NOT be verified: "
                               f"{a.rollback_error}. The fault may still be active; continuing with the others.",
                      action=a.id, verified=False, manual=a.fault.manual_recovery())
            print(f"[scenario] REVERT FAILED for {a.fault_name} on {a.target}: {e}", flush=True)
        try:
            self._journal()
        except OSError as e:
            print(f"[scenario] WARNING: could not update the recovery journal: {e}", flush=True)
        self._sync()
        self._emit("experiment", {"state": self.record["state"], "phase": "fault_rolled_back", "action": a.id,
                                  "verified": a.rollback_verified, "t_s": self._elapsed(),
                                  "active_faults": self._active_count()})
        return a.rollback_verified

    async def _rollback_all(self) -> None:
        order = {aid: n for n, aid in enumerate(self.report["rollback_order"])}
        pending = sorted((a for a in self.actions if a.status in _NEEDS_ROLLBACK),
                         key=lambda a: order.get(a.id, 0))
        for a in pending:
            await self._rollback_one(a)
        # Final check of every affected target, independent of the per-fault verification.
        for target in self.report["affected_services"]:
            fault = next((a.fault for a in self.actions if a.target == target and a.fault), None)
            if fault is None:
                continue
            entry = {"container": fault.container.name, "running": None, "verified": False, "detail": ""}
            try:
                st = await faults.inspect_container(fault.container.id)
                if st["exists"]:
                    entry["running"] = st["running"] and not st["paused"]
                    entry["detail"] = f"running={st['running']} paused={st['paused']}"
                else:
                    current = await faults.service_container(target)
                    entry["running"] = bool(current and current["state"] == "running")
                    entry["detail"] = "original container replaced" if entry["running"] else "no container found"
                entry["verified"] = bool(entry["running"])
            except guard.GuardError as e:
                entry["detail"] = f"could not inspect: {e}"[:400]
            self.record["targets"][target] = entry
            if entry["verified"]:
                self._log("info", f"Final check: the container for {target} is running ({entry['detail']}).",
                          target=target, verified=True)
            else:
                self._log("error", f"Final check FAILED for {target}: {entry['detail']}.", target=target,
                          verified=False)
        self._sync()

    # ---- main ----
    async def run(self) -> dict:
        if not self._prepared:
            raise RuntimeError("ScenarioRun.prepare() must succeed before run()")
        rec = self.record
        cancelled = False
        stage = "pre"          # pre -> injected -> rolled_back
        baseline: dict = {}
        sink_token = guard._command_sink.set(activity.command_sink(rec))
        try:
            async with httpx.AsyncClient(base_url=config.DITTO_URL, auth=(config.DITTO_USER, config.DITTO_PASS),
                                         timeout=config.REQUEST_TIMEOUT_S) as client:
                ids = fleet.current().ids

                async def tick(phase: str) -> dict:
                    t = clock()
                    try:
                        row = await asyncio.wait_for(probe.probe_once(client, ids),
                                                     timeout=guard.PROBE_HARD_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        row = guard.timeout_row(phase)
                    # asyncio.wait_for() on Python 3.11 swallows a cancellation that lands
                    # in the same loop iteration its inner call completes. Without this
                    # check a shutdown/cancel could be silently ignored for a whole run.
                    me = asyncio.current_task()
                    if me is not None and me.cancelling():
                        raise asyncio.CancelledError()
                    row["phase"], row["active_faults"] = phase, self._active_count()
                    rec["timeline"].append(row)
                    activity.health(rec, row)
                    self._sync()
                    self._emit("metric", {"row": row, "source": "controller"})
                    await asyncio.sleep(max(0.0, config.PROBE_INTERVAL_S - (clock() - t)))
                    return row

                # ---- PREFLIGHT: every target, before anything is touched ----
                self._set_state(STATE_PREFLIGHT)
                if self.dry_run:
                    for a in self.actions:
                        container = await guard.resolve_container(a.target, dry_run=True)
                        a.fault = faults.build(a.fault_name, a.target, container, a.params)
                        self._log("info", f"Dry run: using synthetic container '{container.name}' for {a.target}; "
                                          f"Docker is not contacted, so preflight checks are skipped.",
                                  target=a.target, container=container.name, synthetic=True)
                else:
                    self._log("info", "Preflight: checking Docker and every target before anything is touched. "
                                      "If any check fails, nothing is injected.")
                    await guard.docker_preflight()
                    for a in self.actions:
                        container = await guard.resolve_container(a.target, dry_run=False)
                        a.fault = faults.build(a.fault_name, a.target, container, a.params)
                        self._log("info", f"Target {a.target} resolved to container '{container.name}' "
                                          f"(id {container.id[:12]}), running.",
                                  target=a.target, container=container.name, container_id=container.id)
                    for a in self.actions:
                        await a.fault.preflight()
                        self._log("info", f"Preflight passed for {a.fault_name} on {a.target}.", action=a.id)
                self._sync()

                # ---- BASELINE ----
                self._set_state(guard.STATE_BASELINE)
                pre_ticks = max(1, round(self.scn.pre_s / config.PROBE_INTERVAL_S))
                pre = [await tick("before") for _ in range(pre_ticks)]
                if not all(r["direct_ok"] and r["search_ok"] for r in pre[-min(2, len(pre)):]):
                    self._log("decision", "Refusing to inject: Ditto was not healthy on the pre-check ticks, so "
                                          "a fault would be indistinguishable from the existing problem.")
                    raise guard.GuardError("Ditto is not healthy BEFORE the scenario (direct and/or search "
                                           "failing); refusing to inject.")
                self._log("info", f"Pre-check passed: Ditto answered direct reads and searches on the last "
                                  f"{min(2, len(pre))} of {len(pre)} tick(s).")
                file_baseline = None if self.fresh_baseline else guard.load_baseline()
                baseline, source = (file_baseline, "file") if file_baseline is not None \
                    else (probe.summarize(pre), "fresh")
                try:
                    guard.validate_baseline(baseline, source, guard.MAX_BASELINE_AGE_S_DEFAULT)
                except guard.GuardError as e:
                    if source != "file":
                        raise
                    # The saved baseline cannot be trusted (usually: expired). Thresholds are
                    # never taken from it; a baseline is measured now instead, and it has to
                    # pass the same validation or the run is refused. A dry run keeps its
                    # short pre-check, a real run measures for FRESH_BASELINE_MIN_S.
                    extra = 0 if self.dry_run else max(
                        0, round(FRESH_BASELINE_MIN_S / config.PROBE_INTERVAL_S) - len(pre))
                    window_s = (len(pre) + extra) * config.PROBE_INTERVAL_S
                    self._log("warning", f"The saved baseline is not used: {e} Measuring a fresh baseline over "
                                         f"{window_s:g}s instead; nothing is injected unless Ditto is healthy "
                                         f"for that whole window.",
                              baseline_fallback=True)
                    rec["baseline_fallback"] = str(e)
                    pre += [await tick("before") for _ in range(extra)]
                    baseline, source = probe.summarize(pre), "fresh"
                    guard.validate_baseline(baseline, source, guard.MAX_BASELINE_AGE_S_DEFAULT)
                rec["baseline"], rec["baseline_source"] = baseline, source
                activity.thresholds(rec, baseline, self.th, source)

                if self.stop_event.is_set():
                    rec["aborted"], rec["abort_reason"] = True, "manual stop (requested before injection)"
                    rec["recovery_outcome"] = "skipped_manual_abort"
                    self._log("decision", "Stop requested before injection: nothing was touched, so there is "
                                          "nothing to revert.", reason="manual_stop")
                    self._set_state(guard.STATE_ABORTED)
                    return self._finish()

                if self.dry_run:
                    return await self._dry_run_body(tick, baseline)

                # ---- INJECTING / MONITORING ----
                self._journal()  # recovery info for every action is on disk before the first mutation
                rec["started"] = time.time()
                self._t0 = clock()
                self._set_state(guard.STATE_INJECTING)
                stage = "injected"
                window: list[dict] = []
                try:
                    while clock() - self._t0 < self.scn.max_duration_s:
                        elapsed = clock() - self._t0
                        for a in self.actions:
                            if a.status == "pending" and a.offset <= elapsed:
                                await self._apply(a)
                        if rec["state"] == guard.STATE_INJECTING:
                            self._set_state(guard.STATE_MONITORING)
                        for a in self.actions:
                            if a.status == "applied" and a.duration is not None and elapsed >= a.end:
                                self._log("decision", f"{a.fault_name} on {a.target} reached its own "
                                                      f"{a.duration:g}s duration: removing it now.", action=a.id)
                                if not await self._rollback_one(a):
                                    raise guard.RollbackError(f"scheduled removal of {a.fault_name} on "
                                                              f"{a.target} could not be verified")
                        window.append(await tick("fault"))
                        if self.stop_event.is_set():
                            rec["aborted"], rec["abort_reason"] = True, "emergency stop (requested by operator)"
                            rec["detected_s"] = self._elapsed()
                            self._safety("emergency_stop", "stop requested; rolling back every active fault")
                            self._log("decision", "Emergency stop requested from the dashboard: rolling back "
                                                  "every active fault now.", reason="manual_stop")
                            break
                        breached, reason = guard.check_breach(window, baseline, self.th)
                        if breached:
                            rec["aborted"], rec["abort_reason"] = True, reason
                            rec["detected_s"] = self._elapsed()
                            self._safety("kill_switch", reason, "error")
                            self._log("decision", f"Kill switch triggered: {reason}. Rolling back every active "
                                                  f"fault now.", reason=reason, kill_switch=True)
                            print(f"[scenario] !!! KILL SWITCH: {reason} -> reverting", flush=True)
                            break
                        self._log("decision", activity.within_limits(window[-1], baseline, self.th))
                    else:
                        self._log("decision", f"Scenario duration of {self.scn.max_duration_s:g}s reached without "
                                              f"a stop condition: rolling back.", reason="max_duration")
                except asyncio.CancelledError:
                    cancelled = True
                    rec["aborted"] = True
                    rec["abort_reason"] = "cancelled (shutdown or task cancellation) while faults were active"
                    self._log("decision", "The run was cancelled (API shutdown or task cancellation): rolling "
                                          "back every active fault before exiting.", reason="cancelled")
                except Exception as e:  # noqa: BLE001
                    if any(a.status == "uncertain" for a in self.actions):
                        rec["injection_error"] = rec["injection_error"] or f"{type(e).__name__}: {e}"[:800]
                    else:
                        self._log("error", f"Unexpected error while faults were active: {type(e).__name__}: {e}. "
                                           f"Rolling back.")
                    rec["error"] = guard._join(rec["error"], f"{type(e).__name__}: {e}"[:800])
                finally:
                    rec["state"] = guard.STATE_REVERTING
                    order = [a for a in self.actions if a.status in _NEEDS_ROLLBACK]
                    self._log("info", f"Rolling back {len(order)} fault(s), most depended-on service first; each "
                                      f"is verified independently and a failure on one does not stop the others.")
                    self._emit("experiment", {"state": guard.STATE_REVERTING, "phase": guard.STATE_REVERTING,
                                              "t_s": self._elapsed(), "active_faults": self._active_count()})
                    _, was_cancelled = await _uninterruptible(self._rollback_all())
                    cancelled = cancelled or was_cancelled
                    stage = "rolled_back"

                failed = [a for a in self.actions if a.status == "rollback_failed"]
                bad_targets = [t for t, e in rec["targets"].items() if not e["verified"]]
                touched = [a for a in self.actions if a.status != "pending"]
                rec["reverted"] = bool(touched) and not failed
                rec["rollback_verified"] = bool(touched) and not failed and not bad_targets
                rec["reverted_at"] = time.time() if rec["reverted"] else None
                if failed or bad_targets:
                    parts = [f"{a.fault_name}@{a.target}: {a.rollback_error}" for a in failed]
                    parts += [f"{t}: {rec['targets'][t]['detail']}" for t in bad_targets
                              if t not in {a.target for a in failed}]
                    rec["rollback_error"] = "; ".join(parts)
                    rec["error"] = guard._join(rec["error"], f"REVERT NOT VERIFIED: {rec['rollback_error']}")
                    return self._finish(STATE_RECOVERY_REQUIRED, failed, bad_targets)

                # ---- VERIFYING_RECOVERY ----
                if cancelled:
                    rec["recovery_outcome"] = "skipped_cancelled"
                    self._log("warning", "All faults were removed and verified, but health recovery was not "
                                         "measured because the run was cancelled.")
                    return self._finish(guard.STATE_ABORTED)
                self._set_state(guard.STATE_VERIFYING_RECOVERY)
                self._log("info", f"Verifying recovery: {guard.RECOVERY_STREAK} consecutive healthy ticks are "
                                  f"required within {self.scn.recovery_timeout_s:g}s.")
                streak, start, recovered = 0, clock(), False
                try:
                    while clock() - start < self.scn.recovery_timeout_s:
                        row = await tick("recovery")
                        ok_now = guard.is_healthy(row, baseline, self.th)
                        streak = streak + 1 if ok_now else 0
                        self._log("decision",
                                  f"{streak}/{guard.RECOVERY_STREAK} healthy ticks." if ok_now else
                                  f"Not healthy yet (requests failing, latency above the recovery limit, or data "
                                  f"stale): streak reset to 0/{guard.RECOVERY_STREAK}.",
                                  healthy_streak=streak, required=guard.RECOVERY_STREAK)
                        if streak >= guard.RECOVERY_STREAK:
                            rec["recovery_s"] = round(clock() - start
                                                      - (guard.RECOVERY_STREAK - 1) * config.PROBE_INTERVAL_S, 2)
                            rec["recovery_outcome"], recovered = "recovered", True
                            self._log("info", f"Recovery verified: Ditto was healthy again {rec['recovery_s']}s "
                                              f"after the rollback.", recovery_s=rec["recovery_s"])
                            break
                except asyncio.CancelledError:
                    rec["recovery_outcome"] = "skipped_cancelled"
                    return self._finish(guard.STATE_ABORTED)
                if not recovered:
                    rec["recovery_outcome"] = "timeout"
                    rec["error"] = guard._join(rec["error"],
                                               f"not recovered within {self.scn.recovery_timeout_s:g}s")
                    self._safety("recovery_timeout", rec["error"], "error")
                    self._log("error", f"Recovery NOT verified: Ditto did not return to a healthy state within "
                                       f"{self.scn.recovery_timeout_s:g}s. The faults themselves were removed.")
                    return self._finish(guard.STATE_RECOVERY_TIMEOUT)
                return self._finish(guard.STATE_FAILED if rec["error"] else guard.STATE_COMPLETED)

        except asyncio.CancelledError:
            rec["aborted"] = True
            rec["abort_reason"] = rec["abort_reason"] or "cancelled before any fault was applied"
            return self._finish(guard.STATE_ABORTED if stage == "pre" else rec["state"])
        except guard.GuardError as e:
            rec["error"] = guard._join(rec["error"], str(e))
            self._log("error", f"Stopped before anything was injected: {e}" if stage == "pre" else f"Stopped: {e}")
            print(f"[scenario] ERROR: {e}", flush=True)
            return self._finish(guard.STATE_ABORTED if stage == "pre" else guard.STATE_FAILED)
        except Exception as e:  # noqa: BLE001
            rec["error"] = guard._join(rec["error"], f"{type(e).__name__}: {e}")
            self._log("error", f"Unexpected error: {type(e).__name__}: {e}")
            print(f"[scenario] UNEXPECTED ERROR: {e}", flush=True)
            return self._finish(guard.STATE_ABORTED if stage == "pre" else guard.STATE_FAILED)
        finally:
            guard._command_sink.reset(sink_token)
            self.release()

    async def _dry_run_body(self, tick, baseline: dict) -> dict:
        """INJECTING..VERIFYING_RECOVERY for a dry run. Docker is never contacted: apply
        commands are captured through run_cmd's forced dry-run switch (exact argv, not
        executed); rollback steps are shown from each fault's description."""
        rec = self.record
        self._t0 = clock()
        self._set_state(guard.STATE_INJECTING)
        token = guard._force_dry_run.set(True)
        try:
            for a in self.actions:
                self._log("info", f"Dry run: {a.fault_name} on {a.target} would be applied at "
                                  f"+{a.offset:g}s. Showing its command; it is NOT executed.", action=a.id)
                try:
                    await a.fault.apply()
                except Exception:  # noqa: BLE001 -- apply()'s own verification cannot pass when nothing ran
                    pass
                a.status = "previewed"
        finally:
            guard._force_dry_run.reset(token)
        self._set_state(guard.STATE_MONITORING)
        self._log("info", "Dry run: no fault is active. Taking 3 monitoring ticks to show what a real run "
                          f"would check every {config.PROBE_INTERVAL_S:g}s for up to {self.scn.max_duration_s:g}s.")
        for _ in range(3):
            row = await tick("dry_run")
            self._log("decision", activity.within_limits(row, baseline, self.th))
        self._set_state(guard.STATE_REVERTING)
        order = {aid: n for n, aid in enumerate(self.report["rollback_order"])}
        for a in sorted(self.actions, key=lambda a: order.get(a.id, 0)):
            self._log("info", f"Dry run: rollback of {a.fault_name} on {a.target} would run next (not executed).",
                      action=a.id)
            for text in a.fault.planned_commands():
                if text.startswith("(rollback)"):
                    activity.preview_command(rec, text[len("(rollback)"):].strip())
        self._set_state(guard.STATE_VERIFYING_RECOVERY)
        self._log("info", f"Dry run: a real run would now require {guard.RECOVERY_STREAK} consecutive healthy "
                          f"ticks within {self.scn.recovery_timeout_s:g}s. Nothing was applied, so there is "
                          f"nothing to recover from.")
        rec["recovery_outcome"] = "not_applicable_dry_run"
        return self._finish(guard.STATE_COMPLETED)

    def _finish(self, state: str | None = None, failed: list[_Action] | None = None,
                bad_targets: list[str] | None = None) -> dict:
        rec = self.record
        if state:
            rec["state"] = state
        fault_rows = [r for r in rec["timeline"] if r.get("phase") == "fault"]
        rec["during"] = probe.summarize(fault_rows) if fault_rows else None
        rec["profile_impact"] = fleet.profile_impact(rec["timeline"], rec.get("observe_profiles"))
        rec["ended_at_utc"] = guard.utc_now_iso()
        if rec["state"] == STATE_RECOVERY_REQUIRED:
            items = [{"experiment_id": self.id, "action": a.id, "fault": a.fault_name, "target": a.target,
                      "error": a.rollback_error, "manual": a.fault.manual_recovery() if a.fault else []}
                     for a in (failed or [])]
            items += [{"experiment_id": self.id, "action": None, "fault": None, "target": t,
                       "error": rec["targets"][t]["detail"],
                       "manual": [f"docker start {rec['targets'][t]['container']}"]}
                      for t in (bad_targets or []) if t not in {a.target for a in (failed or [])}]
            rec["recovery_instructions"] = items
            try:
                write_lockout(self.id, "rollback could not be verified", items)
            except OSError as e:
                rec["error"] = guard._join(rec["error"], f"could not write the lockout file: {e}")
            self._safety("recovery_required", "rollback could not be verified; mutation is disabled until "
                                              "an operator resolves it", "critical")
            self._log("error", "RECOVERY REQUIRED: at least one fault could not be confirmed as removed. All "
                               "further fault injection is blocked until an operator resolves it.",
                      instructions=items)
        else:
            try:
                os.remove(_journal_path(self.id))
            except OSError:
                pass
        outcome = {guard.STATE_COMPLETED: "completed", guard.STATE_ABORTED: "was aborted",
                   guard.STATE_FAILED: "FAILED", guard.STATE_RECOVERY_TIMEOUT: "ended without verified recovery",
                   STATE_RECOVERY_REQUIRED: "ended with RECOVERY REQUIRED"}.get(rec["state"], rec["state"])
        tail = (" This was a dry run: no command was executed and nothing was changed." if self.dry_run else
                " Every applied fault was removed and verified." if rec["rollback_verified"] else
                "" if not rec["injected"] else " Rollback was NOT fully verified.")
        bad = rec["state"] in (guard.STATE_FAILED, guard.STATE_RECOVERY_TIMEOUT, STATE_RECOVERY_REQUIRED)
        self._log("error" if bad else "info", f"Experiment {outcome}.{tail}", final=True)
        self._sync()
        self._emit("experiment", {"state": rec["state"], "phase": rec["state"], "t_s": self._elapsed(),
                                  "active_faults": self._active_count(), "final": True})
        return rec
