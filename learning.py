"""What the platform learns from its own experiment history -- deterministic, countable,
and inspectable. There is no trained model here and nothing in this file calls an LLM.

"Learning" means exactly four mechanisms, all computed from stored experiment records:

  observe()   reduces one record to measured EFFECTS (latency ratios, error rates, which
              symptom labels were crossed, outcome class). Direct evidence only.
  predict()   states what a candidate is expected to do, from prior runs of the same
              signature (or, failing that, of the same fault / same target), and says how
              many runs that rests on.
  compare()   checks a prediction against what was then observed.
  rank_candidates()  orders possible next experiments by a documented score built from
              sample counts, so stored outcomes change what is proposed next.

Dry-run / simulated records and runs where nothing was injected are never evidence.
A lightweight statistical model is intentionally NOT trained: see MODEL_STATUS.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime

import config
import faults
import guard
import scenario

# Symptom thresholds (direct comparisons of measured values; documented in the README).
LATENCY_RATIO = 1.5          # during p95 >= 1.5x baseline p95
ERROR_RATE_DELTA = 0.05      # error rate rose by >= 5 points
STALE_DELTA_S = 5.0
SLOW_RECOVERY_S = 60.0

INFORMATIVE = {"degraded", "kill_switch", "data_loss"}
REPEAT_WINDOW_S = 7 * 86400
MIN_RUNS_FOR_MODEL = 50

WEIGHTS = {"novelty": 0.30, "outcome_uncertainty": 0.20, "usefulness": 0.20,
           "relevance": 0.15, "follow_up": 0.15, "repetition_penalty": -0.40}

MODEL_STATUS = {
    "trained_model": False,
    "method": "frequency statistics over stored experiment outcomes",
    "reason": f"No statistical model is fitted. Training is only justified with at least "
              f"{MIN_RUNS_FOR_MODEL} real (non-simulated, injected) runs per fault family and a held-out "
              f"validation split; until then predictions are plain counts with their sample size shown.",
}


def actions_of(record: dict) -> list[dict]:
    """Fault actions of a v1 (single fault) or v2 (scenario) record, in one shape."""
    acts = record.get("actions")
    if isinstance(acts, list) and acts:
        return [{"fault": a.get("fault"), "target": a.get("target"), "params": a.get("params") or {},
                 "status": a.get("status")} for a in acts if isinstance(a, dict)]
    if record.get("fault") and record.get("target"):
        return [{"fault": record["fault"], "target": record["target"],
                 "params": record.get("params") or {}, "status": None}]
    return []


def signature_of(record: dict) -> str:
    return record.get("signature") or scenario.signature(actions_of(record))


def is_evidence(record: dict) -> bool:
    """A run counts as evidence only if a fault was really injected AND, when the
    Validation Agent has judged it, the verdict was 'conclusive'. Records from before
    that agent existed have no verdict and are judged on the injected flag alone."""
    if not record.get("injected") or record.get("dry_run") or record.get("simulated"):
        return False
    verdict = (record.get("validation") or {}).get("verdict")
    return verdict in (None, "conclusive")


def _p95(block: dict | None, key: str):
    v = ((block or {}).get(key) or {}).get("p95")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def observe(record: dict) -> dict:
    """Measured effects of one record. Every number is read from the record; a missing
    measurement stays None and produces no symptom."""
    base, during = record.get("baseline") or {}, record.get("during") or {}
    evidence = is_evidence(record)

    def ratio(key):
        b, d = _p95(base, key), _p95(during, key)
        return round(d / b, 2) if b and d is not None else None

    def delta(key):
        b, d = _num(base.get(key)), _num(during.get(key))
        return None if d is None else round(d - (b or 0), 4)

    eff = {
        "evidence": evidence,
        "direct_latency_ratio": ratio("direct_ms"), "search_latency_ratio": ratio("search_ms"),
        "direct_error_delta": delta("direct_error_rate"), "search_error_delta": delta("search_error_rate"),
        "stale_delta_s": delta("stale_p95_s"),
        "lost_acked_max": _num(during.get("lost_acked_max")),
        "search_drift_max": _num(during.get("search_drift_max")),
        "kill_switch": bool(record.get("aborted")) and "stop" not in (record.get("abort_reason") or "").lower()
                       and "cancel" not in (record.get("abort_reason") or "").lower(),
        "abort_reason": record.get("abort_reason"), "detected_s": _num(record.get("detected_s")),
        "recovered": record.get("recovery_outcome") == "recovered",
        "recovery_s": _num(record.get("recovery_s")),
        "rollback_verified": bool(record.get("rollback_verified")),
        "rollback_error": record.get("rollback_error"),
    }
    symptoms = []
    if evidence:
        if (eff["direct_latency_ratio"] or 0) >= LATENCY_RATIO:
            symptoms.append("direct_latency")
        if (eff["search_latency_ratio"] or 0) >= LATENCY_RATIO:
            symptoms.append("search_latency")
        if (eff["direct_error_delta"] or 0) >= ERROR_RATE_DELTA:
            symptoms.append("direct_errors")
        if (eff["search_error_delta"] or 0) >= ERROR_RATE_DELTA:
            symptoms.append("search_errors")
        if (eff["stale_delta_s"] or 0) >= STALE_DELTA_S:
            symptoms.append("staleness")
        if (eff["lost_acked_max"] or 0) > 0:
            symptoms.append("data_loss")
        if (eff["search_drift_max"] or 0) > 0:
            symptoms.append("search_drift")
        if record.get("recovery_outcome") == "timeout":
            symptoms.append("no_recovery")
        elif (eff["recovery_s"] or 0) >= SLOW_RECOVERY_S:
            symptoms.append("slow_recovery")
    eff["symptoms"] = sorted(symptoms)

    if not evidence:
        outcome = "no_evidence"
    elif record.get("rollback_error") or record.get("state") == scenario.STATE_RECOVERY_REQUIRED:
        outcome = "rollback_failure"
    elif "data_loss" in symptoms:
        outcome = "data_loss"
    elif eff["kill_switch"]:
        outcome = "kill_switch"
    elif symptoms:
        outcome = "degraded"
    else:
        outcome = "tolerated"
    eff["outcome_class"] = outcome
    return eff


def to_case(record: dict, analysis: dict | None = None) -> dict:
    """Compact history entry: what the store keeps per experiment and what ranking reads."""
    eff = observe(record)
    acts = actions_of(record)
    return {
        "id": record.get("id"), "kind": record.get("kind") or "single",
        "signature": signature_of(record), "actions": acts,
        "faults": sorted({a["fault"] for a in acts if a["fault"]}),
        "targets": sorted({a["target"] for a in acts if a["target"]}),
        "state": record.get("state"), "started_at_utc": record.get("started_at_utc"),
        "ended_at_utc": record.get("ended_at_utc"),
        "simulated": bool(record.get("dry_run") or record.get("simulated")),
        "evidence": eff["evidence"], "outcome_class": eff["outcome_class"],
        # Tracked separately from `evidence`: a run with an unverified rollback is usually
        # judged inconclusive, but it must still block automatic re-selection.
        "rollback_failure": bool(record.get("injected")) and not record.get("dry_run") and bool(
            record.get("rollback_error") or record.get("state") == scenario.STATE_RECOVERY_REQUIRED),
        "symptoms": eff["symptoms"], "effects": eff,
        "mode": record.get("mode"), "source": record.get("source"),
        "severity": (analysis or {}).get("severity"),
        "finding_titles": [f.get("title") for f in (analysis or {}).get("findings", [])
                           if isinstance(f, dict)],
    }


def _ts(case: dict) -> float:
    try:
        return datetime.fromisoformat(case.get("started_at_utc") or "").timestamp()
    except ValueError:
        return 0.0


def _evidence(history: list[dict]) -> list[dict]:
    return [c for c in history if c.get("evidence")]


# ---------------------------------------------------------------- prediction
def predict(actions: list[dict], history: list[dict]) -> dict:
    sig = scenario.signature(actions)
    ev = _evidence(history)
    direct = [c for c in ev if c["signature"] == sig]
    basis, kind = direct, "direct"
    if not basis:
        keys = {(a["fault"], a["target"]) for a in actions}
        faults_, targets = {k[0] for k in keys}, {k[1] for k in keys}
        basis = [c for c in ev if set(c["faults"]) & faults_ and set(c["targets"]) & targets]
        kind = "analogy" if basis else "none"
        if not basis:
            basis = [c for c in ev if set(c["faults"]) & faults_ or set(c["targets"]) & targets]
            kind = "analogy" if basis else "none"
    n = len(basis)
    if n == 0:
        hints = sorted({s for a in actions for s in (faults.get_spec(a["fault"]).expected_symptoms
                                                     if faults.get_spec(a["fault"]) else ())})
        return {"signature": sig, "basis": "none", "basis_runs": 0, "basis_experiments": [],
                "outcome_class": None, "symptoms": [], "agreement": None,
                "confidence": "none",
                "statement": "No stored run of this fault/target exists. The symptoms listed are the "
                             "fault type's generic description, not a prediction.",
                "generic_symptoms": hints}
    outcomes = Counter(c["outcome_class"] for c in basis)
    outcome, agree = outcomes.most_common(1)[0]
    sym_counts = Counter(s for c in basis for s in c["symptoms"])
    symptoms = sorted(s for s, k in sym_counts.items() if k * 2 >= n)
    if kind == "direct":
        confidence = "high" if n >= 3 and agree / n >= 0.67 else ("medium" if n >= 2 else "low")
    else:
        confidence = "low"
    return {"signature": sig, "basis": kind, "basis_runs": n,
            "basis_experiments": [c["id"] for c in sorted(basis, key=_ts, reverse=True)[:8]],
            "outcome_class": outcome, "symptoms": symptoms, "agreement": f"{agree} of {n}",
            "confidence": confidence,
            "statement": (f"{agree} of {n} prior run(s) of the same scenario ended '{outcome}'."
                          if kind == "direct" else
                          f"No prior run of this exact scenario; {agree} of {n} related run(s) "
                          f"(same fault or same target) ended '{outcome}'."),
            "generic_symptoms": []}


def compare(prediction: dict | None, effects: dict) -> dict:
    if not effects.get("evidence"):
        return {"evaluated": False, "reason": "nothing was injected, so there is no outcome to compare"}
    if not prediction or prediction.get("basis") in (None, "none"):
        return {"evaluated": False, "reason": "no prior evidence existed, so no prediction was made",
                "observed_outcome": effects["outcome_class"], "observed_symptoms": effects["symptoms"]}
    pred, obs = set(prediction.get("symptoms") or []), set(effects["symptoms"])
    union = pred | obs
    return {"evaluated": True,
            "predicted_outcome": prediction.get("outcome_class"), "observed_outcome": effects["outcome_class"],
            "outcome_match": prediction.get("outcome_class") == effects["outcome_class"],
            "confirmed_symptoms": sorted(pred & obs), "unexpected_symptoms": sorted(obs - pred),
            "predicted_but_absent": sorted(pred - obs),
            "symptom_mismatch": f"{len(pred ^ obs)} of {len(union)}" if union else "0 of 0",
            "basis": prediction.get("basis"), "basis_runs": prediction.get("basis_runs")}


# ---------------------------------------------------------------- candidates and ranking
def _candidate_space(dep_map: dict | None, allowed_faults: list[str], allowed_targets: list[str],
                     policy: dict | None) -> list[dict]:
    targets = [t for t in allowed_targets if t in guard.ALLOWLIST]
    usable = [f for f in allowed_faults if f in faults.REGISTRY]
    out = []
    for t in sorted(targets):
        for f in sorted(usable):
            out.append([{"fault": f, "target": t, "params": {}}])
    if config.MAX_AFFECTED_SERVICES >= 2 and (policy or {}).get("max_actions", 2) >= 2:
        net = [f for f in ("delay", "loss", "jitter") if f in usable]
        proc = [f for f in ("kill", "pause") if f in usable]
        seen = set()
        for e in (dep_map or {}).get("edges", []):
            a, b = e.get("from"), e.get("to")
            if a not in targets or b not in targets or a == b or (a, b) in seen:
                continue
            seen.add((a, b))
            if len(net) >= 1:   # slow dependency while its dependant is also impaired
                out.append([{"fault": net[0], "target": b, "params": {}},
                            {"fault": net[-1] if len(net) > 1 else net[0], "target": a, "params": {}}])
            if net and proc:    # dependant down while the dependency is slow
                out.append([{"fault": net[0], "target": b, "params": {}},
                            {"fault": proc[0], "target": a, "params": {}}])
    valid = []
    for actions in out:
        scn = scenario.Scenario(name=scenario.signature(actions), actions=actions,
                                max_duration_s=min(60, (policy or {}).get("max_duration_s", 60)))
        report = scenario.validate(scn, dep_map, policy)
        if report["ok"]:
            valid.append({"actions": [{"fault": a["fault"], "target": a["target"], "params": a["params"]}
                                      for a in report["actions"]],
                          "signature": report["signature"], "intensity": report["cumulative_intensity"]})
    return valid


def rank_candidates(dep_map: dict | None, history: list[dict], allowed_faults: list[str] | None = None,
                    allowed_targets: list[str] | None = None, policy: dict | None = None,
                    pending_validation: set[str] | None = None, now: float | None = None) -> list[dict]:
    """Score every valid candidate. Higher is 'more worth running next'. Every component
    is derived from stored counts; the formula is WEIGHTS above."""
    now = now or time.time()
    allowed_faults = allowed_faults or sorted(faults.REGISTRY)
    allowed_targets = allowed_targets or sorted(guard.ALLOWLIST)
    pending_validation = pending_validation or set()
    ev = _evidence(history)
    fan_in = (dep_map or {}).get("fan_in") or {}
    max_fan = max(fan_in.values(), default=0) or 1

    ranked = []
    for cand in _candidate_space(dep_map, allowed_faults, allowed_targets, policy):
        sig = cand["signature"]
        same = sorted((c for c in ev if c["signature"] == sig), key=_ts)
        n = len(same)
        if any(c.get("rollback_failure") for c in history if c.get("signature") == sig):
            ranked.append({**cand, "score": None, "blocked": True, "components": {}, "runs": n,
                           "rationale": ["A previous run of this scenario ended with an unverified rollback; "
                                         "it is excluded from automatic selection until an operator reviews it."],
                           "related_experiments": [c["id"] for c in same][-5:]})
            continue
        informative = sum(1 for c in same if c["outcome_class"] in INFORMATIVE)
        p = (informative + 1) / (n + 2)
        c_faults = {a["fault"] for a in cand["actions"]}
        c_targets = {a["target"] for a in cand["actions"]}
        related = [c for c in ev if c["signature"] != sig
                   and (set(c["faults"]) & c_faults or set(c["targets"]) & c_targets)]
        rel_inf = sum(1 for c in related if c["outcome_class"] in INFORMATIVE)
        follow = sig in pending_validation
        last_two = [c["outcome_class"] for c in same[-2:]]
        recent = bool(same) and now - _ts(same[-1]) < REPEAT_WINDOW_S
        repeat = n >= 2 and len(set(last_two)) == 1 and recent and not follow
        comp = {
            "novelty": round(1 / (1 + n), 3),
            "outcome_uncertainty": round(4 * p * (1 - p), 3),
            "usefulness": round((rel_inf + 1) / (len(related) + 2), 3),
            "relevance": round(sum(fan_in.get(t, 0) for t in c_targets) / (max_fan * len(c_targets)), 3),
            "follow_up": 1.0 if follow else 0.0,
            "repetition_penalty": 1.0 if repeat else 0.0,
        }
        score = round(sum(WEIGHTS[k] * v for k, v in comp.items()), 4)
        why = [f"{n} stored run(s) of this exact scenario" + (f", {informative} informative" if n else "")]
        why.append(f"{rel_inf} of {len(related)} related run(s) (same fault or target) exposed a weakness")
        if follow:
            why.append("an approved recommendation from an earlier run of this scenario is waiting to be re-tested")
        if repeat:
            why.append(f"the last two runs both ended '{last_two[-1]}' within 7 days -- repeating adds little")
        ranked.append({**cand, "score": score, "blocked": False, "components": comp, "runs": n,
                       "rationale": why, "related_experiments": [c["id"] for c in (same + related)[-5:]]})
    ranked.sort(key=lambda c: (c["blocked"], -(c["score"] or 0), c["signature"]))
    for i, c in enumerate(ranked):
        c["rank"] = i + 1
    return ranked


def influence(sig: str, before: list[dict], after: list[dict]) -> dict:
    """How storing one experiment changed the ranking -- the visible effect of 'learning'."""
    def find(ranked):
        return next((c for c in ranked if c["signature"] == sig), None)
    b, a = find(before), find(after)
    top_b = before[0]["signature"] if before else None
    top_a = after[0]["signature"] if after else None
    return {"signature": sig,
            "score_before": b["score"] if b else None, "score_after": a["score"] if a else None,
            "rank_before": b["rank"] if b else None, "rank_after": a["rank"] if a else None,
            "top_candidate_before": top_b, "top_candidate_after": top_a,
            "top_candidate_changed": top_b != top_a}


def build_learning_record(record: dict, analysis: dict | None, history_before: list[dict],
                          ranked_before: list[dict], ranked_after: list[dict]) -> dict:
    case = to_case(record, analysis)
    eff = case["effects"]
    prediction = record.get("prediction") or predict(case["actions"], history_before)
    comparison = compare(prediction, eff)
    prior = [c for c in _evidence(history_before) if c["signature"] == case["signature"]]
    if not eff["evidence"]:
        new_evidence = "None: no fault was injected, so this run adds no evidence about the system."
    else:
        new_evidence = (f"Run {len(prior) + 1} of '{case['signature']}': outcome '{case['outcome_class']}'"
                        + (f", symptoms {case['symptoms']}" if case["symptoms"] else ", no symptom threshold crossed")
                        + (f", recovered in {eff['recovery_s']}s" if eff["recovery_s"] is not None else ""))
    return {"experiment_id": case["id"], "created_at_utc": guard.utc_now_iso(),
            "signature": case["signature"], "is_evidence": eff["evidence"],
            "new_evidence": new_evidence, "observed": eff, "prediction": prediction,
            "comparison": comparison, "related_experiments": [c["id"] for c in prior][-8:],
            "influence": influence(case["signature"], ranked_before, ranked_after)}


def summarize(history: list[dict]) -> dict:
    """Aggregate, per signature and per fault/target, of what has repeatedly happened."""
    ev = _evidence(history)
    by_sig: dict[str, dict] = {}
    for c in sorted(ev, key=_ts):
        s = by_sig.setdefault(c["signature"], {"signature": c["signature"], "runs": 0, "outcomes": Counter(),
                                               "symptoms": Counter(), "recovery_s": [], "last_run_utc": None,
                                               "experiments": []})
        s["runs"] += 1
        s["outcomes"][c["outcome_class"]] += 1
        s["symptoms"].update(c["symptoms"])
        if c["effects"].get("recovery_s") is not None:
            s["recovery_s"].append(c["effects"]["recovery_s"])
        s["last_run_utc"] = c["started_at_utc"]
        s["experiments"].append(c["id"])
    patterns = []
    for s in by_sig.values():
        rec = s["recovery_s"]
        patterns.append({"signature": s["signature"], "runs": s["runs"], "outcomes": dict(s["outcomes"]),
                         "recurring_symptoms": {k: v for k, v in s["symptoms"].items() if v >= 2},
                         "symptoms": dict(s["symptoms"]),
                         "median_recovery_s": sorted(rec)[len(rec) // 2] if rec else None,
                         "last_run_utc": s["last_run_utc"], "experiments": s["experiments"][-8:]})
    patterns.sort(key=lambda p: -p["runs"])
    all_singles = {f"{f}@{t}" for f in faults.REGISTRY for t in guard.ALLOWLIST}
    covered = {sig for sig in by_sig if "+" not in sig} & all_singles
    return {"total_records": len(history), "evidence_runs": len(ev),
            "simulated_or_not_injected": len(history) - len(ev),
            "single_fault_coverage": {"covered": len(covered), "possible": len(all_singles)},
            "patterns": patterns, "model": MODEL_STATUS, "weights": WEIGHTS}


# ---------------------------------------------------------------- before / after comparison
# (key, label, unit, absolute tolerance, relative tolerance). Lower is better for all of
# them. A change smaller than max(abs, rel * |before|) is reported as "unchanged".
COMPARE_METRICS = [
    ("direct_latency_p95_ms", "Direct read latency p95 (during fault)", "ms", 5.0, 0.10),
    ("search_latency_p95_ms", "Search latency p95 (during fault)", "ms", 5.0, 0.10),
    ("direct_error_rate", "Direct read error rate (during fault)", "ratio", 0.02, 0.0),
    ("search_error_rate", "Search error rate (during fault)", "ratio", 0.02, 0.0),
    ("stale_p95_s", "Twin staleness p95 (during fault)", "s", 0.5, 0.10),
    ("lost_acked_max", "Lost acknowledged updates (max)", "count", 0.0, 0.0),
    ("search_drift_max", "Search drift (max twins)", "count", 0.0, 0.0),
    ("recovery_s", "Recovery time after rollback", "s", 2.0, 0.10),
]
CORRELATION_CAVEAT = ("Observed correlation, not proof of causation: this compares one run before the change "
                      "with one run after it. Load, timing and anything else that differed between the two "
                      "runs can also explain a difference.")


def _metric_value(record: dict, key: str):
    during = record.get("during") or {}
    if key == "direct_latency_p95_ms":
        return _p95(during, "direct_ms")
    if key == "search_latency_p95_ms":
        return _p95(during, "search_ms")
    if key == "recovery_s":
        return _num(record.get("recovery_s"))
    return _num(during.get(key))


def compare_runs(original: dict, followup: dict) -> dict:
    """Metric-by-metric comparison of an original run and its follow-up. A measurement that
    is missing on either side is 'not_comparable' with the reason -- never treated as zero."""
    reasons = []
    for label, rec in (("original", original), ("follow-up", followup)):
        if rec.get("dry_run") or rec.get("simulated"):
            reasons.append(f"the {label} run was a dry run")
        elif not rec.get("injected"):
            reasons.append(f"no fault was injected in the {label} run")
    sig_a, sig_b = signature_of(original), signature_of(followup)
    if sig_a != sig_b:
        reasons.append(f"the scenarios differ ('{sig_a}' versus '{sig_b}')")
    pa = {f"{a['fault']}@{a['target']}": a["params"] for a in actions_of(original)}
    pb = {f"{a['fault']}@{a['target']}": a["params"] for a in actions_of(followup)}
    param_diffs = []
    for key in sorted(set(pa) & set(pb)):
        for name in sorted((set(pa[key]) | set(pb[key])) - {"ttl_s", "iface"}):
            if pa[key].get(name) != pb[key].get(name):
                param_diffs.append(f"{key}.{name}: {pa[key].get(name)} -> {pb[key].get(name)}")
    comparable = not reasons

    metrics = []
    for key, label, unit, abs_tol, rel_tol in COMPARE_METRICS:
        before, after = _metric_value(original, key), _metric_value(followup, key)
        row = {"key": key, "label": label, "unit": unit, "before": before, "after": after,
               "change": None, "change_pct": None, "verdict": "not_comparable", "reason": None}
        if not comparable:
            row["reason"] = "the two runs are not comparable"
        elif key == "recovery_s" and (before is None or after is None):
            ta, tb = original.get("recovery_outcome"), followup.get("recovery_outcome")
            if ta == "timeout" and tb == "recovered":
                row.update(verdict="improved", reason="the original did not recover within its timeout; the "
                                                      "follow-up recovered")
            elif ta == "recovered" and tb == "timeout":
                row.update(verdict="regressed", reason="the original recovered; the follow-up did not recover "
                                                       "within its timeout")
            else:
                row["reason"] = (f"recovery time was not measured in the "
                                 f"{'original' if before is None else 'follow-up'} run "
                                 f"(recovery outcome: {ta if before is None else tb})")
        elif before is None or after is None:
            missing = "both runs" if before is None and after is None else \
                "the original run" if before is None else "the follow-up run"
            row["reason"] = f"not measured in {missing}"
        else:
            delta = after - before
            tol = max(abs_tol, rel_tol * abs(before))
            row["change"] = round(delta, 4)
            row["change_pct"] = round(delta / before * 100, 1) if before else None
            row["verdict"] = "improved" if delta < -tol else "regressed" if delta > tol else "unchanged"
            row["tolerance"] = round(tol, 4)
        metrics.append(row)

    def outcome(rec):
        return {"id": rec.get("id"), "state": rec.get("state"), "outcome_class": observe(rec)["outcome_class"],
                "aborted": bool(rec.get("aborted")), "abort_reason": rec.get("abort_reason"),
                "recovery_outcome": rec.get("recovery_outcome"),
                "fault_ticks": sum(1 for r in rec.get("timeline") or [] if r.get("phase") == "fault"),
                "baseline_source": rec.get("baseline_source"),
                "baseline_direct_p95_ms": _p95(rec.get("baseline"), "direct_ms")}
    counts = Counter(m["verdict"] for m in metrics)
    return {"original_id": original.get("id"), "followup_id": followup.get("id"),
            "signature": sig_a, "comparable": comparable, "not_comparable_reasons": reasons,
            "parameter_differences": param_diffs, "metrics": metrics,
            "summary": {k: counts.get(k, 0) for k in ("improved", "regressed", "unchanged", "not_comparable")},
            "outcomes": {"original": outcome(original), "followup": outcome(followup)},
            "caveat": CORRELATION_CAVEAT}
