// Views of one experiment's documents, shared by the control, history and insights pages.
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { ActionState, AgentRun, Analysis, LearningRecord, Recommendation, TimelineEvent, ValidationReport } from "../api/types";
import { useState } from "react";
import { useAction } from "../hooks";
import { RerunPanel } from "./followup";
import { DASH, clock, duration, humanize, num } from "../lib/format";
import { Badge, Button, Empty, ErrorNote, KV, List, Mono, Note, StateBadge } from "./ui";

export function ActionsTable({ actions }: { actions: ActionState[] | undefined }) {
  if (!actions?.length) return <Empty>No fault actions recorded.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] border-collapse text-xs">
        <thead>
          <tr>
            {["", "Fault", "Target", "Parameters", "Applied", "Removed", "Status", "Removal verified"].map((h) => (
              <th key={h} className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {actions.map((a) => (
            <tr key={a.id}>
              <td className="td num text-faint">{a.id}</td>
              <td className="td text-ink">{a.fault}</td>
              <td className="td">{a.target}</td>
              <td className="td num text-dim">{Object.entries(a.params).map(([k, v]) => `${k}=${v}`).join(" ") || DASH}</td>
              <td className="td num">{num(a.applied_at_s, 1, "s")}</td>
              <td className="td num">{num(a.rolled_back_at_s, 1, "s")}</td>
              <td className="td">
                <StateBadge
                  state={a.status === "rolled_back" ? "completed" : a.status === "rollback_failed" ? "failed" : a.status === "previewed" ? "muted" : a.status}
                  label={a.status === "previewed" ? "previewed, not applied" : a.status.replace(/_/g, " ")}
                />
                {(a.rollback_error || a.error) && <div className="mt-1 max-w-xs text-xxs text-bad">{a.rollback_error || a.error}</div>}
              </td>
              <td className="td">{a.status === "pending" ? DASH : a.status === "previewed" ? <span className="text-faint">nothing to remove</span> : a.rollback_verified ? <Badge tone="ok">verified</Badge> : <Badge tone="bad">not verified</Badge>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PlanReport({ report }: { report: ValidationReport }) {
  return (
    <div className="space-y-3">
      {report.errors.length > 0 && (
        <Note tone="bad">
          <div className="font-medium text-ink">Refused by policy</div>
          <List items={report.errors.map((e) => (e.action !== null ? `action ${e.action + 1}: ${e.message}` : e.message))} />
        </Note>
      )}
      {report.warnings.length > 0 && (
        <Note tone="warn">
          <List items={report.warnings.map((w) => w.message)} />
        </Note>
      )}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] border-collapse text-xs">
          <thead>
            <tr>
              {["", "Fault", "Target", "Window", "Intensity", "Commands that would run"].map((h) => (
                <th key={h} className="th">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {report.actions.map((a) => (
              <tr key={a.id}>
                <td className="td num text-faint">{a.id}</td>
                <td className="td text-ink">{a.fault}</td>
                <td className="td">{a.target}</td>
                <td className="td num">{a.end_s === null ? DASH : `${a.start_offset_s}s → ${a.end_s}s`}</td>
                <td className="td num">{num(a.intensity, 2)}</td>
                <td className="td">
                  {a.commands.length ? a.commands.map((c) => <div key={c} className="num text-xxs text-dim">{c}</div>) : <span className="text-faint">{DASH}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <KV
        items={[
          ["Affected services", `${report.affected_services.join(", ") || DASH} (limit ${report.limits.max_affected_services})`],
          ["Concurrent faults", `${report.max_concurrent_faults} (limit ${report.limits.max_active_faults})`],
          ["Cumulative intensity", `${report.cumulative_intensity} (budget ${report.limits.max_cumulative_intensity})`],
          ["Apply order", report.apply_order.join(" → ")],
          ["Rollback order", `${report.rollback_order.join(" → ")} (most depended-on service first)`],
        ]}
      />
    </div>
  );
}

export function Findings({ analysis }: { analysis: Analysis }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <StateBadge state={analysis.severity} />
        <Badge tone={analysis.analysis_method === "deterministic_fallback" ? "muted" : "accent"}>
          {analysis.analysis_method === "deterministic_fallback" ? "rule-based analysis" : `model: ${analysis.analysis_method}`}
        </Badge>
        <span className="text-dim">{analysis.summary}</span>
      </div>
      {analysis.findings.map((f, i) => (
        <div key={f.id ?? i} className="rounded border border-line bg-raised p-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <Mono>{f.id ?? `F${i + 1}`}</Mono>
            <span className="font-medium text-ink">{f.title}</span>
            <StateBadge state={f.severity} />
            <Badge tone={f.basis === "measured" ? "ok" : "warn"}>{f.basis === "measured" ? "measured" : "hypothesis"}</Badge>
            {f.scope === "product" ? <Badge tone="accent">product: {f.profile}</Badge> : f.scope === "system" ? <Badge>system-level</Badge> : null}
          </div>
          <div className="mt-2">
            <KV
              items={[
                [f.basis === "measured" ? "Measured evidence" : "Stated evidence", <span className="num">{f.evidence}</span>],
                ["Impact", f.impact],
                ["Services", f.affected_services?.join(", ")],
                ["Product categories", f.scope === "product" ? `${f.profile}${f.shared_with_all_profiles ? " (symptom shared by every measured category: service-level)" : " (specific to this category)"}` : f.affected_profiles?.length ? `measured impact on: ${f.affected_profiles.join(", ")}` : null],
                ["Confidence", humanize(f.confidence)],
              ]}
            />
          </div>
        </div>
      ))}
      {analysis.limitations && analysis.limitations.length > 0 && (
        <Note>
          <div className="mb-1 font-medium text-ink">Limits of this analysis</div>
          <List items={analysis.limitations} />
        </Note>
      )}
    </div>
  );
}

export function LearningCard({ rec }: { rec: LearningRecord }) {
  const c = rec.comparison;
  const inf = rec.influence;
  return (
    <div className="space-y-3 text-xs">
      <KV
        items={[
          ["New evidence", rec.new_evidence],
          ["Counts as evidence", rec.is_evidence ? <Badge tone="ok">yes</Badge> : <Badge tone="warn">no ({rec.validation_verdict ?? "not injected"})</Badge>],
          ["Predicted beforehand", rec.prediction.basis === "none" ? "No prediction: there was no prior run to base one on." : `${rec.prediction.statement} Symptoms: ${rec.prediction.symptoms.join(", ") || "none"}.`],
          [
            "Observed",
            c.evaluated ? (
              <span>
                outcome <span className="text-ink">{c.observed_outcome}</span> {c.outcome_match ? <Badge tone="ok">matched prediction</Badge> : <Badge tone="warn">differed from prediction</Badge>}
                {c.unexpected_symptoms?.length ? ` · unexpected: ${c.unexpected_symptoms.join(", ")}` : ""}
                {c.predicted_but_absent?.length ? ` · predicted but absent: ${c.predicted_but_absent.join(", ")}` : ""}
              </span>
            ) : (
              <span className="text-dim">Not evaluated: {c.reason}</span>
            ),
          ],
          ["Related runs", rec.related_experiments.length ? rec.related_experiments.map((id) => <Link key={id} className="num mr-2 text-accent hover:underline" to={`/history/${id}`}>{id}</Link>) : "none"],
          [
            "Effect on ranking",
            inf.score_before === null && inf.score_after === null ? (
              <span className="text-dim">This scenario is not in the planner's candidate set (it was authored by an operator), so it has no score of its own. Its outcome still feeds the "usefulness" of related candidates.</span>
            ) : (
              <span className="num">
                {inf.signature}: score {num(inf.score_before, 3)} {"→"} {num(inf.score_after, 3)}, rank {inf.rank_before ?? DASH} {"→"} {inf.rank_after ?? DASH}
              </span>
            ),
          ],
          ["Next candidate", rec.next_candidate ? <span><span className="num text-ink">{rec.next_candidate.signature}</span>{inf.top_candidate_changed ? " (changed by this run)" : " (unchanged)"}</span> : DASH],
        ]}
      />
      {rec.dataset && <p className="text-xxs text-faint">Dataset: {rec.dataset.evidence_runs} evidence run(s). {rec.dataset.limitation}</p>}
    </div>
  );
}

const NEXT: Record<string, { status: string; label: string }[]> = {
  proposed: [{ status: "accepted", label: "Accept" }, { status: "rejected", label: "Reject" }],
  accepted: [{ status: "implemented", label: "Mark implemented" }, { status: "rejected", label: "Reject" }],
  implemented: [{ status: "accepted", label: "Not implemented after all" }],
  rejected: [{ status: "proposed", label: "Reopen" }],
  verified: [],
  not_effective: [{ status: "accepted", label: "Reopen" }],
};

function productContext(c: NonNullable<Recommendation["item"]["product_context"]>): string {
  if (c.scope === "product") {
    const modes = c.domain_failure_modes?.length ? ` Domain context (not measured): ${c.domain_failure_modes.join("; ")}.` : "";
    return `${c.title ?? c.profiles?.join(", ")}: ${c.basis}.${modes}`;
  }
  return c.profiles?.length ? `System-level; measured impact on ${c.profiles.join(", ")}.` : `System-level. ${c.basis}.`;
}

function RecRow({ rec, onChanged }: { rec: Recommendation; onChanged: () => void }) {
  const act = useAction((status: string) => api.setRecommendation(rec.experiment_id, rec.index, status), onChanged);
  const [rerun, setRerun] = useState(false);
  const canRerun = rec.status !== "rejected" && rec.status !== "verified";
  const it = rec.item;
  return (
    <div className="rounded border border-line bg-raised p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Mono>P{it.priority ?? DASH}</Mono>
          <span className="font-medium text-ink">{rec.title}</span>
          <StateBadge state={rec.severity} />
          <StateBadge state={rec.status} />
        </div>
        <div className="flex gap-2">
          {(NEXT[rec.status] ?? []).map((n) => (
            <Button key={n.status} busy={act.pending} onClick={() => act.run(n.status)}>
              {n.label}
            </Button>
          ))}
          {canRerun && (
            <Button variant="primary" aria-expanded={rerun} onClick={() => setRerun((r) => !r)}>
              {rerun ? "Close re-run" : "Re-run experiment"}
            </Button>
          )}
        </div>
      </div>
      <div className="mt-2">
        <KV
          items={[
            ["Addresses finding", `${it.supporting_findings?.join(", ") ?? ""} ${it.finding_title ?? ""}`],
            ["Evidence", <span className="num">{it.evidence}</span>],
            ["Proposed change", it.proposed_change],
            [`Analyst note (${it.analyst_suggestion_source ?? "n/a"})`, it.analyst_suggestion],
            ["Expected benefit", it.expected_benefit],
            ["Complexity / risk", `${it.complexity ?? DASH} · ${it.risks ?? DASH}`],
            ["Product context", it.product_context ? productContext(it.product_context) : null],
            ["Seen before", it.recurrence ? `${it.recurrence.prior_runs_with_same_finding} of ${it.recurrence.prior_runs} earlier run(s) of this scenario` : DASH],
            ["How to verify", <List items={it.verification_steps} />],
            ["Outcome", rec.validation_note ?? (rec.status === "implemented" ? "Waiting for the next run of this scenario." : DASH)],
            ["From experiment", <Link className="num text-accent hover:underline" to={`/history/${rec.experiment_id}`}>{rec.experiment_id}</Link>],
          ]}
        />
      </div>
      {act.error && <div className="mt-2"><ErrorNote message={act.error} /></div>}
      {rerun && canRerun && (
        <div className="mt-3">
          <RerunPanel rec={rec} onStarted={onChanged} />
        </div>
      )}
    </div>
  );
}

export function Recommendations({ recs, onChanged }: { recs: Recommendation[]; onChanged: () => void }) {
  if (!recs.length) return <Empty>No recommendations yet. They are generated after an experiment has been analysed.</Empty>;
  return (
    <div className="space-y-2">
      <p className="text-xxs text-faint">Nothing here is applied by this tool. After you have made a change yourself, "Re-run experiment" repeats the original scenario as a linked follow-up and compares the two runs; the recommendation is then marked verified or not effective.</p>
      {[...recs].sort((a, b) => (a.item.priority ?? 99) - (b.item.priority ?? 99)).map((r) => (
        <RecRow key={`${r.experiment_id}-${r.index}`} rec={r} onChanged={onChanged} />
      ))}
    </div>
  );
}

function describe(ev: TimelineEvent): { title: string; detail: string; tone: string; label?: string } {
  const d = ev.data as Record<string, string | number | boolean | null | undefined>;
  switch (ev.type) {
    case "proposed":
      return { title: "Experiment proposed", detail: `${d.signature} (${d.method})`, tone: "muted" };
    case "validated":
      return { title: "Policy validation passed", detail: `intensity ${d.cumulative_intensity}, ${d.warnings} warning(s)`, tone: "ok", label: "passed" };
    case "authorized":
      return { title: "Authorized", detail: `${d.kind} by ${d.actor}`, tone: "ok", label: "authorized" };
    case "alert":
      return { title: humanize(String(d.kind ?? "alert")), detail: String(d.message ?? ""), tone: d.level === "critical" || d.level === "error" ? "FAILED" : "ABORTED", label: String(d.level ?? "alert") };
    case "agent":
      return { title: `${d.title ?? d.agent} agent: ${d.status}`, detail: String(d.summary ?? d.error ?? ""), tone: String(d.status) };
    case "experiment": {
      const phase = String(d.phase ?? d.state);
      if (phase === "fault_applied") return { title: "Fault applied", detail: `${d.fault} on ${d.target} (${d.active_faults} active)`, tone: "MONITORING", label: "fault on" };
      if (phase === "fault_rolled_back") return { title: d.verified ? "Fault removed (verified)" : "Fault removal NOT verified", detail: String(d.action ?? ""), tone: d.verified ? "ok" : "FAILED", label: d.verified ? "fault off" : "not verified" };
      return { title: `Entered ${humanize(phase)}`, detail: d.t_s !== null && d.t_s !== undefined ? `t+${d.t_s}s` : "", tone: phase };
    }
    default:
      return { title: ev.type, detail: "", tone: "muted" };
  }
}

export function Timeline({ events }: { events: TimelineEvent[] }) {
  if (!events.length) return <Empty>No persisted events for this experiment (records created before timeline tracking have none).</Empty>;
  return (
    <ol className="space-y-1">
      {events.map((ev) => {
        const x = describe(ev);
        return (
          <li key={ev.id} className="grid grid-cols-[5.5rem_1fr] items-baseline gap-3 text-xs">
            <span className="num text-faint">{clock(ev.ts)}</span>
            <span>
              <StateBadge state={x.tone} label={x.label} /> <span className="ml-1 text-ink">{x.title}</span>
              {x.detail && <span className="ml-2 text-dim">{x.detail}</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export function AgentRuns({ runs }: { runs: AgentRun[] }) {
  if (!runs.length) return <Empty>No agent runs recorded for this experiment.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] border-collapse text-xs">
        <thead>
          <tr>
            {["Agent", "Status", "Duration", "Attempts", "Result"].map((h) => (
              <th key={h} className="th">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[...runs].reverse().map((r) => (
            <tr key={r.id}>
              <td className="td text-ink">{r.title}</td>
              <td className="td">
                <StateBadge state={r.status} />
              </td>
              <td className="td num">{duration(r.duration_ms)}</td>
              <td className="td num">{r.attempts}</td>
              <td className="td text-dim">{r.error ? <span className="text-bad">{r.error}</span> : r.summary}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
