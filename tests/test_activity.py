"""Experiment activity log: event schema, lifecycle instrumentation, command capture,
dry-run guarantees, secret redaction, and the incremental events API. Offline: Docker is
FakeDocker (optionally driven through the REAL guard.run_cmd), HTTP is mocked."""
import argparse
import asyncio
import json
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

import activity
import api
import config
import guard
import monitor as mon
import orchestrator as orch
import probe
import scenario
from conftest import DEP_MAP, OK_ROW, REAL_RUN_CMD, auth, make_probe, scn

KINDS = set(activity.KINDS)


@pytest.fixture
def realcmd(sandbox, monkeypatch):
    """sandbox, but commands go through the REAL guard.run_cmd with subprocess.run backed
    by FakeDocker -- so command events are produced by the production code path."""
    monkeypatch.setattr(guard, "run_cmd", REAL_RUN_CMD)
    monkeypatch.setattr(subprocess, "run", sandbox.as_subprocess)
    return sandbox


async def run(s, **kw):
    r = scenario.ScenarioRun(s, kw.pop("authorization", auth()), dep_map=DEP_MAP, **kw)
    r.prepare()
    return r, await r.run()


def of(rec, kind=None, state=None):
    return [e for e in rec["events"] if (kind is None or e["kind"] == kind) and (state is None or e["state"] == state)]


def assert_well_formed(events):
    assert events, "no events were recorded"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq must be strictly increasing"
    times = [e["t_s"] for e in events]
    assert times == sorted(times) and times[0] >= 0
    for e in events:
        assert set(e) == {"seq", "ts", "t_s", "state", "kind", "message", "detail"}
        assert e["kind"] in KINDS and isinstance(e["message"], str) and e["message"].strip()
        assert e["ts"].endswith("+00:00") and isinstance(e["detail"], dict)


# ================================================================ emit / schema
def test_emit_builds_the_documented_schema():
    rec = {"state": "MONITORING"}
    e = activity.emit(rec, "decision", "Within limits.", reason="ok")
    assert e == rec["events"][0] and e["seq"] == 1 and e["state"] == "MONITORING"
    assert e["kind"] == "decision" and e["detail"] == {"reason": "ok"} and e["t_s"] == 0
    assert activity.emit(rec, "nonsense-kind", "x")["kind"] == "info"


def test_event_limit_is_enforced_without_resetting_the_sequence(monkeypatch):
    monkeypatch.setattr(activity, "MAX_EVENTS", 50)
    rec = {"state": "MONITORING"}
    for i in range(175):
        activity.emit(rec, "info", f"event {i}")
    assert len(rec["events"]) == 50
    assert [e["seq"] for e in rec["events"]] == list(range(126, 176))
    assert activity.emit(rec, "info", "next")["seq"] == 176
    assert activity.events_after(rec, 170)[0]["seq"] == 171


def test_emit_never_raises_into_the_controller():
    class Hostile(dict):
        def setdefault(self, *a):
            raise RuntimeError("storage exploded")
    assert activity.emit(Hostile(state="X"), "info", "m") is None


def test_events_of_handles_legacy_and_malformed_records():
    assert activity.events_of({"id": "old"}) == [] and activity.events_of(None) == []
    assert activity.events_of({"events": "nope"}) == []
    assert activity.events_of({"events": [{"seq": 1}, "junk", {"no": "seq"}]}) == [{"seq": 1}]


def test_configured_secrets_and_credential_patterns_never_reach_an_event(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "tok-9f8e7d6c5b4a")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIzaSyTESTKEY1234567890")
    monkeypatch.setattr(config, "DITTO_PASS", "correct-horse-battery")
    rec = {"state": "PRECHECK"}
    activity.emit(rec, "info", "calling with tok-9f8e7d6c5b4a and key=AIzaSyTESTKEY1234567890",
                  header="Authorization: Bearer abc.def.ghi", nested={"url": "http://x/?password=hunter2hunter2"},
                  items=["X-API-Token: tok-9f8e7d6c5b4a", "correct-horse-battery"])
    activity.command_sink(rec)({"argv": ["docker", "run", "-e", "DB_PASSWORD=sup3rs3cret", "img",
                                         "--token=tok-9f8e7d6c5b4a"], "executed": False})
    blob = json.dumps(rec)
    for secret in ("tok-9f8e7d6c5b4a", "AIzaSyTESTKEY1234567890", "correct-horse-battery", "hunter2hunter2",
                   "abc.def.ghi", "sup3rs3cret"):
        assert secret not in blob, secret
    assert "***" in blob


def test_default_ditto_password_is_not_treated_as_a_secret():
    rec = {"state": "PRECHECK"}
    activity.emit(rec, "info", "container docker-ditto-ui-1 is running")
    assert "docker-ditto-ui-1" in rec["events"][0]["message"]      # not mangled into ***


def test_long_text_is_truncated():
    rec = {"state": "X"}
    activity.emit(rec, "error", "x" * 5000, stderr="y" * 5000)
    e = rec["events"][0]
    assert len(e["message"]) < 700 and len(e["detail"]["stderr"]) < 700 and "truncated" in e["message"]


# ================================================================ run_cmd instrumentation
async def test_run_cmd_reports_real_execution_with_returncode(realcmd):
    seen = []
    out = await guard.run_cmd(["docker", "inspect", "-f", "{{.Id}}", "docker-things-1"], on_command=seen.append)
    assert out == realcmd.containers["things"]["id"]
    assert seen[0]["executed"] is True and seen[0]["returncode"] == 0 and seen[0]["timed_out"] is False
    assert seen[0]["argv"] == ["docker", "inspect", "-f", "{{.Id}}", "docker-things-1"]


async def test_run_cmd_reports_failures_and_timeouts_as_observed(realcmd):
    seen = []
    realcmd.fail["docker kill"] = "Error response from daemon: permission denied"
    with pytest.raises(guard.GuardError):
        await guard.run_cmd(["docker", "kill", "x"], on_command=seen.append)
    assert seen[0]["returncode"] == 1 and "permission denied" in seen[0]["stderr"]
    realcmd.timeout_on = "docker pause"
    with pytest.raises(guard.GuardError, match="timed out"):
        await guard.run_cmd(["docker", "pause", "x"], on_command=seen.append, timeout=3)
    assert seen[1]["timed_out"] is True and seen[1]["returncode"] is None and seen[1]["timeout_s"] == 3


async def test_run_cmd_dry_run_reports_not_executed_and_starts_no_subprocess(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("dry-run must not start a subprocess")
    monkeypatch.setattr(subprocess, "run", boom)
    seen = []
    assert await REAL_RUN_CMD(["docker", "kill", "x"], dry_run=True, on_command=seen.append) == ""
    assert seen == [{"argv": ["docker", "kill", "x"], "executed": False}]


async def test_run_cmd_without_observer_behaves_as_before_and_a_failing_observer_changes_nothing(realcmd):
    assert await guard.run_cmd(["docker", "version"]) == "29.7.0"

    def bad_observer(info):
        raise RuntimeError("observer bug")
    assert await guard.run_cmd(["docker", "version"], on_command=bad_observer) == "29.7.0"


# ================================================================ scenario dry run
async def test_dry_run_emits_ordered_events_through_the_whole_lifecycle(sandbox, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a dry run must not start any subprocess")
    monkeypatch.setattr(guard, "run_cmd", REAL_RUN_CMD)
    monkeypatch.setattr(subprocess, "run", boom)

    r, rec = await run(scn(("delay", "mongodb", {"params": {"delay_ms": 250}}), ("pause", "gateway")),
                       authorization=None, dry_run=True)
    assert_well_formed(rec["events"])
    assert rec["state"] == guard.STATE_COMPLETED and rec["dry_run"] and rec["simulated"]
    assert rec["injected"] is False and rec["rollback_verified"] is False and rec["authorization"] is None
    states = [e["state"] for e in rec["events"]]
    order = ["PRECHECK", "PREFLIGHT", "BASELINE", "INJECTING", "MONITORING", "REVERTING", "VERIFYING_RECOVERY"]
    assert [s for i, s in enumerate(states) if i == 0 or states[i - 1] != s][:7] == order
    assert rec["events"][-1]["detail"]["final"] is True and "dry run" in rec["events"][-1]["message"]

    cmds = of(rec, "command")
    assert cmds and all(c["detail"]["executed"] is False for c in cmds)
    assert all(c["message"].startswith("Would run (dry run, not executed): ") for c in cmds)
    apply_cmds = [c["detail"]["command"] for c in of(rec, "command", "INJECTING")]
    assert any("netem delay 250ms" in c for c in apply_cmds) and any(c.startswith("docker pause") for c in apply_cmds)
    assert of(rec, "command", "REVERTING")                         # rollback steps are shown too
    assert len(of(rec, "health", "MONITORING")) == 3 and len(of(rec, "decision", "MONITORING")) == 3
    assert not os.path.exists(guard.LOCKS_DIR) or not os.listdir(guard.LOCKS_DIR)
    assert not scenario.list_journals()


async def test_dry_run_executes_no_docker_command_at_all(sandbox):
    """With the fake daemon in place of run_cmd: not one call reaches it."""
    await run(scn(("kill", "things"), ("loss", "gateway")), authorization=None, dry_run=True)
    assert sandbox.calls == []
    assert all(c["running"] and not c["paused"] and c["netem"] is None for c in sandbox.containers.values())


async def test_dry_run_is_allowed_during_a_lockout_and_never_counts_as_evidence(sandbox):
    import learning
    scenario.write_lockout("exp-x", "rollback could not be verified", [])
    r, rec = await run(scn(("kill", "things")), authorization=None, dry_run=True)
    assert rec["state"] == guard.STATE_COMPLETED and learning.is_evidence(rec) is False
    with pytest.raises(scenario.ScenarioRejected):
        scenario.ScenarioRun(scn(("kill", "things")), auth()).prepare()      # a real run is still refused


# ================================================================ real-run instrumentation
async def test_real_run_records_executed_commands_with_sanitized_metadata(realcmd):
    r, rec = await run(scn(("delay", "mongodb", {"params": {"delay_ms": 200}})))
    assert_well_formed(rec["events"])
    cmds = of(rec, "command")
    assert all(c["detail"]["executed"] is True for c in cmds)
    apply = next(c for c in cmds if "qdisc replace" in c["detail"]["command"])
    assert apply["state"] == "INJECTING" and apply["detail"]["returncode"] == 0
    assert apply["detail"]["read_only"] is False and apply["message"].startswith("Ran: docker run")
    assert realcmd.containers["mongodb"]["id"] in apply["detail"]["command"]
    inspect = next(c for c in cmds if " inspect " in c["detail"]["command"])
    assert inspect["detail"]["read_only"] is True
    delete = [c for c in of(rec, "command", "REVERTING") if "qdisc del" in c["detail"]["command"]]
    assert delete and delete[0]["detail"]["executed"] is True
    msgs = " | ".join(e["message"] for e in rec["events"])
    for needle in ("Authorized by tester", "Policy check passed", "resolved to container 'docker-mongodb-1'",
                   "Preflight passed for delay on mongodb", "Baseline from fresh pre-check measurements",
                   "Stop conditions:", "delay is active on mongodb", "Removal verified: delay is confirmed gone",
                   "Final check: the container for mongodb is running", "Recovery verified",
                   "Experiment completed. Every applied fault was removed and verified."):
        assert needle in msgs, needle


async def test_every_monitoring_tick_has_a_health_event_followed_by_a_decision(sandbox):
    r, rec = await run(scn(("pause", "things"), max_duration_s=10))
    mon_events = [e for e in rec["events"] if e["state"] == "MONITORING" and e["kind"] in ("health", "decision")]
    fault_ticks = [row for row in rec["timeline"] if row["phase"] == "fault"]
    assert len(of(rec, "health", "MONITORING")) == len(fault_ticks)
    for i in range(0, len(mon_events) - 1, 2):
        assert (mon_events[i]["kind"], mon_events[i + 1]["kind"]) == ("health", "decision")
    first = of(rec, "decision", "MONITORING")[0]["message"]
    assert "direct read 10 ms versus the 200 ms limit" in first and "staleness 1.0 s versus the 15 s limit" in first
    assert first.endswith("no stop condition met, continuing.")
    assert "duration of 10s reached" in of(rec, "decision", "MONITORING")[-1]["message"]


async def test_unavailable_measurements_are_reported_as_unavailable_not_zero(sandbox, monkeypatch):
    row = {**OK_ROW, "lost_acked": None, "stale_p95_s": None, "search_ok": False, "search_drift": None}
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW, OK_ROW, row]))
    r, rec = await run(scn(("pause", "things"), max_duration_s=10))
    h = of(rec, "health", "MONITORING")[0]
    assert "search FAILED" in h["message"] and "lost updates unavailable" in h["message"]
    assert "staleness p95 unavailable" in h["message"]
    assert h["detail"]["lost_acked"] is None and h["detail"]["search_ms"] is None and h["detail"]["stale_p95_s"] is None
    assert "lost-update check unavailable" in of(rec, "decision", "MONITORING")[0]["message"]


async def test_kill_switch_breach_produces_health_then_decision_with_the_reason(sandbox, monkeypatch):
    stale = {**OK_ROW, "stale_p95_s": 16.2}
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW, OK_ROW, stale, stale, stale]))
    r, rec = await run(scn(("delay", "mongodb"), max_duration_s=60))
    trip = [e for e in of(rec, "decision") if e["detail"].get("kill_switch")]
    assert len(trip) == 1 and trip[0]["state"] == "MONITORING"
    assert trip[0]["message"].startswith("Kill switch triggered: twin staleness p95 > 15.0s")
    assert trip[0]["detail"]["reason"] == rec["abort_reason"]
    before = rec["events"][rec["events"].index(trip[0]) - 1]
    assert before["kind"] == "health" and before["detail"]["stale_p95_s"] == 16.2
    earlier = of(rec, "decision", "MONITORING")[0]["message"]
    assert "staleness 16.2 s versus the 15 s limit" in earlier       # measured value, before the window filled


async def test_manual_stop_produces_an_explanatory_decision(sandbox):
    stop = asyncio.Event()
    r = scenario.ScenarioRun(scn(("pause", "things"), max_duration_s=60), auth(), dep_map=DEP_MAP, stop_event=stop)
    r.prepare()
    task = asyncio.create_task(r.run())
    while r.record["state"] != guard.STATE_MONITORING:
        await asyncio.sleep(0)
    stop.set()
    rec = await task
    d = [e for e in of(rec, "decision") if e["detail"].get("reason") == "manual_stop"]
    assert len(d) == 1 and "Emergency stop requested from the dashboard" in d[0]["message"]


async def test_revert_failure_produces_errors_and_never_claims_success(sandbox):
    sandbox.block_netem_delete = True
    r, rec = await run(scn(("delay", "mongodb"), ("pause", "gateway")))
    assert rec["state"] == scenario.STATE_RECOVERY_REQUIRED
    errors = [e["message"] for e in of(rec, "error")]
    assert any("Removal of delay from mongodb could NOT be verified" in m for m in errors)
    assert any(m.startswith("RECOVERY REQUIRED") for m in errors)
    assert errors[-1] == "Experiment ended with RECOVERY REQUIRED. Rollback was NOT fully verified."
    msgs = [e["message"] for e in rec["events"]]
    assert not any("delay is confirmed gone" in m for m in msgs)
    assert any("pause is confirmed gone from gateway" in m for m in msgs)     # the healthy one is still reported
    assert not any("Recovery verified" in m or "Experiment completed" in m for m in msgs)
    assert rec["events"][-1]["state"] == rec["state"]                         # terminal state is consistent
    fail = next(e for e in of(rec, "error") if "could NOT be verified" in e["message"])
    assert "tc qdisc del" in fail["detail"]["manual"][0] and fail["detail"]["verified"] is False


async def test_failed_apply_is_recorded_as_an_error_with_the_observed_state(sandbox):
    sandbox.fail["qdisc replace"] = "command failed (1): Operation not permitted"
    r, rec = await run(scn(("kill", "things"), ("delay", "mongodb")))
    e = next(x for x in of(rec, "error") if "Applying delay to mongodb failed" in x["message"])
    assert "rolled back, not retried" in e["message"] and e["detail"]["observed"]["active"] is False
    assert of(rec)[-1]["message"].startswith("Experiment FAILED.")


async def test_refusal_before_injection_explains_why(sandbox, monkeypatch):
    monkeypatch.setattr(probe, "probe_once", make_probe(default={**OK_ROW, "direct_ok": False}))
    r, rec = await run(scn(("kill", "things")))
    assert any(e["kind"] == "decision" and e["message"].startswith("Refusing to inject") for e in rec["events"])
    assert of(rec, "error")[0]["message"].startswith("Stopped before anything was injected")
    assert of(rec)[-1]["message"] == "Experiment was aborted."
    assert not of(rec, "command", "INJECTING")


async def test_recovery_streak_progress_and_timeout_are_reported(sandbox, monkeypatch):
    bad = {**OK_ROW, "search_ok": False}
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW] * 12 + [bad, OK_ROW, OK_ROW]))
    r, rec = await run(scn(("pause", "things"), max_duration_s=10))
    rec_decisions = [e["message"] for e in of(rec, "decision", "VERIFYING_RECOVERY")]
    assert rec_decisions[0].startswith("Not healthy yet") and rec_decisions[-2:] == ["1/2 healthy ticks.",
                                                                                   "2/2 healthy ticks."]
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW] * 12, default=bad))
    r, rec = await run(scn(("pause", "things"), max_duration_s=10))
    assert rec["state"] == guard.STATE_RECOVERY_TIMEOUT
    assert any(m["message"].startswith("Recovery NOT verified") for m in of(rec, "error"))


async def test_event_recording_failure_does_not_stop_the_rollback(sandbox, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("activity log is broken")
    monkeypatch.setattr(activity, "sanitize", broken)       # every emit now fails internally
    r, rec = await run(scn(("pause", "things"), max_duration_s=10))
    assert rec["state"] == guard.STATE_COMPLETED and rec["rollback_verified"]
    assert not sandbox.containers["things"]["paused"] and rec["events"] == []


# ================================================================ legacy guard.py path
async def test_guard_dry_run_events(monkeypatch, tmp_path):
    def boom(*a, **kw):
        raise AssertionError("dry-run must not start a subprocess")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(config, "PROBE_INTERVAL_S", 0.01)
    monkeypatch.setattr(probe, "probe_once", make_probe())
    container = await guard.resolve_container("mongodb", dry_run=True)
    fault = guard.DelayFault(container, True, 500, 30)
    rec = await guard.run_experiment(fault, "mongodb", container, guard.Thresholds.from_env(), 0.05, 0.02, 1.0,
                                     True, 3600)
    assert_well_formed(rec["events"])
    assert rec["schema_version"] == "1.1" and rec["simulated"] and rec["injected"] is False
    cmds = of(rec, "command")
    assert len(cmds) >= 3 and all(c["detail"]["executed"] is False for c in cmds)
    assert all(c["message"].startswith("Would run (dry run, not executed): docker ") for c in cmds)
    assert "netem delay 500ms" in cmds[0]["detail"]["command"]
    assert "synthetic container" in rec["events"][1]["message"]
    assert rec["events"][-1]["message"].endswith("no command was executed and nothing was changed.")
    assert guard._command_sink.get() is None                 # observer removed when the run ended


async def test_guard_real_path_kill_switch_manual_stop_and_revert_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASELINE_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(config, "PROBE_INTERVAL_S", 0.01)
    th = guard.Thresholds.from_env()
    ref = guard.ContainerRef("docker-things-1", "abc")

    class Fault:
        name, params, dry_run = "kill", {}, False

        def __init__(self, fail_revert=False):
            self.fail_revert = fail_revert

        async def preflight(self): ...
        async def inject(self): ...

        async def revert(self):
            if self.fail_revert:
                raise guard.RollbackError("container did not reach the running state")

    bad = {**OK_ROW, "direct_ok": False}
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW, OK_ROW, bad, bad, bad]))
    rec = await guard.run_experiment(Fault(), "things", ref, th, 5, 0.02, 1.0, True, 3600)
    assert_well_formed(rec["events"])
    trip = next(e for e in of(rec, "decision") if e["detail"].get("kill_switch"))
    assert "direct-read endpoint failed on all of the last 3 ticks" in trip["message"]
    assert "Revert verified" in " ".join(e["message"] for e in rec["events"])

    stop = asyncio.Event()
    monkeypatch.setattr(probe, "probe_once", make_probe())

    def on_update(r):
        if r["state"] == guard.STATE_MONITORING:
            stop.set()
    rec = await guard.run_experiment(Fault(), "things", ref, th, 5, 0.02, 1.0, True, 3600, stop_event=stop,
                                     on_update=on_update)
    assert any(e["detail"].get("reason") == "manual_stop" and "Manual stop requested" in e["message"]
               for e in of(rec, "decision"))

    rec = await guard.run_experiment(Fault(fail_revert=True), "things", ref, th, 0.03, 0.02, 1.0, True, 3600)
    assert rec["state"] == guard.STATE_FAILED
    errs = [e["message"] for e in of(rec, "error")]
    assert any(m.startswith("Revert could NOT be verified") for m in errs) and errs[-1] == "Experiment FAILED."
    assert not any("Revert verified" in e["message"] or "Recovery verified" in e["message"] for e in rec["events"])


def test_guard_cli_dry_run_still_works_without_an_experiment(monkeypatch, capsys):
    def boom(*a, **kw):
        raise AssertionError("no subprocess in dry-run")
    monkeypatch.setattr(subprocess, "run", boom)
    args = argparse.Namespace(fault="kill", target="things", ms=1, ttl=5)
    assert asyncio.run(guard.run_dry_run(args)) == 0


# ================================================================ orchestrator / API
async def test_running_snapshot_exposes_current_events(sandbox):
    started = orch.start_scenario(scn(("pause", "things"), max_duration_s=60), auth())
    assert [e["state"] for e in orch.get_status()["record"]["events"]][0] == "PRECHECK"   # before the task even ran
    while orch.get_status()["record"]["state"] != guard.STATE_MONITORING:
        await asyncio.sleep(0)
    live = orch.get_status()["record"]["events"]
    assert any(e["message"].startswith("pause is active on things") for e in live)
    n = len(live)
    for _ in range(12):
        await asyncio.sleep(0)
    assert len(orch.get_status()["record"]["events"]) > n          # keeps growing while it runs
    orch.request_stop()
    result = await started["task"]
    saved = json.load(open(result["experiment_path"]))
    assert [e["seq"] for e in saved["events"]] == [e["seq"] for e in result["record"]["events"]]
    assert_well_formed(saved["events"])


async def test_dry_run_through_the_orchestrator_skips_agents_and_is_saved(sandbox, monkeypatch):
    monkeypatch.setattr(probe, "current_health", lambda window_s=10, metrics_dir=None: {"status": "unknown"})
    started = orch.start_scenario(scn(("kill", "things")), None, dry_run=True)
    result = await started["task"]
    assert result["runs"] == [] and result["record"]["source"] == "dry_run"
    assert orch.get_status()["status"] == orch.STATUS_DONE and sandbox.calls == []
    assert os.path.isfile(result["experiment_path"])


@pytest.fixture
def client(sandbox, monkeypatch):
    monkeypatch.setattr(mon, "collect_services", lambda: {"available": False, "error": "stub", "services": None})
    with TestClient(api.app) as c:
        c.docker = sandbox
        yield c


def wait_done(client):
    deadline = time.time() + 20
    while time.time() < deadline:
        st = client.get("/api/orchestrator/status").json()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError("did not finish")


SCN = {"actions": [{"fault": "pause", "target": "things"}], "max_duration_s": 10, "pre_s": 2, "recovery_timeout_s": 20}


def test_events_api_filters_incrementally_for_saved_records(client):
    exp = client.post("/api/scenarios/run", json={"authorize": True, "scenario": SCN}).json()["experiment_id"]
    wait_done(client)
    full = client.get(f"/api/experiments/{exp}/events").json()
    assert full["terminal"] is True and full["running"] is False and full["dry_run"] is False
    seqs = [e["seq"] for e in full["events"]]
    assert seqs == list(range(1, len(seqs) + 1)) and full["last_seq"] == seqs[-1]
    part = client.get(f"/api/experiments/{exp}/events?after_seq=10").json()
    assert [e["seq"] for e in part["events"]] == seqs[10:]
    none = client.get(f"/api/experiments/{exp}/events?after_seq={seqs[-1]}").json()
    assert none["events"] == [] and none["last_seq"] == seqs[-1]
    assert client.get(f"/api/experiments/{exp}/events?after_seq=999999").json()["events"] == []
    assert client.get(f"/api/experiments/{exp}").json()["record"]["events"] == full["events"]


def test_events_api_serves_the_running_experiment_from_memory(client, monkeypatch):
    async def slow_probe(c, ids):          # keep the (virtual-time) run alive long enough to observe it
        time.sleep(0.004)
        return dict(OK_ROW)
    monkeypatch.setattr(probe, "probe_once", slow_probe)
    long = {**SCN, "max_duration_s": 300}
    exp = client.post("/api/scenarios/run", json={"authorize": True, "scenario": long}).json()["experiment_id"]
    deadline = time.time() + 10
    while client.get("/api/orchestrator/status").json()["record"]["state"] != "MONITORING":
        assert time.time() < deadline
        time.sleep(0.01)
    a = client.get(f"/api/experiments/{exp}/events").json()
    assert a["running"] is True and a["terminal"] is False and a["events"]
    assert not os.path.exists(os.path.join(config.EXPERIMENTS_DIR, f"{exp}.json"))     # not saved yet
    time.sleep(0.05)
    b = client.get(f"/api/experiments/{exp}/events?after_seq={a['last_seq']}").json()
    assert b["events"] and all(e["seq"] > a["last_seq"] for e in b["events"])           # only the new ones
    client.post("/api/stop")
    wait_done(client)
    c = client.get(f"/api/experiments/{exp}/events?after_seq={b['last_seq']}").json()
    assert c["terminal"] is True and c["running"] is False and c["events"][-1]["detail"]["final"] is True


def test_events_api_validation_and_not_found(client):
    assert client.get("/api/experiments/exp-does-not-exist/events").status_code == 404
    for bad in ("-1", "abc", "1.5"):
        r = client.get(f"/api/experiments/whatever/events?after_seq={bad}")
        assert r.status_code == 422, bad
    spec = client.get("/openapi.json").json()
    assert "ExperimentEventsResponse" in spec["components"]["schemas"]
    assert "/api/experiments/{experiment_id}/events" in spec["paths"]


def test_legacy_records_without_events_load_everywhere(client):
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    legacy = {"schema_version": "1.0", "id": "exp-legacy", "fault": "kill", "target": "things", "state": "COMPLETED",
              "injected": True, "dry_run": False, "timeline": [], "baseline": None, "during": None}
    with open(os.path.join(config.EXPERIMENTS_DIR, "exp-legacy.json"), "w") as f:
        json.dump(legacy, f)
    r = client.get("/api/experiments/exp-legacy/events")
    assert r.status_code == 200 and r.json() == {"experiment_id": "exp-legacy", "state": "COMPLETED",
                                                 "running": False, "terminal": True, "dry_run": False,
                                                 "last_seq": 0, "events": []}
    assert client.get("/api/experiments/exp-legacy").status_code == 200
    assert any(e["id"] == "exp-legacy" for e in client.get("/api/experiments").json())
    assert client.post("/api/insights/analyze?experiment_id=exp-legacy").status_code == 200
    assert json.load(open(os.path.join(config.EXPERIMENTS_DIR, "exp-legacy.json"))) == legacy   # file untouched


def test_dry_run_over_http_needs_no_authorization_and_works_in_observe_mode(client):
    client.post("/api/controller/mode", json={"mode": "observe"})
    r = client.post("/api/scenarios/run", json={"dry_run": True, "scenario": SCN})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    st = wait_done(client)
    assert st["record"]["dry_run"] is True and client.docker.calls == []
    ev = client.get(f"/api/experiments/{r.json()['experiment_id']}/events").json()
    assert ev["dry_run"] is True
    cmds = [e for e in ev["events"] if e["kind"] == "command"]
    assert cmds and all(e["detail"]["executed"] is False for e in cmds)
    assert client.post("/api/scenarios/run", json={"scenario": SCN}).status_code == 400     # real run: still gated
    row = next(e for e in client.get("/api/experiments").json() if e["id"] == r.json()["experiment_id"])
    assert row["simulated"] is True and row["evidence"] is False


def test_events_api_never_returns_configured_secrets(client, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "tok-9f8e7d6c5b4a")
    hdr = {"X-API-Token": "tok-9f8e7d6c5b4a", "X-Operator": "alice"}
    client.headers.update(hdr)          # with a token configured, reads need it too
    exp = client.post("/api/scenarios/run", json={"authorize": True, "scenario": SCN}, headers=hdr).json()["experiment_id"]
    wait_done(client)
    body = client.get(f"/api/experiments/{exp}/events", headers=hdr).text
    assert "tok-9f8e7d6c5b4a" not in body and "Authorized by alice" in body


async def test_dry_run_with_an_expired_baseline_still_previews_and_says_a_real_run_would_be_refused(
        realcmd, monkeypatch, tmp_path):
    sandbox = realcmd       # commands go through the real run_cmd, so previews are recorded
    stale = tmp_path / "baseline.json"
    stale.write_text(json.dumps({"recorded_at": time.time() - 3 * 3600, "direct_ms": {"p95": 20},
                                 "search_ms": {"p95": 20}, "direct_error_rate": 0.0, "search_error_rate": 0.0}))
    monkeypatch.setattr(config, "BASELINE_FILE", str(stale))
    r, rec = await run(scn(("kill", "things")), authorization=None, dry_run=True)
    assert rec["state"] == guard.STATE_COMPLETED and rec["baseline_source"] == "fresh"
    assert "stale" in rec["baseline_file_problem"]
    warn = of(rec, "warning")
    assert len(warn) == 1 and warn[0]["message"].startswith("baseline.json cannot be used")
    assert of(rec, "command", "INJECTING") and sandbox.calls == []
    # a real run no longer fails on a stale file: it measures a fresh baseline and proceeds
    r, rec = await run(scn(("kill", "things")))
    assert rec["state"] == guard.STATE_COMPLETED and rec["baseline_source"] == "fresh" and rec["error"] is None
    assert len([t for t in rec["timeline"] if t["phase"] == "before"]) == 30 and rec["rollback_verified"]
    # ...but it still refuses when Ditto is unhealthy while that baseline is measured
    monkeypatch.setattr(probe, "probe_once", make_probe([OK_ROW] * 10, default={**OK_ROW, "direct_ok": False}))
    n = len(sandbox.mutating_calls())
    r, rec = await run(scn(("kill", "things")))
    assert rec["state"] == guard.STATE_ABORTED and len(sandbox.mutating_calls()) == n
