// Ranked experiment candidates, the "re-run after a fix" action, and the before/after
// comparison of an original run and its follow-up.
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Candidate, CandidatesView, Followup, FollowupComparison, Proposal, Recommendation, RerunPlan } from "../api/types";
import { useAction, useAsync } from "../hooks";
import { DASH, dateTime, humanize, num } from "../lib/format";
import { Badge, Button, Empty, ErrorNote, KV, List, Note, Skeleton, StateBadge } from "./ui";

// ---------------------------------------------------------------- ranked candidates
function evidenceText(c: Candidate): string {
  const e = c.evidence;
  if (e.basis === "none") return "no stored run yet";
  return `${e.agreement} ${e.basis === "direct" ? "runs of this scenario" : "related runs"} (${e.confidence} confidence)`;
}

export function CandidateList({ view, selected, onSelected, disabledReason }: { view: CandidatesView; selected: string | null; onSelected: (p: Proposal) => void; disabledReason: string | null }) {
  const pick = useAction((signature: string) => api.selectCandidate(signature), onSelected);
  const [open, setOpen] = useState<string | null>(null);
  if (!view.candidates.length) return <Empty>No candidate is eligible under the current policy.</Empty>;
  return (
    <div>
      <p className="mb-2 text-xxs text-faint">
        Ranked from {view.evidence_runs} evidence run(s) by novelty, outcome uncertainty, usefulness of related runs, dependency relevance and pending re-tests. Selecting a candidate only loads it as the proposal below; nothing runs until you authorize it.
      </p>
      {view.store_degraded && <div className="mb-2"><Note tone="warn">Knowledge store unavailable; ranked from experiment files only. {view.store_degraded}</Note></div>}
      {pick.error && <div className="mb-2"><ErrorNote message={pick.error} /></div>}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[820px] border-collapse text-xs">
          <thead>
            <tr>
              {["#", "Target", "Fault", "Risk", "Hypothesis and reason", "Historical evidence", ""].map((h, i) => (
                <th key={i} className="th">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.candidates.map((c) => (
              <tr key={c.signature} className={selected === c.signature ? "bg-raised" : ""}>
                <td className="td num text-faint">{c.rank}</td>
                <td className="td text-ink">{c.targets.join(", ")}</td>
                <td className="td">{c.faults.join(" + ")}</td>
                <td className="td">
                  <StateBadge state={c.risk === "unknown" ? "muted" : c.risk} label={c.risk} />
                </td>
                <td className="td max-w-md">
                  <div className="text-dim">{c.hypothesis || DASH}</div>
                  <button type="button" className="mt-0.5 text-xxs text-accent hover:underline" aria-expanded={open === c.signature} onClick={() => setOpen(open === c.signature ? null : c.signature)}>
                    {open === c.signature ? "Hide reason and impact" : "Reason and expected impact"}
                  </button>
                  {open === c.signature && (
                    <div className="mt-1 space-y-1 text-xxs text-faint">
                      <div>
                        <span className="text-dim">Why ranked here:</span> {c.reason}
                      </div>
                      <div>
                        <span className="text-dim">Expected impact:</span> {c.expected_impact}
                      </div>
                      <div className="num">
                        score {num(c.score, 3)} · {Object.entries(c.components).filter(([, v]) => v).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ")}
                      </div>
                    </div>
                  )}
                </td>
                <td className="td">
                  <div className="text-dim">{evidenceText(c)}</div>
                  {c.evidence.experiments.slice(0, 3).map((id) => (
                    <Link key={id} className="num mr-2 text-xxs text-accent hover:underline" to={`/history/${id}`}>
                      {id}
                    </Link>
                  ))}
                </td>
                <td className="td text-right">
                  {c.selectable ? (
                    <Button busy={pick.pending} disabled={Boolean(disabledReason)} title={disabledReason ?? undefined} onClick={() => pick.run(c.signature)} aria-label={`Select candidate ${c.rank}: ${c.signature}`}>
                      {selected === c.signature ? "Selected" : "Select"}
                    </Button>
                  ) : (
                    <span className="text-xxs text-warn" title={c.not_selectable_reason ?? ""}>
                      not selectable: {c.not_selectable_reason}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- re-run after a fix
export function RerunPanel({ rec, onStarted }: { rec: Recommendation; onStarted: () => void }) {
  const plan = useAsync<RerunPlan>((s) => api.rerunPlan(rec.experiment_id, rec.index, s), [rec.experiment_id, rec.index]);
  const [applied, setApplied] = useState(false);
  const [authorize, setAuthorize] = useState(false);
  const [note, setNote] = useState("");
  const [started, setStarted] = useState<string | null>(null);
  const run = useAction(
    () => api.rerun(rec.experiment_id, { recommendation_index: rec.index, authorize: true, confirm_applied: true, note, request_id: `rerun-${Date.now().toString(36)}` }),
    (r) => {
      setStarted(r.experiment_id);
      onStarted();
    },
  );
  if (plan.error) return <ErrorNote message={plan.error} onRetry={plan.reload} />;
  if (!plan.data) return <Skeleton rows={2} />;
  const p = plan.data;
  if (started)
    return (
      <Note tone="ok" role="status">
        Follow-up run <span className="num text-ink">{started}</span> started. Watch it on{" "}
        <Link className="text-accent hover:underline" to="/control">
          Experiment control
        </Link>
        ; the comparison appears on{" "}
        <Link className="text-accent hover:underline" to={`/history/${started}`}>
          its page
        </Link>{" "}
        when it finishes.
      </Note>
    );
  if (!p.ok)
    return (
      <Note tone="warn">
        <div className="font-medium text-ink">The original scenario cannot be re-run as it was. A revised plan is required.</div>
        <List items={p.reasons} />
        <div className="mt-1">Build a new scenario on Experiment control instead; it will not be linked to this recommendation automatically.</div>
      </Note>
    );
  const s = p.scenario!;
  return (
    <div className="space-y-2 rounded border border-line bg-surface p-3" role="group" aria-label="Re-run experiment">
      <KV
        items={[
          ["Repeats", <span className="num">{p.original.signature}</span>],
          ["Same faults and parameters", <List items={s.actions.map((a) => `${a.fault} on ${a.target}${Object.keys(a.params).length ? ` (${Object.entries(a.params).map(([k, v]) => `${k}=${v}`).join(", ")})` : ""}`)} />],
          ["Duration", `${s.max_duration_s}s`],
          ["Baseline", p.baseline_note],
          ["Original outcome", humanize(p.original.outcome_class)],
        ]}
      />
      {p.validation && p.validation.warnings.length > 0 && <Note tone="warn"><List items={p.validation.warnings.map((w) => w.message)} /></Note>}
      <label className="flex items-start gap-2 text-xs text-dim">
        <input type="checkbox" className="mt-0.5" checked={applied} onChange={(e) => setApplied(e.target.checked)} />
        <span>I confirm the change was actually applied outside this tool. (Accepting a recommendation is not evidence that anything changed.)</span>
      </label>
      <label className="flex items-start gap-2 text-xs text-dim">
        <input type="checkbox" className="mt-0.5" checked={authorize} onChange={(e) => setAuthorize(e.target.checked)} />
        <span>I am an operator and I authorize REAL fault injection for this follow-up run against the local Ditto sandbox.</span>
      </label>
      <div>
        <label className="label" htmlFor={`rerun-note-${rec.experiment_id}-${rec.index}`}>
          What was changed (stored with the result)
        </label>
        <input id={`rerun-note-${rec.experiment_id}-${rec.index}`} className="field w-full max-w-xl" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="danger" busy={run.pending} disabled={!applied || !authorize} onClick={() => run.run()}>
          Start follow-up run
        </Button>
        {(!applied || !authorize) && <span className="text-xs text-warn">Both confirmations are required.</span>}
      </div>
      {run.error && <ErrorNote message={run.error} />}
      <p className="text-xxs text-faint">{p.caveat}</p>
    </div>
  );
}

// ---------------------------------------------------------------- before / after
const VERDICT: Record<string, { label: string; tone: "ok" | "bad" | "muted" | "warn" }> = {
  improved: { label: "improved", tone: "ok" },
  regressed: { label: "regressed", tone: "bad" },
  unchanged: { label: "unchanged", tone: "muted" },
  not_comparable: { label: "not comparable", tone: "warn" },
};
const DEMONSTRATED: Record<string, { label: string; tone: "ok" | "bad" | "warn" }> = {
  observed: { label: "benefit observed", tone: "ok" },
  not_observed: { label: "benefit not observed", tone: "bad" },
  inconclusive: { label: "inconclusive", tone: "warn" },
};

function value(v: number | null, unit: string): string {
  if (v === null || v === undefined) return "not measured";
  if (unit === "ratio") return `${(v * 100).toFixed(1)}%`;
  if (unit === "count") return String(v);
  return `${v.toFixed(unit === "ms" ? 0 : 1)} ${unit}`;
}

export function ComparisonView({ result }: { result: FollowupComparison }) {
  if (result.error) return <Note tone="warn">{result.error}</Note>;
  const v = result.verification;
  const d = v ? DEMONSTRATED[v.demonstrated] : null;
  const o = result.outcomes;
  return (
    <div className="space-y-3 text-xs">
      {v && d && (
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={d.tone}>{d.label}</Badge>
          <span className="text-ink">{v.statement}</span>
        </div>
      )}
      {!result.comparable && (
        <Note tone="warn">
          These two runs cannot be compared: <List items={result.not_comparable_reasons} />
        </Note>
      )}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] border-collapse">
          <caption className="sr-only">Before versus after, per metric</caption>
          <thead>
            <tr>
              {["Metric", "Before (original)", "After (follow-up)", "Change", "Result"].map((h) => (
                <th key={h} className="th">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.metrics.map((m) => {
              const x = VERDICT[m.verdict] ?? VERDICT.not_comparable;
              return (
                <tr key={m.key}>
                  <td className="td text-ink">{m.label}</td>
                  <td className={`td num ${m.before === null ? "text-faint" : ""}`}>{value(m.before, m.unit)}</td>
                  <td className={`td num ${m.after === null ? "text-faint" : ""}`}>{value(m.after, m.unit)}</td>
                  <td className="td num">
                    {m.change === null ? DASH : `${m.change > 0 ? "+" : ""}${m.unit === "ratio" ? `${(m.change * 100).toFixed(1)} pts` : m.change}`}
                    {m.change_pct !== null && m.unit !== "ratio" && <span className="ml-1 text-faint">({m.change_pct > 0 ? "+" : ""}{m.change_pct}%)</span>}
                  </td>
                  <td className="td">
                    <Badge tone={x.tone}>{x.label}</Badge>
                    {m.reason && <div className="mt-0.5 max-w-xs text-xxs text-faint">{m.reason}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <KV
        items={[
          ["Original run", <span><Link className="num text-accent hover:underline" to={`/history/${o.original.id}`}>{o.original.id}</Link> · {humanize(o.original.state)} · outcome {humanize(o.original.outcome_class)}{o.original.abort_reason ? ` · ${o.original.abort_reason}` : ""}</span>],
          ["Follow-up run", <span><Link className="num text-accent hover:underline" to={`/history/${o.followup.id}`}>{o.followup.id}</Link> · {humanize(o.followup.state)} · outcome {humanize(o.followup.outcome_class)}{o.followup.abort_reason ? ` · ${o.followup.abort_reason}` : ""}</span>],
          ["Baselines", `original ${num(o.original.baseline_direct_p95_ms, 0)} ms (${o.original.baseline_source ?? "unknown"}), follow-up ${num(o.followup.baseline_direct_p95_ms, 0)} ms (${o.followup.baseline_source ?? "unknown"}) direct read p95`],
          ["Fault samples", `${o.original.fault_ticks} versus ${o.followup.fault_ticks} probe ticks`],
          ["Parameter differences", result.parameter_differences.length ? <List items={result.parameter_differences} /> : "none"],
          ["Remediation tested", result.link ? `${result.link.recommendation_title} (finding: ${result.link.finding_title ?? DASH})` : DASH],
          ["Change confirmed by", result.link ? `${result.link.confirmed_applied_by}${result.link.confirmation_note ? `: "${result.link.confirmation_note}"` : ""}` : DASH],
          ["Expected benefit", v?.expected_benefit ?? DASH],
          ["Totals", `${result.summary.improved} improved · ${result.summary.regressed} regressed · ${result.summary.unchanged} unchanged · ${result.summary.not_comparable} not comparable`],
        ]}
      />
      <Note>{result.caveat}</Note>
    </div>
  );
}

export function Followups({ items, experimentId }: { items: Followup[]; experimentId: string }) {
  return (
    <div className="space-y-4">
      {items.map((f) => (
        <div key={f.followup_id}>
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <span className="text-faint">{f.followup_id === experimentId ? "This run is a follow-up of" : "Follow-up run"}</span>
            <Link className="num text-accent hover:underline" to={`/history/${f.followup_id === experimentId ? f.original_id : f.followup_id}`}>
              {f.followup_id === experimentId ? f.original_id : f.followup_id}
            </Link>
            <span className="text-faint">started {dateTime(f.created_at_utc)}</span>
          </div>
          {f.result ? <ComparisonView result={f.result} /> : <Note role="status">The follow-up run has not finished yet; the comparison appears when it does.</Note>}
        </div>
      ))}
    </div>
  );
}
