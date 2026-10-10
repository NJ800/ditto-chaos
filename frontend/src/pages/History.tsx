import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { ExperimentSummary } from "../api/types";
import { ActivityLog } from "../components/ActivityLog";
import { ActionsTable, AgentRuns, Findings, LearningCard, Recommendations, Timeline } from "../components/experiment";
import { MetricChart } from "../components/MetricChart";
import { Badge, Button, Empty, ErrorNote, KV, List, Note, PageHeader, Panel, Skeleton, StateBadge } from "../components/ui";
import { useAsync } from "../hooks";
import { dateTime, humanize, num } from "../lib/format";
import { useLive } from "../live/LiveContext";

export function filterExperiments(rows: ExperimentSummary[], q: string, outcome: string, evidenceOnly: boolean): ExperimentSummary[] {
  const needle = q.trim().toLowerCase();
  return rows.filter((e) => {
    if (evidenceOnly && !e.evidence) return false;
    if (outcome && e.outcome_class !== outcome) return false;
    if (!needle) return true;
    return [e.id, e.signature, e.state, e.fault, e.target, e.abort_reason, e.error, ...(e.symptoms ?? [])].some((v) => (v ?? "").toLowerCase().includes(needle));
  });
}

export function History() {
  const live = useLive();
  const list = useAsync((s) => api.experiments(s), [live.activity]);
  const [q, setQ] = useState("");
  const [outcome, setOutcome] = useState("");
  const [evidenceOnly, setEvidenceOnly] = useState(false);
  const rows = useMemo(() => filterExperiments(list.data ?? [], q, outcome, evidenceOnly), [list.data, q, outcome, evidenceOnly]);
  const outcomes = useMemo(() => Array.from(new Set((list.data ?? []).map((e) => e.outcome_class))).sort(), [list.data]);

  return (
    <div className="space-y-4">
      <PageHeader title="Experiment history" sub="Every saved record, old and new schema. Dry runs and runs where nothing was injected are listed but never counted as evidence." actions={<Button onClick={list.reload}>Refresh</Button>} />
      <Panel
        title="Experiments"
        meta={list.data ? `${rows.length} of ${list.data.length}` : undefined}
        flush
        actions={
          <>
            <input className="field w-48" type="search" placeholder="Search id, fault, target, reason" aria-label="Search experiments" value={q} onChange={(e) => setQ(e.target.value)} />
            <select className="field" aria-label="Outcome filter" value={outcome} onChange={(e) => setOutcome(e.target.value)}>
              <option value="">all outcomes</option>
              {outcomes.map((o) => (
                <option key={o} value={o}>
                  {humanize(o)}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1.5 text-xs text-dim">
              <input type="checkbox" checked={evidenceOnly} onChange={(e) => setEvidenceOnly(e.target.checked)} /> evidence only
            </label>
          </>
        }
      >
        {list.error ? (
          <div className="p-3">
            <ErrorNote message={list.error} onRetry={list.reload} />
          </div>
        ) : list.loading && !list.data ? (
          <div className="p-3">
            <Skeleton rows={5} />
          </div>
        ) : !list.data?.length ? (
          <Empty>No experiments saved yet.</Empty>
        ) : !rows.length ? (
          <Empty>No experiment matches these filters.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] border-collapse text-xs">
              <thead>
                <tr>
                  {["Started", "Experiment", "Scenario", "State", "Outcome", "Symptoms", "Recovery", "Rollback"].map((h) => (
                    <th key={h} className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((e) => (
                  <tr key={e.id} className="hover:bg-raised">
                    <td className="td num text-xxs text-dim">{dateTime(e.started_at_utc)}</td>
                    <td className="td">
                      <Link className="num text-accent hover:underline" to={`/history/${e.id}`}>
                        {e.id}
                      </Link>
                      <div className="text-xxs text-faint">
                        {e.simulated ? "dry run" : humanize(e.source ?? e.kind)}
                        {e.mode ? ` · ${humanize(e.mode)}` : ""}
                      </div>
                    </td>
                    <td className="td num">{e.signature || `${e.fault ?? "?"}@${e.target ?? "?"}`}</td>
                    <td className="td">
                      <StateBadge state={e.state} />
                    </td>
                    <td className="td">
                      <StateBadge state={e.outcome_class} />
                      {e.validation_verdict && e.validation_verdict !== "conclusive" && <div className="mt-0.5 text-xxs text-warn">{e.validation_verdict}</div>}
                    </td>
                    <td className="td text-dim">{e.symptoms.length ? e.symptoms.map(humanize).join(", ") : "—"}</td>
                    <td className="td num">{e.recovery_s !== null ? `${e.recovery_s}s` : humanize(e.recovery_outcome)}</td>
                    <td className="td">{!e.injected ? <span className="text-faint">not injected</span> : e.rollback_verified ? <Badge tone="ok">verified</Badge> : <Badge tone="bad">not verified</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

export function ExperimentDetailPage() {
  const { id = "" } = useParams();
  const live = useLive();
  const d = useAsync((s) => api.experiment(id, s), [id, live.activity]);
  if (d.error) return <ErrorNote message={d.error} onRetry={d.reload} />;
  if (!d.data) return <Skeleton rows={6} />;
  const { record: rec, analysis, learning, recommendations, agent_runs, timeline } = d.data;
  const v = rec.validation;
  const base = (rec.baseline ?? {}) as { direct_ms?: { p95?: number | null } };

  return (
    <div className="space-y-4">
      <PageHeader
        title={rec.id}
        sub={
          <span>
            <Link className="text-accent hover:underline" to="/history">
              History
            </Link>{" "}
            / <span className="num">{rec.signature ?? `${rec.fault}@${rec.target}`}</span> / schema {rec.schema_version}
          </span>
        }
        actions={
          <>
            <StateBadge state={rec.state} />
            <StateBadge state={rec.recovery_state ?? rec.recovery_report?.recovery_state} />
            {(rec.dry_run || rec.simulated) && <Badge tone="warn">dry run: nothing was injected</Badge>}
          </>
        }
      />
      {rec.state === "RECOVERY_REQUIRED" && (
        <Note tone="bad">
          <div className="font-medium text-ink">Rollback was not verified for this run.</div>
          {rec.rollback_error}
          <List items={(rec.recovery_instructions ?? []).flatMap((i) => i.manual)} empty="" />
        </Note>
      )}
      {rec.error && rec.state !== "RECOVERY_REQUIRED" && <Note tone="warn">{rec.error}</Note>}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel title="Run">
          <KV
            items={[
              ["Started", dateTime(rec.started_at_utc)],
              ["Ended", dateTime(rec.ended_at_utc)],
              ["Authorized by", rec.authorization ? `${rec.authorization.actor} (${humanize(rec.authorization.kind)})` : rec.dry_run || rec.simulated ? "not needed for a dry run" : "not recorded (legacy record)"],
              ["Mode / source", `${humanize(rec.mode)} / ${humanize(rec.source)}`],
              ["Hypothesis", rec.hypothesis],
              ["Stop reason", rec.abort_reason],
              ["Detected after", num(rec.detected_s, 1, " s")],
              ["Recovery", rec.dry_run || rec.simulated ? "not applicable (dry run)" : rec.recovery_outcome ? `${humanize(rec.recovery_outcome)}${rec.recovery_s !== null ? ` in ${num(rec.recovery_s, 1, " s")}` : ""}` : null],
              ["Rollback", !rec.injected ? "nothing injected" : rec.rollback_verified ? "verified" : "NOT verified"],
            ]}
          />
        </Panel>
        <div className="xl:col-span-2">
          <Panel title="Fault actions" flush>
            {rec.actions?.length ? <ActionsTable actions={rec.actions} /> : <div className="p-3"><KV items={[["Fault", rec.fault], ["Target", rec.target], ["Injected", rec.injected ? "yes" : "no"]]} /></div>}
          </Panel>
        </div>
      </div>

      <Panel title="Measurements during the run" meta={`${rec.timeline?.length ?? 0} probe ticks; the shaded band is the fault interval`}>
        <MetricChart
          rows={rec.timeline ?? []}
          unit=" ms"
          series={[
            { key: "direct", label: "direct read", color: "#6b9bd8", value: (r) => (r.direct_ok ? r.direct_ms : null) },
            { key: "search", label: "search", color: "#b08fd8", value: (r) => (r.search_ok ? r.search_ms : null) },
          ]}
          baseline={base.direct_ms?.p95 ? { value: base.direct_ms.p95, label: "baseline p95" } : null}
        />
      </Panel>

      <Panel title="What happened, step by step" meta="recorded by the experiment controller; select a lifecycle step to filter">
        <ActivityLog key={rec.id} experimentId={rec.id} currentState={rec.state} />
      </Panel>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="Validation" actions={v ? <StateBadge state={v.verdict} /> : undefined}>
          {v ? (
            <>
              <ul className="space-y-1 text-xs">
                {v.checks.map((c) => (
                  <li key={c.check} className="grid grid-cols-[4.5rem_1fr] gap-2">
                    <Badge tone={c.ok ? "ok" : "bad"}>{c.ok ? "pass" : "fail"}</Badge>
                    <span className="text-dim">{c.check}</span>
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-dim">
                Hypothesis {v.hypothesis_supported === null ? "not evaluated (no prior evidence to predict from)" : v.hypothesis_supported ? "supported by the outcome" : "not supported by the outcome"}.{" "}
                {v.counts_as_evidence ? "This run counts as evidence." : "This run does NOT count as evidence."}
              </p>
            </>
          ) : (
            <Empty>{rec.dry_run || rec.simulated ? "Not applicable: a dry run injects nothing, so there is no result to validate. It never counts as evidence." : "Not validated (record predates the validation agent)."}</Empty>
          )}
        </Panel>
        <Panel title="Learning record">{learning ? <LearningCard rec={learning} /> : <Empty>{rec.dry_run || rec.simulated ? "None: nothing is learned from a dry run." : "No learning record stored for this experiment."}</Empty>}</Panel>
      </div>

      <Panel title="Findings" meta="measured facts and hypotheses are labelled separately">
        {analysis ? <Findings analysis={analysis} /> : <Empty>This experiment has not been analysed.</Empty>}
      </Panel>
      <Panel title="Recommendations">
        <Recommendations recs={recommendations} onChanged={d.reload} />
      </Panel>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="Timeline" meta="persisted backend events">
          <Timeline events={timeline} />
        </Panel>
        <Panel title="Agent runs" flush>
          <AgentRuns runs={agent_runs} />
        </Panel>
      </div>
    </div>
  );
}
