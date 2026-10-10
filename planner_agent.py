"""Experiment Planner Agent: proposes ONE candidate chaos experiment for a human to
review and (optionally) authorize. It never executes anything.

Hard safety rule, enforced in THIS module independent of the AI: a proposal's
`target` must be in guard.py's ALLOWLIST and its `fault` must be one guard.py
actually implements ("kill" or "delay"), and any "delay" params must fall inside
DelayFault's own bounds. If the AI returns anything outside those bounds -- or
malformed JSON, or a target that doesn't exist -- the WHOLE proposal is rejected and
this module falls back to a deterministic heuristic, exactly like resilience_agent.py
does for analysis. orchestrator.py re-validates the same constraints again at
authorization time; this is deliberate defense in depth, not trust in either layer
alone.

This module does not decide whether an experiment is SAFE TO RUN RIGHT NOW (that is
guard.py's/orchestrator's job, checking live health immediately before injection) --
it only decides what experiment would be USEFUL TO TRY, from dependency/health/history
context that may already be a bit stale by the time a human clicks "authorize".
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import ai_client
import guard

SCHEMA_VERSION = "1.0"
VALID_FAULTS = {"kill", "delay"}
VALID_RISKS = {"low", "medium", "high"}

# Bounds this module enforces on top of DelayFault's own (1..30000ms, 5..600s) range --
# kept tighter so an autonomous-looking proposal can't ask for something technically
# legal but needlessly long/disruptive for a first pass. These are deliberately more
# conservative than guard.py's hard limits.
DELAY_MS_BOUNDS = (50, 5000)
DELAY_TTL_BOUNDS = (10, 180)
MAX_DURATION_BOUNDS = (20, 180)
RECOVERY_TIMEOUT_BOUNDS = (30, 300)
PRE_S_DEFAULT = 10

REQUIRED_KEYS = {"fault", "target", "params", "risk", "hypothesis", "expected_impact",
                 "reason", "success_criteria"}


class PlannerError(Exception):
    """A proposal (AI or otherwise) failed validation. Callers should fall back to
    the deterministic planner rather than trust anything that raises this."""


def _clamp(value, lo, hi, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _validate_and_normalize(raw: dict) -> dict:
    """Raises PlannerError on anything unsafe or malformed. Returns a clean,
    bounded proposal dict -- never the raw AI output passed through untouched."""
    if not isinstance(raw, dict) or not REQUIRED_KEYS.issubset(raw.keys()):
        raise PlannerError(f"proposal missing required keys {REQUIRED_KEYS}")

    fault = raw.get("fault")
    if fault not in VALID_FAULTS:
        raise PlannerError(f"fault '{fault}' is not one of {sorted(VALID_FAULTS)}")

    target = raw.get("target")
    if target not in guard.ALLOWLIST:
        raise PlannerError(f"target '{target}' is not in guard.py's ALLOWLIST {sorted(guard.ALLOWLIST)}")

    risk = raw.get("risk")
    if risk not in VALID_RISKS:
        raise PlannerError(f"risk '{risk}' is not one of {sorted(VALID_RISKS)}")

    params_in = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    if fault == "delay":
        ms = _clamp(params_in.get("delay_ms"), *DELAY_MS_BOUNDS, DELAY_MS_BOUNDS[0])
        ttl = _clamp(params_in.get("ttl_s"), *DELAY_TTL_BOUNDS, DELAY_TTL_BOUNDS[0])
        params = {"delay_ms": int(ms), "ttl_s": int(ttl)}
    else:
        params = {}

    max_duration = _clamp(raw.get("max_duration_s"), *MAX_DURATION_BOUNDS, MAX_DURATION_BOUNDS[0])
    recovery_timeout = _clamp(raw.get("recovery_timeout_s"), *RECOVERY_TIMEOUT_BOUNDS,
                               RECOVERY_TIMEOUT_BOUNDS[1])

    return {
        "fault": fault, "target": target, "params": params, "risk": risk,
        "hypothesis": str(raw.get("hypothesis", ""))[:500],
        "expected_impact": str(raw.get("expected_impact", ""))[:500],
        "reason": str(raw.get("reason", ""))[:500],
        "success_criteria": str(raw.get("success_criteria", ""))[:500],
        "dependencies": [str(d)[:100] for d in raw.get("dependencies", [])][:10]
                        if isinstance(raw.get("dependencies"), list) else [],
        "max_duration_s": max_duration, "recovery_timeout_s": recovery_timeout,
        "pre_s": PRE_S_DEFAULT,
    }


def _context_block(dependency_map: dict | None, health: dict | None, baseline: dict | None,
                   past_experiments: list[dict]) -> str:
    allowlisted = [r for r in (dependency_map or {}).get("fault_target_recommendations", [])
                  if r.get("allowlisted_for_faults")]
    tried_targets = {e.get("target") for e in past_experiments}
    return json.dumps({
        "allowlisted_targets_with_calculated_risk": allowlisted,
        "current_health": health or "unknown",
        "has_baseline": baseline is not None,
        "recent_experiments": past_experiments,
        "targets_already_tried": sorted(t for t in tried_targets if t),
    }, indent=2)


def _ai_propose(dependency_map, health, baseline, past_experiments) -> dict | None:
    if not ai_client.ai_enabled():
        return None
    context = _context_block(dependency_map, health, baseline, past_experiments)
    prompt = f"""You are a chaos-engineering experiment planner for Eclipse Ditto. Propose exactly
ONE experiment. You may ONLY choose a target from allowlisted_targets_with_calculated_risk
below, and fault must be "kill" or "delay" -- nothing else exists. Prefer a target that
hasn't been tried yet (see targets_already_tried) and a hypothesis grounded in the
dependency fan-in/fan-out data given. Do not invent services, fault types, or numbers
not present in this context.

CONTEXT (JSON, the complete ground truth):
{context}

Respond with ONLY a JSON object matching exactly this shape:
{{
  "fault": "kill" | "delay",
  "target": "<one allowlisted service name>",
  "params": {{"delay_ms": <int, only if fault=="delay">, "ttl_s": <int, only if fault=="delay">}},
  "risk": "low" | "medium" | "high",
  "hypothesis": "what you expect to break and why, grounded in the dependency data",
  "expected_impact": "...",
  "reason": "why this experiment over the alternatives",
  "success_criteria": "what a 'the system handled this well' result looks like",
  "dependencies": ["<service names this target's failure could affect>"],
  "max_duration_s": <number, 20-180>,
  "recovery_timeout_s": <number, 30-300>
}}"""
    try:
        raw = ai_client.generate_json(prompt)
    except ai_client.AIUnavailable:
        return None
    try:
        proposal = _validate_and_normalize(raw)
    except PlannerError:
        return None
    return proposal


def _deterministic_propose(dependency_map, health, baseline, past_experiments) -> dict:
    """No AI: pick the highest-risk allowlisted target that hasn't been tried yet
    (explore unexplored failure modes first); if everything's been tried, pick the
    highest fan-in target again to re-verify behavior hasn't regressed. Fault type
    alternates with what was last tried on that target, defaulting to the cheaper,
    simpler 'kill' fault."""
    candidates = [r for r in (dependency_map or {}).get("fault_target_recommendations", [])
                 if r.get("allowlisted_for_faults")]
    if not candidates:
        candidates = [{"service": t, "fan_in": 0, "calculated_risk": "low"} for t in sorted(guard.ALLOWLIST)]

    tried_targets = {e.get("target") for e in past_experiments if e.get("target")}
    risk_rank = {"high": 2, "medium": 1, "low": 0}
    untried = [c for c in candidates if c["service"] not in tried_targets]
    pool = untried or candidates
    pool = sorted(pool, key=lambda c: (-risk_rank.get(c.get("calculated_risk"), 0), -c.get("fan_in", 0)))
    chosen = pool[0]
    target = chosen["service"]

    last_fault_on_target = next((e.get("fault") for e in past_experiments if e.get("target") == target), None)
    fault = "delay" if last_fault_on_target == "kill" else "kill"
    params = {"delay_ms": 500, "ttl_s": 30} if fault == "delay" else {}

    explored_note = "not yet tested this run" if target not in tried_targets else \
        "re-testing because every allowlisted target has already been tried at least once"
    return {
        "fault": fault, "target": target, "params": params,
        "risk": chosen.get("calculated_risk", "low"),
        "hypothesis": f"'{target}' has fan_in={chosen.get('fan_in', '?')} in the dependency map; "
                      f"a {fault} fault may degrade or error out services that depend on it.",
        "expected_impact": f"Direct/search latency and/or error rate may rise while '{target}' "
                           f"is impaired; dependents are: {chosen.get('reasoning', 'see dependency_map.json')}.",
        "reason": f"Deterministic fallback planner: {explored_note}, and it has "
                  f"calculated_risk='{chosen.get('calculated_risk')}'.",
        "success_criteria": "guard.py's kill switch detects any breach within its configured "
                            "thresholds, reverts, and the system returns to baseline health "
                            "within the recovery timeout.",
        "dependencies": [],
        "max_duration_s": 60, "recovery_timeout_s": 120, "pre_s": PRE_S_DEFAULT,
    }


def propose_experiment(dependency_map: dict | None, health: dict | None, baseline: dict | None,
                       past_experiments: list[dict] | None = None) -> dict:
    """Always returns a complete, validated, bounded proposal -- never raises, never
    an out-of-allowlist target, never params outside this module's bounds."""
    past_experiments = past_experiments or []
    ai_proposal = _ai_propose(dependency_map, health, baseline, past_experiments)
    body, method = (ai_proposal, "gemini") if ai_proposal is not None else (
        _deterministic_propose(dependency_map, health, baseline, past_experiments), "deterministic_fallback")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": method,
        **body,
    }


def rank_experiments(dependency_map: dict | None, history: list[dict], risk: dict | None = None,
                     policy: dict | None = None, limit: int = 12) -> list[dict]:
    """Ranked candidate experiments (schema 2.0): each with rank, targets, fault types, risk,
    hypothesis, reason, expected impact and its supporting history, every one validated
    against the fault registry, the allowlist, parameter bounds and the policy. Read-only.

    Ranking uses the dependency map, calculated service risk, experiment history, coverage
    gaps and expected information gain (learning.rank_candidates documents the formula).
    The work lives in learning.py / scenario_planner.py; this is the planner's entry point."""
    import learning
    import scenario_planner
    ranked = learning.rank_candidates(dependency_map, history, (policy or {}).get("allowed_faults"),
                                      (policy or {}).get("allowed_targets"), policy)
    return scenario_planner.propose_candidates(ranked, dependency_map, history, risk, policy, limit)


if __name__ == "__main__":
    import os
    import probe

    dep_map = None
    if os.path.isfile("dependency_map.json"):
        with open("dependency_map.json", encoding="utf-8") as f:
            dep_map = json.load(f)
    proposal = propose_experiment(dep_map, probe.current_health(), probe.load_baseline())
    print(json.dumps(proposal, indent=2))
