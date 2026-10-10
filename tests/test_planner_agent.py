"""Tests for planner_agent.py. ai_client.generate_json is always mocked -- no real
Gemini calls. The allowlist/bounds validation is deterministic Python and must reject
bad proposals regardless of whether they came from the AI or a hypothetical bug."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_client  # noqa: E402
import config  # noqa: E402
import guard  # noqa: E402
import planner_agent as pa  # noqa: E402

DEP_MAP = {
    "fault_target_recommendations": [
        {"service": "mongodb", "fan_in": 4, "fan_out": 0, "calculated_risk": "high",
         "allowlisted_for_faults": True, "reasoning": "4 services depend on it"},
        {"service": "things-search", "fan_in": 1, "fan_out": 1, "calculated_risk": "medium",
         "allowlisted_for_faults": True, "reasoning": "1 service depends on it"},
        {"service": "nginx", "fan_in": 0, "fan_out": 0, "calculated_risk": "low",
         "allowlisted_for_faults": False, "reasoning": "not allowlisted"},
    ]
}
HEALTH = {"status": "healthy", "fresh": True}

VALID_AI_PROPOSAL = {
    "fault": "delay", "target": "mongodb", "params": {"delay_ms": 300, "ttl_s": 20},
    "risk": "high", "hypothesis": "mongodb is a single point of failure for 4 services",
    "expected_impact": "writes/reads across things/things-search/policies/connectivity slow down",
    "reason": "highest fan-in, not yet tested", "success_criteria": "kill switch catches it, recovers",
    "dependencies": ["things", "things-search", "policies", "connectivity"],
    "max_duration_s": 60, "recovery_timeout_s": 120,
}


# ---------------------------------------------------------------- validation
def test_validate_rejects_target_outside_allowlist():
    bad = {**VALID_AI_PROPOSAL, "target": "mongodb-admin-console"}
    try:
        pa._validate_and_normalize(bad)
        assert False, "should have raised PlannerError"
    except pa.PlannerError:
        pass


def test_validate_rejects_unknown_fault_type():
    bad = {**VALID_AI_PROPOSAL, "fault": "reboot-everything"}
    try:
        pa._validate_and_normalize(bad)
        assert False, "should have raised PlannerError"
    except pa.PlannerError:
        pass


def test_validate_rejects_missing_keys():
    try:
        pa._validate_and_normalize({"fault": "kill"})
        assert False, "should have raised PlannerError"
    except pa.PlannerError:
        pass


def test_validate_clamps_out_of_range_delay_params():
    bad = {**VALID_AI_PROPOSAL, "params": {"delay_ms": 999999, "ttl_s": -5}}
    clean = pa._validate_and_normalize(bad)
    assert clean["params"]["delay_ms"] <= pa.DELAY_MS_BOUNDS[1]
    assert clean["params"]["ttl_s"] >= pa.DELAY_TTL_BOUNDS[0]


def test_validate_clamps_out_of_range_durations():
    bad = {**VALID_AI_PROPOSAL, "max_duration_s": 99999, "recovery_timeout_s": -1}
    clean = pa._validate_and_normalize(bad)
    assert pa.MAX_DURATION_BOUNDS[0] <= clean["max_duration_s"] <= pa.MAX_DURATION_BOUNDS[1]
    assert pa.RECOVERY_TIMEOUT_BOUNDS[0] <= clean["recovery_timeout_s"] <= pa.RECOVERY_TIMEOUT_BOUNDS[1]


def test_validate_accepts_a_good_proposal():
    clean = pa._validate_and_normalize(VALID_AI_PROPOSAL)
    assert clean["target"] == "mongodb"
    assert clean["fault"] == "delay"
    assert clean["params"] == {"delay_ms": 300, "ttl_s": 20}


# ---------------------------------------------------------------- deterministic fallback
def test_deterministic_fallback_used_when_ai_disabled(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    proposal = pa.propose_experiment(DEP_MAP, HEALTH, None, [])
    assert proposal["method"] == "deterministic_fallback"
    assert proposal["target"] in guard.ALLOWLIST
    assert proposal["fault"] in pa.VALID_FAULTS


def test_deterministic_fallback_prefers_untried_target(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    past = [{"target": "mongodb", "fault": "kill"}]
    proposal = pa.propose_experiment(DEP_MAP, HEALTH, None, past)
    assert proposal["target"] == "things-search"  # the only other allowlisted candidate


def test_deterministic_fallback_never_targets_non_allowlisted_service(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    for _ in range(5):
        proposal = pa.propose_experiment(DEP_MAP, HEALTH, None, [])
        assert proposal["target"] != "nginx"
        assert proposal["target"] in guard.ALLOWLIST


def test_deterministic_fallback_with_no_dependency_map_still_returns_valid_proposal(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    proposal = pa.propose_experiment(None, None, None, [])
    assert proposal["target"] in guard.ALLOWLIST
    assert proposal["fault"] in pa.VALID_FAULTS


# ---------------------------------------------------------------- AI path
def test_ai_path_used_when_response_is_valid(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(ai_client, "generate_json", lambda prompt, timeout=None: VALID_AI_PROPOSAL)
    proposal = pa.propose_experiment(DEP_MAP, HEALTH, None, [])
    assert proposal["method"] == "gemini"
    assert proposal["target"] == "mongodb"


def test_falls_back_when_ai_proposes_out_of_allowlist_target(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    bad = {**VALID_AI_PROPOSAL, "target": "mongodb-admin-console"}
    monkeypatch.setattr(ai_client, "generate_json", lambda prompt, timeout=None: bad)
    proposal = pa.propose_experiment(DEP_MAP, HEALTH, None, [])
    assert proposal["method"] == "deterministic_fallback"
    assert proposal["target"] in guard.ALLOWLIST


def test_falls_back_when_ai_raises_unavailable(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")

    def boom(prompt, timeout=None):
        raise ai_client.AIUnavailable("503")
    monkeypatch.setattr(ai_client, "generate_json", boom)
    proposal = pa.propose_experiment(DEP_MAP, HEALTH, None, [])
    assert proposal["method"] == "deterministic_fallback"


def test_falls_back_when_ai_returns_non_dict(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(ai_client, "generate_json", lambda prompt, timeout=None: ["not", "a", "dict"])
    proposal = pa.propose_experiment(DEP_MAP, HEALTH, None, [])
    assert proposal["method"] == "deterministic_fallback"


def test_proposal_always_has_schema_version_and_timestamp(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    proposal = pa.propose_experiment(DEP_MAP, HEALTH, None, [])
    assert proposal["schema_version"] == pa.SCHEMA_VERSION
    assert "generated_at_utc" in proposal
