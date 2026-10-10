"""Agents and their coordinator.

Seven agents, each with one responsibility, a typed place in the lifecycle, and its own
persisted run status. They never call each other: the Coordinator invokes them in a fixed
order and hands each one the validated outputs of the earlier ones through `ctx`.

  plan pipeline       risk -> fault_injection
  post-run pipeline   recovery -> validation -> insights -> learning -> remediation

| agent            | kind          | decides                                              |
|------------------|---------------|------------------------------------------------------|
| risk             | deterministic | blast radius and under-tested services               |
| fault_injection  | hybrid (LLM picks among pre-validated candidates) | the scenario proposal |
| recovery         | deterministic | recovery state, one automatic retry, recovery report |
| validation       | deterministic | whether a run is conclusive enough to count as evidence |
| insights         | hybrid (LLM analysis, deterministic fallback)     | findings      |
| learning         | deterministic | knowledge-store update, prediction check, re-ranking |
| remediation      | hybrid (catalog + analyst text)                   | recommendations |

What agents can NOT do: nothing here executes a fault, issues a Docker command on behalf
of a model, changes a limit, or clears a lockout. Injection goes through
orchestrator -> scenario.ScenarioRun; stopping and rollback are scenario.py's own
deterministic code and run whether or not any agent, or the AI provider, is alive.

A failed, timed-out or skipped agent is recorded as exactly that. Later agents run with
whatever evidence exists; nothing is marked successful on another agent's behalf.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import ai_client
import config
import guard
import learning
import probe
import profiles
import remediation_planner
import resilience_agent
import scenario
import scenario_planner
import store
from events import bus

AGENT_SCHEMA_VERSION = "1.0"
MAX_OUTPUT_BYTES = 60_000

RECOVERY_STATES = ("RECOVERY_NOT_REQUIRED", "FAULTS_ACTIVE", "STOP_REQUESTED", "ROLLBACK_IN_PROGRESS",
                   "VERIFYING_RECOVERY", "RECOVERED", "HEALTH_NOT_CONFIRMED", "RECOVERY_REQUIRED")


def recovery_state(record: dict | None, stop_requested: bool = False) -> str:
    """Pure mapping from an experiment record to the recovery state machine."""
    if not record:
        return "RECOVERY_NOT_REQUIRED"
    st = record.get("state")
    if st == scenario.STATE_RECOVERY_REQUIRED or record.get("rollback_error"):
        return "RECOVERY_REQUIRED"
    if st == guard.STATE_REVERTING:
        return "ROLLBACK_IN_PROGRESS"
    if st == guard.STATE_VERIFYING_RECOVERY:
        return "VERIFYING_RECOVERY"
    if not record.get("injected"):
        return "RECOVERY_NOT_REQUIRED"
    if st in (guard.STATE_INJECTING, guard.STATE_MONITORING):
        return "STOP_REQUESTED" if (stop_requested or record.get("aborted")) else "FAULTS_ACTIVE"
    if record.get("rollback_verified") and record.get("recovery_outcome") == "recovered":
        return "RECOVERED"
    if record.get("rollback_verified"):
        return "HEALTH_NOT_CONFIRMED"
    return "RECOVERY_REQUIRED"


class Agent:
    name = "agent"
    title = "Agent"
    role = ""
    version = "1.0"
    kind = "deterministic"      # deterministic | hybrid
    timeout_s = 60.0
    retries = 0

    def run(self, ctx: dict) -> dict:
        raise NotImplementedError

    async def execute(self, ctx: dict) -> dict:
        return await asyncio.to_thread(self.run, ctx)

    def summary(self, out: dict) -> str:
        return ""

    def evidence_ids(self, ctx: dict, out: dict) -> list[str]:
        rec = ctx.get("record") or {}
        return [rec["id"]] if rec.get("id") else []

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "role": self.role, "version": self.version,
                "kind": self.kind, "timeout_s": self.timeout_s, "retries": self.retries}


# ---------------------------------------------------------------- 6. risk
class RiskAgent(Agent):
    name, title, kind = "risk", "Dependency & Risk", "deterministic"
    role = "Computes blast radius from the dependency graph and flags services with no stored evidence."

    def run(self, ctx: dict) -> dict:
        dep_map, history = ctx.get("dep_map") or {}, ctx.get("history") or []
        dependants: dict[str, set] = {}
        for e in dep_map.get("edges", []):
            if e.get("from") and e.get("to"):
                dependants.setdefault(e["to"], set()).add(e["from"])

        def radius(service: str) -> list[str]:
            seen, stack = set(), [service]
            while stack:
                for d in dependants.get(stack.pop(), ()):
                    if d not in seen and d != service:
                        seen.add(d)
                        stack.append(d)
            return sorted(seen)

        rated = {r.get("service"): r for r in dep_map.get("fault_target_recommendations", [])}
        ev = [c for c in history if c.get("evidence")]
        targets = {}
        for svc in sorted(guard.ALLOWLIST):
            runs = sum(1 for c in ev if svc in c.get("targets", []))
            targets[svc] = {"fan_in": (dep_map.get("fan_in") or {}).get(svc),
                            "fan_out": (dep_map.get("fan_out") or {}).get(svc),
                            "blast_radius": radius(svc), "calculated_risk": (rated.get(svc) or {}).get("calculated_risk"),
                            "evidence_runs": runs, "undertested": runs == 0}
        limits = [] if dep_map.get("edges") else ["no dependency map is available: blast radius is unknown, not zero"]
        fl = ctx.get("fleet") or {}
        enabled = sorted((fl.get("profiles") or {}))
        if not enabled:
            limits.append("no fleet description is available: product exposure is unknown")
        measured: dict[str, dict] = {}
        for c in ev:
            for k, o in (c.get("profile_outcomes") or {}).items():
                if o.get("status") == "affected":
                    for svc in c.get("targets", []):
                        measured.setdefault(svc, {}).setdefault(k, 0)
                        measured[svc][k] += 1
        for svc, t in targets.items():
            t["fleet_exposure"] = {
                "profiles": [] if svc == "connectivity" else enabled,
                "data_path": scenario_planner.FLEET_DATA_PATH.get(svc, "unknown"),
                "basis": "documented Ditto data path for this simulator (not measured)",
                "measured_affected_profiles": measured.get(svc, {}),
            }
        return {"targets": targets, "undertested": [s for s, t in targets.items() if t["undertested"]],
                "widest_blast_radius": sorted(targets, key=lambda s: -len(targets[s]["blast_radius"]))[:3],
                "fleet": {"profiles": fl.get("profiles"), "total_twins": fl.get("total_twins"),
                          "signature": fl.get("signature")},
                "limitations": limits}

    def summary(self, out: dict) -> str:
        return f"{len(out['undertested'])} of {len(out['targets'])} allowlisted services have no stored evidence"

    def evidence_ids(self, ctx, out):
        return []


# ---------------------------------------------------------------- 1. fault injection
class FaultInjectionAgent(Agent):
    name, title, kind = "fault_injection", "Fault Injection", "hybrid"
    role = ("Proposes the next scenario from ranked, pre-validated candidates. Execution is done by the "
            "orchestrator through the fault registry, never by this agent.")
    timeout_s = config.AI_REQUEST_TIMEOUT_S + 20

    def run(self, ctx: dict) -> dict:
        policy = ctx.get("policy")
        ranked = learning.rank_candidates(
            ctx.get("dep_map"), ctx.get("history") or [],
            allowed_faults=(policy or {}).get("allowed_faults"),
            allowed_targets=(policy or {}).get("allowed_targets"), policy=policy,
            pending_validation=ctx.get("pending_validation"))
        ctx["ranked"] = ranked
        return scenario_planner.propose_scenario(ranked, ctx.get("dep_map"), ctx.get("health"),
                                                 ctx.get("history") or [], ctx.get("outputs", {}).get("risk"), policy)

    def summary(self, out: dict) -> str:
        return f"{out['validation']['signature']} ({out['method']}, rank #{out['ranking']['rank']})"

    def evidence_ids(self, ctx, out):
        return list(out.get("related_experiments") or [])


# ---------------------------------------------------------------- 2. recovery
class RecoveryAgent(Agent):
    name, title, kind = "recovery", "Recovery & Safety", "deterministic"
    role = ("Reports what the deterministic rollback did for every fault and target, retries an "
            "unverified rollback once, and keeps the lockout in place if it still cannot be verified.")
    timeout_s = config.ROLLBACK_STEP_TIMEOUT_S * 4 + 30

    async def execute(self, ctx: dict) -> dict:
        record = ctx["record"]
        state = recovery_state(record)
        retry = None
        if state == "RECOVERY_REQUIRED":
            # One automatic retry through the same idempotent, verifying rollback code.
            retry = await scenario.recover_orphans()
            mine = [f for f in retry["failed"] if f.get("experiment_id") == record.get("id")]
            if not mine and retry["lockout"] is None:
                state = "RECOVERED"
                record["recovery_resolved_by"] = "automatic retry by the recovery agent"
        record["recovery_state"] = state
        acts = record.get("actions") or []
        applied = [a for a in acts if a.get("status") not in (None, "pending")]
        report = {
            "recovery_state": state,
            "faults_touched": len(applied),
            "faults_verified_removed": sum(1 for a in applied if a.get("rollback_verified")),
            "actions": [{"id": a.get("id"), "fault": a.get("fault"), "target": a.get("target"),
                         "status": a.get("status"), "rollback_verified": a.get("rollback_verified"),
                         "applied_at_s": a.get("applied_at_s"), "rolled_back_at_s": a.get("rolled_back_at_s"),
                         "error": a.get("rollback_error") or a.get("error")} for a in acts],
            "targets": record.get("targets") or {},
            "stop_reason": record.get("abort_reason"), "detected_s": record.get("detected_s"),
            "recovery_s": record.get("recovery_s"), "recovery_outcome": record.get("recovery_outcome"),
            "safety_events": record.get("safety_events") or [],
            "retry": retry, "lockout": scenario.read_lockout(),
            "instructions": record.get("recovery_instructions") or [],
            "mutation_blocked": scenario.read_lockout() is not None,
        }
        if not acts and record.get("injected") is not None:   # legacy single-fault record
            report["faults_touched"] = int(bool(record.get("injected")))
            report["faults_verified_removed"] = int(bool(record.get("rollback_verified")))
        impact = record.get("profile_impact")
        if impact:
            prof = {k: {"title": v.get("title"), "status": v.get("status"), "recovered": v.get("recovered"),
                        "fresh_ratio_after": ((v.get("recovery") or {}).get("fresh_ratio_mean")),
                        "notes": v.get("notes") or []}
                    for k, v in (impact.get("profiles") or {}).items()}
            report["profiles"] = prof
            report["profiles_not_recovered"] = [k for k, v in prof.items() if v["recovered"] is False]
            report["profile_recovery_basis"] = ("the last two recovery ticks of each profile: fresh-twin ratio back "
                                                "within 0.1 of its pre-fault level and no lost updates")
        else:
            report["profiles"] = None
            report["profile_recovery_basis"] = "no per-profile measurements in this record (legacy car-only run)"
        record["recovery_report"] = report
        return report

    def summary(self, out: dict) -> str:
        return (f"{out['recovery_state']}: {out['faults_verified_removed']} of {out['faults_touched']} "
                f"fault(s) verified removed")


# ---------------------------------------------------------------- 7. validation
class ValidationAgent(Agent):
    name, title, kind = "validation", "Experiment Validation", "deterministic"
    role = ("Judges whether a run is conclusive. Only conclusive runs count as evidence for prediction, "
            "ranking and recommendation tracking.")
    MIN_FAULT_TICKS = 3

    def run(self, ctx: dict) -> dict:
        record = ctx["record"]
        timeline = record.get("timeline") or []
        fault_rows = [r for r in timeline if r.get("phase") == "fault"]
        rec_rows = [r for r in timeline if r.get("phase") == "recovery"]
        uncertain = [a for a in record.get("actions") or [] if a.get("status") == "uncertain" or a.get("error")]
        checks = [
            {"check": "a fault was really injected (not a dry run)",
             "ok": bool(record.get("injected")) and not record.get("dry_run") and not record.get("simulated"),
             "fatal": True},
            {"check": "a valid baseline was recorded", "ok": bool(record.get("baseline")), "fatal": False},
            {"check": f"at least {self.MIN_FAULT_TICKS} probe ticks during the fault",
             "ok": len(fault_rows) >= self.MIN_FAULT_TICKS, "fatal": False},
            {"check": "every fault applied cleanly (none in an uncertain state)", "ok": not uncertain, "fatal": False},
            {"check": "rollback was verified", "ok": bool(record.get("rollback_verified")), "fatal": False},
            {"check": "post-recovery health was measured",
             "ok": bool(rec_rows) and record.get("recovery_outcome") in ("recovered", "timeout"), "fatal": False},
        ]
        if any(c["fatal"] and not c["ok"] for c in checks):
            verdict = "invalid"
        elif all(c["ok"] for c in checks):
            verdict = "conclusive"
        else:
            verdict = "inconclusive"
        cmp_ = learning.compare(record.get("prediction"), learning.observe({**record, "validation": None}))
        supported = cmp_.get("outcome_match") if cmp_.get("evaluated") else None
        returned = None
        if rec_rows:
            returned = record.get("recovery_outcome") == "recovered"
        out = {"verdict": verdict, "counts_as_evidence": verdict == "conclusive",
               "checks": [{"check": c["check"], "ok": c["ok"]} for c in checks],
               "failed_checks": [c["check"] for c in checks if not c["ok"]],
               "hypothesis_supported": supported, "hypothesis_check": cmp_,
               "returned_to_baseline": returned, "fault_ticks": len(fault_rows)}
        record["validation"] = out
        return out

    def summary(self, out: dict) -> str:
        return f"{out['verdict']}" + (f" ({len(out['failed_checks'])} check(s) failed)" if out["failed_checks"] else "")


# ---------------------------------------------------------------- 3. insights
class InsightsAgent(Agent):
    name, title, kind = "insights", "Insights & Root Cause", "hybrid"
    role = ("Turns the measured before/fault/recovery windows into findings, separating what was "
            "measured from what is hypothesised, and compares the outcome with the prediction and with history.")
    timeout_s = config.AI_REQUEST_TIMEOUT_S + 20

    def run(self, ctx: dict) -> dict:
        record, history = ctx["record"], ctx.get("history") or []
        analysis = resilience_agent.analyze_experiment(record, ctx.get("dep_map"))
        timeline = [r for r in record.get("timeline") or [] if isinstance(r, dict)]
        windows = {}
        for phase in ("before", "fault", "recovery"):
            rows = [r for r in timeline if r.get("phase") == phase]
            windows[phase] = None if not rows else {
                "from_ts": rows[0].get("ts"), "to_ts": rows[-1].get("ts"), **probe.summarize(rows)}
        fault_win = windows.get("fault") or {}
        targets = sorted({a["target"] for a in learning.actions_of(record) if a["target"]})
        effects = learning.observe(record)
        product = analysis.get("product_impact") or {}
        for i, f in enumerate(analysis["findings"]):
            measured = f.get("confidence") == "supported_by_measurements"
            f.update({"id": f"F{i + 1}", "basis": "measured" if measured else "hypothesis",
                      "affected_services": targets,
                      "affected_profiles": [f["profile"]] if f.get("scope") == "product"
                      else product.get("affected_profiles"),
                      "interval": {"from_ts": fault_win.get("from_ts"), "to_ts": fault_win.get("to_ts")}})
        limitations = []
        if not record.get("baseline"):
            limitations.append("no baseline was recorded, so before/during comparisons are unavailable")
        if windows["fault"] is None:
            limitations.append("no probe ticks were recorded while a fault was active")
        elif windows["fault"]["lost_acked_max"] is None:
            limitations.append("lost-update detection had no data (the simulator's state file was not readable)")
        if windows["recovery"] is None and record.get("injected"):
            limitations.append("post-recovery behaviour was not measured")
        limitations.append("only gateway-level direct-read and search probes are measured; per-service "
                           "internal metrics are not collected, so any root cause below the API is a hypothesis")
        if not product.get("available"):
            limitations.append("no per-product-profile measurements exist for this run, so no product category "
                               "can be said to be affected or unaffected")
        elif product.get("not_measured_profiles"):
            limitations.append("twin data could not be read during the fault for: "
                               + ", ".join(product["not_measured_profiles"])
                               + " -- their product-level impact is unknown, not zero")
        sig = learning.signature_of(record)
        similar = [{"id": c["id"], "signature": c["signature"], "outcome_class": c["outcome_class"],
                    "symptoms": c["symptoms"], "same_scenario": c["signature"] == sig}
                   for c in history if c.get("evidence") and c.get("id") != record.get("id")
                   and (c["signature"] == sig or set(c.get("targets", [])) & set(targets))][-6:]
        analysis.update({
            "windows": windows, "observed": {"outcome_class": effects["outcome_class"],
                                             "symptoms": effects["symptoms"], "effects": effects},
            "hypothesis": record.get("hypothesis"), "prediction": record.get("prediction"),
            "hypothesis_check": learning.compare(record.get("prediction"), effects),
            "similar_experiments": similar, "limitations": limitations,
            "validation": record.get("validation"),
        })
        resilience_agent.save_report(analysis)
        return analysis

    def summary(self, out: dict) -> str:
        return f"{len(out['findings'])} finding(s), severity {out['severity']} ({out['analysis_method']})"


# ---------------------------------------------------------------- 4. learning
class LearningAgent(Agent):
    name, title, kind = "learning", "Learning & Optimization", "deterministic"
    role = ("Stores the run in the knowledge base, checks the earlier prediction against the outcome, "
            "re-ranks candidate experiments, and re-tests implemented recommendations.")

    def run(self, ctx: dict) -> dict:
        record, analysis = ctx["record"], ctx.get("outputs", {}).get("insights")
        policy = ctx.get("policy")
        before = ctx.get("history") or []
        ranked_before = ctx.get("ranked") or learning.rank_candidates(
            ctx.get("dep_map"), before, (policy or {}).get("allowed_faults"),
            (policy or {}).get("allowed_targets"), policy, ctx.get("pending_validation"))
        case = store.upsert_experiment(record, analysis, ctx.get("record_path"))
        evaluated = store.evaluate_recommendations(case)
        after = store.history()
        ranked_after = learning.rank_candidates(
            ctx.get("dep_map"), after, (policy or {}).get("allowed_faults"),
            (policy or {}).get("allowed_targets"), policy, store.pending_validation_signatures())
        rec = learning.build_learning_record(record, analysis, before, ranked_before, ranked_after)
        rec["profiles"] = _profile_learning(case)
        n = sum(1 for c in after if c.get("evidence"))
        rec.update({
            "validation_verdict": (record.get("validation") or {}).get("verdict"),
            "evaluation_status": ("evaluated" if rec["comparison"].get("evaluated")
                                  else "not_evaluated: " + rec["comparison"].get("reason", "")),
            "recommendations_evaluated": evaluated,
            "next_candidate": ({"signature": ranked_after[0]["signature"], "score": ranked_after[0]["score"],
                                "rationale": ranked_after[0]["rationale"]} if ranked_after else None),
            "dataset": {"evidence_runs": n, "limitation": learning.MODEL_STATUS["reason"]},
        })
        store.save_learning(rec)
        ctx["ranked_after"] = ranked_after
        return rec

    def summary(self, out: dict) -> str:
        inf = out["influence"]
        return (f"{'evidence stored' if out['is_evidence'] else 'no evidence'}; score of {inf['signature']} "
                f"{inf['score_before']} -> {inf['score_after']}")


def _profile_learning(case: dict) -> dict:
    outcomes = case.get("profile_outcomes")
    if not outcomes:
        return {"available": False, "reason": case.get("legacy_fleet") or "no per-profile measurements"}
    shared = set(case.get("system_wide_symptoms") or [])
    return {"available": True, "system_wide_symptoms": sorted(shared),
            "product_specific": {k: sorted(set(o.get("symptoms") or []) - shared) for k, o in outcomes.items()
                                 if set(o.get("symptoms") or []) - shared},
            "affected_profiles": case.get("affected_profiles") or [],
            "not_measured_profiles": [k for k, o in outcomes.items() if o.get("status") == "not_measured"],
            "counts_as_evidence": case.get("evidence")}


# ---------------------------------------------------------------- 5. remediation
# Engineering patterns, keyed by a keyword found in a finding's title. Text is generic by
# design; the evidence and the re-test procedure attached to each recommendation are specific.
REMEDIATION_CATALOG = [
    ("lost acknowledged", {
        "title": "Validate write durability before acknowledging",
        "change": "Audit the write path so an update is acknowledged only after it is durably persisted; add an "
                  "end-to-end consistency check that compares acknowledged sequence numbers with stored ones.",
        "benefit": "Removes silent loss of acknowledged updates.", "complexity": "high",
        "risks": "Stricter acknowledgement adds write latency; needs load testing."}),
    ("rollback", {
        "title": "Restore the affected target manually and review the rollback path",
        "change": "Follow the recovery instructions for this experiment, then re-run the scenario's preflight.",
        "benefit": "Returns the environment to a known state so testing can continue.", "complexity": "low",
        "risks": "None beyond the manual steps themselves."}),
    ("recovery not verified", {
        "title": "Add readiness checks and graceful restart handling",
        "change": "Expose a readiness probe that reflects dependency connectivity and make dependants wait or "
                  "retry with exponential backoff and jitter instead of failing permanently after a restart.",
        "benefit": "Shorter, more predictable recovery after a dependency returns.", "complexity": "medium",
        "risks": "Retries without a cap can amplify load during recovery; bound attempts."}),
    ("search availability", {
        "title": "Degrade search gracefully and isolate it behind a circuit breaker",
        "change": "Put a circuit breaker with a short timeout in front of search calls and return a clear "
                  "'search temporarily unavailable' response (or fall back to direct reads) instead of erroring.",
        "benefit": "A search outage stops propagating to callers that do not need search.", "complexity": "medium",
        "risks": "Fallback reads can be more expensive; a mis-tuned breaker can flap."}),
    ("latency", {
        "title": "Tune timeouts and add backpressure on the slow path",
        "change": "Set explicit request timeouts below the caller's own deadline, bound connection pools and "
                  "queues, and shed or queue load when the dependency slows down.",
        "benefit": "Slow dependencies cause bounded latency instead of thread/connection exhaustion.",
        "complexity": "medium", "risks": "Timeouts that are too tight turn slowness into errors."}),
    ("staleness", {
        "title": "Bound update lag and alert on it",
        "change": "Add an alert on twin staleness and buffer or apply backpressure to inbound updates while a "
                  "dependency is impaired so they are replayed rather than delayed indefinitely.",
        "benefit": "Stale twins are detected early and catch up after recovery.", "complexity": "medium",
        "risks": "Buffers need a size bound and a drop policy."}),
    ("freshness", {
        "title": "Protect telemetry freshness for the affected product category",
        "change": "Alert on the per-profile fresh-twin ratio, give this category's update path a bounded "
                  "retry with backoff on the device/gateway side, and replay buffered updates in order once the "
                  "dependency recovers instead of dropping them.",
        "benefit": "Twins of this product stay current, or are visibly flagged as stale, during a dependency "
                   "slowdown.", "complexity": "medium",
        "risks": "Replaying a large backlog can overload the recovering service; rate-limit replay."}),
    ("data-quality", {
        "title": "Validate product telemetry at ingestion",
        "change": "Apply this profile's schema and consistency rules where updates enter the platform (or in a "
                  "WoT thing model / validation hook) and reject or quarantine inconsistent updates.",
        "benefit": "Inconsistent device data is caught at the edge rather than by downstream consumers.",
        "complexity": "medium", "risks": "Strict validation can reject legitimate edge cases; log rejects."}),
    ("search drift", {
        "title": "Monitor and bound search-index lag per product category",
        "change": "Track search lag per product category and make listing views fall back to direct reads, or "
                  "show an 'index catching up' notice, while drift is above the tolerance.",
        "benefit": "Fleet listings stay correct while the search index catches up.", "complexity": "low",
        "risks": "Fallback reads cost more; limit them to the affected category."}),
    ("no significant degradation", {
        "title": "Increase test strength or add observability",
        "change": "The system tolerated this fault at this intensity. Re-test with a longer duration or higher "
                  "intensity inside the configured limits, or add per-service metrics to see smaller effects.",
        "benefit": "Distinguishes real resilience from a test that was too gentle to show anything.",
        "complexity": "low", "risks": "None; limits are enforced by the policy engine."}),
]
_DEFAULT_PATTERN = {
    "title": "Investigate and harden the affected dependency path",
    "change": "Review timeout, retry (exponential backoff with jitter) and health-check behaviour between the "
              "affected services.",
    "benefit": "Qualitative: reduces the impact observed in this experiment.", "complexity": "medium",
    "risks": "Depends on the change chosen; verify with the re-test below."}
_SEV = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class RemediationAgent(Agent):
    name, title, kind = "remediation", "Recommendation & Remediation", "hybrid"
    role = ("Maps each finding to a concrete engineering change with its evidence, risk and a re-test "
            "procedure. Never applies anything.")

    def run(self, ctx: dict) -> dict:
        record, analysis = ctx["record"], ctx.get("outputs", {}).get("insights")
        if not analysis:
            raise AgentSkipped("no findings are available (the insights agent did not produce an analysis)")
        learn = ctx.get("outputs", {}).get("learning") or {}
        sig = learning.signature_of(record)
        prior = [c for c in ctx.get("history") or [] if c.get("evidence") and c["signature"] == sig]
        llm = analysis.get("analysis_method") != "deterministic_fallback"
        recs = []
        for i, f in enumerate(analysis.get("findings", [])):
            title = (f.get("title") or "").lower()
            pattern = next((p for key, p in REMEDIATION_CATALOG if key in title), _DEFAULT_PATTERN)
            recurrence = sum(1 for c in prior if f.get("title") in (c.get("finding_titles") or []))
            recs.append({
                "id": f"R-{record.get('id')}-{i + 1}", "index": i,
                "title": pattern["title"], "finding_title": f.get("title"),
                "description": f.get("impact"), "severity": f.get("severity"),
                "supporting_findings": [f.get("id")], "evidence": f.get("evidence"),
                "evidence_basis": f.get("basis"),
                "proposed_change": pattern["change"],
                "analyst_suggestion": f.get("recommended_fix"),
                "analyst_suggestion_source": "llm" if llm else "deterministic rule",
                "expected_benefit": pattern["benefit"] + " (qualitative; no measurement supports a number)",
                "complexity": pattern["complexity"], "risks": pattern["risks"],
                "verification_steps": [
                    f"Apply the change outside this tool, then mark this recommendation 'implemented'.",
                    f"Re-run scenario '{sig}'.",
                    f"Verified if the finding '{f.get('title')}' does not recur; marked not effective if it does."],
                "recurrence": {"prior_runs_with_same_finding": recurrence, "prior_runs": len(prior)},
                "category": remediation_planner.classify(f), "status": "proposed", "auto_applied": False,
                "product_context": _product_context(f, analysis),
            })
            if f.get("scope") == "product":
                recs[-1]["verification_steps"][-1] += (f" Check the '{f.get('profile')}' row of the per-profile "
                                                       f"impact table on the follow-up run.")
        order = sorted(recs, key=lambda r: (_SEV.get(r["severity"], 9), -r["recurrence"]["prior_runs_with_same_finding"]))
        for n, r in enumerate(order):
            r["priority"] = n + 1
        plan = remediation_planner.build_plan(analysis)     # legacy markdown plan, same item order
        remediation_planner.save_plan(plan)
        store.upsert_recommendations(record.get("id"), sig, recs)
        return {"experiment_id": record.get("id"), "signature": sig, "recommendations": recs,
                "none_auto_applied": True, "learning_reference": learn.get("experiment_id"),
                "policy_note": plan["policy_note"]}

    def summary(self, out: dict) -> str:
        return f"{len(out['recommendations'])} recommendation(s), none applied automatically"


def _product_context(finding: dict, analysis: dict) -> dict:
    """Which product categories a recommendation concerns, and on what basis."""
    product = analysis.get("product_impact") or {}
    if finding.get("scope") == "product":
        key = finding.get("profile")
        prof = profiles.REGISTRY.get(key)
        return {"scope": "product", "profiles": [key],
                "title": prof.title if prof else key,
                "domain_failure_modes": prof.failure_modes if prof else [],
                "shared_with_all_profiles": finding.get("shared_with_all_profiles"),
                "basis": "measured impact on this profile; failure modes are registered domain knowledge"}
    if not product.get("available"):
        return {"scope": "system", "profiles": None, "basis": "no per-profile measurements for this run"}
    return {"scope": "system", "profiles": product.get("affected_profiles") or [],
            "basis": "system-level finding; profiles listed are those with measured impact in this run"}


class AgentSkipped(Exception):
    """Raised by an agent that cannot do meaningful work with the evidence available."""


# ---------------------------------------------------------------- coordinator
class Coordinator:
    PLAN = ("risk", "fault_injection")
    POST_RUN = ("recovery", "validation", "insights", "learning", "remediation")

    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self._live: dict[str, dict] = {}

    def register(self, agent: Agent) -> None:
        self.agents[agent.name] = agent
        self._live[agent.name] = {"state": "idle", "task": None, "since": None, "last": None,
                                  "last_success_utc": None, "failures": 0, "runs": 0}

    # ---- status ----
    def status(self) -> list[dict]:
        out = []
        for name, agent in self.agents.items():
            live = self._live[name]
            recent: list[dict] = []
            try:
                recent = [{k: v for k, v in r.items() if k != "output"}
                          for r in store.agent_runs(agent=name, limit=5)]
            except store.StoreError:
                pass
            out.append({**agent.describe(), **live, "recent": recent,
                        "provider": ai_client.model_info() if agent.kind == "hybrid" else None})
        return out

    def _publish(self, run: dict) -> None:
        slim = {k: v for k, v in run.items() if k != "output"}
        bus.publish("agent", slim)

    # ---- one invocation: never raises ----
    async def invoke(self, name: str, ctx: dict, experiment_id: str | None = None,
                     proposal_id: str | None = None, input_ref: str | None = None) -> dict:
        agent = self.agents[name]
        live = self._live[name]
        run = {"schema_version": AGENT_SCHEMA_VERSION, "id": f"run-{uuid.uuid4().hex[:12]}",
               "agent": name, "title": agent.title, "version": agent.version, "kind": agent.kind,
               "experiment_id": experiment_id, "proposal_id": proposal_id,
               "input_ref": input_ref or experiment_id or proposal_id, "output_ref": None,
               "status": "running", "attempts": 0, "started_at_utc": guard.utc_now_iso(),
               "finished_at_utc": None, "duration_ms": None, "error": None, "summary": None,
               "method": None, "evidence_ids": [],
               "provider": ai_client.model_info() if agent.kind == "hybrid" else None, "output": None}
        live.update(state="running", task=experiment_id or proposal_id, since=run["started_at_utc"])
        live["runs"] += 1
        self._publish(run)
        t0 = time.monotonic()
        out = None
        for attempt in range(agent.retries + 1):
            run["attempts"] = attempt + 1
            try:
                out = await asyncio.wait_for(agent.execute(ctx), timeout=agent.timeout_s)
                run["status"], run["error"] = "completed", None
                break
            except AgentSkipped as e:
                run["status"], run["error"] = "skipped", str(e)
                break
            except asyncio.TimeoutError:
                run["status"], run["error"] = "timeout", f"no result within {agent.timeout_s:g}s"
            except asyncio.CancelledError:
                run["status"], run["error"] = "failed", "cancelled"
                self._finish(run, live, t0, None, ctx)
                raise
            except Exception as e:  # noqa: BLE001 -- an agent failure is data, not a crash
                run["status"], run["error"] = "failed", f"{type(e).__name__}: {e}"[:600]
        self._finish(run, live, t0, out, ctx)
        return run

    def _finish(self, run: dict, live: dict, t0: float, out: dict | None, ctx: dict) -> None:
        agent = self.agents[run["agent"]]
        run["finished_at_utc"] = guard.utc_now_iso()
        run["duration_ms"] = int((time.monotonic() - t0) * 1000)
        if out is not None:
            method = out.get("method") or out.get("analysis_method")
            run["method"] = method
            if agent.kind == "hybrid" and method == "deterministic_fallback" and ai_client.ai_enabled():
                run["status"] = "fallback"     # worked, but not with the model it was meant to use
            try:
                run["summary"] = agent.summary(out)[:300]
                run["evidence_ids"] = [e for e in agent.evidence_ids(ctx, out) if e][:20]
            except Exception as e:  # noqa: BLE001
                run["summary"] = f"(summary unavailable: {e})"
            blob = json.dumps(out, default=str)
            run["output"] = out if len(blob) <= MAX_OUTPUT_BYTES else {
                "truncated": True, "bytes": len(blob), "keys": sorted(out)[:40]}
            run["output_ref"] = f"agent_runs/{run['id']}"
            ctx.setdefault("outputs", {})[run["agent"]] = out
        ok = run["status"] in ("completed", "fallback")
        live.update(state={"completed": "idle", "fallback": "idle", "skipped": "idle"}.get(run["status"], "failed"),
                    task=None, since=None,
                    last={k: run[k] for k in ("id", "status", "summary", "error", "experiment_id",
                                              "finished_at_utc", "duration_ms", "method", "attempts")})
        if ok:
            live["last_success_utc"] = run["finished_at_utc"]
        elif run["status"] != "skipped":
            live["failures"] += 1
        try:
            store.save_agent_run(json.loads(json.dumps(run, default=str)))
            store.add_timeline("agent", {"agent": run["agent"], "title": run["title"], "status": run["status"],
                                         "summary": run["summary"], "error": run["error"],
                                         "duration_ms": run["duration_ms"], "run_id": run["id"]},
                               run["experiment_id"], run["proposal_id"])
        except store.StoreError as e:
            print(f"[agents] could not persist run of '{run['agent']}': {e}", flush=True)
        self._publish(run)

    def set_state(self, name: str, state: str, task: str | None = None) -> None:
        """Live-only state for an agent whose work is being done by deterministic code
        elsewhere (e.g. recovery 'monitoring' while a scenario runs)."""
        if name in self._live:
            self._live[name].update(state=state, task=task, since=guard.utc_now_iso() if task else None)
            bus.publish("agent", {"agent": name, "status": state, "experiment_id": task, "live_only": True})

    # ---- pipelines ----
    async def plan(self, ctx: dict, proposal_id: str) -> dict:
        runs = [await self.invoke(n, ctx, proposal_id=proposal_id) for n in self.PLAN]
        return {"proposal": ctx.get("outputs", {}).get("fault_injection"), "runs": runs}

    async def post_run(self, ctx: dict) -> dict:
        exp_id = (ctx.get("record") or {}).get("id")
        runs = []
        for name in self.POST_RUN:
            runs.append(await self.invoke(name, ctx, experiment_id=exp_id))
            if ctx.get("after_agent"):
                ctx["after_agent"](name, runs[-1])
        return {"outputs": ctx.get("outputs", {}), "runs": runs}


coordinator = Coordinator()
for _agent in (FaultInjectionAgent(), RecoveryAgent(), InsightsAgent(), LearningAgent(), RemediationAgent(),
               RiskAgent(), ValidationAgent()):
    coordinator.register(_agent)
