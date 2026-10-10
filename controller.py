"""Autonomy controller: the operating mode, and the autonomous sandbox loop.

Modes (config AUTONOMY_MODE, shown in the dashboard):
  observe             nothing may be injected by anyone.
  recommend           agents propose; an operator may authorize a PROPOSED scenario.
  manual              an operator may also author and authorize a custom scenario.
  autonomous_sandbox  the loop below may run proposed scenarios without a click per
                      fault -- only inside the sandbox policy, and only while every
                      sandbox check passes. Operator-authorized runs still work.

Autonomy can never be enabled from the API or the dashboard alone: it requires
AUTONOMY_SANDBOX_ENABLED=true in the process configuration. A model's output is never an
authorization; the only two places an Authorization object is created are
operator_authorization() (an explicit operator action) and _autonomous_grant() (this
controller, after its own deterministic checks).

The loop stops itself -- it does not retry -- on any refusal, any failed sandbox check,
or any run that did not end cleanly recovered.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import config
import faults
import guard
import orchestrator as orch
import probe
import scenario
import store
from events import bus

MODES = ("observe", "recommend", "manual", "autonomous_sandbox")
MUTATING_MODES = ("recommend", "manual", "autonomous_sandbox")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class ModeError(Exception):
    """The requested action is not permitted in the current mode/configuration."""


class Controller:
    def __init__(self) -> None:
        mode = config.AUTONOMY_MODE if config.AUTONOMY_MODE in MODES else "recommend"
        if mode == "autonomous_sandbox" and not config.AUTONOMY_SANDBOX_ENABLED:
            mode = "recommend"
        self.mode = mode
        self.loop = {"running": False, "started_at_utc": None, "cycles": 0, "executed": 0,
                     "last_decision": None, "last_experiment_id": None, "stopped_reason": None,
                     "next_cycle_at": None, "phase": "idle"}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---- policy ----
    def policy(self) -> dict:
        """What the autonomous loop is allowed to do. scenario.validate() enforces it."""
        return {"allowed_faults": [f for f in config.AUTONOMOUS_FAULTS if f in faults.REGISTRY],
                "allowed_targets": [t for t in config.AUTONOMOUS_TARGETS if t in guard.ALLOWLIST],
                "max_actions": config.AUTONOMOUS_MAX_ACTIONS,
                "max_duration_s": config.AUTONOMOUS_MAX_DURATION_S}

    def status(self) -> dict:
        return {"mode": self.mode, "modes": list(MODES),
                "sandbox_enabled_in_config": config.AUTONOMY_SANDBOX_ENABLED,
                "policy": {**self.policy(), "max_experiments": config.AUTONOMOUS_MAX_EXPERIMENTS,
                           "cooldown_s": config.AUTONOMOUS_COOLDOWN_S},
                "limits": scenario.limits_from_config(), "loop": dict(self.loop),
                "lockout": scenario.read_lockout(), "auth_required": bool(config.API_TOKEN)}

    def _publish(self) -> None:
        bus.publish("controller", self.status())

    def _audit(self, actor: str, action: str, detail: dict) -> None:
        try:
            store.audit(actor, action, detail)
        except store.StoreError as e:
            print(f"[controller] audit not persisted ({action}): {e}", flush=True)

    # ---- mode ----
    async def set_mode(self, mode: str, actor: str) -> dict:
        if mode not in MODES:
            raise ModeError(f"unknown mode '{mode}'; expected one of {list(MODES)}")
        if mode == "autonomous_sandbox" and not config.AUTONOMY_SANDBOX_ENABLED:
            raise ModeError("autonomous_sandbox cannot be selected: AUTONOMY_SANDBOX_ENABLED is not true in "
                            "this process's configuration. Set it in .env and restart the API.")
        previous = self.mode
        if previous == "autonomous_sandbox" and mode != previous:
            await self.stop_autonomous(f"mode changed to {mode}")
        self.mode = mode
        self._audit(actor, "mode_changed", {"from": previous, "to": mode})
        self._publish()
        return self.status()

    # ---- authorizations ----
    def operator_authorization(self, actor: str, custom: bool) -> scenario.Authorization:
        """An operator explicitly asked to run. `custom` = the scenario was authored by the
        operator rather than proposed by the planner."""
        if self.mode == "observe":
            raise ModeError("the platform is in observe mode: no fault may be injected. "
                            "Change the mode to run an experiment.")
        if self.mode == "recommend" and custom:
            raise ModeError("recommend mode only allows authorizing a scenario the planner proposed. "
                            "Switch to manual mode to run a custom scenario.")
        return scenario.Authorization(kind="operator", actor=actor, note=f"mode={self.mode}")

    def _autonomous_grant(self, checks: list[dict]) -> scenario.Authorization:
        if self.mode != "autonomous_sandbox" or not config.AUTONOMY_SANDBOX_ENABLED:
            raise ModeError("autonomous execution is not enabled")
        failed = [c["name"] for c in checks if not c["ok"]]
        if failed:
            raise ModeError(f"sandbox checks failed: {', '.join(failed)}")
        return scenario.Authorization(kind="autonomous_policy", actor="autonomous-controller",
                                      note=f"sandbox policy {self.policy()}")

    # ---- sandbox verification (read-only) ----
    async def sandbox_checks(self) -> list[dict]:
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        add("sandbox enabled in configuration", config.AUTONOMY_SANDBOX_ENABLED,
            "AUTONOMY_SANDBOX_ENABLED=" + str(config.AUTONOMY_SANDBOX_ENABLED).lower())
        host = urlparse(config.DITTO_URL).hostname or ""
        add("Ditto is local", host in LOOPBACK, f"DITTO_URL host is '{host}' (must be loopback)")
        pol = self.policy()
        add("policy allows at least one fault and target", bool(pol["allowed_faults"] and pol["allowed_targets"]),
            f"faults={pol['allowed_faults']} targets={pol['allowed_targets']}")
        lock = scenario.read_lockout()
        add("no recovery lockout", lock is None, "clear" if lock is None else str(lock.get("reason")))
        try:
            await guard.docker_preflight()
            add("Docker daemon reachable", True, "ok")
            missing = []
            for t in pol["allowed_targets"]:
                try:
                    await guard.resolve_container(t, dry_run=False)
                except guard.GuardError as e:
                    missing.append(f"{t}: {str(e).splitlines()[0][:120]}")
            add("every allowed target is running", not missing, "; ".join(missing) or "all running")
        except guard.GuardError as e:
            add("Docker daemon reachable", False, str(e).splitlines()[0][:200])
            add("every allowed target is running", False, "not checked: Docker unreachable")
        try:
            guard.validate_baseline(guard.load_baseline(), "file", guard.MAX_BASELINE_AGE_S_DEFAULT)
            add("baseline is present, healthy and fresh", True, "ok")
        except guard.GuardError as e:
            add("baseline is present, healthy and fresh", False, str(e)[:200])
        h = probe.current_health()
        add("live health is fresh and healthy", h.get("status") == "healthy" and h.get("fresh") is True,
            f"status={h.get('status')} fresh={h.get('fresh')} samples={h.get('sample_count')}")
        return checks

    # ---- the loop ----
    async def start_autonomous(self, actor: str) -> dict:
        if self.mode != "autonomous_sandbox":
            raise ModeError("select autonomous_sandbox mode first")
        if self.loop["running"]:
            raise ModeError("the autonomous loop is already running")
        checks = await self.sandbox_checks()
        failed = [c for c in checks if not c["ok"]]
        if failed:
            raise ModeError("sandbox is not ready: " + "; ".join(f"{c['name']} ({c['detail']})" for c in failed))
        self._stop = asyncio.Event()
        self.loop.update(running=True, started_at_utc=guard.utc_now_iso(), cycles=0, executed=0,
                         stopped_reason=None, last_decision=None, phase="starting")
        self._audit(actor, "autonomous_started", {"policy": self.policy()})
        self._task = asyncio.create_task(self._run(), name="autonomous-loop")
        self._publish()
        return self.status()

    async def stop_autonomous(self, reason: str = "stopped by operator", stop_experiment: bool = False) -> dict:
        """Stops scheduling new experiments. The experiment in flight keeps its own
        safety controller; pass stop_experiment=True to also request its emergency stop."""
        if self.loop["running"]:
            self.loop["stopped_reason"] = reason
        self._stop.set()
        if stop_experiment:
            orch.request_stop()
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass   # it is waiting on a running experiment and will exit when that finishes
        if self.loop["running"] and (task is None or task.done()):
            self.loop["running"] = False
        self.loop["stopped_reason"] = self.loop["stopped_reason"] or reason
        self._publish()
        return self.status()

    def _end(self, reason: str) -> None:
        self.loop.update(running=False, stopped_reason=reason, phase="idle", next_cycle_at=None)
        self._audit("autonomous-controller", "autonomous_stopped", {"reason": reason, **self.loop})
        self._publish()

    async def _sleep(self, seconds: float) -> bool:
        """True if a stop was requested during the wait."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        try:
            while True:
                if self._stop.is_set():
                    return self._end(self.loop["stopped_reason"] or "stopped by operator")
                if self.loop["executed"] >= config.AUTONOMOUS_MAX_EXPERIMENTS:
                    return self._end(f"experiment budget reached ({config.AUTONOMOUS_MAX_EXPERIMENTS})")
                self.loop["cycles"] += 1

                self.loop["phase"] = "checking sandbox"
                self._publish()
                checks = await self.sandbox_checks()
                try:
                    grant = self._autonomous_grant(checks)
                except ModeError as e:
                    return self._end(str(e))

                self.loop["phase"] = "planning"
                self._publish()
                try:
                    proposal = await orch.generate_scenario_plan(policy=self.policy(), mode=self.mode)
                    scn = scenario.Scenario(**proposal["scenario"])
                    started = orch.start_scenario(scn, grant, policy=self.policy(), proposal=proposal,
                                                  mode=self.mode, source="autonomous")
                except (orch.OrchestratorError, scenario.ScenarioRejected, ValueError) as e:
                    return self._end(f"refused: {e}")
                self.loop.update(phase="running experiment", last_experiment_id=started["experiment_id"],
                                 last_decision={"signature": started["signature"], "method": proposal["method"],
                                                "rank": proposal["ranking"]["rank"], "at_utc": guard.utc_now_iso()})
                self._publish()
                try:
                    result = await started["task"]
                except Exception as e:  # noqa: BLE001
                    return self._end(f"experiment {started['experiment_id']} crashed: {type(e).__name__}: {e}")
                self.loop["executed"] += 1
                state = result["record"]["state"]
                if state != guard.STATE_COMPLETED or result["record"].get("recovery_outcome") != "recovered":
                    return self._end(f"experiment {started['experiment_id']} ended {state} "
                                     f"(recovery_outcome={result['record'].get('recovery_outcome')}); "
                                     f"not continuing without an operator")
                self.loop.update(phase="cooldown", next_cycle_at=time.time() + config.AUTONOMOUS_COOLDOWN_S)
                self._publish()
                if await self._sleep(config.AUTONOMOUS_COOLDOWN_S):
                    return self._end(self.loop["stopped_reason"] or "stopped by operator")
        except asyncio.CancelledError:
            self._end("cancelled (API shutting down)")
            raise
        except Exception as e:  # noqa: BLE001 -- the loop must never die silently
            self._end(f"loop error: {type(e).__name__}: {e}")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


controller = Controller()
