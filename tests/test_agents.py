"""Agents, coordinator, learning and knowledge store -- unit tests per agent, then the
full lifecycle through orchestrator.start_scenario(). Docker is FakeDocker, time is
virtual, the AI provider is either disabled or replaced by a stub."""
import asyncio
import json
import os

import pytest

import agents
import ai_client
import config
import guard
import learning
import orchestrator as orch
import probe
import resilience_agent
import scenario
import scenario_planner
import store
from conftest import DEP_MAP, OK_ROW, auth, scn

POLICY = {"allowed_faults": ["kill", "delay"], "allowed_targets": ["things", "things-search", "gateway"],
          "max_actions": 2, "max_duration_s": 60}


def record(exp_id, fault="kill", target="things-search", *, injected=True, dry_run=False, aborted=False,
           abort_reason=None, during=None, state="COMPLETED", recovery_s=4.0, started="2026-10-01T10:00:00+00:00",
           rollback_error=None, **extra):
    base = {"direct_ms": {"p95": 20}, "search_ms": {"p95": 20}, "direct_error_rate": 0.0,
            "search_error_rate": 0.0, "stale_p95_s": 1.0, "lost_acked_max": 0, "search_drift_max": 0}
    return {"schema_version": "1.0", "id": exp_id, "fault": fault, "target": target, "params": {},
            "state": state, "dry_run": dry_run, "injected": injected, "aborted": aborted,
            "abort_reason": abort_reason, "baseline": base, "during": during or dict(base),
            "reverted": injected, "rollback_verified": injected and not rollback_error,
            "rollback_error": rollback_error, "recovery_s": recovery_s,
            "recovery_outcome": "recovered" if injected else None, "started_at_utc": started,
            "ended_at_utc": started, "timeline": [], **extra}


DEGRADED = {"direct_ms": {"p95": 90}, "search_ms": {"p95": 20}, "direct_error_rate": 0.0,
            "search_error_rate": 0.6, "stale_p95_s": 1.0, "lost_acked_max": 0, "search_drift_max": 0}


def cases(*records):
    return [learning.to_case(r) for r in records]


def write_experiments(*records):
    os.makedirs(config.EXPERIMENTS_DIR, exist_ok=True)
    for r in records:
        with open(os.path.join(config.EXPERIMENTS_DIR, f"{r['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(r, f)


def stub_ai(monkeypatch, response):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")

    def fake(prompt, timeout=None):
        if isinstance(response, Exception):
            raise response
        return response(prompt) if callable(response) else response
    monkeypatch.setattr(ai_client, "generate_json", fake)


async def run_scenario(s, **kw):
    started = orch.start_scenario(s, kw.pop("authorization", auth()), **kw)
    return started, await started["task"]


# ================================================================ learning (pure)
def test_observe_reads_measured_effects_only():
    eff = learning.observe(record("e1", during=DEGRADED))
    assert eff["direct_latency_ratio"] == 4.5 and eff["search_error_delta"] == 0.6
    assert eff["symptoms"] == ["direct_latency", "search_errors"] and eff["outcome_class"] == "degraded"
    assert learning.observe(record("e2"))["outcome_class"] == "tolerated"
    missing = learning.observe(record("e3", during={"direct_ms": {}, "search_ms": {}}))
    assert missing["direct_latency_ratio"] is None and missing["symptoms"] == []   # unknown is not a symptom


def test_dry_runs_and_uninjected_runs_are_never_evidence():
    assert learning.observe(record("d", dry_run=True, during=DEGRADED))["outcome_class"] == "no_evidence"
    assert learning.observe(record("n", injected=False))["outcome_class"] == "no_evidence"
    # the exact shape the old dry-run bug wrote: dry_run with injected=True
    assert learning.is_evidence({"injected": True, "dry_run": True}) is False


def test_inconclusive_validation_verdict_removes_a_run_from_evidence():
    r = record("e1", during=DEGRADED)
    assert learning.is_evidence(r)
    assert not learning.is_evidence({**r, "validation": {"verdict": "inconclusive"}})
    assert learning.is_evidence({**r, "validation": {"verdict": "conclusive"}})


def test_predict_states_its_basis_and_sample_size():
    act = [{"fault": "kill", "target": "things-search", "params": {}}]
    none = learning.predict(act, [])
    assert none["basis"] == "none" and none["confidence"] == "none" and none["outcome_class"] is None
    hist = cases(record("a", during=DEGRADED), record("b", during=DEGRADED), record("c", during=DEGRADED))
    direct = learning.predict(act, hist)
    assert (direct["basis"], direct["basis_runs"], direct["agreement"]) == ("direct", 3, "3 of 3")
    assert direct["outcome_class"] == "degraded" and direct["confidence"] == "high"
    analog = learning.predict([{"fault": "kill", "target": "gateway", "params": {}}], hist)
    assert analog["basis"] == "analogy" and analog["confidence"] == "low"


def test_compare_reports_match_and_surprises():
    pred = learning.predict([{"fault": "kill", "target": "things-search", "params": {}}],
                            cases(record("a", during=DEGRADED)))
    same = learning.compare(pred, learning.observe(record("b", during=DEGRADED)))
    assert same["evaluated"] and same["outcome_match"] and same["unexpected_symptoms"] == []
    diff = learning.compare(pred, learning.observe(record("c")))
    assert diff["outcome_match"] is False and diff["predicted_but_absent"] == ["direct_latency", "search_errors"]
    assert learning.compare(None, learning.observe(record("d")))["evaluated"] is False


def test_ranking_prefers_untested_and_penalises_uninformative_repeats():
    now = 1791000000.0
    recent = "2026-10-03T10:00:00+00:00"
    hist = cases(record("a", started=recent), record("b", started=recent))     # kill@things-search twice, tolerated
    ranked = learning.rank_candidates(DEP_MAP, hist, ["kill", "delay"], ["things-search", "gateway"], now=now)
    by = {c["signature"]: c for c in ranked}
    tried, untried = by["kill@things-search"], by["delay@gateway"]
    assert tried["runs"] == 2 and tried["components"]["repetition_penalty"] == 1.0
    assert untried["components"]["novelty"] == 1.0 and untried["score"] > tried["score"]
    assert ranked[-1]["signature"] == "kill@things-search"
    assert all(set(c["components"]) == set(learning.WEIGHTS) for c in ranked)


def test_ranking_changes_when_history_changes():
    targets, fl = ["things-search", "gateway"], ["kill"]
    before = learning.rank_candidates(DEP_MAP, [], fl, targets)
    assert before[0]["signature"] == "kill@things-search"          # higher fan-in wins a tie on novelty
    after = learning.rank_candidates(DEP_MAP, cases(record("a")), fl, targets)
    assert after[0]["signature"] == "kill@gateway"                  # the stored run moved the other one up
    inf = learning.influence("kill@things-search", before, after)
    assert inf["score_after"] < inf["score_before"] and inf["top_candidate_changed"]


def test_scenario_with_an_unverified_rollback_is_blocked_from_selection():
    hist = cases(record("a", rollback_error="netem still present", state="FAILED"))
    ranked = learning.rank_candidates(DEP_MAP, hist, ["kill"], ["things-search", "gateway"])
    blocked = next(c for c in ranked if c["signature"] == "kill@things-search")
    assert blocked["blocked"] and blocked["score"] is None and ranked[-1] is blocked


def test_pending_recommendation_raises_priority_of_a_retest():
    hist = cases(record("a", during=DEGRADED, started="2026-10-03T10:00:00+00:00"))
    plain = learning.rank_candidates(DEP_MAP, hist, ["kill"], ["things-search"])[0]
    follow = learning.rank_candidates(DEP_MAP, hist, ["kill"], ["things-search"],
                                      pending_validation={"kill@things-search"})[0]
    assert follow["score"] == pytest.approx(plain["score"] + learning.WEIGHTS["follow_up"])


def test_candidate_space_respects_policy_and_contains_valid_pairs():
    ranked = learning.rank_candidates(DEP_MAP, [], POLICY["allowed_faults"], POLICY["allowed_targets"], POLICY)
    sigs = {c["signature"] for c in ranked}
    assert "delay@things+kill@gateway" in sigs                     # dependency slow while dependant is down
    assert not any("mongodb" in s or "pause" in s for s in sigs)
    for c in ranked:
        s = scenario.Scenario(actions=c["actions"])
        assert scenario.validate(s, DEP_MAP, POLICY)["ok"]


def test_no_model_is_claimed():
    assert learning.MODEL_STATUS["trained_model"] is False
    assert learning.summarize(cases(record("a"), record("b", dry_run=True)))["evidence_runs"] == 1


# ================================================================ store
def test_sync_imports_legacy_files_and_survives_malformed_ones(sandbox):
    write_experiments(record("exp-1", during=DEGRADED), record("exp-2", dry_run=True))
    for name, body in (("broken.json", "{nope"), ("list.json", "[1,2]"), ("noid.json", "{}")):
        with open(os.path.join(config.EXPERIMENTS_DIR, name), "w") as f:
            f.write(body)
    result = store.sync_from_files()
    assert result["imported"] == 2 and len(result["errors"]) == 3
    assert store.sync_from_files()["imported"] == 0                 # idempotent
    hist = {c["id"]: c for c in store.history()}
    assert hist["exp-1"]["outcome_class"] == "degraded" and hist["exp-2"]["evidence"] is False


def test_store_raises_store_error_when_database_is_unusable(sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "KNOWLEDGE_DB", str(tmp_path))      # a directory, not a file
    with pytest.raises(store.StoreError):
        store.history()
    assert store.ping()["available"] is False


def test_recommendation_status_is_never_overwritten_by_a_reupsert(sandbox):
    item = {"title": "T", "finding_title": "F", "severity": "high", "category": "C"}
    store.upsert_recommendations("exp-1", "kill@things", [item])
    store.set_recommendation_status("exp-1", 0, "implemented")
    store.upsert_recommendations("exp-1", "kill@things", [item])
    assert store.get_recommendation("exp-1", 0)["status"] == "implemented"
    with pytest.raises(store.StoreError):
        store.set_recommendation_status("exp-1", 0, "auto_applied")


# ================================================================ individual agents
def test_risk_agent_computes_transitive_blast_radius():
    out = agents.RiskAgent().run({"dep_map": DEP_MAP, "history": cases(record("a"))})
    assert out["targets"]["mongodb"]["blast_radius"] == ["gateway", "things", "things-search"]
    assert out["targets"]["gateway"]["blast_radius"] == []
    assert out["targets"]["things-search"]["undertested"] is False and "things" in out["undertested"]
    assert agents.RiskAgent().run({"dep_map": {}, "history": []})["limitations"]


def _propose(history=(), policy=POLICY, risk=None):
    ranked = learning.rank_candidates(DEP_MAP, list(history), policy["allowed_faults"],
                                      policy["allowed_targets"], policy)
    return scenario_planner.propose_scenario(ranked, DEP_MAP, {"status": "healthy"}, list(history), risk, policy)


def test_fault_agent_proposal_is_complete_and_valid_without_ai(sandbox):
    p = _propose()
    assert p["method"] == "deterministic_fallback" and p["validation"]["ok"]
    for key in ("objective", "hypothesis", "scenario", "expected_symptoms", "metrics_to_observe", "safety_risks",
                "stop_conditions", "preflight_requirements", "learning_value", "related_experiments",
                "confidence", "prediction", "ranking", "execution", "alternatives"):
        assert key in p
    assert p["confidence"]["level"] == "none" and "No stored run" in p["hypothesis"]
    assert all(a["target"] in POLICY["allowed_targets"] for a in p["scenario"]["actions"])


def test_fault_agent_uses_a_valid_model_choice(sandbox, monkeypatch):
    stub_ai(monkeypatch, {"candidate_index": 2, "objective": "O", "hypothesis": "H", "sequential": False,
                          "safety_risks": ["model risk"], "params": {}})
    p = _propose()
    assert p["method"] == "gemini" and p["objective"] == "O" and "model risk" in p["safety_risks"]
    assert p["ranking"]["rank"] == 3
    assert p["stop_conditions"] == scenario_planner.stop_conditions(guard.Thresholds.from_env())


@pytest.mark.parametrize("response,expect", [
    ({"candidate_index": 99}, "invalid choice"),
    ({"candidate_index": "0"}, "invalid choice"),
    ({"target": "mongodb", "fault": "kill"}, "invalid choice"),          # tries to invent a scenario
    ("not a dict", "invalid choice"),
    (ai_client.AIUnavailable("HTTP 503"), "unavailable"),
])
def test_fault_agent_falls_back_on_invalid_or_missing_model_output(sandbox, monkeypatch, response, expect):
    stub_ai(monkeypatch, response)
    p = _propose()
    assert p["method"] == "deterministic_fallback" and expect in p["fallback_note"]
    assert p["validation"]["ok"] and p["ranking"]["rank"] == 1


def test_model_cannot_push_parameters_past_the_bounds(sandbox, monkeypatch):
    def respond(prompt):
        return {"candidate_index": 0, "params": {"delay@things": {"delay_ms": 999999},
                                                 "kill@things": {"force": 1}, "delay@gateway": {"delay_ms": 999999},
                                                 "delay@things-search": {"delay_ms": 999999}}}
    stub_ai(monkeypatch, respond)
    p = _propose(policy={**POLICY, "allowed_faults": ["delay"]})
    assert p["method"] == "deterministic_fallback" and "rejected by policy" in p["fallback_note"]
    assert all(a["params"]["delay_ms"] <= 5000 for a in p["validation"]["actions"])


def test_fault_agent_refuses_when_policy_leaves_no_candidate(sandbox):
    from planner_agent import PlannerError
    with pytest.raises(PlannerError, match="no eligible"):
        _propose(policy={**POLICY, "allowed_targets": []})


def test_validation_agent_verdicts():
    v = agents.ValidationAgent()
    rows = [{"phase": "fault"}] * 4 + [{"phase": "recovery"}] * 2
    good = record("a", timeline=rows)
    assert v.run({"record": good})["verdict"] == "conclusive"
    short = record("b", timeline=[{"phase": "fault"}, {"phase": "recovery"}])
    out = v.run({"record": short})
    assert out["verdict"] == "inconclusive" and out["counts_as_evidence"] is False
    assert v.run({"record": record("c", dry_run=True, timeline=rows)})["verdict"] == "invalid"
    assert short["validation"]["verdict"] == "inconclusive"         # written into the record itself


def test_recovery_state_machine():
    rs = agents.recovery_state
    assert rs(None) == "RECOVERY_NOT_REQUIRED"
    assert rs({"state": "BASELINE", "injected": False}) == "RECOVERY_NOT_REQUIRED"
    assert rs({"state": "MONITORING", "injected": True}) == "FAULTS_ACTIVE"
    assert rs({"state": "MONITORING", "injected": True}, stop_requested=True) == "STOP_REQUESTED"
    assert rs({"state": "REVERTING", "injected": True}) == "ROLLBACK_IN_PROGRESS"
    assert rs({"state": "VERIFYING_RECOVERY", "injected": True}) == "VERIFYING_RECOVERY"
    assert rs({"state": "COMPLETED", "injected": True, "rollback_verified": True,
               "recovery_outcome": "recovered"}) == "RECOVERED"
    assert rs({"state": "RECOVERY_TIMEOUT", "injected": True, "rollback_verified": True}) == "HEALTH_NOT_CONFIRMED"
    assert rs({"state": "RECOVERY_REQUIRED", "injected": True}) == "RECOVERY_REQUIRED"
    assert rs({"state": "FAILED", "injected": True, "rollback_error": "x"}) == "RECOVERY_REQUIRED"


def test_remediation_agent_links_every_recommendation_to_a_finding(sandbox):
    rec = record("exp-r", during=DEGRADED)
    analysis = agents.InsightsAgent().run({"record": rec, "dep_map": DEP_MAP, "history": []})
    out = agents.RemediationAgent().run({"record": rec, "history": [], "outputs": {"insights": analysis}})
    ids = {f["id"] for f in analysis["findings"]}
    assert out["recommendations"] and out["none_auto_applied"] is True
    for r in out["recommendations"]:
        assert set(r["supporting_findings"]) <= ids and r["evidence"] and r["verification_steps"]
        assert r["status"] == "proposed" and r["auto_applied"] is False
        for key in ("id", "title", "priority", "severity", "proposed_change", "expected_benefit",
                    "complexity", "risks"):
            assert r[key]
    assert [r["priority"] for r in sorted(out["recommendations"], key=lambda r: r["priority"])] == \
        list(range(1, len(out["recommendations"]) + 1))
    assert len(store.recommendations()) == len(out["recommendations"])


def test_remediation_agent_skips_rather_than_invents_when_there_is_no_analysis(sandbox):
    with pytest.raises(agents.AgentSkipped):
        agents.RemediationAgent().run({"record": record("x"), "outputs": {}})


def test_insights_agent_separates_measured_findings_from_hypotheses(sandbox, monkeypatch):
    stub_ai(monkeypatch, {"summary": "s", "severity": "high", "root_cause_hypotheses": [], "findings": [
        {"title": "Search errors", "severity": "high", "evidence": "search_error_rate 0 -> 0.6", "impact": "i",
         "confidence": "supported_by_measurements", "recommended_fix": "f"},
        {"title": "Pool exhaustion", "severity": "medium", "evidence": "none directly", "impact": "i",
         "confidence": "plausible_hypothesis", "recommended_fix": "f"},
        {"title": "Dropped: bad confidence", "severity": "high", "evidence": "e", "impact": "i",
         "confidence": "certain", "recommended_fix": "f"}]})
    out = agents.InsightsAgent().run({"record": record("exp-i", during=DEGRADED), "dep_map": DEP_MAP, "history": []})
    assert [(f["id"], f["basis"]) for f in out["findings"]] == [("F1", "measured"), ("F2", "hypothesis")]
    assert out["analysis_method"] == "gemini" and out["evidence"]["during_search_error_rate"] == 0.6
    assert out["limitations"] and out["observed"]["symptoms"] == ["direct_latency", "search_errors"]


def test_insights_agent_never_fabricates_numbers_when_measurements_are_missing(sandbox):
    rec = record("exp-m", during=None, injected=False)
    rec["baseline"] = rec["during"] = None
    out = agents.InsightsAgent().run({"record": rec, "dep_map": None, "history": []})
    assert out["evidence"]["baseline_direct_p95_ms"] is None and out["evidence"]["during_direct_p95_ms"] is None
    assert any("no baseline" in lim for lim in out["limitations"])
    assert out["hypothesis_check"]["evaluated"] is False


# ================================================================ coordinator
async def test_coordinator_records_failure_timeout_and_skip_independently(sandbox, monkeypatch):
    class Boom(agents.Agent):
        name, title = "boom", "Boom"

        def run(self, ctx):
            raise RuntimeError("kaput")

    class Slow(agents.Agent):
        name, title, timeout_s = "slow", "Slow", 0.05

        async def execute(self, ctx):
            await asyncio.Event().wait()

    class Skip(agents.Agent):
        name, title = "skip", "Skip"

        def run(self, ctx):
            raise agents.AgentSkipped("nothing to do")
    co = agents.Coordinator()
    for a in (Boom(), Slow(), Skip()):
        co.register(a)
    ctx = {}
    runs = [await co.invoke(n, ctx, experiment_id="exp-c") for n in ("boom", "slow", "skip")]
    assert [r["status"] for r in runs] == ["failed", "timeout", "skipped"]
    assert "kaput" in runs[0]["error"] and "outputs" not in ctx
    assert [r["agent"] for r in store.agent_runs("exp-c")] and len(store.agent_runs("exp-c")) == 3
    status = {a["name"]: a for a in co.status()}
    assert status["boom"]["state"] == "failed" and status["boom"]["failures"] == 1
    assert status["skip"]["state"] == "idle"
    for r in runs:
        assert r["started_at_utc"] and r["finished_at_utc"] and r["duration_ms"] is not None and r["version"]


async def test_coordinator_retries_then_succeeds(sandbox):
    class Flaky(agents.Agent):
        name, title, retries = "flaky", "Flaky", 2
        n = 0

        def run(self, ctx):
            Flaky.n += 1
            if Flaky.n < 3:
                raise RuntimeError("transient")
            return {"ok": True}
    co = agents.Coordinator()
    co.register(Flaky())
    run = await co.invoke("flaky", {})
    assert run["status"] == "completed" and run["attempts"] == 3


class _Good(agents.Agent):
    name, title = "good", "Good"

    def run(self, ctx):
        return {"answer": 42}

    def summary(self, out):
        return "answer 42"


class _Bad(agents.Agent):
    name, title = "bad", "Bad"

    def run(self, ctx):
        raise RuntimeError("kaput")


async def test_coordinator_publishes_start_and_finish_events_and_persists_the_run(sandbox, monkeypatch):
    """The dashboard's agent lane depends on these: a 'running' event when an agent starts, a
    terminal event when it ends, and the stored run (with its output document) for replay."""
    seen = []
    monkeypatch.setattr(agents.bus, "publish", lambda type_, data: seen.append((type_, dict(data))))
    co = agents.Coordinator()
    for a in (_Good(), _Bad()):
        co.register(a)
    ctx = {}
    await co.invoke("good", ctx, experiment_id="exp-e")
    await co.invoke("bad", ctx, experiment_id="exp-e")
    events = [d for t, d in seen if t == "agent"]
    assert [(e["agent"], e["status"]) for e in events] == [("good", "running"), ("good", "completed"),
                                                           ("bad", "running"), ("bad", "failed")]
    start, end = events[0], events[1]
    assert start["id"] == end["id"] and start["experiment_id"] == end["experiment_id"] == "exp-e"
    assert start["started_at_utc"] and start["finished_at_utc"] is None
    assert end["finished_at_utc"] and end["duration_ms"] is not None and end["summary"] == "answer 42"
    assert "output" not in end          # events stay small; the document is read from the store
    assert events[3]["error"].startswith("RuntimeError")
    stored = {r["agent"]: r for r in store.agent_runs("exp-e")}
    assert stored["good"]["output"] == {"answer": 42} and stored["good"]["input_ref"] == "exp-e"
    assert stored["bad"]["status"] == "failed" and stored["bad"]["output"] is None


async def test_agent_events_do_not_change_the_result(sandbox, monkeypatch):
    co = agents.Coordinator()
    co.register(_Good())
    with_events = await co.invoke("good", {}, experiment_id="exp-a")
    monkeypatch.setattr(agents.bus, "publish", lambda *a, **k: None)
    without = await co.invoke("good", {}, experiment_id="exp-b")
    same = ("status", "summary", "output", "error", "attempts", "method")
    assert {k: with_events[k] for k in same} == {k: without[k] for k in same}


async def test_hybrid_agent_that_fell_back_is_labelled_fallback_not_completed(sandbox, monkeypatch):
    stub_ai(monkeypatch, ai_client.AIUnavailable("down"))
    ctx = orch.gather_context(POLICY)
    result = await agents.coordinator.plan(ctx, "prop-1")
    assert [r["status"] for r in result["runs"]] == ["completed", "fallback"]
    assert result["proposal"]["method"] == "deterministic_fallback"


def test_registered_agents_and_pipelines_are_consistent():
    names = set(agents.coordinator.agents)
    assert names == {"fault_injection", "recovery", "insights", "learning", "remediation", "risk", "validation"}
    assert set(agents.Coordinator.PLAN) | set(agents.Coordinator.POST_RUN) == names
    for a in agents.coordinator.agents.values():
        assert a.role and a.kind in ("deterministic", "hybrid") and a.timeout_s > 0


# ================================================================ full lifecycle
async def test_complete_lifecycle_plan_run_recover_analyze_learn_recommend(sandbox):
    proposal = await orch.generate_scenario_plan(policy=POLICY, mode="manual")
    assert orch.get_status()["status"] == orch.STATUS_PLANNED
    s = scenario.Scenario(**proposal["scenario"])
    started, result = await run_scenario(s, proposal=proposal, policy=POLICY, mode="manual", source="proposal")
    rec = result["record"]

    assert rec["state"] == guard.STATE_COMPLETED and rec["recovery_state"] == "RECOVERED"
    assert [r["agent"] for r in result["runs"]] == list(agents.Coordinator.POST_RUN)
    assert all(r["status"] == "completed" for r in result["runs"])
    assert rec["validation"]["verdict"] == "conclusive" and rec["proposal"]["proposal_id"] == proposal["proposal_id"]
    assert rec["prediction"]["basis"] == "none"

    status = orch.get_status()
    assert status["status"] == orch.STATUS_DONE and status["error"] is None
    assert status["analysis"]["findings"] and status["remediation_plan"]["recommendations"]

    saved = json.load(open(os.path.join(config.EXPERIMENTS_DIR, f"{rec['id']}.json")))
    assert saved["validation"]["verdict"] == "conclusive" and saved["recovery_report"]["recovery_state"] == "RECOVERED"

    learn = store.get_learning(rec["id"])
    assert learn["is_evidence"] and learn["influence"]["score_after"] < learn["influence"]["score_before"]
    assert learn["influence"]["top_candidate_changed"] and learn["evaluation_status"].startswith("not_evaluated")

    kinds = [t["type"] for t in store.timeline(rec["id"])]
    assert kinds[:3] == ["agent", "agent", "proposed"]              # risk, fault_injection, proposal
    for needed in ("validated", "authorized", "experiment"):
        assert needed in kinds
    agent_rows = [t["data"]["agent"] for t in store.timeline(rec["id"]) if t["type"] == "agent"]
    assert agent_rows == ["risk", "fault_injection", *agents.Coordinator.POST_RUN]
    assert store.audit_log()[0]["action"] == "scenario_authorized"


async def test_second_run_is_predicted_from_the_first_and_evaluated(sandbox):
    s = scn(("kill", "things-search"))
    _, first = await run_scenario(s)
    orch.reset()
    _, second = await run_scenario(s)
    assert second["record"]["prediction"]["basis"] == "direct"
    assert second["record"]["prediction"]["basis_experiments"] == [first["record"]["id"]]
    learn = store.get_learning(second["record"]["id"])
    assert learn["comparison"]["evaluated"] and learn["comparison"]["outcome_match"] is True
    assert learn["related_experiments"] == [first["record"]["id"]]
    assert second["record"]["validation"]["hypothesis_supported"] is True


async def test_insights_failure_is_recorded_and_other_agents_still_run(sandbox, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("analysis exploded")
    monkeypatch.setattr(resilience_agent, "analyze_experiment", boom)
    _, result = await run_scenario(scn(("kill", "things")))
    statuses = {r["agent"]: r["status"] for r in result["runs"]}
    assert statuses == {"recovery": "completed", "validation": "completed", "insights": "failed",
                        "learning": "completed", "remediation": "skipped"}
    st = orch.get_status()
    assert st["status"] == orch.STATUS_DONE and "insights" in st["error"]      # never stuck in "analyzing"
    assert sandbox.containers["things"]["running"]
    assert store.get_learning(result["record"]["id"])["is_evidence"]


async def test_agent_timeout_does_not_block_the_pipeline(sandbox, monkeypatch):
    async def hang(ctx):
        await asyncio.Event().wait()
    monkeypatch.setattr(agents.coordinator.agents["insights"], "execute", hang)
    monkeypatch.setattr(agents.coordinator.agents["insights"], "timeout_s", 0.05)
    _, result = await run_scenario(scn(("kill", "things")))
    statuses = {r["agent"]: r["status"] for r in result["runs"]}
    assert statuses["insights"] == "timeout" and statuses["learning"] == "completed"
    assert orch.get_status()["status"] == orch.STATUS_DONE


async def test_recovery_agent_retry_resolves_an_initially_failed_rollback(sandbox, monkeypatch):
    sandbox.block_netem_delete = True
    real = scenario.recover_orphans

    async def tc_works_again(*a, **kw):
        sandbox.block_netem_delete = False
        return await real(*a, **kw)
    monkeypatch.setattr(scenario, "recover_orphans", tc_works_again)
    _, result = await run_scenario(scn(("delay", "mongodb")))
    rec = result["record"]
    assert rec["state"] == scenario.STATE_RECOVERY_REQUIRED          # what the engine observed, kept as history
    assert rec["recovery_state"] == "RECOVERED" and "automatic retry" in rec["recovery_resolved_by"]
    assert scenario.read_lockout() is None and sandbox.containers["mongodb"]["netem"] is None
    # the unverified rollback still marks this scenario as needing review before auto-selection
    ranked = learning.rank_candidates(DEP_MAP, store.history(), ["delay"], ["mongodb"])
    assert ranked[0]["blocked"]


async def test_unresolved_recovery_locks_out_new_runs_but_keeps_the_evidence(sandbox):
    sandbox.block_netem_delete = True
    _, result = await run_scenario(scn(("delay", "mongodb"), ("pause", "gateway")))
    rec = result["record"]
    assert rec["recovery_state"] == "RECOVERY_REQUIRED" and rec["recovery_report"]["mutation_blocked"]
    assert rec["recovery_report"]["faults_verified_removed"] == 1 and rec["recovery_report"]["faults_touched"] == 2
    assert rec["recovery_report"]["instructions"][0]["manual"]
    assert {r["agent"]: r["status"] for r in result["runs"]}["insights"] == "completed"   # evidence preserved
    assert orch.get_status()["analysis"] is not None
    orch.reset()
    with pytest.raises(scenario.ScenarioRejected) as e:
        orch.start_scenario(scn(("kill", "things")), auth())
    assert e.value.code == "recovery_required"
    assert orch.get_status()["status"] != orch.STATUS_RUNNING                   # the claim was rolled back

    sandbox.block_netem_delete = False
    resolved = await orch.resolve_lockout("op")
    assert resolved["lockout"] is None and store.audit_log()[0]["action"] == "lockout_resolve"
    orch.start_scenario(scn(("kill", "things")), auth())["task"].cancel()


async def test_force_clearing_a_lockout_requires_a_note(sandbox):
    sandbox.block_netem_delete = True
    await run_scenario(scn(("delay", "mongodb")))
    orch.reset()
    with pytest.raises(orch.OrchestratorError, match="note"):
        await orch.resolve_lockout("op", force=True)
    assert scenario.read_lockout() is not None
    out = await orch.resolve_lockout("op", force=True, note="removed qdisc by hand, verified with tc")
    assert out["forced"] and scenario.read_lockout() is None


async def test_duplicate_submission_and_concurrent_requests(sandbox):
    s = scn(("kill", "things"), max_duration_s=30)
    first = orch.start_scenario(s, auth(), request_id="req-1")
    again = orch.start_scenario(s, auth(), request_id="req-1")
    assert again["duplicate"] and again["experiment_id"] == first["experiment_id"]
    with pytest.raises(orch.OrchestratorConflict):
        orch.start_scenario(scn(("delay", "mongodb")), auth(), request_id="req-2")
    with pytest.raises(orch.OrchestratorConflict):
        orch.start_scenario(s, auth())
    while orch.get_status()["record"]["state"] != guard.STATE_MONITORING:
        await asyncio.sleep(0)
    assert orch.request_stop() is True
    result = await first["task"]
    assert result["record"]["abort_reason"].startswith("emergency stop")
    assert sum(1 for c in sandbox.calls if c[1] == "kill") == 1                # injected exactly once


async def test_refuses_to_start_without_independent_monitoring(sandbox, monkeypatch):
    monkeypatch.setattr(probe, "current_health", lambda window_s=10, metrics_dir=None: {"status": "unknown"})
    with pytest.raises(orch.OrchestratorConflict, match="UNKNOWN"):
        orch.start_scenario(scn(("kill", "things")), auth())
    assert sandbox.calls == [] and orch.get_status()["status"] == orch.STATUS_IDLE


async def test_recommendation_is_verified_by_a_follow_up_experiment(sandbox, monkeypatch):
    """Search fails while things-search is down -> finding + recommendation. The operator
    marks it implemented; the next run of the same scenario no longer shows the finding."""
    fixed = {"on": False}

    async def probe_fn(client, ids):
        up = sandbox.containers["things-search"]["running"] or fixed["on"]
        return {**OK_ROW, "search_ok": up}
    monkeypatch.setattr(probe, "probe_once", probe_fn)
    s = scn(("kill", "things-search"), max_duration_s=20)
    _, first = await run_scenario(s)
    exp1 = first["record"]["id"]
    recs = [r for r in store.recommendations() if r["experiment_id"] == exp1]
    target = next(r for r in recs if r["item"]["finding_title"] == "Search availability degraded")
    assert target["status"] == "proposed"

    store.set_recommendation_status(exp1, target["index"], "implemented")
    assert "kill@things-search" in store.pending_validation_signatures()
    fixed["on"] = True                                                # the "fix" took effect
    orch.reset()
    _, second = await run_scenario(s)
    after = store.get_recommendation(exp1, target["index"])
    assert after["status"] == "verified" and after["validated_by"] == second["record"]["id"]
    assert store.get_learning(second["record"]["id"])["recommendations_evaluated"][0]["status"] == "verified"


async def test_recommendation_is_marked_not_effective_when_the_finding_recurs(sandbox, monkeypatch):
    async def probe_fn(client, ids):
        return {**OK_ROW, "search_ok": sandbox.containers["things-search"]["running"]}
    monkeypatch.setattr(probe, "probe_once", probe_fn)
    s = scn(("kill", "things-search"), max_duration_s=20)
    _, first = await run_scenario(s)
    exp1 = first["record"]["id"]
    target = next(r for r in store.recommendations() if r["item"]["finding_title"] == "Search availability degraded")
    store.set_recommendation_status(exp1, target["index"], "implemented")
    orch.reset()
    await run_scenario(s)
    assert store.get_recommendation(exp1, target["index"])["status"] == "not_effective"


async def test_planning_and_execution_survive_an_unavailable_knowledge_store(sandbox, monkeypatch, tmp_path):
    write_experiments(record("exp-old", during=DEGRADED))
    monkeypatch.setattr(config, "KNOWLEDGE_DB", str(tmp_path))       # unusable
    proposal = await orch.generate_scenario_plan(policy=POLICY)
    assert proposal["store_degraded"] and proposal["validation"]["ok"]
    _, result = await run_scenario(scn(("kill", "things")))
    assert result["record"]["state"] == guard.STATE_COMPLETED and sandbox.containers["things"]["running"]
    statuses = {r["agent"]: r["status"] for r in result["runs"]}
    assert statuses["recovery"] == "completed" and statuses["learning"] == "failed"
    assert orch.get_status()["status"] == orch.STATUS_DONE


async def test_legacy_plan_ignores_dry_run_history(sandbox, monkeypatch):
    write_experiments(record("exp-dry", target="mongodb", dry_run=True))
    assert orch._recent_experiments() == []


async def test_shutdown_waits_for_rollback(sandbox):
    started = orch.start_scenario(scn(("pause", "things"), max_duration_s=60), auth())
    while orch.get_status()["record"]["state"] != guard.STATE_MONITORING:
        await asyncio.sleep(0)
    await orch.shutdown(timeout=10)
    assert started["task"].done() and not sandbox.containers["things"]["paused"]
    assert not scenario.list_journals()
