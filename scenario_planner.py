"""Scenario proposals (schema 2.0) -- the planning half of the Fault Injection Agent.

Two entry points, both READ-ONLY (no Docker, no lock, nothing is authorized or started):

  propose_candidates()  the ranked list of candidate experiments, each one validated
                        against the fault registry, the allowlist, parameter bounds and
                        the policy, with its risk, hypothesis, reason and evidence.
  propose_scenario()    one full proposal. The model may pick ONE of the ranked
                        candidates and adjust numeric parameters, which are validated
                        again; anything invalid falls back to the top-ranked candidate.
  proposal_for()        the full proposal for a candidate an operator selected.

The model never invents a scenario. Stop conditions and preflight requirements always
come from configuration, never from model output. Selecting or proposing is not
authorizing: a run still needs scenario.Authorization from controller.py.

planner_agent.py (schema 1.0, single fault) is kept for the legacy CLI path and
re-exports rank_experiments() from here.
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
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}


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


def _target_risk(targets: list[str], dependency_map: dict | None, risk: dict | None) -> tuple[str, list[str]]:
    """Highest calculated risk among the targets, and the union of their blast radius.
    'unknown' when the dependency map has no rating -- never guessed."""
    rated = {r.get("service"): r.get("calculated_risk")
             for r in (dependency_map or {}).get("fault_target_recommendations", [])}
    risk_targets = (risk or {}).get("targets") or {}
    levels = [rated.get(t) or (risk_targets.get(t) or {}).get("calculated_risk") for t in targets]
    known = [lv for lv in levels if lv in _RISK_RANK]
    level = max(known, key=lambda lv: _RISK_RANK[lv]) if known else "unknown"
    radius = sorted({s for t in targets for s in ((risk_targets.get(t) or {}).get("blast_radius") or [])}
                    - set(targets))
    return level, radius


def _narrative(cand: dict, report: dict, pred: dict, level: str, radius: list[str]) -> dict:
    """Deterministic text for one candidate, built only from counts and stored outcomes."""
    what = " + ".join(f"{a['fault']} on {a['target']}" for a in report["actions"])
    return {
        "objective": f"Measure how Ditto behaves under {what} and whether it recovers on its own.",
        "hypothesis": pred["statement"] + (f" Expected symptoms: {', '.join(pred['symptoms'])}."
                                           if pred["symptoms"] else ""),
        "reason": "; ".join(cand["rationale"]),
        "expected_impact": (
            ("Unknown: there is no stored evidence for this scenario."
             if pred["basis"] == "none" else
             f"Based on {pred['basis_runs']} stored run(s): outcome '{pred['outcome_class']}'.")
            + (f" A failure here can reach {len(radius)} dependent service(s): {', '.join(radius)}."
               if radius else "")),
        "expected_symptoms": pred["symptoms"] or pred.get("generic_symptoms", []),
        "risk": level,
    }


def propose_candidates(ranked: list[dict], dependency_map: dict | None, history: list[dict],
                       risk: dict | None = None, policy: dict | None = None, limit: int = 12) -> list[dict]:
    """The ranked candidate list for the dashboard. Every entry is re-validated here; an
    entry that fails, or that is blocked by its history, is still listed -- with the reason
    and `selectable: false` -- so the operator can see why it is not offered."""
    out = []
    for cand in ranked[:limit]:
        pred = learning.predict(cand["actions"], history)
        entry = {"rank": cand["rank"], "signature": cand["signature"], "actions": cand["actions"],
                 "targets": sorted({a["target"] for a in cand["actions"]}),
                 "faults": sorted({a["fault"] for a in cand["actions"]}),
                 "score": cand["score"], "components": cand["components"], "runs": cand["runs"],
                 "blocked": bool(cand.get("blocked")), "selectable": False, "not_selectable_reason": None,
                 "evidence": {"basis": pred["basis"], "basis_runs": pred["basis_runs"],
                              "agreement": pred["agreement"], "confidence": pred["confidence"],
                              "experiments": pred["basis_experiments"]},
                 "prediction": pred}
        level, radius = _target_risk(entry["targets"], dependency_map, risk)
        try:
            scn = build_scenario(cand, None, False, policy)
            report = scenario.validate(scn, dependency_map, policy)
        except Exception as e:  # noqa: BLE001 -- an unbuildable candidate is reported, never offered
            entry.update(risk=level, hypothesis="", reason="; ".join(cand["rationale"]), expected_impact="",
                         validation_errors=[f"{type(e).__name__}: {e}"[:300]],
                         not_selectable_reason="could not be built into a valid scenario")
            out.append(entry)
            continue
        text = _narrative(cand, report, pred, level, radius)
        entry.update(risk=text["risk"], hypothesis=text["hypothesis"], reason=text["reason"],
                     expected_impact=text["expected_impact"], expected_symptoms=text["expected_symptoms"],
                     blast_radius=radius, scenario=scn.model_dump(),
                     warnings=[w["message"] for w in report["warnings"]],
                     validation_errors=[e["message"] for e in report["errors"]],
                     cumulative_intensity=report["cumulative_intensity"])
        if entry["blocked"]:
            entry["not_selectable_reason"] = cand["rationale"][0]
        elif not report["ok"]:
            entry["not_selectable_reason"] = "refused by policy: " + "; ".join(entry["validation_errors"])
        else:
            entry["selectable"] = True
        out.append(entry)
    return out


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


def _assemble(chosen: dict, scn: scenario.Scenario, report: dict, ranked: list[dict],
              dependency_map: dict | None, risk: dict | None, method: str, note: str | None,
              choice: dict | None) -> dict:
    th = guard.Thresholds.from_env()
    pred = chosen["prediction"]
    targets = report["affected_services"]
    risk_targets = (risk or {}).get("targets") or {}
    edges = [f"{e['from']}->{e['to']}" for e in (dependency_map or {}).get("edges", [])
             if e.get("from") in targets or e.get("to") in targets]
    level, radius = _target_risk(targets, dependency_map, risk)
    text = _narrative(chosen, report, pred, level, radius)
    risks = [w["message"] for w in report["warnings"]]
    for t in targets:
        r = (risk_targets.get(t) or {}).get("blast_radius") or []
        if r:
            risks.append(f"{t}: {len(r)} service(s) depend on it directly or transitively ({', '.join(r)})")
    body = {
        "objective": text["objective"], "hypothesis": text["hypothesis"], "target_reason": text["reason"],
        "expected_impact": text["expected_impact"], "expected_symptoms": text["expected_symptoms"],
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
        "risk": level,
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


def proposal_for(signature: str, ranked: list[dict], dependency_map: dict | None, history: list[dict],
                 risk: dict | None = None, policy: dict | None = None) -> dict:
    """Full proposal for the candidate an operator selected from the ranked list. Validated
    again here. Raises PlannerError if it is unknown, blocked or refused by policy."""
    chosen = next((c for c in ranked if c["signature"] == signature), None)
    if chosen is None:
        raise PlannerError(f"'{signature}' is not in the current ranked candidate list; rank again")
    if chosen.get("blocked"):
        raise PlannerError(f"'{signature}' is blocked: {chosen['rationale'][0]}")
    chosen["prediction"] = learning.predict(chosen["actions"], history)
    scn = build_scenario(chosen, None, False, policy)
    report = scenario.validate(scn, dependency_map, policy)
    if not report["ok"]:
        raise PlannerError(f"'{signature}' is refused by policy: "
                           + "; ".join(e["message"] for e in report["errors"]))
    return _assemble(chosen, scn, report, ranked, dependency_map, risk, "operator_selected",
                     "selected by an operator from the ranked candidate list", None)


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
    return _assemble(chosen, scn, report, ranked, dependency_map, risk, method, note, choice)
