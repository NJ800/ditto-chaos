"""Product-aware analysis, learning, agents, scenario scope, mixed-fleet experiments and
the fleet API. Docker is FakeDocker, Ditto HTTP and probes are mocked (see conftest)."""
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import agents
import api
import config
import fleet
import learning
import monitor as mon
import probe
import resilience_agent
import scenario
import scenario_planner
from conftest import DEP_MAP, OK_ROW, auth, make_probe, scn

MIXED_ENV = {"SIM_FLEET": "ev:4,industrial_machine:4,smart_home:4", "SIM_SEED": "5"}


def pblock(**over):
    b = {"title": "x", "measured": True, "twins": 4, "observed": 4, "missing": 0, "fresh": 4, "stale": 0,
         "fresh_ratio": 1.0, "stale_p95_s": 2.0, "freshness_limit_s": 8.0, "valid": 4, "invalid": 0,
         "valid_ratio": 1.0, "lost_acked": 0, "search_drift": 0, "violations": {}, "operational": {},
         "signals": {}}
    b.update(over)
    return b


def prow(phase=None, **profiles_over):
    """A probe row with system fields from OK_ROW and per-profile blocks."""
    names = {"ev": "Electric vehicles", "industrial_machine": "Industrial machinery", "smart_home": "Smart home devices"}
    row = {**OK_ROW, "profiles": {k: pblock(title=t, **profiles_over.get(k, {})) for k, t in names.items()}}
    if phase:
        row["phase"] = phase
    return row


def record_with_impact(fault_over, rec_over=None, **kw):
    tl = ([prow("before")] * 3 + [prow("fault", **fault_over)] * 3
          + [prow("recovery", **(rec_over or {}))] * 3)
    rec = {"id": kw.get("id", "exp-p1"), "kind": "scenario", "fault": "delay", "target": "mongodb",
           "signature": "delay@mongodb", "state": "COMPLETED", "injected": True, "rollback_verified": True,
           "recovery_outcome": "recovered", "baseline": {"direct_ms": {"p95": 20}, "search_ms": {"p95": 20}},
           "during": {"direct_ms": {"p95": 22}, "search_ms": {"p95": 22}}, "timeline": tl,
           "actions": [{"fault": "delay", "target": "mongodb", "params": {}, "status": "rolled_back"}],
           "fleet": {"signature": "abc", "profiles": {"ev": {}, "industrial_machine": {}, "smart_home": {}}},
           "started_at_utc": kw.get("ts", "2026-10-11T00:00:00+00:00")}
    rec["profile_impact"] = fleet.profile_impact(tl, kw.get("scope"))
    return rec


STALE = {"fresh_ratio": 0.25, "stale_p95_s": 30.0, "fresh": 1, "stale": 3}


# ---------------------------------------------------------------- analysis
def test_insights_report_product_findings_only_from_measured_profiles():
    rec = record_with_impact({"ev": STALE})
    a = resilience_agent.analyze_experiment(rec)
    product = [f for f in a["findings"] if f.get("scope") == "product"]
    assert [f["profile"] for f in product] == ["ev"]
    assert product[0]["title"].startswith("Electric vehicles: twin freshness dropped")
    assert "1.0 -> 0.25" in product[0]["evidence"] and product[0]["severity"] == "high"
    assert product[0]["shared_with_all_profiles"] is False
    pi = a["product_impact"]
    assert pi["affected_profiles"] == ["ev"]
    assert set(pi["unaffected_profiles"]) == {"industrial_machine", "smart_home"}
    assert "NOT a measurement" in pi["domain_context"]["ev"]["basis"]
    assert all(f.get("scope") in ("system", "product") for f in a["findings"])
    assert a["evidence"]["profile_impact"]["profiles"]["ev"]["fresh_ratio_during"] == 0.25


def test_unreadable_profiles_are_not_called_healthy_or_affected():
    rec = record_with_impact({k: {"measured": False, "fresh_ratio": None, "stale_p95_s": None}
                              for k in ("ev", "industrial_machine", "smart_home")})
    a = resilience_agent.analyze_experiment(rec)
    assert not [f for f in a["findings"] if f.get("scope") == "product"]
    assert set(a["product_impact"]["not_measured_profiles"]) == {"ev", "industrial_machine", "smart_home"}
    out = agents.InsightsAgent().run({"record": rec, "history": []})
    assert any("impact is unknown, not zero" in x for x in out["limitations"])


def test_shared_symptoms_are_labelled_service_level():
    rec = record_with_impact({k: STALE for k in ("ev", "industrial_machine", "smart_home")})
    a = resilience_agent.analyze_experiment(rec)
    product = [f for f in a["findings"] if f.get("scope") == "product"]
    assert len(product) == 3 and all(f["shared_with_all_profiles"] for f in product)
    assert "service-level effect" in product[0]["impact"]
    assert a["product_impact"]["system_wide_symptoms"] == ["freshness_loss", "staleness"]


def test_legacy_car_record_has_no_product_claims():
    rec = {"id": "exp-legacy", "fault": "kill", "target": "things", "injected": True, "timeline": [],
           "baseline": {}, "during": {}}
    a = resilience_agent.analyze_experiment(rec)
    assert a["product_impact"]["available"] is False
    assert not [f for f in a["findings"] if f.get("scope") == "product"]
    case = learning.to_case(rec)
    assert case["legacy_fleet"].startswith("car-only") and case["profile_outcomes"] is None


# ---------------------------------------------------------------- learning
def test_learning_separates_product_specific_from_system_wide():
    c1 = learning.to_case(record_with_impact({"ev": STALE}, id="e1"))
    c2 = learning.to_case(record_with_impact({k: STALE for k in ("ev", "industrial_machine", "smart_home")}, id="e2"))
    pats = learning.profile_patterns([c1, c2])
    by = {p["profile"]: p for p in pats["profiles"]}
    assert pats["runs_with_profile_data"] == 2
    assert by["ev"]["runs_affected"] == 2 and by["industrial_machine"]["runs_affected"] == 1
    assert by["ev"]["product_specific_symptoms"] == {"freshness_loss": 1, "staleness": 1}
    assert pats["system_wide_symptoms"] == {"freshness_loss": 1, "staleness": 1}
    assert by["ev"]["affected_by_targets"] == {"mongodb": 2}
    assert learning.summarize([c1, c2])["by_profile"]["runs_with_profile_data"] == 2


def test_learning_agent_records_profile_split(sandbox):
    rec = record_with_impact({"ev": STALE})
    analysis = resilience_agent.analyze_experiment(rec)
    out = agents.LearningAgent().run({"record": rec, "history": [], "outputs": {"insights": analysis},
                                      "dep_map": DEP_MAP})
    assert out["profiles"]["product_specific"] == {"ev": ["freshness_loss", "staleness"]}
    assert out["profiles"]["system_wide_symptoms"] == []


# ---------------------------------------------------------------- agents
def test_risk_agent_reports_fleet_exposure_by_documented_data_path():
    ctx = {"dep_map": DEP_MAP, "history": [learning.to_case(record_with_impact({"ev": STALE}))],
           "fleet": {"profiles": {"ev": {}, "smart_home": {}}, "total_twins": 8, "signature": "s"}}
    out = agents.RiskAgent().run(ctx)
    assert out["targets"]["mongodb"]["fleet_exposure"]["profiles"] == ["ev", "smart_home"]
    assert out["targets"]["connectivity"]["fleet_exposure"]["profiles"] == []
    assert out["targets"]["mongodb"]["fleet_exposure"]["measured_affected_profiles"] == {"ev": 1}
    assert "not measured" in out["targets"]["things"]["fleet_exposure"]["basis"]


def test_recovery_agent_reports_per_profile_recovery():
    rec = record_with_impact({"ev": STALE}, rec_over={"ev": {"fresh_ratio": 0.2}})
    import asyncio
    out = asyncio.run(agents.RecoveryAgent().execute({"record": rec}))
    assert out["profiles_not_recovered"] == ["ev"]
    assert out["profiles"]["smart_home"]["recovered"] is True


def test_remediation_carries_product_context():
    rec = record_with_impact({"industrial_machine": STALE})
    analysis = resilience_agent.analyze_experiment(rec)
    out = agents.RemediationAgent().run({"record": rec, "history": [], "outputs": {"insights": analysis}})
    prod = [r for r in out["recommendations"] if r["product_context"]["scope"] == "product"]
    assert prod and prod[0]["product_context"]["profiles"] == ["industrial_machine"]
    assert prod[0]["title"] == "Protect telemetry freshness for the affected product category"
    assert any("vibration" in m for m in prod[0]["product_context"]["domain_failure_modes"])
    assert "industrial_machine" in prod[0]["verification_steps"][-1]


def test_proposal_observes_every_enabled_profile(monkeypatch):
    monkeypatch.setattr(fleet, "_current", fleet.load(env=MIXED_ENV))
    ranked = learning.rank_candidates(DEP_MAP, [])
    prop = scenario_planner.propose_scenario(ranked, DEP_MAP, None, [], None, None)
    assert prop["scenario"]["observe_profiles"] == ["ev", "industrial_machine", "smart_home"]
    obs = prop["observation"]
    assert obs["profiles"] == ["ev", "industrial_machine", "smart_home"] and "not measured" in obs["exposure_basis"]
    scenario.Scenario(**prop["scenario"])          # still a valid, runnable scenario


# ---------------------------------------------------------------- scenario scope + mixed-fleet runs
def test_observation_scope_is_validated_against_the_fleet(monkeypatch):
    monkeypatch.setattr(fleet, "_current", fleet.load(env=MIXED_ENV))
    ok = scenario.validate(scn(("kill", "things"), observe_profiles=["machine", "home"]), DEP_MAP)
    assert ok["ok"] and ok["observe_profiles"] == ["industrial_machine", "smart_home"]
    assert any(w["code"] == "observation_scope" for w in ok["warnings"])
    bad = scenario.validate(scn(("kill", "things"), observe_profiles=["energy", "toaster"]), DEP_MAP)
    assert {e["code"] for e in bad["errors"]} == {"profile_not_in_fleet", "unknown_profile"}


async def run(s):
    r = scenario.ScenarioRun(s, auth(), dep_map=DEP_MAP)
    r.prepare()
    return await r.run()


async def test_mixed_fleet_experiment_reports_impact_per_profile(sandbox, monkeypatch):
    monkeypatch.setattr(fleet, "_current", fleet.load(env=MIXED_ENV))
    rows = [prow()] * 5 + [prow(industrial_machine=STALE)] * 6
    monkeypatch.setattr(probe, "probe_once", make_probe(rows, default=prow()))
    rec = await run(scn(("delay", "mongodb"), max_duration_s=10, observe_profiles=["ev"]))
    assert rec["state"] == "COMPLETED" and not rec["aborted"]
    assert rec["fleet"]["profiles"]["industrial_machine"]["count"] == 4
    imp = rec["profile_impact"]
    assert imp["affected_profiles"] == ["industrial_machine"]
    assert imp["profiles"]["industrial_machine"]["in_scope"] is False      # impact reported outside the scope
    assert imp["profiles"]["ev"]["in_scope"] is True and imp["profiles"]["ev"]["status"] == "no_measured_impact"
    assert rec["observe_profiles"] == ["ev"]


async def test_product_data_quality_alone_never_trips_the_kill_switch(sandbox, monkeypatch):
    """Profile validity is a data-quality signal, not a safety limit: with healthy system
    metrics the run continues to its planned end."""
    monkeypatch.setattr(fleet, "_current", fleet.load(env=MIXED_ENV))
    bad_quality = prow(**{k: {"valid": 0, "invalid": 4, "valid_ratio": 0.0} for k in ("ev", "smart_home")})
    monkeypatch.setattr(probe, "probe_once", make_probe([prow()] * 5, default=bad_quality))
    rec = await run(scn(("delay", "mongodb"), max_duration_s=10))
    assert not rec["aborted"] and rec["state"] == "COMPLETED"
    assert set(rec["profile_impact"]["affected_profiles"]) == {"ev", "smart_home"}


async def test_system_breach_still_stops_a_mixed_fleet_run(sandbox, monkeypatch):
    monkeypatch.setattr(fleet, "_current", fleet.load(env=MIXED_ENV))
    bad = {**prow(), "direct_ok": False}
    monkeypatch.setattr(probe, "probe_once", make_probe([prow()] * 5 + [bad] * 3, default=prow()))
    rec = await run(scn(("delay", "mongodb"), max_duration_s=30))
    assert rec["aborted"] and "direct-read endpoint failed" in rec["abort_reason"]
    assert rec["rollback_verified"] and all(c["netem"] is None for c in sandbox.containers.values())


# ---------------------------------------------------------------- API
@pytest.fixture
def client(sandbox, monkeypatch):
    monkeypatch.setattr(mon, "collect_services", lambda: {"available": False, "error": "stub", "services": None})
    fl = fleet.load(env=MIXED_ENV)
    monkeypatch.setattr(fleet, "_current", fl)
    monkeypatch.setattr(config, "SENT_FILE", os.path.join(config.METRICS_DIR, "..", "sent.json"))
    os.makedirs(config.METRICS_DIR, exist_ok=True)
    row = {**prow(), "ts": round(time.time(), 3), "fleet": {"signature": fl.signature, "twins": 12, "observed": 12}}
    row["profiles"]["ev"]["violations"] = {"ev.odometer_monotonic": 1}
    with open(os.path.join(config.METRICS_DIR, "probe-20990101-000000.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    status = {t.id: {"profile": t.profile, "kind": t.kind, "present": True, "fresh": True, "valid": True,
                     "seq": 3, "sent_at": time.time(), "age_s": 1.0, "violations": []} for t in fl.twins}
    status[fl.twins[0].id] = {"profile": "ev", "kind": "passenger_ev", "present": True, "fresh": False,
                              "valid": True, "age_s": 40.0}
    status[fl.twins[5].id] = {"profile": "industrial_machine", "kind": "motor_pump", "present": False,
                              "fresh": False}
    probe.write_twin_status(status, time.time())
    with TestClient(api.app) as c:
        c.fleet = fl
        yield c


def test_fleet_endpoint_describes_profiles_and_latest_quality(client):
    body = client.get("/api/fleet").json()
    assert body["enabled"] == ["ev", "industrial_machine", "smart_home"] and body["total_twins"] == 12
    assert body["registry"]["industrial_machine"]["signals"]["battery_pct"] == {"applicable": False}
    assert body["latest"]["fresh"] is True
    assert body["latest"]["profiles"]["ev"]["violations"] == {"ev.odometer_monotonic": 1}   # full, not slimmed
    assert body["synthetic"] is True and "Synthetic" in body["synthetic_notice"]


def test_history_rows_are_slimmed_but_keep_profile_metrics(client):
    rows = client.get("/api/metrics/history").json()["rows"]
    ev = rows[-1]["profiles"]["ev"]
    assert "violations" not in ev and ev["violation_count"] == 1 and ev["fresh_ratio"] == 1.0


def test_health_reports_the_fleet(client):
    h = client.get("/api/health").json()
    assert h["num_twins"] == 12 and h["num_cars"] == 4
    assert h["fleet"]["profiles"] == {"ev": 4, "industrial_machine": 4, "smart_home": 4}


def test_twin_listing_filters(client):
    all_ = client.get("/api/fleet/twins").json()
    assert all_["total"] == 12 and all_["counts"]["stale"] == 1 and all_["counts"]["missing"] == 1
    m = client.get("/api/fleet/twins", params={"profile": "machine"}).json()
    assert {t["profile"] for t in m["items"]} == {"industrial_machine"} and m["total"] == 4
    assert client.get("/api/fleet/twins", params={"status": "missing"}).json()["items"][0]["profile"] == \
        "industrial_machine"
    lights = client.get("/api/fleet/twins", params={"kind": "smart_light"}).json()["items"]
    assert lights and all(t["model"] == "SH-Lumen A19" for t in lights)
    assert client.get("/api/fleet/twins", params={"q": "car-002"}).json()["total"] == 1
    assert client.get("/api/fleet/twins", params={"profile": "toaster"}).status_code == 422
    assert client.get("/api/fleet/twins", params={"status": "weird"}).status_code == 422


def test_twin_detail_reads_ditto_and_validates(client, monkeypatch):
    fl = client.fleet
    spec = next(t for t in fl.twins if t.profile == "smart_home")
    import simulator
    thing = {"thingId": spec.id, **fleet.thing_payload(fl, spec, simulator.Twin(spec, fl).next_features())}
    monkeypatch.setattr(api.httpx, "get", lambda url, **kw: httpx.Response(200, json=thing,
                                                                             request=httpx.Request("GET", url)))
    body = client.get(f"/api/fleet/twins/{spec.id}").json()
    assert body["twin"]["kind"] == spec.kind and body["validation"]["violations"] == []
    assert body["signals"]["humidity_pct"]["applicable"] is True
    assert client.get("/api/fleet/twins/org.other:not-planned").status_code == 404


def test_experiment_listing_filters_by_profile_service_and_twin(client):
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    new = record_with_impact({"ev": STALE}, id="exp-new")
    legacy = {"id": "exp-old", "fault": "kill", "target": "things", "injected": True, "state": "COMPLETED",
              "started_at_utc": "2026-01-01T00:00:00+00:00"}
    for r in (new, legacy):
        with open(os.path.join(config.EXPERIMENTS_DIR, f"{r['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(r, f)
    ids = lambda **p: sorted(e["id"] for e in client.get("/api/experiments", params=p).json())  # noqa: E731
    assert ids(profile="ev") == ["exp-new", "exp-old"]                  # legacy car-only counts as EV
    assert ids(profile="home") == ["exp-new"]
    assert ids(service="things") == ["exp-old"]
    assert ids(twin=f"{config.NAMESPACE}:machine-001") == ["exp-new"]
    row = next(e for e in client.get("/api/experiments").json() if e["id"] == "exp-new")
    assert row["affected_profiles"] == ["ev"]
    assert client.get("/api/experiments", params={"twin": "nope:x"}).status_code == 404


def test_validate_endpoint_accepts_an_observation_scope(client):
    r = client.post("/api/scenarios/validate", json={"actions": [{"fault": "kill", "target": "things"}],
                                                     "observe_profiles": ["ev"]}).json()
    assert r["ok"] and r["observe_profiles"] == ["ev"]


# ---------------------------------------------------------------- recovery check is fleet-aware
def test_recovery_staleness_allowance_follows_the_slowest_profile(monkeypatch):
    """Found live: a fleet with 5 s energy/environment twins idles at ~5-6 s staleness p95,
    which the car-derived allowance (3 x SEND_INTERVAL_S = 6 s) kept declaring unrecovered."""
    import guard
    th = guard.Thresholds.from_env()
    base = {"direct_ms": {"p95": 100}}
    row = {**OK_ROW, "direct_ms": 50, "stale_p95_s": 9.0}
    monkeypatch.setattr(fleet, "_current", fleet.load(env={"SIM_FLEET": "ev:5"}))
    assert guard.slowest_update_interval_s() == config.SEND_INTERVAL_S
    assert not guard.is_healthy(row, base, th)                 # car-only: unchanged behaviour
    monkeypatch.setattr(fleet, "_current", fleet.load(env={"SIM_FLEET": "ev:5,energy:2"}))
    assert guard.slowest_update_interval_s() == 5.0
    assert guard.is_healthy(row, base, th)
    assert not guard.is_healthy({**row, "stale_p95_s": 16.0}, base, th)
