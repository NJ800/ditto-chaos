"""Fault registry: parameter validation, preflight, apply, inspect, verified rollback.
Docker is FakeDocker (tests/conftest.py) -- nothing here touches a real daemon."""
import pytest

import faults
import guard


def ref(docker, svc):
    c = docker.containers[svc]
    return guard.ContainerRef(c["name"], c["id"])


def build(docker, name, svc="things-search", params=None):
    return faults.build(name, svc, ref(docker, svc), params)


# ---------------------------------------------------------------- registry / params
def test_catalog_lists_supported_and_unsupported_with_reasons():
    cat = {f["name"]: f for f in faults.catalog()}
    assert {"kill", "delay", "loss", "jitter", "bandwidth", "stop", "restart", "pause", "cpu_throttle"} <= set(cat)
    for name in ("cpu_stress", "memory_pressure", "request_failure"):
        assert cat[name]["supported"] is False and cat[name]["unsupported_reason"]
    # an unsupported fault never claims verification; supported ones point at the real-Docker suite
    assert not any(f["live_verified"] for f in cat.values() if not f["supported"])
    assert all("tests/integration" in f["verification_note"] for f in cat.values() if f["supported"])


def test_unsupported_fault_cannot_be_built(sandbox):
    with pytest.raises(guard.GuardError, match="not supported"):
        build(sandbox, "memory_pressure")


@pytest.mark.parametrize("name,params", [
    ("delay", {"delay_ms": 0}), ("delay", {"delay_ms": 5001}), ("delay", {"ttl_s": 601}),
    ("loss", {"loss_pct": 101}), ("loss", {"loss_pct": 0}), ("jitter", {"delay_ms": 10, "jitter_ms": 50}),
    ("bandwidth", {"rate_kbit": 1}), ("stop", {"grace_s": 31}), ("cpu_throttle", {"cpu_pct": 95}),
    ("delay", {"delay_ms": "500"}), ("delay", {"delay_ms": True}), ("kill", {"anything": 1}),
    ("delay", {"delay_ms": float("nan")}),
])
def test_out_of_range_or_unknown_params_are_rejected_not_clamped(sandbox, name, params):
    with pytest.raises(guard.GuardError):
        build(sandbox, name, params=params)


def test_defaults_are_filled_in(sandbox):
    assert build(sandbox, "loss").params == {"loss_pct": 10, "ttl_s": 60}


# ---------------------------------------------------------------- process faults
@pytest.mark.parametrize("name,down_key", [("kill", "running"), ("stop", "running"), ("pause", "paused")])
async def test_process_fault_apply_inspect_rollback(sandbox, name, down_key):
    f = build(sandbox, name)
    c = sandbox.containers["things-search"]
    await f.preflight()
    await f.apply()
    assert (c["running"], c["paused"]) != (True, False)
    assert (await f.inspect())["active"] is True
    await f.rollback()
    assert c["running"] and not c["paused"]
    assert await f.verify_clean() == (True, "running=True paused=False")
    # every mutating command addressed the container by full id, never by name
    assert all(call[-1] == c["id"] for call in sandbox.mutating_calls())


async def test_restart_fault_is_self_reverting(sandbox):
    f = build(sandbox, "restart")
    await f.apply()
    assert (await f.inspect())["active"] is False
    await f.rollback()   # verifies running; issues no start
    assert ["docker", "start", f.container.id] not in sandbox.calls


async def test_rollback_is_idempotent(sandbox):
    f = build(sandbox, "kill")
    await f.apply()
    await f.rollback()
    n = len(sandbox.mutating_calls())
    await f.rollback()
    assert len(sandbox.mutating_calls()) == n   # second call found it running and did nothing


async def test_preflight_rejects_a_target_that_is_not_cleanly_running(sandbox):
    sandbox.containers["things-search"]["paused"] = True
    with pytest.raises(guard.GuardError, match="clean running state"):
        await build(sandbox, "kill").preflight()


async def test_rollback_never_starts_a_different_container_when_identity_changed(sandbox):
    f = build(sandbox, "kill")
    await f.apply()
    # compose recreated the service: same name, NEW id, already running
    c = sandbox.containers["things-search"]
    c.update(id="id-replacement-" + "9" * 20, running=True)
    await f.rollback()
    assert f.state["container_replaced_by"] == c["id"]
    assert not any(call[1] == "start" for call in sandbox.calls)
    ok, detail = await f.verify_clean()
    assert ok and "replacement" in detail


async def test_rollback_fails_closed_when_container_is_gone_and_nothing_replaced_it(sandbox):
    f = build(sandbox, "kill")
    await f.apply()
    del sandbox.containers["things-search"]
    with pytest.raises(guard.RollbackError, match="no longer exists"):
        await f.rollback()


async def test_rollback_raises_when_start_never_takes_effect(sandbox):
    f = build(sandbox, "kill")
    await f.apply()
    sandbox.fail["docker start"] = "command failed (1): boom"
    with pytest.raises(guard.RollbackError, match="did not reach a running state"):
        await f.rollback()


async def test_rollback_fails_closed_when_docker_cannot_answer(sandbox):
    f = build(sandbox, "kill")
    await f.apply()
    sandbox.daemon_up = False
    with pytest.raises(guard.RollbackError, match="could not read the state"):
        await f.rollback()
    assert (await f.verify_clean())[0] is False   # unknown is never reported as clean


# ---------------------------------------------------------------- netem faults
@pytest.mark.parametrize("name,params,expected", [
    ("delay", {"delay_ms": 300}, "delay 300ms"), ("jitter", {"delay_ms": 200, "jitter_ms": 50}, "delay 200ms 50ms"),
    ("loss", {"loss_pct": 20}, "loss 20%"), ("bandwidth", {"rate_kbit": 512}, "rate 512kbit"),
])
async def test_netem_fault_apply_and_verified_cleanup(sandbox, name, params, expected):
    f = build(sandbox, name, "mongodb", params)
    c = sandbox.containers["mongodb"]
    await f.preflight()
    assert f.state["original_qdisc"].startswith("qdisc noqueue")
    await f.apply()
    assert c["netem"] == expected and f.state["helper"] in sandbox.helpers
    assert (await f.inspect())["active"] is True
    await f.rollback()
    assert c["netem"] is None and not sandbox.helpers
    assert (await f.verify_clean())[0] is True


async def test_netem_helper_name_is_known_before_apply(sandbox):
    f = build(sandbox, "delay", "mongodb")
    assert f.recovery_info()["state"]["helper"].startswith("chaos-delay-")


async def test_netem_preflight_rejects_foreign_rules(sandbox):
    sandbox.containers["mongodb"]["netem"] = "delay 9ms"
    with pytest.raises(guard.GuardError, match="non-default qdisc"):
        await build(sandbox, "loss", "mongodb").preflight()


async def test_netem_preflight_requires_helper_image(sandbox):
    sandbox.image_present = False
    with pytest.raises(guard.GuardError, match="docker pull"):
        await build(sandbox, "delay", "mongodb").preflight()


async def test_netem_apply_raises_if_rule_did_not_take(sandbox):
    f = build(sandbox, "delay", "mongodb")
    sandbox.fail["qdisc replace"] = "command failed (1): RTNETLINK answers: Operation not permitted"
    with pytest.raises(guard.GuardError):
        await f.apply()


async def test_netem_rollback_raises_when_rule_cannot_be_removed(sandbox):
    f = build(sandbox, "delay", "mongodb")
    await f.apply()
    sandbox.fail["qdisc del"] = "command failed (1): nope"
    with pytest.raises(guard.RollbackError, match="could not confirm"):
        await f.rollback()


async def test_netem_rollback_on_stopped_target_is_observed_clean_not_assumed(sandbox):
    f = build(sandbox, "delay", "mongodb")
    await f.apply()
    sandbox.containers["mongodb"]["running"] = False
    await f.rollback()
    assert "netns is gone" in f.state["cleared_because"]


# ---------------------------------------------------------------- cpu throttle
async def test_cpu_throttle_restores_original_quota(sandbox):
    f = build(sandbox, "cpu_throttle", "things", {"cpu_pct": 20})
    c = sandbox.containers["things"]
    await f.preflight()
    await f.apply()
    assert c["quota"] == 20000 and (await f.inspect())["active"] is True
    await f.rollback()
    assert c["quota"] == -1 and (await f.verify_clean())[0] is True


async def test_cpu_throttle_refuses_container_configured_with_cpus(sandbox):
    sandbox.containers["things"]["nano"] = 2_000_000_000
    with pytest.raises(guard.GuardError, match="NanoCpus"):
        await build(sandbox, "cpu_throttle", "things").preflight()


# ---------------------------------------------------------------- recovery info round trip
async def test_fault_can_be_rebuilt_from_recovery_info_and_rolled_back(sandbox):
    f = build(sandbox, "delay", "mongodb", {"delay_ms": 100})
    await f.preflight()
    await f.apply()
    clone = faults.from_recovery_info(f.recovery_info())   # as a restarted process would
    assert clone.state["helper"] == f.state["helper"]
    await clone.rollback()
    assert sandbox.containers["mongodb"]["netem"] is None


async def test_environment_report_says_unavailable_instead_of_guessing(sandbox):
    sandbox.daemon_up = False
    rep = await faults.environment_report()
    assert rep["docker_available"] is False and rep["netshoot_image_present"] is None
