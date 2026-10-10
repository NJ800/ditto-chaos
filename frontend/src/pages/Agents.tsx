import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { AgentStatus } from "../api/types";
import { Timeline } from "../components/experiment";
import { Badge, Button, Empty, ErrorNote, KV, PageHeader, Panel, Skeleton, StateBadge } from "../components/ui";
import { useAsync } from "../hooks";
import { dateTime, duration, humanize } from "../lib/format";
import { useLive } from "../live/LiveContext";

const ORDER = ["risk", "fault_injection", "recovery", "validation", "insights", "learning", "remediation"];
const STAGE: Record<string, string> = {
  risk: "plan 1",
  fault_injection: "plan 2",
  recovery: "post-run 1",
  validation: "post-run 2",
  insights: "post-run 3",
  learning: "post-run 4",
  remediation: "post-run 5",
};

function AgentRow({ a }: { a: AgentStatus }) {
  const [open, setOpen] = useState(false);
  // After an API restart nothing has run "in this session" yet: fall back to the newest persisted run.
  const last = a.last ?? a.recent[0] ?? null;
  const fromStore = !a.last && Boolean(a.recent[0]);
  return (
    <>
      <tr>
        <td className="td">
          <button type="button" className="text-left" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            <span className="font-medium text-ink">{a.title}</span>
            <span className="ml-2 text-xxs text-faint">{open ? "hide" : "details"}</span>
          </button>
          <div className="text-xxs text-faint">{STAGE[a.name] ?? ""}</div>
        </td>
        <td className="td">
          <Badge tone={a.kind === "hybrid" ? "accent" : "muted"}>{a.kind === "hybrid" ? (a.provider?.enabled ? `LLM + rules (${a.provider.model})` : "LLM off: rules only") : "deterministic"}</Badge>
        </td>
        <td className="td">
          <StateBadge state={a.state} />
          {a.task && <div className="num mt-0.5 text-xxs text-faint">{a.task}</div>}
        </td>
        <td className="td">
          {last ? (
            <>
              <StateBadge state={last.status} /> <span className="num text-xxs text-faint">{duration(last.duration_ms)}</span>
              <div className={`mt-0.5 max-w-md text-xxs ${last.error ? "text-bad" : "text-dim"}`}>{last.error ?? last.summary}</div>
              {fromStore && <div className="text-xxs text-faint">from the knowledge store ({dateTime(last.finished_at_utc)})</div>}
            </>
          ) : (
            <span className="text-faint">never run</span>
          )}
        </td>
        <td className="td num text-xxs">{dateTime(a.last_success_utc ?? a.recent.find((r) => r.status === "completed" || r.status === "fallback")?.finished_at_utc)}</td>
        <td className="td num">
          {a.runs} / <span className={a.failures ? "text-bad" : ""}>{a.failures}</span>
        </td>
      </tr>
      {open && (
        <tr>
          <td className="td bg-raised" colSpan={6}>
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              <KV
                items={[
                  ["Responsibility", a.role],
                  ["Version", a.version],
                  ["Timeout / retries", `${a.timeout_s}s / ${a.retries}`],
                  ["Model", a.provider ? `${a.provider.provider} ${a.provider.model} (${a.provider.enabled ? "enabled" : "disabled; deterministic fallback in use"})` : "none: no model call is made"],
                ]}
              />
              <div>
                <div className="label">Recent runs (persisted)</div>
                {a.recent.length ? (
                  <ul className="space-y-1 text-xs">
                    {a.recent.map((r) => (
                      <li key={r.id} className="grid grid-cols-[7.5rem_5.5rem_1fr] items-baseline gap-2">
                        <span className="num text-xxs text-faint">{dateTime(r.started_at_utc)}</span>
                        <StateBadge state={r.status} />
                        <span className="text-dim">
                          {r.error ?? r.summary}
                          {r.experiment_id && (
                            <Link className="num ml-2 text-accent hover:underline" to={`/history/${r.experiment_id}`}>
                              {r.experiment_id}
                            </Link>
                          )}
                          {r.attempts > 1 && <span className="ml-2 text-warn">{r.attempts} attempts</span>}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <span className="text-xs text-faint">none stored</span>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export function Agents() {
  const live = useLive();
  const agents = [...live.agents].sort((a, b) => ORDER.indexOf(a.name) - ORDER.indexOf(b.name));
  const expId = live.orchestrator?.experiment_id ?? null;
  const detail = useAsync((s) => (expId ? api.experiment(expId, s) : Promise.resolve(null)), [expId, live.activity, live.orchestrator?.status]);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Agents"
        sub="Seven agents, invoked in a fixed order by the coordinator. They exchange validated documents, never call each other, and cannot execute a fault: injection, stopping and rollback are deterministic code in the controller."
      />
      <Panel title="Agent status" meta="state is live; runs and results are read from the knowledge store" flush>
        {agents.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] border-collapse text-xs">
              <thead>
                <tr>
                  {["Agent", "Type", "State", "Last result", "Last success", "Runs / failures (this session)"].map((h) => (
                    <th key={h} className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {agents.map((a) => (
                  <AgentRow key={a.name} a={a} />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No agents are registered.</Empty>
        )}
      </Panel>

      <Panel
        title="Experiment timeline"
        meta={expId ? <span className="num">{expId}</span> : "persisted events of the most recent experiment"}
        actions={
          <Button onClick={detail.reload} busy={detail.loading && Boolean(detail.data)}>
            Refresh
          </Button>
        }
      >
        {!expId ? (
          <Empty>No experiment has run in this backend session. Past timelines are on each experiment's page under {humanize("History")}.</Empty>
        ) : detail.error ? (
          <ErrorNote message={detail.error} onRetry={detail.reload} />
        ) : detail.loading && !detail.data ? (
          <Skeleton />
        ) : (
          <Timeline events={detail.data?.timeline ?? []} />
        )}
      </Panel>
    </div>
  );
}
