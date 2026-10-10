"""HTTP contract of the v2 API. Uses TestClient as a context manager so the lifespan
(startup recovery, monitor) and background experiment tasks really run. Docker is
FakeDocker; nothing here reaches a real daemon or a real Ditto."""
import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import api
import config
import guard
import monitor as mon
import orchestrator as orch
import scenario
import store
from conftest import scn
from controller import controller

SCN = {"actions": [{"fault": "kill", "target": "things-search"}], "max_duration_s": 10, "pre_s": 2,
       "recovery_timeout_s": 20}
MULTI = {"actions": [{"fault": "delay", "target": "mongodb", "params": {"delay_ms": 300}},
                     {"fault": "loss", "target": "gateway", "params": {"loss_pct": 5}}],
         "max_duration_s": 10, "pre_s": 2, "recovery_timeout_s": 20}


@pytest.fixture
def client(sandbox, monkeypatch):
    monkeypatch.setattr(mon, "collect_services", lambda: {"available": False, "error": "stub", "services": None})
    with TestClient(api.app) as c:
        c.docker = sandbox
        yield c


def wait_done(client, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/orchestrator/status").json()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError(f"experiment did not finish: {st['status']}")


# ---------------------------------------------------------------- liveness / readiness
def test_livez_and_readyz(client):
    assert client.get("/api/livez").json()["status"] == "alive"
    r = client.get("/api/readyz")
    assert r.status_code == 200 and r.json()["ready"] is True
    scenario.write_lockout("exp-x", "rollback could not be verified", [])
    r = client.get("/api/readyz")
    assert r.status_code == 503 and r.json()["checks"]["no_recovery_lockout"]["ok"] is False


def test_readyz_reports_an_unavailable_store(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "KNOWLEDGE_DB", str(tmp_path))
    r = client.get("/api/readyz")
    assert r.status_code == 503 and r.json()["checks"]["knowledge_store"]["ok"] is False
    assert client.get("/api/learning").status_code == 503
    assert client.get("/api/livez").status_code == 200           # alive, just not ready


def test_openapi_documents_the_contract(client):
    spec = client.get("/openapi.json").json()
    for path in ("/api/scenarios/run", "/api/scenarios/validate", "/api/stream", "/api/readyz",
                 "/api/controller/mode", "/api/agents", "/api/recovery/resolve"):
        assert path in spec["paths"]
    assert "Scenario" in spec["components"]["schemas"]


# ---------------------------------------------------------------- faults / preview
def test_faults_endpoint_lists_registry_limits_and_environment(client):
    body = client.get("/api/faults").json()
    names = {f["name"]: f for f in body["faults"]}
    assert names["loss"]["supported"] and not names["memory_pressure"]["supported"]
    assert body["limits"]["max_affected_services"] == config.MAX_AFFECTED_SERVICES
    assert body["environment"]["docker_available"] is True and "mongodb" in body["targets"]


def test_validate_is_a_preview_that_touches_nothing(client):
    r = client.post("/api/scenarios/validate", json=MULTI)
    body = r.json()
    assert r.status_code == 200 and body["ok"] and body["rollback_order"] == ["a1", "a2"]
    assert body["actions"][0]["commands"] and body["prediction"]["basis"] == "none"
    assert client.docker.calls == []
    assert orch.get_status()["status"] == orch.STATUS_IDLE


def test_validate_returns_structured_policy_errors(client):
    bad = {"actions": [{"fault": "kill", "target": "things"}, {"fault": "pause", "target": "gateway"}]}
    body = client.post("/api/scenarios/validate", json=bad).json()
    assert body["ok"] is False and body["errors"][0]["code"] == "untested_combination"


def test_malformed_scenario_is_a_422(client):
    assert client.post("/api/scenarios/validate", json={"actions": []}).status_code == 422
    assert client.post("/api/scenarios/validate",
                       json={"actions": [{"fault": "kill", "target": "things", "shell": "x"}]}).status_code == 422


def test_preflight_endpoint_is_read_only(client):
    body = client.post("/api/scenarios/preflight", json=MULTI).json()
    assert body["ok"] and client.docker.mutating_calls() == []


# ---------------------------------------------------------------- authorization gates
def test_run_requires_explicit_authorize(client):
    r = client.post("/api/scenarios/run", json={"scenario": SCN})
    assert r.status_code == 400 and "authorize" in r.json()["detail"]
    assert client.docker.calls == []


def test_token_is_required_on_every_state_changing_route_when_configured(client, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "s3cret")
    posts = [("/api/scenarios/run", {"authorize": True, "scenario": SCN}), ("/api/stop", None),
             ("/api/plan", None), ("/api/controller/mode", {"mode": "observe"}),
             ("/api/controller/autonomous/start", None), ("/api/recovery/resolve", {}),
             ("/api/agents/plan", None), ("/api/orchestrator/reset", None),
             ("/api/experiments/run", {"proposal_id": "x", "authorize": True})]
    for path, body in posts:
        assert client.post(path, json=body).status_code == 401, path
        assert client.post(path, json=body, headers={"X-API-Token": "wrong"}).status_code == 401, path
    assert client.docker.calls == [] and controller.mode == "manual"
    ok = client.post("/api/controller/mode", json={"mode": "observe"}, headers={"X-API-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()["mode"] == "observe"


def test_token_also_protects_every_read_except_the_health_probes(client, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "s3cret")
    reads = ["/api/snapshot", "/api/experiments", "/api/experiments/x/events", "/api/orchestrator/status",
             "/api/agents", "/api/learning", "/api/recommendations", "/api/audit", "/api/faults",
             "/api/controller", "/api/recovery", "/api/health", "/api/no-such-route"]
    for path in reads:
        r = client.get(path)
        assert r.status_code == 401 and r.json()["code"] == "unauthorized", path
        assert client.get(path, headers={"X-API-Token": "nope"}).status_code == 401, path
    assert client.get("/api/livez").status_code == 200 and client.get("/api/readyz").status_code == 200
    snap = client.get("/api/snapshot", headers={"X-API-Token": "s3cret"})
    assert snap.status_code == 200 and "s3cret" not in snap.text     # the token is never echoed back
    # a 401 still carries CORS headers, so the dashboard can tell "unauthorized" from "unreachable"
    r = client.get("/api/snapshot", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_stream_needs_a_single_use_ticket_when_a_token_is_configured(client, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "s3cret")
    hdr = {"X-API-Token": "s3cret"}
    assert client.get("/api/stream?max_events=1").status_code == 401
    assert client.get("/api/stream?max_events=1&ticket=made-up").status_code == 401
    assert client.get("/api/stream?max_events=1&token=s3cret").status_code == 401      # the token itself is never a URL credential
    assert client.post("/api/stream/ticket").status_code == 401
    ticket = client.post("/api/stream/ticket", headers=hdr).json()["ticket"]
    assert "s3cret" not in ticket
    api.bus.publish("alert", {"level": "info", "message": "x"})
    with client.stream("GET", f"/api/stream?last_seq=0&max_events=1&ticket={ticket}") as r:
        assert r.status_code == 200
        list(r.iter_lines())
    assert client.get(f"/api/stream?max_events=1&ticket={ticket}").status_code == 401   # already used
    monkeypatch.setattr(api, "STREAM_TICKET_TTL_S", -1.0)
    expired = client.post("/api/stream/ticket", headers=hdr).json()["ticket"]
    assert client.get(f"/api/stream?max_events=1&ticket={expired}").status_code == 401


def test_without_a_token_everything_stays_open_on_loopback(client):
    assert client.get("/api/snapshot").status_code == 200
    with client.stream("GET", "/api/stream?last_seq=999999&boot=x&max_events=1") as r:
        assert r.status_code == 200
        list(r.iter_lines())


def test_observe_mode_refuses_every_run(client):
    client.post("/api/controller/mode", json={"mode": "observe"})
    r = client.post("/api/scenarios/run", json={"authorize": True, "scenario": SCN})
    assert r.status_code == 403 and r.json()["code"] == "mode_forbids"
    assert client.docker.calls == []


def test_recommend_mode_refuses_custom_scenarios_but_runs_a_proposal(client):
    client.post("/api/controller/mode", json={"mode": "recommend"})
    assert client.post("/api/scenarios/run", json={"authorize": True, "scenario": SCN}).status_code == 403
    proposal = client.post("/api/plan").json()
    assert client.get("/api/plan").json()["proposal_id"] == proposal["proposal_id"]
    assert client.post("/api/scenarios/run", json={"authorize": True, "proposal_id": "stale"}).status_code == 409
    r = client.post("/api/scenarios/run", json={"authorize": True, "proposal_id": proposal["proposal_id"]})
    assert r.status_code == 200
    st = wait_done(client)
    assert st["record"]["source"] == "proposal" and st["record"]["state"] == "COMPLETED"


def test_policy_violation_is_422_with_errors_and_lockout_is_423(client):
    bad = {"actions": [{"fault": "delay", "target": "things"}, {"fault": "loss", "target": "things"}]}
    r = client.post("/api/scenarios/run", json={"authorize": True, "scenario": bad})
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "conflicting_faults"
    scenario.write_lockout("exp-x", "rollback could not be verified", [])
    r = client.post("/api/scenarios/run", json={"authorize": True, "scenario": SCN})
    assert r.status_code == 423 and r.json()["code"] == "recovery_required"
    assert client.docker.mutating_calls() == []


def test_autonomous_mode_cannot_be_enabled_through_the_api(client):
    r = client.post("/api/controller/mode", json={"mode": "autonomous_sandbox"})
    assert r.status_code == 403 and "AUTONOMY_SANDBOX_ENABLED" in r.json()["detail"]
    assert client.post("/api/controller/mode", json={"mode": "godmode"}).status_code == 422
    assert client.post("/api/controller/autonomous/start").status_code == 403
    assert client.get("/api/controller").json()["sandbox_enabled_in_config"] is False


# ---------------------------------------------------------------- run / stop / status
def test_multi_fault_run_end_to_end_over_http(client):
    r = client.post("/api/scenarios/run", json={"authorize": True, "scenario": MULTI, "request_id": "r1"},
                    headers={"X-Operator": "alice"})
    assert r.status_code == 200 and r.json()["duplicate"] is False
    exp_id = r.json()["experiment_id"]
    dup = client.post("/api/scenarios/run", json={"authorize": True, "scenario": MULTI, "request_id": "r1"}).json()
    assert dup["duplicate"] is True and dup["experiment_id"] == exp_id
    st = wait_done(client)
    assert st["status"] == "done" and st["recovery_state"] == "RECOVERED"
    assert [a["status"] for a in st["record"]["actions"]] == ["rolled_back", "rolled_back"]
    assert all(c["netem"] is None for c in client.docker.containers.values())

    detail = client.get(f"/api/experiments/{exp_id}").json()
    assert detail["record"]["authorization"]["actor"] == "alice"
    assert detail["learning"]["is_evidence"] and detail["recommendations"], detail["record"]["validation"]
    assert [r["agent"] for r in detail["agent_runs"]][::-1] == ["recovery", "validation", "insights",
                                                               "learning", "remediation"]
    types = [t["type"] for t in detail["timeline"]]
    assert types[:2] == ["validated", "authorized"] and "experiment" in types and "agent" in types

    row = next(e for e in client.get("/api/experiments").json() if e["id"] == exp_id)
    assert row["kind"] == "scenario" and row["faults"] == ["delay", "loss"] and row["evidence"] is True
    assert row["validation_verdict"] == "conclusive"

    learn = client.get("/api/learning").json()
    assert learn["summary"]["evidence_runs"] == 1 and learn["records"][0]["experiment_id"] == exp_id
    assert learn["summary"]["model"]["trained_model"] is False
    agents_view = {a["name"]: a for a in client.get("/api/agents").json()["agents"]}
    assert agents_view["learning"]["last"]["status"] == "completed" and agents_view["learning"]["recent"]


def test_second_request_while_running_is_409_and_stop_rolls_back(client, monkeypatch):
    # Virtual time alone can finish the whole 300s run before the first status poll on a
    # slow machine; a small real delay per tick keeps it observable until the stop.
    virtual_sleep = asyncio.sleep

    async def paced_sleep(delay, *a, **kw):
        await virtual_sleep(delay)
        await asyncio.tasks.sleep(0.01)
    monkeypatch.setattr(asyncio, "sleep", paced_sleep)
    long ={**SCN, "max_duration_s": 300, "actions": [{"fault": "pause", "target": "things"}]}
    assert client.post("/api/scenarios/run", json={"authorize": True, "scenario": long}).status_code == 200
    r = client.post("/api/scenarios/run", json={"authorize": True, "scenario": SCN})
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    assert client.post("/api/orchestrator/reset").status_code == 409
    deadline = time.time() + 10
    while client.get("/api/orchestrator/status").json()["record"]["state"] != "MONITORING":
        assert time.time() < deadline
        time.sleep(0.01)
    assert client.docker.containers["things"]["paused"] is True
    assert client.post("/api/stop").json()["status"] == "stop_requested"
    st = wait_done(client)
    assert st["record"]["abort_reason"].startswith("emergency stop")
    assert client.docker.containers["things"]["paused"] is False
    assert client.post("/api/stop").status_code == 409


def test_recommendation_status_can_be_tracked(client):
    client.post("/api/scenarios/run", json={"authorize": True, "scenario": SCN})
    st = wait_done(client)
    exp_id = st["experiment_id"]
    r = client.post(f"/api/recommendations/{exp_id}/0/status", json={"status": "implemented", "note": "raised timeout"})
    assert r.status_code == 200 and r.json()["status"] == "implemented"
    assert client.post(f"/api/recommendations/{exp_id}/0/status", json={"status": "verified"}).status_code == 422
    assert client.post(f"/api/recommendations/{exp_id}/99/status", json={"status": "accepted"}).status_code == 404
    assert client.get("/api/recommendations?status=implemented").json()[0]["experiment_id"] == exp_id
    assert client.get("/api/audit").json()[0]["action"] == "recommendation_status"


# ---------------------------------------------------------------- recovery
def test_recovery_required_is_visible_and_resolvable(client):
    client.docker.block_netem_delete = True
    client.post("/api/scenarios/run", json={"authorize": True, "scenario": MULTI})
    st = wait_done(client)
    assert st["record"]["state"] == "RECOVERY_REQUIRED" and st["recovery_state"] == "RECOVERY_REQUIRED"
    rec = client.get("/api/recovery").json()
    assert rec["lockout"]["experiment_id"] == st["experiment_id"] and rec["journals"]
    assert client.get("/api/controller").json()["lockout"] is not None
    assert client.post("/api/recovery/resolve", json={}).json()["lockout"] is not None      # still broken
    assert client.post("/api/recovery/resolve", json={"force": True}).status_code == 400    # needs a note
    client.docker.block_netem_delete = False
    assert client.post("/api/recovery/resolve", json={}).json()["lockout"] is None
    assert client.get("/api/readyz").status_code == 200


def test_startup_replays_the_journal_of_a_crashed_process(sandbox, monkeypatch):
    """Backend restart with a fault still applied: lifespan startup must clean it up."""
    monkeypatch.setattr(mon, "collect_services", lambda: {"available": False, "error": "stub", "services": None})
    c = sandbox.containers["things"]
    c["paused"] = True
    os.makedirs(config.JOURNAL_DIR, exist_ok=True)
    with open(os.path.join(config.JOURNAL_DIR, "exp-crashed.json"), "w") as f:
        json.dump({"id": "exp-crashed", "actions": [{"id": "a1", "status": "applied", "recovery": {
            "fault": "pause", "service": "things", "container": {"name": c["name"], "id": c["id"]},
            "params": {}, "state": {}}}]}, f)
    with TestClient(api.app) as client:
        body = client.get("/api/readyz").json()
        assert body["startup_recovery"]["recovered"][0]["fault"] == "pause" and body["ready"] is True
    assert c["paused"] is False and not scenario.list_journals()


# ---------------------------------------------------------------- snapshot / stream
def test_snapshot_then_stream_has_no_gap(client):
    snap = client.get("/api/snapshot").json()
    for key in ("seq", "boot", "health", "monitoring", "services", "rows", "orchestrator", "controller", "agents"):
        assert key in snap
    assert snap["services"]["services"] is None and snap["monitoring"]["state"] in ("ok", "stale")
    api.bus.publish("alert", {"level": "info", "message": "after snapshot"})
    with client.stream("GET", f"/api/stream?last_seq={snap['seq']}&boot={snap['boot']}&max_events=1") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in r.iter_lines() if ln]
    assert lines[0] == f"id: {snap['seq'] + 1}" or int(lines[0][4:]) > snap["seq"]
    event = json.loads(lines[1][6:])
    assert event["seq"] > snap["seq"] and event["boot"] == snap["boot"]


def test_stream_resyncs_a_client_from_an_older_process(client):
    with client.stream("GET", "/api/stream?last_seq=5&boot=dead-process&max_events=1") as r:
        event = json.loads([ln for ln in r.iter_lines() if ln.startswith("data: ")][0][6:])
    assert event["type"] == "resync"


# ---------------------------------------------------------------- history robustness
def test_history_survives_corrupt_and_legacy_files(client):
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    files = {"exp-v1.json": json.dumps({"id": "exp-v1", "fault": "kill", "target": "things", "state": "COMPLETED",
                                        "injected": True, "dry_run": True}),
             "exp-min.json": json.dumps({"id": "exp-min"}),
             "exp-broken.json": "{not json", "exp-list.json": "[]"}
    for name, body in files.items():
        with open(os.path.join(config.EXPERIMENTS_DIR, name), "w") as f:
            f.write(body)
    r = client.get("/api/experiments")
    assert r.status_code == 200 and r.headers["X-Unreadable-Count"] == "2"
    rows = {e["id"]: e for e in r.json()}
    assert rows["exp-v1"]["simulated"] is True and rows["exp-v1"]["evidence"] is False   # old dry-run bug shape
    assert rows["exp-min"]["signature"] == "" and rows["exp-min"]["recovery_state"] == "RECOVERY_NOT_REQUIRED"
    assert client.get("/api/experiments/exp-min").status_code == 200
    assert client.get("/api/experiments/exp-broken").status_code == 422
    assert client.get("/api/experiments/exp-list").status_code == 422
    assert client.get("/api/experiments/nope").status_code == 404
    assert client.get("/api/experiments/..%5C..%5Csecrets").status_code in (400, 404)


def test_legacy_real_run_is_refused_in_observe_mode_and_during_lockout(client, monkeypatch):
    monkeypatch.setattr(orch, "_recent_experiments", lambda limit=10: [])
    pid = client.post("/api/agents/plan").json()["proposal_id"]
    scenario.write_lockout("exp-x", "r", [])
    assert client.post("/api/experiments/run", json={"proposal_id": pid, "authorize": True}).status_code == 423
    scenario.clear_lockout()
    client.post("/api/controller/mode", json={"mode": "observe"})
    assert client.post("/api/experiments/run", json={"proposal_id": pid, "authorize": True}).status_code == 403
    assert client.docker.mutating_calls() == []


def test_serve_reports_a_port_conflict_clearly(monkeypatch, capsys):
    import socket

    import main
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        monkeypatch.setattr(config, "API_PORT", s.getsockname()[1])
        monkeypatch.setattr(config, "API_HOST", "127.0.0.1")
        assert main.cmd_serve(None) == 2
    finally:
        s.close()
    assert "already in use" in capsys.readouterr().out


def test_serve_refuses_a_public_bind_without_a_token(monkeypatch, capsys):
    import main
    monkeypatch.setattr(config, "API_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "API_TOKEN", "")
    assert main.cmd_serve(None) == 2 and "API_TOKEN" in capsys.readouterr().out


# ---------------------------------------------------------------- built dashboard served by the API
def test_api_serves_the_built_dashboard_and_keeps_api_routes_separate(client, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ditto-chaos</title>")
    (dist / "assets" / "index-abc.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside the dist folder")
    monkeypatch.setattr(api, "FRONTEND_DIST", str(dist))

    for path in ("/", "/control", "/history/exp-1"):          # client-side routes get the app shell
        r = client.get(path)
        assert r.status_code == 200 and "ditto-chaos" in r.text and r.headers["cache-control"] == "no-cache"
    asset = client.get("/assets/index-abc.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert client.get("/assets/missing.js").status_code == 404
    missing_api = client.get("/api/no-such-route")
    assert missing_api.status_code == 404 and missing_api.headers["content-type"].startswith("application/json")
    assert client.get("/api/livez").json()["status"] == "alive"              # real API routes still win
    for sneaky in ("/..%2fsecret.txt", "/assets/..%2f..%2fsecret.txt", "/%2e%2e/secret.txt"):
        assert "outside the dist folder" not in client.get(sneaky).text


def test_missing_build_gives_a_clear_404_not_a_crash(client, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "FRONTEND_DIST", str(tmp_path / "nope"))
    r = client.get("/")
    assert r.status_code == 404 and "npm run build" in r.json()["detail"]
    assert client.get("/api/livez").status_code == 200
