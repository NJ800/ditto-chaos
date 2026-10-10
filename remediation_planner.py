"""Remediation Planner: classifies each finding's recommended fix into a safety
category and produces a plan. It NEVER applies anything itself.

Category A (safe automated recovery): already fully implemented by guard.py's own
    rollback (remove the injected fault, verify container/network state, measure
    recovery). This module doesn't duplicate that -- it just recognizes when a
    finding's fix IS that recovery path and marks it "already automated by guard.py".
Category B (bounded configuration remediation): a reversible, local, explicitly
    allowlisted config change (e.g. tuning one specific timeout). NOT implemented for
    Day 1 -- the allowlist below is intentionally empty; see the Day-1 decision below.
Category C (application/infrastructure change): anything touching source code,
    schemas, dependency architecture, or persistent data. Always requires human
    approval. This module only ever produces a markdown/JSON plan, never a diff or a
    change it applies on its own.

Day-1 decision, stated plainly: Category B's automated path is NOT implemented, even
though the data model supports adding it later, because a same-day deterministic bug
in THIS project (comparing a short vs. full container-ID string) caused a real, if
brief, outage of a running container during this project's own development. Adding
an auto-apply path for ANY config change, on the same day that happened, is not a
risk worth taking for a hackathon demo. Every finding here becomes a human-reviewable
recommendation -- see remediation_plan_<id>.md.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import config

# Category B allowlist: deliberately empty for Day 1 (see module docstring). Each
# entry, if ever added, would need: the exact config key, a reversible value range,
# and a verification procedure -- none of that exists yet, so nothing here auto-applies.
CATEGORY_B_ALLOWLIST: dict[str, dict] = {}

_CATEGORY_A_PHRASES = ("guard.py", "already reverted", "already automated")


def classify(finding: dict) -> str:
    """Deliberately simple: Day 1 only recognizes "this is already what guard.py's
    rollback does" (Category A, via an explicit phrase match -- not a guess) and
    "this is an allowlisted bounded config change" (Category B, currently always
    empty). Everything else is Category C: human approval required, no exceptions."""
    fix = (finding.get("recommended_fix") or "").lower()
    if any(p in fix for p in _CATEGORY_A_PHRASES):
        return "A"
    for key in CATEGORY_B_ALLOWLIST:
        if key.lower() in fix:
            return "B"
    return "C"


def build_plan(analysis: dict) -> dict:
    """Takes a resilience_agent.analyze_experiment() result and classifies each
    finding. Pure function, no side effects, no network, no Docker."""
    items = []
    for f in analysis.get("findings", []):
        category = classify(f)
        status = {
            "A": "already_automated_by_guard_py",
            "B": "eligible_for_bounded_auto_remediation",  # currently unreachable: allowlist is empty
            "C": "awaiting_human_approval",
        }[category]
        items.append({
            "finding_title": f.get("title"), "severity": f.get("severity"),
            "confidence": f.get("confidence"), "recommended_fix": f.get("recommended_fix"),
            "category": category, "status": status,
            "auto_applied": False,  # Day 1: always False. Explicit so nobody has to infer it.
        })
    return {
        "schema_version": "1.0",
        "experiment_id": analysis.get("experiment_id"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "analysis_method": analysis.get("analysis_method"),
        "items": items,
        "none_auto_applied": True,
        "policy_note": ("Day 1 policy: nothing beyond guard.py's own fault rollback is applied "
                        "automatically. Every Category B/C item requires a human to review and "
                        "apply it -- see this module's docstring for why."),
    }


def verification_result(comparison: dict, finding_title: str | None, followup_finding_titles: list[str] | None,
                        followup_verdict: str | None, expected_benefit: str | None = None) -> dict:
    """Was the remediation's expected benefit demonstrated by the follow-up run?

    Pure function over the before/after comparison. It never claims proof: the strongest
    answer is "observed", meaning the targeted finding did not recur and nothing got worse
    in one follow-up run. `demonstrated` is one of observed | not_observed | inconclusive."""
    summary = comparison.get("summary") or {}
    improved, regressed = summary.get("improved", 0), summary.get("regressed", 0)
    recurred = None if followup_finding_titles is None or not finding_title else \
        finding_title in followup_finding_titles
    if not comparison.get("comparable"):
        demonstrated = "inconclusive"
        statement = "The two runs cannot be compared: " + "; ".join(comparison.get("not_comparable_reasons") or [])
    elif followup_verdict not in (None, "conclusive"):
        demonstrated = "inconclusive"
        statement = (f"The follow-up run was judged '{followup_verdict}' by the validation agent, so it does "
                     f"not count as evidence either way.")
    elif recurred is None:
        demonstrated = "inconclusive"
        statement = ("The follow-up run could not be analysed, so it is unknown whether the finding recurred. "
                     f"{improved} metric(s) improved and {regressed} regressed.")
    elif recurred:
        demonstrated = "not_observed"
        statement = (f"The finding '{finding_title}' recurred in the follow-up run "
                     f"({improved} metric(s) improved, {regressed} regressed).")
    elif regressed and not improved:
        demonstrated = "not_observed"
        statement = (f"The finding '{finding_title}' did not recur, but {regressed} metric(s) regressed and "
                     f"none improved.")
    else:
        demonstrated = "observed"
        statement = (f"The finding '{finding_title}' did not recur in the follow-up run; {improved} metric(s) "
                     f"improved, {regressed} regressed.")
    return {"demonstrated": demonstrated, "statement": statement, "finding_title": finding_title,
            "finding_recurred": recurred, "expected_benefit": expected_benefit,
            "evidence_strength": "observed correlation from one follow-up run",
            "caveat": comparison.get("caveat")}


def to_markdown(plan: dict) -> str:
    lines = [
        f"# Remediation plan for experiment `{plan['experiment_id']}`\n",
        f"_{plan['generated_at_utc']}_ -- analysis method: `{plan['analysis_method']}`\n",
        f"> {plan['policy_note']}\n",
        "| severity | finding | category | status | recommended fix |",
        "|---|---|---|---|---|",
    ]
    for i in plan["items"]:
        lines.append(f"| {i['severity']} | {i['finding_title']} | {i['category']} | {i['status']} | "
                     f"{i['recommended_fix']} |")
    if not plan["items"]:
        lines.append("| -- | (no findings) | -- | -- | -- |")
    return "\n".join(lines) + "\n"


def save_plan(plan: dict, reports_dir: str | None = None) -> str:
    reports_dir = reports_dir or config.REPORTS_DIR
    os.makedirs(reports_dir, exist_ok=True)
    md_path = os.path.join(reports_dir, f"remediation_plan_{plan['experiment_id']}.md")
    tmp = md_path + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(to_markdown(plan))
    os.replace(tmp, md_path)
    return md_path


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: python remediation_planner.py reports/experiment_<id>_analysis.json")
        raise SystemExit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        analysis = json.load(f)
    plan = build_plan(analysis)
    path = save_plan(plan)
    print(f"[remediation] {len(plan['items'])} item(s), none auto-applied")
    print(f"[remediation] saved {path}")
