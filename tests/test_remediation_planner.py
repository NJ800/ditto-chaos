"""Tests for remediation_planner.py -- pure functions, no AI, no network, no Docker."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import remediation_planner as rp  # noqa: E402

ANALYSIS = {
    "experiment_id": "exp-1", "analysis_method": "deterministic_fallback",
    "findings": [
        {"title": "A", "severity": "high", "confidence": "supported_by_measurements",
         "recommended_fix": "Review timeout/retry behavior for the connectivity service."},
        {"title": "B", "severity": "critical", "confidence": "supported_by_measurements",
         "recommended_fix": "Already reverted by guard.py's rollback; verify manually if unsure."},
    ],
}


def test_category_c_is_the_default_for_unclassified_fixes():
    assert rp.classify(ANALYSIS["findings"][0]) == "C"


def test_category_a_recognized_only_by_explicit_phrase():
    assert rp.classify(ANALYSIS["findings"][1]) == "A"


def test_category_b_allowlist_is_empty_by_design():
    assert rp.CATEGORY_B_ALLOWLIST == {}
    # so nothing can ever classify as B right now, even if a fix happens to look bounded
    f = {"recommended_fix": "Increase the request timeout slightly."}
    assert rp.classify(f) == "C"


def test_build_plan_never_marks_anything_auto_applied():
    plan = rp.build_plan(ANALYSIS)
    assert plan["none_auto_applied"] is True
    assert all(item["auto_applied"] is False for item in plan["items"])


def test_build_plan_statuses_match_category():
    plan = rp.build_plan(ANALYSIS)
    by_title = {i["finding_title"]: i for i in plan["items"]}
    assert by_title["A"]["category"] == "C" and by_title["A"]["status"] == "awaiting_human_approval"
    assert by_title["B"]["category"] == "A" and by_title["B"]["status"] == "already_automated_by_guard_py"


def test_build_plan_handles_zero_findings():
    plan = rp.build_plan({"experiment_id": "exp-empty", "analysis_method": "x", "findings": []})
    assert plan["items"] == []
    md = rp.to_markdown(plan)
    assert "no findings" in md


def test_to_markdown_contains_the_policy_note():
    plan = rp.build_plan(ANALYSIS)
    md = rp.to_markdown(plan)
    assert "Day 1 policy" in md
    assert "exp-1" in md


def test_save_plan_is_atomic(tmp_path):
    plan = rp.build_plan(ANALYSIS)
    path = rp.save_plan(plan, reports_dir=str(tmp_path))
    assert os.path.exists(path)
    assert not any(".tmp" in n for n in os.listdir(tmp_path))
