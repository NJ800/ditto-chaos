"""Scenario proposals (schema 2.0) -- the planning half of the Fault Injection Agent.

The model never invents a scenario. It picks ONE of the candidates that
learning.rank_candidates() already produced and scenario.validate() already accepted, and
may adjust numeric parameters -- which are then validated again. Invalid output, an
unavailable provider, or a policy violation all fall back to the top-ranked candidate
with deterministic text, labelled `deterministic_fallback`. Stop conditions and preflight
requirements are always generated from configuration, never from model output.

planner_agent.py (schema 1.0, single fault) is kept unchanged for the legacy CLI path.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import ai_client
import faults
import guard
import learning
import scenario
from planner_agent import PlannerError

PROPOSAL_SCHEMA_VERSION = "2.0"
MAX_CANDIDATES_SHOWN = 6
METRICS = ["direct_ms p95", "search_ms p95", "direct/search error rate", "stale_p95_s",
           "lost_acked", "search_drift", "recovery_s"]


def stop_conditions(th: guard.Thresholds) -> list[str]:
    return [
        "any acknowledged update is lost (immediate)",
        f"direct-read or search endpoint fails on {th.breach_window} consecutive probe ticks "
        f"(a probe that itself times out counts as a failed tick)",
        f"more than {th.max_error_rate:.0%} of the last {th.error_rate_window} ticks failed",
        f"direct-read latency above {th.max_latency_factor:g}x baseline p95 for {th.breach_window} ticks",
        f"twin staleness p95 above {th.max_stale_s:g}s for {th.breach_window} ticks",
        "operator emergency stop",
        "scenario max duration reached",
    ]


def _text_list(value, limit: int = 8) -> list[str]:
    return [str(v)[:300] for v in value][:limit] if isinstance(value, list) else []


def build_scenario(candidate: dict, overrides: dict | None = None, sequential: bool = False,
                   policy: dict | None = None) -> scenario.Scenario:
    actions = []
    for i, a in enumerate(candidate["actions"]):
        params = dict(a["params"])
        extra = (overrides or {}).get(f"{a['fault']}@{a['target']}")
        if isinstance(extra, dict):
            params.update({k: v for k, v in extra.items()
                           if isinstance(v, (int, float)) and not isinstance(v, bool)})
        actions.append({"fault": a["fault"], "target": a["target"], "params": params,
                        "start_offset_s": 10.0 * i if sequential else 0.0})
    duration = min(60.0, float((policy or {}).get("max_duration_s", 60)))
    return scenario.Scenario(name=candidate["signature"][:120], actions=actions, max_duration_s=duration)


def _ai_choice(candidates: list[dict], context: dict) -> dict | None:
    prompt = (
        "You are the fault-injection planner of a chaos-engineering platform for Eclipse Ditto.\n"
        "Choose exactly ONE of the numbered candidate scenarios in the context. You cannot add targets\n"
        "or fault types: every candidate already passed the deterministic safety policy. You may adjust\n"
        "numeric parameters inside parameter_bounds. Ground every sentence in the context; do not invent\n"
        "measurements. If there is no history, say the hypothesis is untested.\n\n"
        "CONTEXT (JSON, the complete ground truth):\n"
        + json.dumps(context, indent=2)[:14000] +
        "\n\nRespond with ONLY a JSON object with these keys:\n"
        '  "candidate_index": int index into candidates\n'
        '  "objective": string, "hypothesis": string, "target_reason": string, "expected_impact": string\n'
        '  "expected_symptoms": [string], "metrics_to_observe": [string], "safety_risks": [string]\n'
        '  "learning_value": string\n'
        '  "sequential": true to stagger faults 10s apart, false to apply them together\n'
        '  "params": {"<fault>@<target>": {"<param>": number}}  (optional)\n')
    try:
        raw = ai_client.generate_json(prompt)
    except ai_client.AIUnavailable:
        return None
    if not isinstance(raw, dict):
        return None
    idx = raw.get("candidate_index")
    if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < len(candidates)):
        return None
    return raw


def propose_scenario(ranked: list[dict], dependency_map: dict | None, health: dict | None,
                     history: list[dict], risk: dict | None = None, policy: dict | None = None) -> dict:
    """One validated proposal. Raises PlannerError only when no candidate is eligible at
    all; never returns a scenario that scenario.validate() would refuse."""
    th = guard.Thresholds.from_env()
    candidates = [c for c in ranked if not c.get("blocked")][:MAX_CANDIDATES_SHOWN]
    if not candidates:
        raise PlannerError("no eligible experiment candidate under the current policy (the allowed "
                           "fault/target lists may be empty, or every candidate is blocked)")
    for c in candidates:
        c["prediction"] = learning.predict(c["actions"], history)
    risk_targets = (risk or {}).get("targets") or {}

    method, note, choice = "deterministic_fallback", None, None
    chosen, scn, report = candidates[0], None, None
    if ai_client.ai_enabled():
        in_play = {a["target"] for c in candidates for a in c["actions"]}
        context = {
            "candidates": [{"index": i, "actions": c["actions"], "score": c["score"], "runs_so_far": c["runs"],
                            "why_ranked_here": c["rationale"], "prediction_from_history": c["prediction"]}
                           for i, c in enumerate(candidates)],
            "parameter_bounds": {f: [vars(p) for p in faults.REGISTRY[f].spec.params]
                                 for f in sorted({a["fault"] for c in candidates for a in c["actions"]})},
            "current_health": health or "unknown",
            "target_risk": {t: v for t, v in risk_targets.items() if t in in_play},
            "stop_conditions_enforced_by_code": stop_conditions(th),
        }
        choice = _ai_choice(candidates, context)
        if choice is None:
            note = "AI provider unavailable or returned an invalid choice"
        else:
            cand = candidates[choice["candidate_index"]]
            trial_report = None
            try:
                trial = build_scenario(cand, choice.get("params") if isinstance(choice.get("params"), dict) else {},
                                       bool(choice.get("sequential")), policy)
                trial_report = scenario.validate(trial, dependency_map, policy)
            except Exception as e:  # noqa: BLE001 -- the model's output is simply refused
                note = f"model output rejected: {type(e).__name__}: {e}"[:300]
            if trial_report and trial_report["ok"]:
                chosen, scn, report, method = cand, trial, trial_report, ai_client.provider_name()
            else:
                if trial_report:
                    note = ("model output rejected by policy: "
                            + "; ".join(e["message"] for e in trial_report["errors"]))[:300]
                choice = None
    if scn is None:
        scn = build_scenario(chosen, None, False, policy)
        report = scenario.validate(scn, dependency_map, policy)
        if not report["ok"]:   # cannot happen for a ranked candidate; refuse rather than guess
            raise PlannerError("top-ranked candidate failed validation: "
                               + "; ".join(e["message"] for e in report["errors"]))

    pred = chosen["prediction"]
    targets = report["affected_services"]
    edges = [f"{e['from']}->{e['to']}" for e in (dependency_map or {}).get("edges", [])
             if e.get("from") in targets or e.get("to") in targets]
    what = " + ".join(f"{a['fault']} on {a['target']}" for a in report["actions"])
    risks = [w["message"] for w in report["warnings"]]
    for t in targets:
        radius = (risk_targets.get(t) or {}).get("blast_radius") or []
        if radius:
            risks.append(f"{t}: {len(radius)} service(s) depend on it directly or transitively "
                         f"({', '.join(radius)})")
    body = {
        "objective": f"Measure how Ditto behaves under {what} and whether it recovers on its own.",
        "hypothesis": pred["statement"] + (f" Expected symptoms: {', '.join(pred['symptoms'])}."
                                           if pred["symptoms"] else ""),
        "target_reason": "; ".join(chosen["rationale"]),
        "expected_impact": ("Unknown -- there is no stored evidence for this scenario."
                            if pred["basis"] == "none" else
                            f"Based on {pred['basis_runs']} stored run(s): outcome '{pred['outcome_class']}'."),
        "expected_symptoms": pred["symptoms"] or pred.get("generic_symptoms", []),
        "metrics_to_observe": METRICS,
        "learning_value": (f"Ranked #{chosen['rank']} (score {chosen['score']}): novelty "
                           f"{chosen['components'].get('novelty')}, outcome uncertainty "
                           f"{chosen['components'].get('outcome_uncertainty')}."),
    }
    model_risks: list[str] = []
    if choice is not None:
        for k in ("objective", "hypothesis", "target_reason", "expected_impact", "learning_value"):
            if isinstance(choice.get(k), str) and choice[k].strip():
                body[k] = choice[k].strip()[:800]
        for k in ("expected_symptoms", "metrics_to_observe"):
            if _text_list(choice.get(k)):
                body[k] = _text_list(choice.get(k))
        model_risks = _text_list(choice.get("safety_risks"))
    scn = scn.model_copy(update={"hypothesis": body["hypothesis"][:1000]})
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": method, "fallback_note": note,
        **body,
        "safety_risks": risks + [r for r in model_risks if r not in risks],
        "scenario": scn.model_dump(), "validation": report,
        "execution": {"order": report["apply_order"], "rollback_order": report["rollback_order"],
                      "max_concurrent_faults": report["max_concurrent_faults"]},
        "dependencies": edges,
        "stop_conditions": stop_conditions(th),
        "preflight_requirements": ["Docker daemon reachable", "every target container running",
                                   "fresh, healthy baseline", "no recovery-required lockout",
                                   "scenario lock and per-target locks free"],
        "prediction": pred,
        "confidence": {"level": pred["confidence"], "basis": pred["basis"],
                       "basis_runs": pred["basis_runs"], "agreement": pred["agreement"]},
        "related_experiments": chosen["related_experiments"],
        "ranking": {"rank": chosen["rank"], "score": chosen["score"], "components": chosen["components"],
                    "rationale": chosen["rationale"]},
        "alternatives": [{"signature": c["signature"], "score": c["score"], "rank": c["rank"],
                          "blocked": c["blocked"], "rationale": c["rationale"]}
                         for c in ranked if c is not chosen][:6],
    }
