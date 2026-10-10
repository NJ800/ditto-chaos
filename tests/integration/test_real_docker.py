"""REAL Docker tests for the fault registry. Opt-in and isolated.

These never touch the Ditto containers. They start ONE throwaway `alpine` container,
labelled as service `things` of a private compose project name (`chaos-it`), exercise
each fault's preflight -> apply -> inspect -> rollback -> verify against it, and remove it.

Run (PowerShell), with Docker Desktop running:
    $env:DITTO_CHAOS_REAL_DOCKER = "1"
    docker pull alpine:3.20 ; docker pull nicolaka/netshoot
    python -m pytest tests/integration -q

Everything else in this repository's test suite uses a mocked Docker. Until this file
has been run and passes on your machine, treat every fault as "mock-tested only".
"""
import os
import subprocess
import uuid

import pytest

import faults
import guard

pytestmark = pytest.mark.skipif(os.getenv("DITTO_CHAOS_REAL_DOCKER") != "1",
                                reason="set DITTO_CHAOS_REAL_DOCKER=1 to run against a real Docker daemon")
PROJECT = "chaos-it"


def _docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120, check=check).stdout.strip()


@pytest.fixture
def target(monkeypatch):
    name = f"chaos-it-{uuid.uuid4().hex[:8]}"
    _docker("run", "-d", "--name", name, "--label", f"com.docker.compose.project={PROJECT}",
            "--label", "com.docker.compose.service=things", "alpine:3.20", "sleep", "600")
    monkeypatch.setattr(guard, "COMPOSE_PROJECT", PROJECT)
    try:
        yield name
    finally:
        _docker("rm", "-f", name, check=False)


async def _cycle(name, params=None):
    container = await guard.resolve_container("things", dry_run=False)
    assert len(container.id) == 64                      # full id, never the 12-char short one
    fault = faults.build(name, "things", container, params)
    await fault.preflight()
    await fault.apply()
    return fault


@pytest.mark.parametrize("name,params", [("kill", None), ("stop", {"grace_s": 1}), ("pause", None)])
async def test_process_faults_real(target, name, params):
    fault = await _cycle(name, params)
    assert (await fault.inspect())["active"] is True
    await fault.rollback()
    assert await fault.verify_clean() == (True, "running=True paused=False")
    await fault.rollback()                              # idempotent


async def test_restart_real(target):
    fault = await _cycle("restart", {"grace_s": 1})
    await fault.rollback()
    assert (await fault.verify_clean())[0] is True


@pytest.mark.parametrize("name,params", [
    ("delay", {"delay_ms": 200, "ttl_s": 30}), ("jitter", {"delay_ms": 100, "jitter_ms": 20, "ttl_s": 30}),
    ("loss", {"loss_pct": 10, "ttl_s": 30}), ("bandwidth", {"rate_kbit": 512, "ttl_s": 30}),
])
async def test_netem_faults_real(target, name, params):
    fault = await _cycle(name, params)
    state = await fault.inspect()
    assert state["active"] is True and "netem" in state["detail"]
    await fault.rollback()
    ok, detail = await fault.verify_clean()
    assert ok and "netem" not in detail
    assert fault.state["helper"] not in _docker("ps", "-a", "--format", "{{.Names}}")


async def test_cpu_throttle_real_restores_unlimited_quota(target):
    fault = await _cycle("cpu_throttle", {"cpu_pct": 20})
    assert (await fault.inspect())["active"] is True
    await fault.rollback()
    assert (await fault.verify_clean())[0] is True
    assert int(_docker("inspect", "-f", "{{.HostConfig.CpuQuota}}", target)) <= 0


async def test_kill_rollback_refuses_nothing_but_observes_a_replacement(target):
    fault = await _cycle("kill")
    _docker("rm", "-f", target)
    with pytest.raises(guard.RollbackError):
        await fault.rollback()                          # gone, and nothing replaced it
