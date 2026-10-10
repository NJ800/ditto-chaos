"""Scenario engine: policy validation, non-mutating preview/preflight, multi-fault
execution, partial failure, emergency stop, cancellation, lockout and journal recovery.
Docker is FakeDocker; time is virtual (see conftest.sandbox)."""
import asyncio
import json
import os
import subprocess

import pytest

import config
import guard
import probe
import scenario
from conftest import DEP_MAP, OK_ROW, auth, make_probe, scn


def codes(report):
    return {e["code"] for e in report["errors"]}


async def run(s, **kw):
    r = scenario.ScenarioRun(s, kw.pop("authorization", auth()), dep_map=DEP_MAP, **kw)
    r.prepare()
    return r, await r.run()


# ================================================================ validation (pure)
def test_valid_two_fault_scenario_passes_and_orders_rollback_by_dependency():
    rep = scenario.validate(scn(("loss", "gateway"), ("delay", "mongodb")), DEP_MAP)
    assert rep["ok"] and rep["affected_services"] == ["gateway", "mongodb"]
    assert rep["apply_order"] == ["a1", "a2"]
    assert rep["rollback_order"] == ["a2", "a1"]     # mongodb (fan_in 2) is restored first
    assert rep["signature"] == "delay@mongodb+loss@gateway"
    assert rep["actions"][1]["params"]["ttl_s"] == 40    # window 10s + 30s margin, not the 60s default


@pytest.mark.parametrize("actions,code", [
    ([("explode", "things")], "unknown_fault"),
    ([("memory_pressure", "things")], "unsupported_fault"),
    ([("kill", "nginx")], "target_not_allowlisted"),
    ([("delay", "things", {"params": {"delay_ms": 99999}})], "invalid_params"),
    ([("delay", "things"), ("delay", "things")], "duplicate_action"),
    ([("delay", "things"), ("loss", "things")], "conflicting_faults"),      # one root qdisc
    ([("kill", "things"), ("delay", "things")], "conflicting_faults"),      # netem needs it running
    ([("kill", "things"), ("pause", "gateway")], "untested_combination"),   # two services down at once
    ([("delay", "things", {"start_offset_s": 50})], "offset_past_end"),
])
def test_invalid_scenarios_are_refused(actions, code):
    assert code in codes(scenario.validate(scn(*actions), DEP_MAP))


def test_limits_are_enforced(monkeypatch):
    three = scn(("delay", "things"), ("loss", "gateway"), ("jitter", "mongodb"))
    assert "limit_services" in codes(scenario.validate(three))
    monkeypatch.setattr(config, "MAX_AFFECTED_SERVICES", 3)
    monkeypatch.setattr(config, "MAX_ACTIVE_FAULTS", 2)
    assert "limit_active_faults" in codes(scenario.validate(three))
    monkeypatch.setattr(config, "MAX_CUMULATIVE_INTENSITY", 0.5)
    assert "limit_intensity" in codes(scenario.validate(scn(("kill", "things"))))
    monkeypatch.setattr(config, "MAX_SCENARIO_DURATION_S", 5)
    assert "limit_duration" in codes(scenario.validate(scn(("delay", "things"))))


def test_sequential_faults_do_not_count_as_concurrent(monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_FAULTS", 1)
    s = scn(("delay", "things", {"duration_s": 4}), ("loss", "gateway", {"start_offset_s": 5}), max_duration_s=12)
    rep = scenario.validate(s)
    assert rep["ok"] and rep["max_concurrent_faults"] == 1


def test_policy_can_only_narrow():
    pol = {"allowed_faults": ["delay"], "allowed_targets": ["things"], "max_actions": 1, "max_duration_s": 5}
    rep = scenario.validate(scn(("kill", "gateway"), ("delay", "things")), DEP_MAP, pol)
    assert {"policy_fault", "policy_target", "policy_actions", "policy_duration"} <= codes(rep)


def test_schema_rejects_unknown_fields_and_bad_bounds():
    with pytest.raises(ValueError):
        scenario.Scenario(actions=[{"fault": "kill", "target": "things", "cmd": "rm -rf /"}])
    with pytest.raises(ValueError):
        scenario.Scenario(actions=[])
    with pytest.raises(ValueError):
        scenario.Scenario(actions=[{"fault": "kill", "target": "things"}], max_duration_s=100000)


# ================================================================ preview / preflight never mutate
def test_validate_issues_no_docker_command_at_all(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("validate() must not start a subprocess")

    async def boom_async(*a, **kw):
        raise AssertionError("validate() must not call guard.run_cmd")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(guard, "run_cmd", boom_async)
    rep = scenario.validate(scn(("kill", "things"), ("delay", "mongodb")), DEP_MAP)
    assert rep["ok"] and rep["actions"][0]["commands"]


async def test_preflight_check_issues_only_read_only_commands(sandbox):
    result = await scenario.preflight_check(scn(("kill", "things"), ("delay", "mongodb"),
                                                ("cpu_throttle", "gateway")), DEP_MAP)
    assert result["validation"]["ok"] is False   # 3 services > limit: refused before Docker
    assert sandbox.calls == []
    result = await scenario.preflight_check(scn(("kill", "things"), ("delay", "mongodb")), DEP_MAP)
    assert result["ok"] and [r["ok"] for r in result["results"]] == [True, True]
    assert sandbox.mutating_calls() == []
    assert all(c["running"] and c["netem"] is None for c in sandbox.containers.values())


async def test_preflight_check_reports_per_target_failure(sandbox):
    sandbox.containers["mongodb"]["running"] = False
    result = await scenario.preflight_check(scn(("kill", "things"), ("delay", "mongodb")), DEP_MAP)
    assert result["ok"] is False and "not running" in result["results"][1]["detail"]
    assert sandbox.mutating_calls() == []


def test_guard_dry_run_never_starts_a_subprocess_and_is_labelled(monkeypatch, capsys):
    def boom(*a, **kw):
        raise AssertionError("dry-run must never start a subprocess")
    monkeypatch.setattr(subprocess, "run", boom)
    import argparse
    args = argparse.Namespace(fault="delay", target="mongodb", ms=500, ttl=30)
    assert asyncio.run(guard.run_dry_run(args)) == 0
    out = capsys.readouterr().out
    assert "(dry-run, NOT executed) $ docker run" in out
    assert "\n[guard] $ " not in out      # no line that reads like a real execution


async def test_guard_dry_run_record_does_not_claim_injection(sandbox, monkeypatch):
    class DryFault:
        name, params, dry_run = "kill", {}, True

        async def preflight(self): ...
        async def inject(self): ...
        async def revert(self): ...
    rec = await guard.run_experiment(DryFault(), "things", guard.ContainerRef("n", "dry-run-fake-id"),
                                     guard.Thresholds.from_env(), 2, 1, 5, True, 3600)
    assert rec["simulated"] is True
    assert rec["injected"] is False and rec["reverted"] is False and rec["rollback_verified"] is False


# ================================================================ authorization / lockout gates
def test_prepare_refuses_without_authorization(sandbox):
    r = scenario.ScenarioRun(scn(("kill", "things")), None)
    with pytest.raises(scenario.ScenarioRejected) as e:
        r.prepare()
    assert e.value.code == "unauthorized" and sandbox.calls == []


def test_prepare_refuses_a_dict_pretending_to_be_an_authorization(sandbox):
    r = scenario.ScenarioRun(scn(("kill", "things")), {"kind": "operator", "actor": "llm"})
    with pytest.raises(scenario.ScenarioRejected):
        r.prepare()


def test_prepare_refuses_during_lockout(sandbox):
    scenario.write_lockout("exp-x", "rollback could not be verified", [])
    with pytest.raises(scenario.ScenarioRejected) as e:
        scenario.ScenarioRun(scn(("kill", "things")), auth()).prepare()
    assert e.value.code == "recovery_required"


def test_unreadable_lockout_file_still_locks(sandbox):
    os.makedirs(os.path.dirname(config.LOCKOUT_FILE), exist_ok=True)
    with open(config.LOCKOUT_FILE, "w") as f:
        f.write("{not json")
    assert scenario.read_lockout() is not None


def test_scenario_lock_and_target_locks_block_a_second_run(sandbox):
    first = scenario.ScenarioRun(scn(("kill", "things")), auth())
    first.prepare()
    with pytest.raises(scenario.ScenarioRejected) as e:
        scenario.ScenarioRun(scn(("delay", "mongodb")), auth()).prepare()
    assert e.value.code == "locked"
    first.release()
    # a CLI guard.py run holding one target's lock also blocks a scenario on that target
    held = guard.acquire_lock("mongodb")
    with pytest.raises(scenario.ScenarioRejected):
        scenario.ScenarioRun(scn(("delay", "mongodb")), auth()).prepare()
    guard.release_lock(held)
    assert not os.listdir(guard.LOCKS_DIR)     # the failed prepare released what it took


# ================================================================ execution
async def test_single_fault_runs_and_recovers(sandbox):
    r, rec = await run(scn(("kill", "things-search")))
    assert rec["state"] == guard.STATE_COMPLETED and rec["recovery_outcome"] == "recovered"
    assert rec["injected"] and rec["reverted"] and rec["rollback_verified"]
    assert rec["actions"][0]["status"] == "rolled_back" and rec["actions"][0]["rollback_verified"]
    assert rec["targets"]["things-search"]["verified"] is True
    assert rec["fault"] == "kill" and rec["target"] == "things-search"       # v1-compatible fields
    assert rec["schema_version"] == "2.0" and rec["authorization"]["actor"] == "tester"
    assert sandbox.containers["things-search"]["running"]
    assert not os.path.exists(scenario._journal_path(rec["id"]))
    assert not os.listdir(guard.LOCKS_DIR)


async def test_multi_fault_applies_concurrently_and_rolls_back_all(sandbox, monkeypatch):
    seen = {}

    async def spy(client, ids):
        both = (sandbox.containers["mongodb"]["netem"], sandbox.containers["gateway"]["netem"])
        if all(both):
            seen["netem"], seen["gw"] = both
        return dict(OK_ROW)
    monkeypatch.setattr(probe, "probe_once", spy)
    r, rec = await run(scn(("delay", "mongodb", {"params": {"delay_ms": 200}}),
                           ("loss", "gateway", {"params": {"loss_pct": 5}})))
    assert seen == {"netem": "delay 200ms", "gw": "loss 5%"}     # both active at the same time
    assert rec["state"] == guard.STATE_COMPLETED and rec["fault"] == "multi"
    assert [a["status"] for a in rec["actions"]] == ["rolled_back", "rolled_back"]
    assert all(c["netem"] is None for c in sandbox.containers.values())
    assert max(row["active_faults"] for row in rec["timeline"]) == 2


async def test_sequenced_fault_with_own_duration_is_removed_early(sandbox):
    s = scn(("delay", "mongodb", {"duration_s": 3}), ("loss", "gateway", {"start_offset_s": 5}), max_duration_s=10)
    r, rec = await run(s)
    a1, a2 = rec["actions"]
    assert a1["rolled_back_at_s"] < a2["applied_at_s"]
    assert a2["applied_at_s"] >= 5 and rec["state"] == guard.STATE_COMPLETED


async def test_preflight_is_atomic_nothing_applied_if_one_target_fails(sandbox):
    sandbox.image_present = False       # the delay's preflight fails; the kill's would pass
    r, rec = await run(scn(("kill", "things"), ("delay", "mongodb")))
    assert rec["state"] == guard.STATE_ABORTED and rec["injected"] is False
    assert sandbox.mutating_calls() == [] and sandbox.containers["things"]["running"]
    assert "docker pull" in rec["error"]


async def test_unhealthy_before_start_refuses_injection(sandbox, monkeypatch):
    monkeypatch.setattr(probe, "probe_once", make_probe(default={**OK_ROW, "direct_ok": False}))
    r, rec = await run(scn(("kill", "things")))
    assert rec["state"] == guard.STATE_ABORTED and sandbox.mutating_calls() == []


async def test_later_fault_failing_rolls_back_the_earlier_one(sandbox):
    sandbox.fail["qdisc replace"] = "command failed (1): Operation not permitted"
    r, rec = await run(scn(("kill", "things"), ("delay", "mongodb")))
    kill, delay = rec["actions"]
    assert kill["status"] == "rolled_back" and kill["rollback_verified"]
    assert delay["status"] == "rolled_back" and delay["error"]         # was 'uncertain', then cleaned
    assert delay["observed"]["active"] is False
    assert rec["state"] == guard.STATE_FAILED and rec["injection_error"]
    assert rec["rollback_verified"] and sandbox.containers["things"]["running"]
    assert scenario.read_lockout() is None


async def test_timed_out_apply_is_inspected_not_assumed_failed(sandbox, monkeypatch):
    """The command 'fails' but actually took effect: the engine must look, and clean up."""
    real = sandbox._run

    def took_effect_then_errored(args, joined):
        out = real(args, joined)
        if args[1] == "kill":
            raise guard.GuardError("command timed out after 30s: docker kill")
        return out
    sandbox._run = took_effect_then_errored
    r, rec = await run(scn(("kill", "things")))
    a = rec["actions"][0]
    assert "timed out" in a["error"]
    assert a["status"] == "rolled_back" and sandbox.containers["things"]["running"]
    assert any(e["kind"] == "apply_failed" and "running=False" in e["detail"] for e in rec["safety_events"])


async def test_kill_switch_stops_and_rolls_back_everything(sandbox, monkeypatch):
    bad = {**OK_ROW, "direct_ok": False}
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW, OK_ROW, bad, bad, bad]))
    r, rec = await run(scn(("delay", "mongodb"), ("loss", "gateway"), max_duration_s=30))
    assert rec["aborted"] and "direct-read endpoint failed" in rec["abort_reason"]
    assert rec["detected_s"] is not None and rec["detected_s"] < 30
    assert rec["state"] == guard.STATE_COMPLETED and rec["rollback_verified"]
    assert all(c["netem"] is None for c in sandbox.containers.values())
    assert any(e["kind"] == "kill_switch" for e in rec["safety_events"])


async def test_lost_update_trips_immediately_without_waiting_for_a_window(sandbox, monkeypatch):
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW, OK_ROW, {**OK_ROW, "lost_acked": 2}]))
    r, rec = await run(scn(("delay", "mongodb"), max_duration_s=30))
    assert "silent data loss" in rec["abort_reason"] and len([t for t in rec["timeline"] if t["phase"] == "fault"]) == 1


async def test_monitoring_blackout_trips_the_kill_switch(sandbox, monkeypatch):
    """Probes hang (monitoring lost): the run must stop and recover, not continue blind."""
    calls = {"n": 0}

    async def hang_after_baseline(client, ids):
        calls["n"] += 1
        if calls["n"] <= 2:
            return dict(OK_ROW)
        raise asyncio.TimeoutError()
    monkeypatch.setattr(probe, "probe_once", hang_after_baseline)

    async def healthy_again(client, ids):
        return dict(OK_ROW)
    s = scn(("delay", "mongodb"), max_duration_s=30)
    r = scenario.ScenarioRun(s, auth(), dep_map=DEP_MAP)
    r.prepare()
    task = asyncio.create_task(r.run())
    while r.record["state"] not in (guard.STATE_REVERTING, guard.STATE_VERIFYING_RECOVERY) and not task.done():
        await asyncio.sleep(0)
    monkeypatch.setattr(probe, "probe_once", healthy_again)
    rec = await task
    assert rec["aborted"] and rec["timeline"][2].get("probe_timeout") is True
    assert sandbox.containers["mongodb"]["netem"] is None and rec["rollback_verified"]


async def test_emergency_stop_rolls_back_every_active_fault(sandbox):
    stop = asyncio.Event()
    s = scn(("delay", "mongodb"), ("pause", "gateway"), max_duration_s=60)
    r = scenario.ScenarioRun(s, auth(), dep_map=DEP_MAP, stop_event=stop)
    r.prepare()
    task = asyncio.create_task(r.run())
    while r.record["state"] != guard.STATE_MONITORING:
        await asyncio.sleep(0)
    stop.set()
    rec = await task
    assert rec["abort_reason"].startswith("emergency stop") and rec["detected_s"] < 60
    assert not sandbox.containers["gateway"]["paused"] and sandbox.containers["mongodb"]["netem"] is None
    assert rec["state"] == guard.STATE_COMPLETED


async def test_stop_before_injection_touches_nothing(sandbox):
    stop = asyncio.Event()
    stop.set()
    r, rec = await run(scn(("kill", "things")), stop_event=stop)
    assert rec["state"] == guard.STATE_ABORTED and sandbox.mutating_calls() == []


async def test_task_cancellation_still_rolls_back_and_returns_a_record(sandbox):
    s = scn(("kill", "things"), ("delay", "mongodb"), max_duration_s=60)
    r = scenario.ScenarioRun(s, auth(), dep_map=DEP_MAP)
    r.prepare()
    task = asyncio.create_task(r.run())
    while r.record["state"] != guard.STATE_MONITORING:
        await asyncio.sleep(0)
    task.cancel()
    rec = await task          # the engine absorbs the cancel so the record can be saved
    assert rec["state"] == guard.STATE_ABORTED and "cancelled" in rec["abort_reason"]
    assert rec["rollback_verified"] and rec["recovery_outcome"] == "skipped_cancelled"
    assert sandbox.containers["things"]["running"] and sandbox.containers["mongodb"]["netem"] is None
    assert not os.listdir(guard.LOCKS_DIR)


async def test_unexpected_exception_mid_scenario_still_rolls_back(sandbox, monkeypatch):
    calls = {"n": 0}

    async def explode(client, ids):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("probe blew up")
        return dict(OK_ROW)
    monkeypatch.setattr(probe, "probe_once", explode)
    r, rec = await run(scn(("pause", "things")))
    assert rec["state"] == guard.STATE_FAILED and "probe blew up" in rec["error"]
    assert not sandbox.containers["things"]["paused"] and rec["rollback_verified"]


async def test_recovery_timeout_is_distinct_from_rollback_failure(sandbox, monkeypatch):
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW] * 12, default={**OK_ROW, "search_ok": False}))
    r, rec = await run(scn(("restart", "things"), max_duration_s=10))
    assert rec["state"] == guard.STATE_RECOVERY_TIMEOUT and rec["rollback_verified"]
    assert scenario.read_lockout() is None


# ================================================================ rollback failure -> lockout
async def test_one_rollback_failing_does_not_stop_the_others_and_locks_out(sandbox):
    sandbox.block_netem_delete = True
    r, rec = await run(scn(("delay", "mongodb"), ("pause", "gateway")))
    by = {a["fault"]: a for a in rec["actions"]}
    assert by["delay"]["status"] == "rollback_failed" and by["pause"]["status"] == "rolled_back"
    assert not sandbox.containers["gateway"]["paused"]           # the healthy rollback still happened
    assert rec["state"] == scenario.STATE_RECOVERY_REQUIRED and rec["rollback_verified"] is False
    lock = scenario.read_lockout()
    assert lock["experiment_id"] == rec["id"]
    assert "tc qdisc del" in lock["items"][0]["manual"][0]
    assert os.path.exists(scenario._journal_path(rec["id"]))     # kept for recovery
    with pytest.raises(scenario.ScenarioRejected) as e:
        scenario.ScenarioRun(scn(("kill", "things")), auth()).prepare()
    assert e.value.code == "recovery_required"


async def test_recover_orphans_clears_the_lockout_only_after_verified_cleanup(sandbox):
    sandbox.block_netem_delete = True
    r, rec = await run(scn(("delay", "mongodb")))
    assert scenario.read_lockout() is not None
    result = await scenario.recover_orphans()           # still failing
    assert result["lockout"] is not None and result["failed"]
    sandbox.block_netem_delete = False
    result = await scenario.recover_orphans()
    assert result["lockout"] is None and result["recovered"][0]["fault"] == "delay"
    assert sandbox.containers["mongodb"]["netem"] is None and not scenario.list_journals()
    scenario.ScenarioRun(scn(("kill", "things")), auth()).prepare()      # mutation allowed again


async def test_journal_is_on_disk_before_the_first_mutation(sandbox):
    seen = {}
    real = sandbox._run

    def check(args, joined):
        if args[1] == "kill" and "journal" not in seen:
            with open(os.path.join(config.JOURNAL_DIR, os.listdir(config.JOURNAL_DIR)[0])) as f:
                seen["journal"] = json.load(f)
        return real(args, joined)
    sandbox._run = check
    await run(scn(("kill", "things"), ("delay", "mongodb")))
    statuses = {a["recovery"]["fault"]: a["status"] for a in seen["journal"]["actions"]}
    assert statuses == {"kill": "applying", "delay": "pending"}
    assert seen["journal"]["actions"][1]["recovery"]["state"]["helper"].startswith("chaos-delay-")


async def test_crashed_process_is_cleaned_up_from_its_journal_on_restart(sandbox):
    """Simulates a hard kill of the API mid-scenario: faults applied, journal on disk,
    no process left to roll back. A fresh process replays the journal."""
    r = scenario.ScenarioRun(scn(("pause", "things"), ("loss", "gateway")), auth(), dep_map=DEP_MAP)
    r.prepare()
    for a in r.actions:
        c = sandbox.containers[a.target]
        a.fault = __import__("faults").build(a.fault_name, a.target, guard.ContainerRef(c["name"], c["id"]), a.params)
    r._journal()
    for a in r.actions:
        await r._apply(a)
    r.release()                                   # the process died here
    assert sandbox.containers["things"]["paused"] and sandbox.containers["gateway"]["netem"]

    result = await scenario.recover_orphans()
    assert {x["fault"] for x in result["recovered"]} == {"pause", "loss"} and not result["failed"]
    assert not sandbox.containers["things"]["paused"] and sandbox.containers["gateway"]["netem"] is None
    assert result["lockout"] is None and not scenario.list_journals()


async def test_restart_recovery_fails_closed_when_docker_is_down(sandbox):
    r = scenario.ScenarioRun(scn(("pause", "things")), auth(), dep_map=DEP_MAP)
    r.prepare()
    c = sandbox.containers["things"]
    r.actions[0].fault = __import__("faults").build("pause", "things", guard.ContainerRef(c["name"], c["id"]))
    await r._apply(r.actions[0])
    r.release()
    sandbox.daemon_up = False
    result = await scenario.recover_orphans()
    assert result["lockout"] is not None and "docker unpause" in result["failed"][0]["manual"][0]


async def test_events_report_real_state_transitions(sandbox):
    events = []
    r, rec = await run(scn(("kill", "things")), on_event=lambda kind, data: events.append((kind, data)))
    phases = [d["phase"] for k, d in events if k == "experiment"]
    assert phases[:3] == ["PREFLIGHT", "BASELINE", "INJECTING"]
    for expected in ("fault_applied", "MONITORING", "REVERTING", "fault_rolled_back", "VERIFYING_RECOVERY"):
        assert expected in phases
    assert phases[-1] == "COMPLETED" and events[-1][1]["final"] is True
    assert sum(1 for k, _ in events if k == "metric") == len(rec["timeline"])
