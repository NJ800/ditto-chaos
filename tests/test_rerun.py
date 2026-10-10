"""Feature A (ranked candidate planner) and Feature B (re-run after a fix, with a
before/after comparison). Offline: Docker is FakeDocker, time is virtual, AI is off."""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import api
import config
import faults
import guard
import learning
import monitor as mon
import orchestrator as orch
import planner_agent
import probe
import remediation_planner
import scenario
import store
from conftest import DEP_MAP, OK_ROW, auth, scn
from controller import controller

FINDING = "Search availability degraded"


def write_record(rec):
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    with open(os.path.join(config.EXPERIMENTS_DIR, f"{rec['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)


def v1(exp_id, fault="kill", target="things-search", **over):
    base = {"direct_ms": {"p95": 20}, "search_ms": {"p95": 20}, "direct_error_rate": 0.0,
            "search_error_rate": 0.0, "stale_p95_s": 1.0, "lost_acked_max": 0, "search_drift_max": 0}
    return {"schema_version": "1.0", "id": exp_id, "fault": fault, "target": target, "params": {},
            "state": "COMPLETED", "dry_run": False, "injected": True, "aborted": False, "abort_reason": None,
            "baseline": base, "during": dict(base), "reverted": True, "rollback_verified": True,
            "rollback_error": None, "recovery_s": 4.0, "recovery_outcome": "recovered",
            "started_at_utc": "2026-10-01T10:00:00+00:00", "ended_at_utc": "2026-10-01T10:01:00+00:00",
            "timeline": [], **over}


# ================================================================ Feature A: ranked candidates
def test_ranked_candidates_carry_every_required_field_and_are_valid(sandbox):
    view = orch.rank_candidates_view()
    cands = view["candidates"]
    assert [c["rank"] for c in cands] == list(range(1, len(cands) + 1)) and len(cands) >= 5
    for c in cands:
        for key in ("rank", "signature", "targets", "faults", "risk", "hypothesis", "reason", "expected_impact",
                    "evidence", "score", "components", "selectable", "scenario"):
            assert key in c, key
        assert c["risk"] in ("low", "medium", "high", "unknown")
        assert set(c["targets"]) <= guard.ALLOWLIST and set(c["faults"]) <= set(faults.REGISTRY)
        assert c["hypothesis"] and c["reason"] and c["expected_impact"]
        assert c["evidence"]["basis"] == "none" and c["evidence"]["experiments"] == []      # empty history
        assert scenario.validate(scenario.Scenario(**c["scenario"]), DEP_MAP)["ok"] is c["selectable"]
    assert view["weights"] == learning.WEIGHTS and view["evidence_runs"] == 0


def test_ranking_is_read_only_and_authorizes_nothing(sandbox):
    orch.rank_candidates_view()
    assert sandbox.calls == [] and sandbox.previewed == []
    assert orch.get_status()["status"] == orch.STATUS_IDLE and orch.get_scenario_proposal() is None
    assert not os.path.exists(guard.LOCKS_DIR) or not os.listdir(guard.LOCKS_DIR)


def test_history_changes_rank_and_supplies_evidence(sandbox):
    before = {c["signature"]: c for c in orch.rank_candidates_view()["candidates"]}
    degraded = {"direct_ms": {"p95": 20}, "search_ms": {"p95": 20}, "search_error_rate": 0.6,
                "direct_error_rate": 0.0, "stale_p95_s": 1.0, "lost_acked_max": 0, "search_drift_max": 0}
    write_record(v1("exp-a", "kill", "mongodb", during=degraded))
    write_record(v1("exp-b", "kill", "mongodb", during=degraded, started_at_utc="2026-10-02T10:00:00+00:00"))
    view = orch.rank_candidates_view()
    after = {c["signature"]: c for c in view["candidates"]}
    c = after.get("kill@mongodb") or next(x for x in orch._state.candidates_ctx["ranked"]
                                           if x["signature"] == "kill@mongodb")
    assert view["evidence_runs"] == 2
    assert before["kill@mongodb"]["rank"] < c["rank"]                    # tested twice: no longer near the top
    import agents
    risk = agents.RiskAgent().run({"dep_map": DEP_MAP, "history": store.history()})
    full = planner_agent.rank_experiments(DEP_MAP, store.history(), risk, limit=200)
    km = next(x for x in full if x["signature"] == "kill@mongodb")
    assert km["evidence"] == {"basis": "direct", "basis_runs": 2, "agreement": "2 of 2", "confidence": "medium",
                              "experiments": ["exp-b", "exp-a"]}
    assert "2 of 2 prior run(s)" in km["hypothesis"] and km["risk"] == "high"
    assert "can reach 3 dependent service(s)" in km["expected_impact"]


def test_risk_is_unknown_not_guessed_without_a_dependency_map():
    cands = planner_agent.rank_experiments(None, [], limit=5)
    assert cands and all(c["risk"] == "unknown" for c in cands)


def test_policy_narrows_the_candidate_list(sandbox):
    pol = {"allowed_faults": ["delay"], "allowed_targets": ["things"], "max_actions": 1, "max_duration_s": 30}
    cands = orch.rank_candidates_view(pol)["candidates"]
    assert [c["signature"] for c in cands] == ["delay@things"]
    assert cands[0]["scenario"]["max_duration_s"] == 30


def test_candidate_with_an_unverified_rollback_is_listed_but_not_selectable(sandbox):
    write_record(v1("exp-bad", "kill", "gateway", state="FAILED", rollback_verified=False,
                    rollback_error="container did not start"))
    orch.rank_candidates_view()
    full = planner_agent.rank_experiments(DEP_MAP, store.history(), limit=200)
    blocked = next(c for c in full if c["signature"] == "kill@gateway")
    assert blocked["blocked"] and blocked["selectable"] is False
    assert "unverified rollback" in blocked["not_selectable_reason"]
    with pytest.raises(orch.OrchestratorError, match="blocked"):
        orch.select_candidate("kill@gateway")


def test_selecting_a_candidate_creates_a_proposal_but_runs_nothing(sandbox):
    with pytest.raises(orch.OrchestratorConflict, match="rank candidates first"):
        orch.select_candidate("delay@things")
    cands = orch.rank_candidates_view()["candidates"]
    pick = cands[3]
    proposal = orch.select_candidate(pick["signature"], "recommend")
    assert proposal["validation"]["signature"] == pick["signature"] and proposal["method"] == "operator_selected"
    assert proposal["ranking"]["rank"] == pick["rank"] and proposal["stop_conditions"]
    assert orch.get_status()["status"] == orch.STATUS_PLANNED            # planned, not running
    assert sandbox.calls == [] and orch.get_status()["record"] is None
    with pytest.raises(orch.OrchestratorError, match="not in the current ranked candidate list"):
        orch.select_candidate("kill@nginx")
    # still needs a real Authorization to run
    with pytest.raises(scenario.ScenarioRejected) as e:
        orch.start_scenario(scenario.Scenario(**proposal["scenario"]), None, proposal=proposal)
    assert e.value.code == "unauthorized" and sandbox.mutating_calls() == []


# ================================================================ comparison (pure)
def test_compare_runs_reports_improvement_regression_and_unchanged_per_metric():
    worse = {"direct_ms": {"p95": 200}, "search_ms": {"p95": 900}, "direct_error_rate": 0.0,
             "search_error_rate": 0.6, "stale_p95_s": 9.0, "lost_acked_max": 0, "search_drift_max": 4}
    better = {"direct_ms": {"p95": 205}, "search_ms": {"p95": 120}, "direct_error_rate": 0.1,
              "search_error_rate": 0.0, "stale_p95_s": 2.0, "lost_acked_max": 0, "search_drift_max": 0}
    cmp_ = learning.compare_runs(v1("o", during=worse, recovery_s=40.0), v1("f", during=better, recovery_s=5.0))
    by = {m["key"]: m for m in cmp_["metrics"]}
    assert cmp_["comparable"] and len(by) == 8
    assert by["search_latency_p95_ms"]["verdict"] == "improved" and by["search_latency_p95_ms"]["change"] == -780
    assert by["search_latency_p95_ms"]["change_pct"] == -86.7
    assert by["search_error_rate"]["verdict"] == "improved"
    assert by["direct_latency_p95_ms"]["verdict"] == "unchanged"         # +5 ms is inside the tolerance
    assert by["direct_error_rate"]["verdict"] == "regressed"
    assert by["stale_p95_s"]["verdict"] == "improved" and by["search_drift_max"]["verdict"] == "improved"
    assert by["lost_acked_max"]["verdict"] == "unchanged" and by["recovery_s"]["verdict"] == "improved"
    assert cmp_["summary"] == {"improved": 5, "regressed": 1, "unchanged": 2, "not_comparable": 0}
    assert "not proof of causation" in cmp_["caveat"]


def test_compare_runs_handles_missing_and_incomparable_measurements_explicitly():
    gaps = {"direct_ms": {"p95": 50}, "search_ms": {}, "direct_error_rate": 0.0, "search_error_rate": 0.0,
            "stale_p95_s": None, "lost_acked_max": None, "search_drift_max": 0}
    full = {"direct_ms": {"p95": 50}, "search_ms": {"p95": 60}, "direct_error_rate": 0.0,
            "search_error_rate": 0.0, "stale_p95_s": 2.0, "lost_acked_max": 0, "search_drift_max": 0}
    cmp_ = learning.compare_runs(v1("o", during=gaps, recovery_s=None, recovery_outcome="timeout"),
                                 v1("f", during=full))
    by = {m["key"]: m for m in cmp_["metrics"]}
    assert by["search_latency_p95_ms"]["verdict"] == "not_comparable"
    assert by["search_latency_p95_ms"]["reason"] == "not measured in the original run"
    assert by["lost_acked_max"]["verdict"] == "not_comparable" and by["lost_acked_max"]["change"] is None
    assert by["stale_p95_s"]["reason"] == "not measured in the original run"
    assert by["recovery_s"]["verdict"] == "improved" and "did not recover" in by["recovery_s"]["reason"]

    diff = learning.compare_runs(v1("o", "kill", "things"), v1("f", "kill", "gateway"))
    assert diff["comparable"] is False and "scenarios differ" in diff["not_comparable_reasons"][0]
    assert all(m["verdict"] == "not_comparable" for m in diff["metrics"])
    dry = learning.compare_runs(v1("o"), v1("f", dry_run=True))
    assert dry["comparable"] is False and "follow-up run was a dry run" in dry["not_comparable_reasons"][0]
    changed = learning.compare_runs(v1("o", "delay", "things", params={"delay_ms": 500}),
                                    v1("f", "delay", "things", params={"delay_ms": 200}))
    assert changed["parameter_differences"] == ["delay@things.delay_ms: 500 -> 200"]


def test_verification_result_never_claims_more_than_the_evidence():
    ok = {"comparable": True, "summary": {"improved": 2, "regressed": 0}, "caveat": learning.CORRELATION_CAVEAT}
    vr = remediation_planner.verification_result
    seen = vr(ok, FINDING, ["Search latency degradation"], "conclusive", "contained outage")
    assert seen["demonstrated"] == "observed" and seen["finding_recurred"] is False
    assert seen["evidence_strength"] == "observed correlation from one follow-up run" and "not proof" in seen["caveat"]
    assert vr(ok, FINDING, [FINDING], "conclusive")["demonstrated"] == "not_observed"
    assert vr({**ok, "summary": {"improved": 0, "regressed": 2}}, FINDING, [], "conclusive")["demonstrated"] == "not_observed"
    assert vr(ok, FINDING, [], "inconclusive")["demonstrated"] == "inconclusive"
    assert vr(ok, FINDING, None, "conclusive")["demonstrated"] == "inconclusive"
    assert vr({"comparable": False, "not_comparable_reasons": ["the scenarios differ"]}, FINDING, [],
              "conclusive")["demonstrated"] == "inconclusive"


# ================================================================ Feature B: re-run workflow
async def original_run(sandbox, monkeypatch, fixed):
    """An original run in which search fails while things-search is down, unless `fixed`."""
    async def probe_fn(client, ids):
        up = sandbox.containers["things-search"]["running"] or fixed["on"]
        return {**OK_ROW, "search_ok": up}
    monkeypatch.setattr(probe, "probe_once", probe_fn)
    started = orch.start_scenario(scn(("kill", "things-search"), max_duration_s=20), auth())
    result = await started["task"]
    exp = result["record"]["id"]
    rec = next(r for r in store.recommendations() if r["experiment_id"] == exp and r["item"]["finding_title"] == FINDING)
    orch.reset()
    return exp, rec


async def test_rerun_plan_reuses_the_original_scenario_and_recaptures_a_baseline(sandbox, monkeypatch):
    exp, rec = await original_run(sandbox, monkeypatch, {"on": False})
    n = len(sandbox.calls)
    plan = orch.plan_rerun(exp, rec["index"])
    assert plan["ok"] and plan["requires_revised_plan"] is False and plan["reasons"] == []
    assert plan["scenario"]["actions"] == [{"fault": "kill", "target": "things-search", "params": {},
                                            "start_offset_s": 0.0, "duration_s": None}]
    assert plan["scenario"]["max_duration_s"] == 20 and plan["scenario"]["pre_s"] == orch.RERUN_PRE_S
    assert "measures a fresh baseline" in plan["baseline_note"] and plan["original"]["id"] == exp
    assert len(sandbox.calls) == n                                     # planning touched nothing


async def test_rerun_requires_confirmation_that_the_change_was_applied(sandbox, monkeypatch):
    exp, rec = await original_run(sandbox, monkeypatch, {"on": False})
    store.set_recommendation_status(exp, rec["index"], "accepted")      # accepted is not applied
    n = len(sandbox.mutating_calls())
    with pytest.raises(orch.OrchestratorError, match="confirm that the change was actually applied"):
        orch.start_rerun(exp, rec["index"], auth(), confirmed_applied=False, actor="op")
    assert len(sandbox.mutating_calls()) == n and store.followups(exp) == []
    assert store.get_recommendation(exp, rec["index"])["status"] == "accepted"   # nothing was assumed
    with pytest.raises(scenario.ScenarioRejected) as e:                 # and it still needs an authorization
        orch.start_rerun(exp, rec["index"], None, confirmed_applied=True, actor="op")
    assert e.value.code == "unauthorized" and store.followups(exp) == []


async def test_full_rerun_workflow_links_compares_and_verifies(sandbox, monkeypatch):
    fixed = {"on": False}
    exp, rec = await original_run(sandbox, monkeypatch, fixed)
    fixed["on"] = True                                                 # the operator's fix took effect
    started = orch.start_rerun(exp, rec["index"], auth("alice"), confirmed_applied=True, actor="alice",
                               note="added a search fallback", mode="manual")
    assert store.get_recommendation(exp, rec["index"])["status"] == "implemented"
    result = await started["task"]
    follow = result["record"]

    assert follow["followup_of"]["experiment_id"] == exp and follow["source"] == "rerun"
    assert follow["followup_of"]["confirmed_applied_by"] == "alice"
    assert follow["followup_of"]["recommendation_id"] == rec["item"]["id"]
    assert follow["baseline_source"] == "fresh"                        # re-captured, not read from a file
    assert follow["signature"] == "kill@things-search" and follow["rollback_verified"]
    assert follow["authorization"]["actor"] == "alice"

    cmp_ = follow["followup_comparison"]
    by = {m["key"]: m for m in cmp_["metrics"]}
    assert cmp_["comparable"] and by["search_error_rate"]["verdict"] == "improved"
    assert by["search_error_rate"]["before"] > 0 and by["search_error_rate"]["after"] == 0
    assert cmp_["outcomes"]["original"]["outcome_class"] == "kill_switch"
    assert cmp_["outcomes"]["followup"]["outcome_class"] == "tolerated"
    assert cmp_["verification"]["demonstrated"] == "observed" and cmp_["verification"]["finding_recurred"] is False
    assert "not proof of causation" in cmp_["caveat"]

    saved = json.load(open(os.path.join(config.EXPERIMENTS_DIR, f"{follow['id']}.json")))
    assert saved["followup_comparison"]["verification"]["demonstrated"] == "observed"
    links = store.followups(exp)
    assert len(links) == 1 and links[0]["followup_id"] == follow["id"] and links[0]["status"] == "observed"
    assert links[0]["confirmed_by"] == "alice" and links[0]["confirmation_note"] == "added a search fallback"
    assert store.followups(follow["id"]) == links                     # reachable from either side
    final = store.get_recommendation(exp, rec["index"])
    assert final["status"] == "verified" and final["validated_by"] == follow["id"]
    assert [a["action"] for a in store.audit_log()].count("rerun_started") == 1
    assert "rerun" in [t["type"] for t in store.timeline(follow["id"])]


async def test_rerun_that_still_fails_is_reported_as_not_observed(sandbox, monkeypatch):
    exp, rec = await original_run(sandbox, monkeypatch, {"on": False})   # the "fix" changed nothing
    started = orch.start_rerun(exp, rec["index"], auth(), confirmed_applied=True, actor="op")
    follow = (await started["task"])["record"]
    v = follow["followup_comparison"]["verification"]
    assert v["demonstrated"] == "not_observed" and v["finding_recurred"] is True
    assert store.followups(exp)[0]["status"] == "not_observed"
    assert store.get_recommendation(exp, rec["index"])["status"] == "not_effective"


async def test_rerun_goes_through_the_normal_safety_path(sandbox, monkeypatch):
    exp, rec = await original_run(sandbox, monkeypatch, {"on": False})
    scenario.write_lockout("exp-x", "rollback could not be verified", [])
    with pytest.raises(scenario.ScenarioRejected) as e:
        orch.start_rerun(exp, rec["index"], auth(), confirmed_applied=True, actor="op")
    assert e.value.code == "recovery_required" and store.followups(exp) == []
    scenario.clear_lockout()
    first = orch.start_rerun(exp, rec["index"], auth(), confirmed_applied=True, actor="op")
    with pytest.raises(orch.OrchestratorConflict):                       # one run at a time still applies
        orch.start_rerun(exp, rec["index"], auth(), confirmed_applied=True, actor="op")
    await first["task"]
    assert len(store.followups(exp)) == 1 and sandbox.containers["things-search"]["running"]


async def test_rerun_is_refused_when_ditto_is_unhealthy_during_the_fresh_baseline(sandbox, monkeypatch):
    exp, rec = await original_run(sandbox, monkeypatch, {"on": False})

    async def unhealthy(client, ids):
        return {**OK_ROW, "direct_ok": False}
    monkeypatch.setattr(probe, "probe_once", unhealthy)
    n = len(sandbox.mutating_calls())
    follow = (await orch.start_rerun(exp, rec["index"], auth(), confirmed_applied=True, actor="op")["task"])["record"]
    assert follow["state"] == guard.STATE_ABORTED and follow["injected"] is False
    assert len(sandbox.mutating_calls()) == n
    cmp_ = follow["followup_comparison"]
    assert cmp_["comparable"] is False and cmp_["verification"]["demonstrated"] == "inconclusive"
    assert store.followups(exp)[0]["status"] == "inconclusive"


def test_rerun_plan_explains_why_an_original_cannot_be_repeated(sandbox, monkeypatch):
    item = {"id": "R-1", "title": "T", "finding_title": FINDING, "severity": "high", "category": "C"}
    cases = {
        "exp-dry": (v1("exp-dry", dry_run=True, injected=False), "dry run"),
        "exp-noinj": (v1("exp-noinj", injected=False), "no fault was injected"),
        "exp-nginx": (v1("exp-nginx", "kill", "nginx"), "not in the allowlist"),
        "exp-gone": (v1("exp-gone", "netsplit", "things"), "unknown fault type"),
        "exp-bounds": ({**v1("exp-bounds"), "scenario": {"actions": [{"fault": "delay", "target": "things",
                                                                      "params": {"delay_ms": 20000}}]}},
                       "outside the allowed range"),
        "exp-multi": (v1("exp-multi", "multi", "a, b"), "does not contain a fault scenario"),
    }
    for exp_id, (rec, expected) in cases.items():
        write_record(rec)
        store.upsert_recommendations(exp_id, "sig", [item])
        plan = orch.plan_rerun(exp_id, 0)
        assert plan["ok"] is False and plan["requires_revised_plan"] is True, exp_id
        assert any(expected in r for r in plan["reasons"]), (exp_id, plan["reasons"])
        with pytest.raises(scenario.ScenarioRejected) as e:
            orch.start_rerun(exp_id, 0, auth(), confirmed_applied=True, actor="op")
        assert e.value.errors[0]["code"] == "rerun_not_possible"
    assert sandbox.mutating_calls() == [] and store.followups() == []
    with pytest.raises(orch.OrchestratorError, match="no recommendation"):
        orch.plan_rerun("exp-dry", 7)
    with pytest.raises(orch.OrchestratorError, match="no saved experiment"):
        orch.plan_rerun("exp-missing", 0)


def test_rerun_of_a_legacy_v1_record_maps_its_parameters(sandbox):
    write_record(v1("exp-old", "delay", "mongodb", params={"delay_ms": 800, "ttl_s": 60, "iface": "eth0"}))
    store.upsert_recommendations("exp-old", "delay@mongodb", [{"id": "R", "title": "T", "finding_title": "F",
                                                              "severity": "medium", "category": "C"}])
    plan = orch.plan_rerun("exp-old", 0)
    assert plan["ok"], plan["reasons"]
    assert plan["scenario"]["actions"][0]["params"] == {"delay_ms": 800, "ttl_s": 60}    # unknown key dropped


def test_a_rejected_recommendation_cannot_be_rerun(sandbox):
    write_record(v1("exp-r"))
    store.upsert_recommendations("exp-r", "kill@things-search", [{"id": "R", "title": "T", "finding_title": "F",
                                                                 "severity": "medium", "category": "C"}])
    store.set_recommendation_status("exp-r", 0, "rejected")
    assert "rejected" in orch.plan_rerun("exp-r", 0)["reasons"][0]


# ================================================================ HTTP
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


def test_candidates_and_selection_over_http(client):
    view = client.get("/api/plan/candidates").json()
    assert view["candidates"][0]["rank"] == 1 and client.docker.calls == []
    assert client.post("/api/plan/select", json={"signature": "kill@nginx"}).status_code == 400
    pick = next(c for c in view["candidates"] if c["selectable"])
    proposal = client.post("/api/plan/select", json={"signature": pick["signature"]}).json()
    assert proposal["proposal_id"] and client.get("/api/plan").json()["proposal_id"] == proposal["proposal_id"]
    assert client.get("/api/orchestrator/status").json()["status"] == "planned"
    assert client.docker.calls == []                                    # selecting ran nothing
    # running the selected candidate still needs the authorization flag
    assert client.post("/api/scenarios/run", json={"proposal_id": proposal["proposal_id"]}).status_code == 400
    controller.mode = "recommend"                                       # a selected candidate counts as proposed
    r = client.post("/api/scenarios/run", json={"authorize": True, "proposal_id": proposal["proposal_id"]})
    assert r.status_code == 200
    assert wait_done(client)["record"]["source"] == "proposal"


def test_rerun_over_http_end_to_end(client, monkeypatch):
    fixed = {"on": False}

    async def probe_fn(c, ids):
        return {**OK_ROW, "search_ok": client.docker.containers["things-search"]["running"] or fixed["on"]}
    monkeypatch.setattr(probe, "probe_once", probe_fn)
    body = {"authorize": True, "scenario": {"actions": [{"fault": "kill", "target": "things-search"}],
                                            "max_duration_s": 20, "pre_s": 2, "recovery_timeout_s": 30}}
    exp = client.post("/api/scenarios/run", json=body).json()["experiment_id"]
    wait_done(client)
    client.post("/api/orchestrator/reset")
    recs = client.get(f"/api/experiments/{exp}").json()["recommendations"]
    idx = next(r["index"] for r in recs if r["item"]["finding_title"] == FINDING)

    plan = client.get(f"/api/experiments/{exp}/rerun-plan?recommendation_index={idx}").json()
    assert plan["ok"] and plan["scenario"]["actions"][0]["fault"] == "kill"
    url = f"/api/experiments/{exp}/rerun"
    assert client.post(url, json={"recommendation_index": idx, "confirm_applied": True}).status_code == 400
    r = client.post(url, json={"recommendation_index": idx, "authorize": True})
    assert r.status_code == 400 and "confirm_applied" in r.json()["detail"]
    assert client.get(f"/api/followups?experiment_id={exp}").json() == []

    fixed["on"] = True
    r = client.post(url, json={"recommendation_index": idx, "authorize": True, "confirm_applied": True,
                               "note": "raised the search timeout"}, headers={"X-Operator": "alice"})
    assert r.status_code == 200 and r.json()["original_id"] == exp
    follow_id = r.json()["experiment_id"]
    wait_done(client)

    detail = client.get(f"/api/experiments/{follow_id}").json()
    cmp_ = detail["record"]["followup_comparison"]
    assert cmp_["verification"]["demonstrated"] == "observed" and cmp_["original_id"] == exp
    assert detail["followups"][0]["original_id"] == exp and detail["followups"][0]["status"] == "observed"
    original = client.get(f"/api/experiments/{exp}").json()
    assert original["followups"][0]["followup_id"] == follow_id
    assert original["recommendations"][idx if False else 0]["experiment_id"] == exp
    assert next(x for x in original["recommendations"] if x["index"] == idx)["status"] == "verified"
    assert client.get("/api/followups").json()[0]["confirmed_by"] == "alice"


def test_rerun_http_refusals(client):
    write_record(v1("exp-dry", dry_run=True, injected=False))
    store.upsert_recommendations("exp-dry", "kill@things-search", [{"id": "R", "title": "T", "finding_title": "F",
                                                                   "severity": "medium", "category": "C"}])
    body = {"recommendation_index": 0, "authorize": True, "confirm_applied": True}
    r = client.post("/api/experiments/exp-dry/rerun", json=body)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "rerun_not_possible"
    assert "dry run" in r.json()["errors"][0]["message"]
    assert client.post("/api/experiments/nope/rerun", json=body).status_code == 404
    assert client.get("/api/experiments/nope/rerun-plan?recommendation_index=0").status_code == 404
    assert client.get("/api/experiments/exp-dry/rerun-plan?recommendation_index=-1").status_code == 422
    assert client.get("/api/experiments/exp-dry/rerun-plan?recommendation_index=9").status_code == 400
    client.post("/api/controller/mode", json={"mode": "observe"})
    assert client.post("/api/experiments/exp-dry/rerun", json=body).status_code == 403
    assert client.docker.mutating_calls() == []
