"""Autonomy modes, sandbox checks, and the autonomous loop. Docker is FakeDocker."""
import json
import time

import pytest

import config
import guard
import orchestrator as orch
import probe
import scenario
import store
from conftest import BASELINE, OK_ROW, scn
from controller import Controller, ModeError, controller


@pytest.fixture
def auto(sandbox, monkeypatch, tmp_path):
    """Sandbox fully configured for autonomy: flag on, fresh baseline, tight policy."""
    monkeypatch.setattr(config, "AUTONOMY_SANDBOX_ENABLED", True)
    monkeypatch.setattr(config, "AUTONOMOUS_FAULTS", ["kill", "delay"])
    monkeypatch.setattr(config, "AUTONOMOUS_TARGETS", ["things-search", "gateway"])
    monkeypatch.setattr(config, "AUTONOMOUS_MAX_EXPERIMENTS", 2)
    monkeypatch.setattr(config, "AUTONOMOUS_COOLDOWN_S", 0.01)
    monkeypatch.setattr(config, "AUTONOMOUS_MAX_ACTIONS", 1)
    monkeypatch.setattr(config, "AUTONOMOUS_MAX_DURATION_S", 20)
    monkeypatch.setattr(config, "DITTO_URL", "http://localhost:8080")
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({**BASELINE, "recorded_at": time.time()}))
    monkeypatch.setattr(config, "BASELINE_FILE", str(path))
    return sandbox


async def wait_loop():
    await controller._task
    return controller.loop


# ---------------------------------------------------------------- modes
def test_default_mode_is_non_autonomous(monkeypatch):
    monkeypatch.setattr(config, "AUTONOMY_MODE", "autonomous_sandbox")
    monkeypatch.setattr(config, "AUTONOMY_SANDBOX_ENABLED", False)
    assert Controller().mode == "recommend"          # config asked for autonomy without enabling the sandbox
    monkeypatch.setattr(config, "AUTONOMY_MODE", "nonsense")
    assert Controller().mode == "recommend"


async def test_autonomous_mode_cannot_be_selected_without_the_config_flag(sandbox, monkeypatch):
    monkeypatch.setattr(config, "AUTONOMY_SANDBOX_ENABLED", False)
    with pytest.raises(ModeError, match="AUTONOMY_SANDBOX_ENABLED"):
        await controller.set_mode("autonomous_sandbox", "op")
    assert controller.mode == "manual"
    with pytest.raises(ModeError):
        await controller.start_autonomous("op")


async def test_mode_change_is_audited(sandbox):
    await controller.set_mode("observe", "alice")
    entry = store.audit_log()[0]
    assert (entry["actor"], entry["action"], entry["detail"]) == ("alice", "mode_changed",
                                                                 {"from": "manual", "to": "observe"})


async def test_operator_authorization_follows_the_mode(sandbox):
    await controller.set_mode("observe", "op")
    with pytest.raises(ModeError, match="observe"):
        controller.operator_authorization("op", custom=False)
    await controller.set_mode("recommend", "op")
    assert controller.operator_authorization("op", custom=False).kind == "operator"
    with pytest.raises(ModeError, match="custom"):
        controller.operator_authorization("op", custom=True)
    await controller.set_mode("manual", "op")
    assert controller.operator_authorization("op", custom=True).actor == "op"


def test_autonomous_grant_requires_mode_flag_and_passing_checks(auto):
    ok = [{"name": "x", "ok": True, "detail": ""}]
    controller.mode = "manual"
    with pytest.raises(ModeError):
        controller._autonomous_grant(ok)
    controller.mode = "autonomous_sandbox"
    with pytest.raises(ModeError, match="sandbox checks failed: docker"):
        controller._autonomous_grant([{"name": "docker", "ok": False, "detail": ""}])
    assert controller._autonomous_grant(ok).kind == "autonomous_policy"


# ---------------------------------------------------------------- sandbox checks
async def test_sandbox_checks_pass_in_a_configured_sandbox(auto):
    checks = await controller.sandbox_checks()
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]
    assert auto.mutating_calls() == []


@pytest.mark.parametrize("breakage,failing", [
    (lambda d, mp: mp.setattr(config, "DITTO_URL", "http://10.0.0.5:8080"), "Ditto is local"),
    (lambda d, mp: setattr(d, "daemon_up", False), "Docker daemon reachable"),
    (lambda d, mp: d.containers["gateway"].update(running=False), "every allowed target is running"),
    (lambda d, mp: mp.setattr(config, "BASELINE_FILE", "missing.json"), "baseline is present, healthy and fresh"),
    (lambda d, mp: mp.setattr(probe, "current_health", lambda window_s=10, metrics_dir=None: {"status": "unknown"}),
     "live health is fresh and healthy"),
    (lambda d, mp: scenario.write_lockout("e", "r", []), "no recovery lockout"),
    (lambda d, mp: mp.setattr(config, "AUTONOMOUS_FAULTS", ["memory_pressure"]),
     "policy allows at least one fault and target"),
])
async def test_each_sandbox_check_fails_closed(auto, monkeypatch, breakage, failing):
    breakage(auto, monkeypatch)
    failed = [c["name"] for c in await controller.sandbox_checks() if not c["ok"]]
    assert failing in failed
    controller.mode = "autonomous_sandbox"
    with pytest.raises(ModeError, match="not ready"):
        await controller.start_autonomous("op")
    assert controller.loop["running"] is False and auto.mutating_calls() == []


# ---------------------------------------------------------------- the loop
async def test_autonomous_loop_runs_within_policy_then_stops_at_its_budget(auto):
    await controller.set_mode("autonomous_sandbox", "op")
    await controller.start_autonomous("op")
    loop = await wait_loop()
    assert loop["executed"] == 2 and "budget reached" in loop["stopped_reason"] and loop["running"] is False

    hist = store.history()
    assert len(hist) == 2 and all(c["evidence"] for c in hist)
    assert hist[0]["signature"] != hist[1]["signature"]            # the first result changed the second choice
    for c in hist:
        assert set(c["targets"]) <= {"things-search", "gateway"} and set(c["faults"]) <= {"kill", "delay"}
    recs = [json.load(open(f"{config.EXPERIMENTS_DIR}/{c['id']}.json")) for c in hist]
    assert all(r["authorization"]["kind"] == "autonomous_policy" and r["source"] == "autonomous" for r in recs)
    assert all(r["state"] == guard.STATE_COMPLETED for r in recs)
    assert all(c["running"] and c["netem"] is None for c in auto.containers.values())
    actions = [a["action"] for a in store.audit_log()]
    assert "autonomous_started" in actions and "autonomous_stopped" in actions


async def test_loop_stops_itself_when_a_run_does_not_recover(auto, monkeypatch):
    state = {"n": 0}

    async def never_recovers(client, ids):
        state["n"] += 1
        return dict(OK_ROW) if state["n"] <= 14 else {**OK_ROW, "search_ok": False, "direct_ok": False}
    monkeypatch.setattr(probe, "probe_once", never_recovers)
    await controller.set_mode("autonomous_sandbox", "op")
    await controller.start_autonomous("op")
    loop = await wait_loop()
    assert loop["executed"] == 1 and "not continuing without an operator" in loop["stopped_reason"]


async def test_loop_stops_when_sandbox_degrades_between_cycles(auto, monkeypatch):
    await controller.set_mode("autonomous_sandbox", "op")
    real = orch.start_scenario

    def start_then_break(*a, **kw):
        out = real(*a, **kw)
        monkeypatch.setattr(config, "BASELINE_FILE", "gone.json")   # baseline disappears after cycle 1
        return out
    monkeypatch.setattr(orch, "start_scenario", start_then_break)
    await controller.start_autonomous("op")
    loop = await wait_loop()
    assert loop["executed"] == 1 and "sandbox checks failed" in loop["stopped_reason"]


async def test_leaving_autonomous_mode_stops_the_loop(auto, monkeypatch):
    monkeypatch.setattr(config, "AUTONOMOUS_MAX_EXPERIMENTS", 50)
    monkeypatch.setattr(config, "AUTONOMOUS_COOLDOWN_S", 30)
    await controller.set_mode("autonomous_sandbox", "op")
    await controller.start_autonomous("op")
    while controller.loop["phase"] != "cooldown":
        import asyncio
        await asyncio.sleep(0)
    await controller.set_mode("recommend", "op")
    loop = await wait_loop()
    assert loop["running"] is False and loop["executed"] == 1 and "mode changed" in loop["stopped_reason"]


async def test_loop_never_exceeds_policy_even_if_planner_is_bypassed(auto):
    """Defence in depth: the grant alone does not let an out-of-policy scenario through."""
    controller.mode = "autonomous_sandbox"
    grant = controller._autonomous_grant([{"name": "x", "ok": True, "detail": ""}])
    with pytest.raises(scenario.ScenarioRejected) as e:
        orch.start_scenario(scn(("pause", "mongodb")), grant, policy=controller.policy(), source="autonomous")
    assert {x["code"] for x in e.value.errors} == {"policy_fault", "policy_target"}
    assert auto.mutating_calls() == []
