"""Tests for resilience_agent.py. ai_client.generate_json is always mocked here --
no real Gemini calls, no network, no cost, fully deterministic."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_client  # noqa: E402
import config  # noqa: E402
import resilience_agent as ra  # noqa: E402

REAL_RECORD = {
    "id": "exp-test-0001", "fault": "kill", "target": "things-search", "params": {},
    "state": "RECOVERY_TIMEOUT", "aborted": True,
    "abort_reason": "search endpoint failed on all of the last 3 ticks",
    "detected_s": 15.27, "reverted": True, "rollback_verified": True, "rollback_error": None,
    "recovery_s": None, "recovery_outcome": "timeout",
    "baseline": {"direct_ms": {"p95": 132.2}, "search_ms": {"p95": 103.1},
                "direct_error_rate": 0.0, "search_error_rate": 0.0,
                "stale_p95_s": 10.0, "lost_acked_max": 0, "search_drift_max": 0},
    "during": {"direct_ms": {"p95": 47.9}, "search_ms": {"p95": None},
              "direct_error_rate": 0.0, "search_error_rate": 1.0,
              "stale_p95_s": 27.0, "lost_acked_max": 0, "search_drift_max": None},
}

VALID_AI_RESPONSE = {
    "summary": "Search failed completely during the fault.",
    "severity": "critical",
    "root_cause_hypotheses": [{"hypothesis": "single point of failure",
                               "supporting_evidence": "100% search error rate",
                               "contradicting_evidence": "", "confidence": "supported_by_measurements",
                               "affected_services": ["things-search"]}],
    "findings": [{"title": "Search outage", "severity": "critical", "evidence": "err rate 0->1.0",
                 "impact": "search unusable", "confidence": "supported_by_measurements",
                 "recommended_fix": "add redundancy"}],
    "recovery_verified": False,
    "recommended_next_experiment": "retry with replicas",
}


def test_extract_evidence_pulls_only_real_numbers():
    e = ra.extract_evidence(REAL_RECORD)
    assert e["baseline_direct_p95_ms"] == 132.2
    assert e["during_direct_p95_ms"] == 47.9
    assert e["direct_p95_delta_ms"] == -84.3
    assert e["during_search_error_rate"] == 1.0
    assert e["during_search_p95_ms"] is None  # None stays None, never fabricated as 0


def test_extract_evidence_handles_missing_baseline_and_during():
    e = ra.extract_evidence({"id": "x", "fault": "kill", "target": "things"})
    assert e["baseline_direct_p95_ms"] is None
    assert e["direct_p95_delta_ms"] is None  # no crash, no fabricated delta


def test_deterministic_fallback_used_when_ai_disabled(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    report = ra.analyze_experiment(REAL_RECORD)
    assert report["analysis_method"] == "deterministic_fallback"
    titles = {f["title"] for f in report["findings"]}
    assert "Search availability degraded" in titles
    assert report["severity"] == "high"  # highest severity among triggered findings


def test_deterministic_fallback_flags_lost_acked_as_critical(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    record = {**REAL_RECORD, "during": {**REAL_RECORD["during"], "lost_acked_max": 3}}
    report = ra.analyze_experiment(record)
    assert report["severity"] == "critical"
    assert any(f["title"] == "Confirmed lost acknowledged updates" and f["severity"] == "critical"
               for f in report["findings"])


def test_deterministic_fallback_flags_unverified_rollback_as_critical(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    record = {**REAL_RECORD, "rollback_error": "netem delay still present"}
    report = ra.analyze_experiment(record)
    assert report["severity"] == "critical"
    assert any("Rollback was not cleanly verified" in f["title"] for f in report["findings"])


def test_healthy_experiment_produces_low_severity_finding(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    clean = {
        "id": "exp-clean", "fault": "delay", "target": "mongodb",
        "recovery_outcome": "recovered", "reverted": True, "rollback_error": None,
        "baseline": {"direct_ms": {"p95": 50}, "search_ms": {"p95": 60},
                    "direct_error_rate": 0.0, "search_error_rate": 0.0,
                    "stale_p95_s": 1.0, "lost_acked_max": 0, "search_drift_max": 0},
        "during": {"direct_ms": {"p95": 50}, "search_ms": {"p95": 60},
                  "direct_error_rate": 0.0, "search_error_rate": 0.0,
                  "stale_p95_s": 1.0, "lost_acked_max": 0, "search_drift_max": 0},
    }
    report = ra.analyze_experiment(clean)
    assert report["severity"] == "low"
    assert "No significant degradation" in report["findings"][0]["title"]


def test_ai_path_used_when_response_is_valid(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(ai_client, "generate_json", lambda prompt, timeout=None: VALID_AI_RESPONSE)
    report = ra.analyze_experiment(REAL_RECORD)
    assert report["analysis_method"] == "gemini"
    assert report["severity"] == "critical"
    assert report["findings"][0]["title"] == "Search outage"


def test_falls_back_when_ai_response_is_malformed(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    bad = {"summary": "x", "severity": "not-a-real-severity", "findings": []}
    monkeypatch.setattr(ai_client, "generate_json", lambda prompt, timeout=None: bad)
    report = ra.analyze_experiment(REAL_RECORD)
    assert report["analysis_method"] == "deterministic_fallback"


def test_falls_back_when_ai_raises_unavailable(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")

    def boom(prompt, timeout=None):
        raise ai_client.AIUnavailable("503")
    monkeypatch.setattr(ai_client, "generate_json", boom)
    report = ra.analyze_experiment(REAL_RECORD)
    assert report["analysis_method"] == "deterministic_fallback"


def test_malformed_individual_findings_are_dropped_not_crashed_on(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    mixed = {
        "summary": "x", "severity": "high",
        "findings": [
            {"title": "ok one", "severity": "high", "evidence": "e", "impact": "i",
             "confidence": "supported_by_measurements", "recommended_fix": "f"},
            {"title": "missing keys"},  # malformed -- must be dropped, not crash
            {"title": "bad severity", "severity": "catastrophic", "evidence": "e", "impact": "i",
             "confidence": "supported_by_measurements", "recommended_fix": "f"},
        ],
        "recovery_verified": True, "recommended_next_experiment": "",
    }
    monkeypatch.setattr(ai_client, "generate_json", lambda prompt, timeout=None: mixed)
    report = ra.analyze_experiment(REAL_RECORD)
    assert report["analysis_method"] == "gemini"
    assert len(report["findings"]) == 1
    assert report["findings"][0]["title"] == "ok one"


def test_save_report_is_atomic_and_appends_to_latest_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    report = ra.analyze_experiment(REAL_RECORD)
    path = ra.save_report(report, reports_dir=str(tmp_path))
    assert os.path.exists(path)
    assert not any(".tmp" in n for n in os.listdir(tmp_path))
    with open(path) as f:
        assert json.load(f)["experiment_id"] == "exp-test-0001"
    findings_md = (tmp_path / "latest_findings.md").read_text()
    assert "exp-test-0001" in findings_md

    # A second report for a different experiment APPENDS, doesn't overwrite.
    report2 = ra.analyze_experiment({**REAL_RECORD, "id": "exp-test-0002"})
    ra.save_report(report2, reports_dir=str(tmp_path))
    findings_md = (tmp_path / "latest_findings.md").read_text()
    assert "exp-test-0001" in findings_md and "exp-test-0002" in findings_md


def test_dependency_context_is_included_when_map_provided(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake")
    captured = {}

    def fake_generate(prompt, timeout=None):
        captured["prompt"] = prompt
        return VALID_AI_RESPONSE
    monkeypatch.setattr(ai_client, "generate_json", fake_generate)
    dep_map = {"edges": [{"from": "things-search", "to": "mongodb", "source": "known-architecture"}]}
    ra.analyze_experiment(REAL_RECORD, dep_map)
    assert "things-search->mongodb" in captured["prompt"]
