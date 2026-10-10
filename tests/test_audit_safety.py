"""Safety regressions found by the system audit.

1. Dry runs of EVERY supported fault, through the real guard.run_cmd, must never start a
   subprocess -- including a dry run that is stopped half way.
2. Cross-site request forgery / DNS rebinding: with no API_TOKEN the API is protected only
   by being on loopback. A browser page from another origin could send a "simple" POST
   (no Content-Type, so no CORS preflight) that FastAPI still parses as JSON. State-changing
   requests from foreign browser origins must be refused.
3. Values interpolated into the netshoot `sh -c` script must be shell-safe.
"""
import asyncio
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

import api
import config
import faults
import guard
import monitor as mon
import scenario
from conftest import DEP_MAP, REAL_RUN_CMD, scn

SUPPORTED = sorted(n for n, s in ((f, faults.get_spec(f)) for f in faults.REGISTRY) if s.supported)


# ---------------------------------------------------------------- 1. dry runs never execute
@pytest.fixture
def no_subprocess(sandbox, monkeypatch):
    calls = []

    def spy(args, **kw):
        calls.append(list(args))
        raise AssertionError(f"a dry run started a subprocess: {args}")
    monkeypatch.setattr(guard, "run_cmd", REAL_RUN_CMD)
    monkeypatch.setattr(subprocess, "run", spy)
    return calls


@pytest.mark.parametrize("fault", SUPPORTED)
async def test_dry_run_of_every_fault_starts_no_subprocess(no_subprocess, fault):
    target = "mongodb" if faults.get_spec(fault).category == "network" else "things-search"
    r = scenario.ScenarioRun(scn((fault, target)), None, dep_map=DEP_MAP, dry_run=True)
    r.prepare()
    rec = await r.run()
    assert no_subprocess == []
    assert rec["state"] == guard.STATE_COMPLETED and rec["dry_run"] and not rec["injected"]
    assert not scenario.list_journals()


async def test_dry_run_stopped_half_way_starts_no_subprocess(no_subprocess):
    stop = asyncio.Event()
    r = scenario.ScenarioRun(scn(("kill", "things"), ("loss", "gateway")), None, dep_map=DEP_MAP,
                             dry_run=True, stop_event=stop)
    r.prepare()
    task = asyncio.create_task(r.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if r.record["state"] in (guard.STATE_INJECTING, guard.STATE_MONITORING):
            break
    stop.set()
    rec = await task
    assert no_subprocess == [] and not rec["injected"]


def test_dry_run_flag_does_not_leak_to_other_tasks():
    seen = {}

    async def other():
        seen["other"] = guard._force_dry_run.get()

    async def main():
        token = guard._force_dry_run.set(True)
        try:
            await asyncio.create_task(asyncio.sleep(0))
        finally:
            guard._force_dry_run.reset(token)
        await asyncio.create_task(other())
    asyncio.run(main())
    assert seen["other"] is False


# ---------------------------------------------------------------- 2. CSRF / DNS rebinding
@pytest.fixture
def client(sandbox, monkeypatch):
    monkeypatch.setattr(mon, "collect_services", lambda: {"available": False, "error": "stub", "services": None})
    with TestClient(api.app) as c:
        yield c


RUN = {"authorize": True, "scenario": {"actions": [{"fault": "kill", "target": "things"}], "max_duration_s": 10,
                                       "pre_s": 2, "recovery_timeout_s": 20}}


def test_foreign_origin_cannot_trigger_bodyless_actions(client):
    """Cross-site 'simple' POSTs need no CORS preflight. FastAPI (0.143) refuses JSON bodies
    without a JSON Content-Type, but routes that take no body (stop, reset, plan, remediation
    decisions, refresh) were reachable from any web page in the operator's browser."""
    for path in ("/api/stop", "/api/orchestrator/reset", "/api/plan", "/api/dependencies/refresh"):
        r = client.post(path, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and r.json()["code"] == "forbidden_origin", path
    assert client.post("/api/stop", headers={"Origin": "null"}).status_code == 403   # sandboxed iframe


def test_dns_rebinding_cannot_start_a_real_run(client):
    """A rebound hostname makes the attacker's page same-origin, so it can send JSON."""
    from controller import controller
    controller.mode = "manual"
    r = client.post("/api/scenarios/run", json=RUN, headers={"Origin": "http://evil.example:8000",
                                                             "Host": "evil.example:8000"})
    assert r.status_code == 403
    assert client.get("/api/orchestrator/status").json()["status"] == "idle"


def test_dashboard_cli_and_same_origin_requests_still_work(client):
    dev = config.FRONTEND_ORIGINS[0]
    assert client.post("/api/orchestrator/reset", headers={"Origin": dev}).status_code == 200
    assert client.post("/api/orchestrator/reset", headers={"Origin": "http://127.0.0.1:8000",
                                                           "Host": "127.0.0.1:8000"}).status_code == 200
    assert client.post("/api/orchestrator/reset").status_code == 200          # no Origin: CLI / curl
    assert client.get("/api/livez", headers={"Origin": "https://evil.example"}).status_code == 200


# ---------------------------------------------------------------- 3. shell-safe interpolation
@pytest.mark.parametrize("var,value", [("NETEM_IFACE", "eth0; reboot"), ("NETEM_IFACE", "$(id)"),
                                       ("NETSHOOT_IMAGE", "img; rm -rf /"), ("COMPOSE_PROJECT", "a b")])
def test_unsafe_environment_values_are_rejected(var, value):
    with pytest.raises(ValueError, match=var):
        guard.env_setting(var, value)


def test_the_configured_values_passed_the_same_check():
    for var in ("NETEM_IFACE", "NETSHOOT_IMAGE", "COMPOSE_PROJECT"):
        assert guard.env_setting(var, getattr(guard, var)) == getattr(guard, var)


# ---------------------------------------------------------------- 4. lockout covers the legacy paths
class _Fault:
    name, params, dry_run = "kill", {}, False

    def __init__(self, fail_revert=False):
        self.injected = self.reverted = False
        self.fail_revert = fail_revert

    async def preflight(self):
        pass

    async def inject(self):
        self.injected = True

    async def revert(self):
        self.reverted = True
        if self.fail_revert:
            raise guard.RollbackError("container did not come back")


async def _legacy_run(fault, **kw):
    return await guard.run_experiment(fault, "things", guard.ContainerRef("docker-things-1", "id-things"),
                                      guard.Thresholds.from_env(), 0.05, 0.05, 0.3, True, 3600, **kw)


async def test_guard_cli_path_refuses_to_inject_during_a_lockout(sandbox):
    scenario.write_lockout("exp-x", "rollback could not be verified", [])
    fault = _Fault()
    rec = await _legacy_run(fault)
    assert rec["state"] == guard.STATE_ABORTED and "RECOVERY REQUIRED" in rec["error"]
    assert not fault.injected and not rec["injected"]


async def test_failed_legacy_rollback_locks_out_every_further_injection(sandbox):
    rec = await _legacy_run(_Fault(fail_revert=True))
    assert rec["rollback_error"] and not rec["rollback_verified"]
    lock = scenario.read_lockout()
    assert lock and lock["items"][0]["manual"] == ["docker start docker-things-1"]
    with pytest.raises(scenario.ScenarioRejected):
        scenario.ScenarioRun(scn(("kill", "things-search")), scenario.Authorization(kind="operator", actor="t")).prepare()
    nxt = _Fault()
    assert (await _legacy_run(nxt))["state"] == guard.STATE_ABORTED and not nxt.injected


def test_main_run_claim_is_refused_during_a_lockout(sandbox, monkeypatch):
    import orchestrator as orch
    monkeypatch.setattr(orch._state, "proposal", {"target": "things", "fault": "kill"})
    monkeypatch.setattr(orch._state, "proposal_id", "p1")
    scenario.write_lockout("exp-x", "r", [])
    with pytest.raises(orch.OrchestratorError, match="RECOVERY REQUIRED"):
        orch._claim("p1", dry_run=False)


# ---------------------------------------------------------------- 5. locks left by a crashed process
def _write_lock(target, pid, host=None):
    import json as _json
    import os as _os
    import socket
    import time as _time
    _os.makedirs(guard.LOCKS_DIR, exist_ok=True)
    with open(_os.path.join(guard.LOCKS_DIR, f"{target}.lock"), "w", encoding="utf-8") as f:
        _json.dump({"pid": pid, "host": host or socket.gethostname(), "started_at": _time.time()}, f)


def test_lock_of_a_crashed_process_is_reclaimed(sandbox):
    """Found live by scripts/chaos_matrix.py: after the API process was killed mid-fault,
    startup recovery restored the target but its lock files blocked every further run
    for LOCK_STALE_S (2 h)."""
    import sys
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _write_lock("things", dead.pid)
    lock = guard.acquire_lock("things")
    guard.release_lock(lock)


def test_lock_of_a_live_process_or_another_host_is_respected(sandbox):
    import os as _os
    _write_lock("things", _os.getpid())
    with pytest.raises(guard.GuardError, match="already appears to be running"):
        guard.acquire_lock("things")
    _write_lock("mongodb", 999999, host="some-other-machine")
    with pytest.raises(guard.GuardError, match="already appears to be running"):
        guard.acquire_lock("mongodb")


def test_pid_liveness_check_never_signals_the_process():
    import os as _os
    assert guard.pid_alive(_os.getpid()) is True       # on Windows os.kill(pid, 0) would terminate us
    assert guard.pid_alive(0) is False


def test_old_format_lock_without_host_is_reclaimed_when_its_process_is_gone(sandbox):
    import json as _json
    import os as _os
    import sys
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _os.makedirs(guard.LOCKS_DIR, exist_ok=True)
    with open(_os.path.join(guard.LOCKS_DIR, "_scenario.lock"), "w", encoding="utf-8") as f:
        _json.dump({"pid": dead.pid, "started_at": __import__("time").time()}, f)
    guard.release_lock(guard.acquire_lock("_scenario"))
