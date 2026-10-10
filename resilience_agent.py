"""AI Resilience Engineer: turns one saved guard.py experiment record into a
structured, evidence-grounded findings report.

Day-1 scope: ANALYSIS AND RECOMMENDATIONS ONLY -- nothing here applies a fix. See
remediation_planner.py for why auto-applying anything beyond guard.py's own rollback
stays a human-approved step.

Grounding rule: the prompt sent to the model contains ONLY numbers pulled directly
out of the experiment record by extract_evidence() -- never invented. The model's
JSON response is validated against a fixed schema before being trusted; any missing
key, invalid severity/confidence value, or malformed entry gets dropped (findings) or
rejected entirely (the whole response), not silently repaired or guessed.

If AI is disabled, unconfigured, or the call fails/returns something invalid, this
module falls back to a deterministic, rule-based comparison of the SAME real numbers
-- labeled `"analysis_method": "deterministic_fallback"` so nobody mistakes a
mechanical before/after diff for a model's judgement.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import ai_client
import config

SCHEMA_VERSION = "1.0"
REQUIRED_FINDING_KEYS = {"title", "severity", "evidence", "impact", "confidence", "recommended_fix"}
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_CONFIDENCE = {"supported_by_measurements", "plausible_hypothesis", "insufficient_evidence"}
_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _delta(before, after):
    return None if before is None or after is None else round(after - before, 2)


def _pct_change(before, after):
    if before is None or after is None or before == 0:
        return None
    return round((after - before) / before * 100, 1)


def extract_evidence(record: dict) -> dict:
    """Pull ONLY real numbers out of the experiment record. This dict is both what
    gets shown to the model as ground truth and what the deterministic fallback
    compares -- the single source of truth for every claim this module makes."""
    baseline, during = record.get("baseline") or {}, record.get("during") or {}

    def p95(block: dict, key: str):
        return (block.get(key) or {}).get("p95")

    b_direct, d_direct = p95(baseline, "direct_ms"), p95(during, "direct_ms")
    b_search, d_search = p95(baseline, "search_ms"), p95(during, "search_ms")
    return {
        "experiment_id": record.get("id"), "fault": record.get("fault"), "target": record.get("target"),
        "params": record.get("params"), "state": record.get("state"),
        "aborted": record.get("aborted"), "abort_reason": record.get("abort_reason"),
        "detected_s": record.get("detected_s"),
        "reverted": record.get("reverted"), "rollback_verified": record.get("rollback_verified"),
        "rollback_error": record.get("rollback_error"),
        "recovery_s": record.get("recovery_s"), "recovery_outcome": record.get("recovery_outcome"),
        "baseline_direct_p95_ms": b_direct, "during_direct_p95_ms": d_direct,
        "direct_p95_delta_ms": _delta(b_direct, d_direct),
        "direct_p95_pct_change": _pct_change(b_direct, d_direct),
        "baseline_search_p95_ms": b_search, "during_search_p95_ms": d_search,
        "search_p95_delta_ms": _delta(b_search, d_search),
        "search_p95_pct_change": _pct_change(b_search, d_search),
        "baseline_direct_error_rate": baseline.get("direct_error_rate"),
        "during_direct_error_rate": during.get("direct_error_rate"),
        "baseline_search_error_rate": baseline.get("search_error_rate"),
        "during_search_error_rate": during.get("search_error_rate"),
        "baseline_stale_p95_s": baseline.get("stale_p95_s"), "during_stale_p95_s": during.get("stale_p95_s"),
        "baseline_lost_acked_max": baseline.get("lost_acked_max"),
        "during_lost_acked_max": during.get("lost_acked_max"),
        "baseline_search_drift_max": baseline.get("search_drift_max"),
        "during_search_drift_max": during.get("search_drift_max"),
    }


def _validate_findings(findings) -> list[dict] | None:
    if not isinstance(findings, list):
        return None
    clean = []
    for f in findings:
        if not isinstance(f, dict) or not REQUIRED_FINDING_KEYS.issubset(f.keys()):
            continue  # drop malformed entries rather than forward them half-trusted
        if f.get("severity") not in VALID_SEVERITIES or f.get("confidence") not in VALID_CONFIDENCE:
            continue
        clean.append({k: f[k] for k in REQUIRED_FINDING_KEYS})
    return clean


def _ai_analysis(evidence: dict, dependency_context: str) -> dict | None:
    prompt = f"""You are a site reliability engineer analyzing ONE real chaos-engineering
experiment against Eclipse Ditto. Use ONLY the measurements given below -- never invent a
number or a measurement that isn't present. If the evidence doesn't support a claim, mark
its confidence "insufficient_evidence" rather than guessing.

REAL EXPERIMENT EVIDENCE (JSON, the complete ground truth -- nothing else exists):
{json.dumps(evidence, indent=2)}

DEPENDENCY CONTEXT:
{dependency_context}

Respond with ONLY a JSON object matching exactly this shape (no extra commentary):
{{
  "summary": "one or two sentences, grounded in the evidence above",
  "severity": "critical" | "high" | "medium" | "low",
  "root_cause_hypotheses": [
    {{"hypothesis": "...", "supporting_evidence": "...", "contradicting_evidence": "...",
      "confidence": "supported_by_measurements" | "plausible_hypothesis" | "insufficient_evidence",
      "affected_services": ["..."]}}
  ],
  "findings": [
    {{"title": "...", "severity": "critical" | "high" | "medium" | "low",
      "evidence": "cite the specific numbers from above",
      "impact": "...",
      "confidence": "supported_by_measurements" | "plausible_hypothesis" | "insufficient_evidence",
      "recommended_fix": "..."}}
  ],
  "recovery_verified": true | false,
  "recommended_next_experiment": "..."
}}"""
    try:
        raw = ai_client.generate_json(prompt)
    except ai_client.AIUnavailable:
        return None
    if not isinstance(raw, dict):
        return None
    findings = _validate_findings(raw.get("findings"))
    if findings is None or raw.get("severity") not in VALID_SEVERITIES:
        return None  # malformed response -- caller falls back, never trusts partial output
    hyps = raw.get("root_cause_hypotheses")
    return {
        "summary": str(raw.get("summary", ""))[:2000], "severity": raw["severity"],
        "root_cause_hypotheses": hyps if isinstance(hyps, list) else [],
        "findings": findings,
        "recovery_verified": bool(raw.get("recovery_verified")),
        "recommended_next_experiment": str(raw.get("recommended_next_experiment", ""))[:1000],
    }


def _deterministic_analysis(evidence: dict) -> dict:
    """No AI: a plain, mechanical comparison of the same real numbers in `evidence`.
    Exists so analyze_experiment() always returns something trustworthy, even with
    AI disabled, unconfigured, or unreachable."""
    findings = []

    if evidence.get("during_lost_acked_max"):
        findings.append({
            "title": "Confirmed lost acknowledged updates", "severity": "critical",
            "evidence": f"during_lost_acked_max={evidence['during_lost_acked_max']}",
            "impact": "Acknowledged writes were not durably observable afterward -- silent data loss.",
            "confidence": "supported_by_measurements",
            "recommended_fix": "Investigate the write/persistence path for the target service for "
                               "a durability or acknowledgement-ordering bug.",
        })

    d_delta = evidence.get("direct_p95_delta_ms")
    if d_delta is not None and d_delta > 0 and evidence.get("baseline_direct_p95_ms"):
        pct = evidence.get("direct_p95_pct_change") or 0
        findings.append({
            "title": "Direct-read latency degradation", "severity": "high" if pct > 100 else "medium",
            "evidence": f"direct p95 {evidence['baseline_direct_p95_ms']}ms -> "
                       f"{evidence['during_direct_p95_ms']}ms ({pct}% change)",
            "impact": "Direct reads of digital twins were slower during the fault.",
            "confidence": "supported_by_measurements",
            "recommended_fix": f"Review timeout/retry/connection-pool behavior for "
                               f"'{evidence.get('target')}' and its dependencies.",
        })

    b_serr, d_serr = evidence.get("baseline_search_error_rate") or 0, evidence.get("during_search_error_rate") or 0
    if d_serr > b_serr:
        findings.append({
            "title": "Search availability degraded", "severity": "high",
            "evidence": f"search_error_rate {b_serr} -> {d_serr}",
            "impact": "Fleet-wide search queries started failing during the fault.",
            "confidence": "supported_by_measurements",
            "recommended_fix": f"Check whether search can degrade gracefully (e.g. fall back to "
                               f"direct reads) when '{evidence.get('target')}' is impaired, instead "
                               f"of failing outright.",
        })
    elif (evidence.get("search_p95_delta_ms") or 0) > 0:
        findings.append({
            "title": "Search latency degradation", "severity": "medium",
            "evidence": f"search p95 {evidence.get('baseline_search_p95_ms')}ms -> "
                       f"{evidence.get('during_search_p95_ms')}ms",
            "impact": "Search queries were slower during the fault.",
            "confidence": "supported_by_measurements",
            "recommended_fix": f"Review '{evidence.get('target')}'s search-indexing path for added latency.",
        })

    stale_delta = _delta(evidence.get("baseline_stale_p95_s"), evidence.get("during_stale_p95_s"))
    if stale_delta is not None and stale_delta > 1.0:
        findings.append({
            "title": "Digital-twin staleness increased", "severity": "medium",
            "evidence": f"stale p95 {evidence.get('baseline_stale_p95_s')}s -> "
                       f"{evidence.get('during_stale_p95_s')}s",
            "impact": "Twins fell further behind real-world updates during the fault.",
            "confidence": "plausible_hypothesis",
            "recommended_fix": "Confirm the fleet simulator was actively sending updates during "
                               "this window before treating this as fault-induced rather than idle "
                               "-- staleness naturally grows if nothing is sending data.",
        })

    if evidence.get("recovery_outcome") == "timeout":
        findings.append({
            "title": "Recovery not verified within the configured timeout", "severity": "high",
            "evidence": f"recovery_outcome=timeout, recovery_s={evidence.get('recovery_s')}",
            "impact": "guard.py could not confirm the application returned to a healthy state "
                     "after rollback within the allotted time.",
            "confidence": "supported_by_measurements",
            "recommended_fix": "Check whether this was driven by staleness with no live traffic "
                               "(run the simulator) rather than an actual unresolved service outage "
                               "before treating it as a regression.",
        })

    if evidence.get("rollback_error"):
        findings.append({
            "title": "Rollback was not cleanly verified", "severity": "critical",
            "evidence": f"rollback_error={evidence['rollback_error']}",
            "impact": "The injected fault may still be active -- this requires manual verification.",
            "confidence": "supported_by_measurements",
            "recommended_fix": "Manually verify and clear the fault on "
                               f"'{evidence.get('target')}' immediately; see the README's "
                               "manual-recovery section.",
        })

    if not findings:
        findings.append({
            "title": "No significant degradation detected in the saved measurements",
            "severity": "low", "evidence": "all before/during deltas are within noise",
            "impact": "No evidence of a resilience weakness from this experiment.",
            "confidence": "supported_by_measurements",
            "recommended_fix": "Consider a longer or larger fault parameter to get a more "
                               "informative signal next time.",
        })

    findings.sort(key=lambda f: -_SEVERITY_RANK[f["severity"]])
    overall = findings[0]["severity"]

    return {
        "summary": f"Deterministic comparison of baseline vs. during-fault measurements for "
                  f"experiment {evidence.get('experiment_id')} ({evidence.get('fault')} fault on "
                  f"'{evidence.get('target')}').",
        "severity": overall,
        "root_cause_hypotheses": [],
        "findings": findings,
        "recovery_verified": bool(evidence.get("reverted")) and evidence.get("recovery_outcome") == "recovered",
        "recommended_next_experiment": "Not generated -- AI analysis was unavailable for this run. "
                                       "See the findings above for what to investigate manually.",
    }


def analyze_experiment(record: dict, dependency_map: dict | None = None) -> dict:
    """Always returns a complete, schema-valid report -- never raises for a missing
    AI provider, and never presents a fallback as if it were the model's opinion."""
    evidence = extract_evidence(record)
    dep_context = "no dependency map available"
    if dependency_map:
        target = evidence.get("target")
        edges = [f"{e['from']}->{e['to']}" for e in dependency_map.get("edges", [])
                if target in (e["from"], e["to"])]
        dep_context = f"edges touching '{target}': {edges}" if edges else f"no known edges touch '{target}'"

    ai_result = _ai_analysis(evidence, dep_context) if ai_client.ai_enabled() else None
    body, method = (ai_result, "gemini") if ai_result is not None else (_deterministic_analysis(evidence),
                                                                        "deterministic_fallback")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "experiment_id": evidence["experiment_id"],
        "analysis_method": method,
        "evidence": evidence,
        **body,
    }


def save_report(analysis: dict, reports_dir: str | None = None) -> str:
    reports_dir = reports_dir or config.REPORTS_DIR
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"experiment_{analysis['experiment_id']}_analysis.json")
    tmp = path + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2)
    os.replace(tmp, path)
    _append_latest_findings(analysis, reports_dir)
    return path


def _append_latest_findings(analysis: dict, reports_dir: str) -> None:
    path = os.path.join(reports_dir, "latest_findings.md")
    lines = [f"\n## Experiment `{analysis['experiment_id']}` ({analysis['analysis_method']})\n",
            f"_{analysis['generated_at_utc']}_ -- severity: **{analysis['severity']}**\n\n",
            f"{analysis['summary']}\n"]
    for f in analysis["findings"]:
        lines.append(f"- **[{f['severity']}] {f['title']}** ({f['confidence']}) -- {f['evidence']}\n"
                    f"  - Impact: {f['impact']}\n  - Fix: {f['recommended_fix']}\n")
    with open(path, "a", encoding="utf-8") as out:
        out.write("\n".join(lines))


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: python resilience_agent.py experiments/<file>.json")
        raise SystemExit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        rec = json.load(f)
    dep_map = None
    if os.path.isfile("dependency_map.json"):
        with open("dependency_map.json", encoding="utf-8") as f:
            dep_map = json.load(f)
    report = analyze_experiment(rec, dep_map)
    path = save_report(report)
    print(f"[resilience_agent] method={report['analysis_method']} severity={report['severity']}")
    print(f"[resilience_agent] saved {path}")
