import * as Dialog from "@radix-ui/react-dialog";
import { Activity, Ban, Check, Circle, ClipboardList, Eye, Gauge, Lightbulb, OctagonX, ScanSearch, ShieldCheck, TriangleAlert, Undo2, X, Zap, type LucideIcon } from "lucide-react";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, request } from "../api/client";
import type { AgentRun, AgentStatus, Analysis, ExperimentEvent, ExperimentRecord, MetricRow, Thresholds } from "../api/types";
import { Badge, Empty, ErrorNote, Note, PageHeader, Panel, Skeleton } from "../components/ui";
import { useAsync, useExperimentEvents } from "../hooks";
import { humanize, num } from "../lib/format";
import { useLive, useNow } from "../live/LiveContext";

// ------------------------------------------------------------------ model
/** One card per lifecycle phase. `states` are the controller states that mean "this phase is happening now". */
const STAGES: { key: string; title: string; icon: LucideIcon; states: string[]; what: string; fault?: boolean }[] = [
  { key: "plan", title: "Plan and authorize", icon: ClipboardList, states: [], what: "A scenario is chosen and an operator approves it" },
  { key: "policy", title: "Policy check", icon: ShieldCheck, states: ["PRECHECK"], what: "Allowlist, parameter bounds and scenario limits" },
  { key: "preflight", title: "Preflight", icon: ScanSearch, states: ["PREFLIGHT"], what: "Docker and every target container are inspected" },
  { key: "baseline", title: "Baseline", icon: Gauge, states: ["BASELINE"], what: "Health is measured; nothing is injected unless Ditto is healthy" },
  { key: "inject", title: "Inject", icon: Zap, states: ["INJECTING"], what: "The fault is applied to the target container", fault: true },
  { key: "monitor", title: "Monitor", icon: Activity, states: ["MONITORING"], what: "Health is probed every tick; the kill switch is armed" },
  { key: "rollback", title: "Roll back and verify", icon: Undo2, states: ["REVERTING", "VERIFYING_RECOVERY"], what: "The fault is removed, the clean state confirmed, recovery timed", fault: true },
  { key: "analyse", title: "Analyse", icon: Lightbulb, states: [], what: "Agents turn the measurements into findings and recommendations" },
];
const MONITOR = 5;
const ROLLBACK = 6;
const ANALYSE = 7;
/** The coordinator's fixed order (agents.Coordinator.PLAN / POST_RUN). */
const PLAN_AGENTS = ["risk", "fault_injection"];
const POST_RUN_AGENTS = ["recovery", "validation", "insights", "learning", "remediation"];

export type StageState = "pending" | "active" | "done" | "stopped" | "failed" | "skipped" | "previewed";
export type AgentChipState = "idle" | "queued" | "running" | "completed" | "failed" | "skipped";

export interface RunView {
  /** The orchestrator's status, or "saved" for a finished run opened from its record. */
  status: string;
  record: ExperimentRecord | null;
  analysis: Analysis | null;
  agentRuns: AgentRun[];
}

const stageOfState = (state: string | null | undefined): number => STAGES.findIndex((s) => s.states.includes(state ?? ""));
const isDry = (rec: ExperimentRecord) => rec.dry_run || Boolean(rec.simulated);
const operatorStop = (reason: string | null) => Boolean(reason && /stop/i.test(reason) && /operator|manual|emergency/i.test(reason));

/** Where each stage stands, derived only from the controller's record and status. */
export function stageStates(view: Pick<RunView, "status" | "record" | "analysis">): StageState[] {
  const rec = view.record;
  const out: StageState[] = STAGES.map(() => "pending");
  if (!rec) return out;
  const dry = isDry(rec);
  const finished = (i: number): StageState => (dry && STAGES[i].fault ? "previewed" : "done");
  const at = stageOfState(rec.state);
  if (at >= 0) {
    for (let i = 0; i < at; i++) out[i] = finished(i);
    out[at] = "active";
    return out;
  }
  if (!rec.injected && !dry) {
    // Refused or stopped before anything was touched: mark the stage it got to.
    const seen = (rec.events ?? []).map((e) => stageOfState(e.state)).filter((i) => i >= 0);
    const reached = seen.length ? Math.max(...seen) : 1;
    for (let i = 0; i < reached; i++) out[i] = "done";
    out[reached] = rec.state === "FAILED" ? "failed" : "stopped";
    for (let i = reached + 1; i <= ANALYSE; i++) out[i] = "skipped";
    return out;
  }
  for (let i = 0; i <= ROLLBACK; i++) out[i] = finished(i);
  if (rec.abort_reason && !dry) out[MONITOR] = "stopped";
  if (!dry && (rec.state !== "COMPLETED" || !rec.rollback_verified || rec.recovery_outcome !== "recovered")) out[ROLLBACK] = "failed";
  const analysed = view.analysis?.experiment_id === rec.id;
  out[ANALYSE] = view.status === "analyzing" ? "active" : analysed ? "done" : dry ? "skipped" : view.status === "error" ? "failed" : view.status === "saved" ? "skipped" : "pending";
  return out;
}

/** State of one agent for this run, from its persisted run, its live status, and the fixed order. */
export function agentChipState(name: string, view: RunView, status: AgentStatus | undefined): { state: AgentChipState; run: Partial<AgentRun> | null; note: string | null } {
  const rec = view.record;
  const plan = PLAN_AGENTS.includes(name);
  const running = status?.state === "running" && (plan || !rec || status.task === rec.id);
  if (running) return { state: "running", run: null, note: null };
  const mine = plan
    ? rec?.source === "proposal" ? status?.last ?? null : null
    : view.agentRuns.find((r) => r.agent === name) ?? (rec && status?.last?.experiment_id === rec.id ? status.last : null);
  if (mine?.status) {
    const s = mine.status;
    return { state: s === "completed" || s === "fallback" ? "completed" : s === "skipped" ? "skipped" : "failed", run: mine, note: null };
  }
  if (plan) return { state: "idle", run: null, note: rec ? "not used: the operator wrote this scenario" : null };
  if (!rec) return { state: "idle", run: null, note: null };
  if (view.status === "analyzing") return { state: "queued", run: null, note: null };
  if (isDry(rec)) return { state: "skipped", run: null, note: "a dry run has nothing to analyse" };
  if (!rec.injected) return { state: "skipped", run: null, note: "nothing was injected" };
  return { state: view.status === "running" ? "idle" : "skipped", run: null, note: view.status === "running" ? "runs after the rollback" : "no run recorded" };
}

interface Check {
  direct_ok: boolean;
  direct_ms: number | null;
  search_ok: boolean;
  search_ms: number | null;
  stale: number | null;
}
const checksOf = (events: ExperimentEvent[]): Check[] =>
  events
    .filter((e) => e.kind === "health" && (e.state === "MONITORING" || e.state === "INJECTING"))
    .map((e) => ({ direct_ok: e.detail.direct_ok !== false, direct_ms: e.detail.direct_ms ?? null, search_ok: e.detail.search_ok !== false, search_ms: e.detail.search_ms ?? null, stale: e.detail.stale_p95_s ?? null }));

const p95 = (block: unknown, key: string): number | null => {
  const v = (block as Record<string, { p95?: number | null } | undefined> | null)?.[key]?.p95;
  return typeof v === "number" ? v : null;
};
export const latencyLimit = (rec: ExperimentRecord, th: Thresholds | null): number | null => {
  const b = p95(rec.baseline, "direct_ms");
  return th && b !== null ? Math.max(b * th.max_latency_factor, th.min_latency_floor_ms) : null;
};

/** The stop condition closest to firing: how many of the newest checks in a row breached it. */
export function closestCondition(checks: Check[], limit: number | null, th: Thresholds | null): { label: string; count: number; required: number } {
  const required = th?.breach_window ?? 3;
  const streak = (bad: (c: Check) => boolean) => {
    let n = 0;
    for (let i = checks.length - 1; i >= 0 && bad(checks[i]); i--) n++;
    return Math.min(n, required);
  };
  const all = [
    { label: "search failures", count: streak((c) => !c.search_ok) },
    { label: "read failures", count: streak((c) => !c.direct_ok) },
    { label: "latency above the limit", count: limit === null ? 0 : streak((c) => c.direct_ok && c.direct_ms !== null && c.direct_ms > limit) },
    { label: "stale data", count: th ? streak((c) => c.stale !== null && c.stale > th.max_stale_s) : 0 },
  ];
  return { ...all.reduce((a, b) => (b.count > a.count ? b : a)), required };
}

const reducedMotion = () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const secs = (s: number) => (s < 60 ? `${Math.max(0, Math.round(s))} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);

// ------------------------------------------------------------------ small parts
const LOOK: Record<StageState, { box: string; word: string; icon: LucideIcon; tone: string; fill: string }> = {
  pending: { box: "border-line bg-surface opacity-60", word: "pending", icon: Circle, tone: "text-faint", fill: "" },
  active: { box: "border-accent bg-accent/5 ring-2 ring-accent/30", word: "active", icon: Circle, tone: "text-accent", fill: "" },
  done: { box: "border-ok/40 bg-surface", word: "done", icon: Check, tone: "text-ok", fill: "bg-ok" },
  stopped: { box: "border-warn bg-warn/5", word: "stopped", icon: OctagonX, tone: "text-warn", fill: "bg-warn" },
  failed: { box: "border-bad bg-bad/5", word: "failed", icon: TriangleAlert, tone: "text-bad", fill: "bg-bad" },
  skipped: { box: "border-dashed border-line bg-surface opacity-60", word: "skipped", icon: Ban, tone: "text-faint", fill: "" },
  previewed: { box: "border-dashed border-line bg-surface", word: "previewed", icon: Eye, tone: "text-dim", fill: "bg-faint" },
};

/** A value that briefly highlights when it changes. The highlight is CSS-only and off under reduced motion. */
function Val({ children }: { children: string }) {
  const prev = useRef(children);
  const changed = prev.current !== children;
  prev.current = children;
  return (
    <span key={children} className={`num rounded px-0.5 text-ink ${changed ? "pl-flash" : ""}`}>
      {children}
    </span>
  );
}

function Sparkline({ checks, limit }: { checks: Check[]; limit: number | null }) {
  const last = checks.slice(-24);
  const values = last.map((c) => (c.direct_ok ? c.direct_ms : null));
  const top = Math.max(limit ? limit * 1.25 : 0, ...values.map((v) => v ?? 0), 1);
  const W = 160, H = 32;
  const x = (i: number) => (last.length > 1 ? (i / (last.length - 1)) * W : W / 2);
  const y = (v: number) => H - 2 - (Math.min(v, top) / top) * (H - 4);
  let d = "";
  values.forEach((v, i) => {
    if (v === null) return;
    d += `${d && values[i - 1] !== null && values[i - 1] !== undefined ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
  });
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="mt-1.5 h-8 w-full max-w-[14rem]" role="img" aria-label={`Read latency of the last ${last.length} checks${limit ? `, stop limit ${limit.toFixed(0)} ms dashed` : ""}`}>
      {limit !== null && <line x1="0" x2={W} y1={y(limit)} y2={y(limit)} stroke="rgb(var(--warn))" strokeWidth="1" strokeDasharray="4 3" />}
      {d && <path d={d} fill="none" stroke="rgb(var(--accent))" strokeWidth="1.5" strokeLinejoin="round" />}
      {values.map((v, i) => (v === null ? <circle key={i} cx={x(i)} cy={H - 3} r="1.6" fill="rgb(var(--bad))" /> : null))}
    </svg>
  );
}

function KillSwitchMeter({ label, count, required, fired }: { label: string; count: number; required: number; fired: boolean }) {
  const tone = fired || count >= required ? "bg-bad" : count >= required - 1 && count > 0 ? "bg-warn" : "bg-accent";
  return (
    <div className="mt-1.5 flex items-center gap-2 text-xs text-dim" data-testid="kill-switch-meter">
      <span className="flex gap-0.5" aria-hidden="true">
        {Array.from({ length: required }, (_, i) => (
          <span key={i} className={`h-1.5 w-4 rounded-sm transition-colors ${i < count ? tone : "bg-line"}`} />
        ))}
      </span>
      <span>
        {label} <Val>{`${count} of ${required}`}</Val>
        {fired ? " (fired)" : ""}
      </span>
    </div>
  );
}

// ------------------------------------------------------------------ stage bodies
function StageBody({ index, state, view, events, th }: { index: number; state: StageState; view: RunView; events: ExperimentEvent[]; th: Thresholds | null }) {
  const rec = view.record!;
  const dry = isDry(rec);
  const actions = rec.actions ?? [];
  const line = "break-words";
  switch (STAGES[index].key) {
    case "plan":
      return (
        <>
          <li className={`${line} num`}>{rec.signature ?? `${rec.fault}@${rec.target}`}</li>
          <li className={line}>{dry ? "dry run: no authorization needed" : rec.authorization ? `approved by ${rec.authorization.actor} (${humanize(rec.authorization.kind)})` : "authorization not recorded"}</li>
          {rec.mode && <li className={line}>{`${humanize(rec.mode)} mode${rec.source ? `, from ${humanize(rec.source)}` : ""}`}</li>}
        </>
      );
    case "policy":
      return (
        <>
          <li className={line}>{`${actions.length} fault(s) on ${new Set(actions.map((a) => a.target)).size} service(s)`}</li>
          {rec.scenario && <li className={line}>{`${rec.scenario.max_duration_s} s at most`}</li>}
          {state === "done" && <li className={line}>passed</li>}
        </>
      );
    case "preflight":
      if (dry) return <li className={line}>dry run: Docker is not contacted</li>;
      return (
        <>
          {Object.entries(rec.targets ?? {}).slice(0, 3).map(([t, v]) => (
            <li key={t} className={line}>{`${t}: ${v.running ? "running" : v.running === false ? "not running" : "state unknown"}`}</li>
          ))}
        </>
      );
    case "baseline":
      if (!rec.baseline) return null;
      return (
        <>
          <li className={line}>{`read p95 ${num(p95(rec.baseline, "direct_ms"), 0, " ms")}`}</li>
          <li className={line}>{`search p95 ${num(p95(rec.baseline, "search_ms"), 0, " ms")}`}</li>
          <li className={line}>healthy before the fault</li>
        </>
      );
    case "inject":
      return (
        <>
          {actions.slice(0, 3).map((a) => (
            <li key={a.id} className={line}>
              {dry ? `${a.fault} on ${a.target}: previewed, not executed` : `${a.fault} on ${a.target}: ${a.applied_at_s !== null ? `applied at ${num(a.applied_at_s, 1, " s")}` : humanize(a.status)}`}
            </li>
          ))}
        </>
      );
    case "monitor": {
      const checks = checksOf(events);
      const last = checks[checks.length - 1];
      const limit = latencyLimit(rec, th);
      const cond = closestCondition(checks, limit, th);
      const fired = Boolean(rec.abort_reason) && !operatorStop(rec.abort_reason);
      return (
        <>
          <li className={line}>
            {state === "active" ? "checks so far" : "checks during the fault"} <Val>{String(checks.length)}</Val>
          </li>
          {last && (
            <li className={line}>
              read <Val>{last.direct_ok && last.direct_ms !== null ? `${last.direct_ms.toFixed(0)} ms` : "FAILED"}</Val>
              {limit !== null ? ` / limit ${limit.toFixed(0)} ms` : ""} · search <Val>{last.search_ok && last.search_ms !== null ? `${last.search_ms.toFixed(0)} ms` : "FAILED"}</Val>
            </li>
          )}
          {!dry && checks.length > 0 && (
            <li>
              <KillSwitchMeter label={cond.label} count={cond.count} required={cond.required} fired={fired} />
            </li>
          )}
          {checks.length > 1 && (
            <li>
              <Sparkline checks={checks} limit={limit} />
            </li>
          )}
          {state === "done" && !rec.abort_reason && <li className={line}>no stop condition met</li>}
        </>
      );
    }
    case "rollback": {
      if (dry) return <li className={line}>previewed, not executed: nothing to remove</li>;
      const order = events.find((e) => Array.isArray(e.detail.rollback_order))?.detail.rollback_order as string[] | undefined;
      const streak = [...events].reverse().find((e) => typeof e.detail.healthy_streak === "number")?.detail as { healthy_streak: number; required: number } | undefined;
      return (
        <>
          {actions.length > 1 && order && <li className={line}>{`order: ${order.map((id) => actions.find((a) => a.id === id)?.target ?? id).join(" → ")}`}</li>}
          {actions.slice(0, 3).map((a) => {
            const bad = a.status === "rollback_failed" || Boolean(a.rollback_error) || (a.rolled_back_at_s !== null && !a.rollback_verified);
            const text = bad ? "verify failed" : a.rollback_verified ? "removed, verified" : a.status === "rolling_back" || state === "active" ? "removing…" : humanize(a.status);
            return (
              <li key={a.id} className={`${line} ${bad ? "font-semibold text-bad" : ""}`}>
                {`${a.fault} on ${a.target}: `}
                <Val>{text}</Val>
              </li>
            );
          })}
          {rec.recovery_outcome ? (
            <li className={line}>{`${humanize(rec.recovery_outcome)}${rec.recovery_s !== null ? ` in ${num(rec.recovery_s, 1, " s")}` : ""}`}</li>
          ) : streak ? (
            <li className={line}>
              <Val>{`${streak.healthy_streak} of ${streak.required}`}</Val> healthy checks
            </li>
          ) : null}
        </>
      );
    }
    case "analyse": {
      const a = view.analysis?.experiment_id === rec.id ? view.analysis : null;
      if (!a) {
        const done = POST_RUN_AGENTS.filter((n) => view.agentRuns.some((r) => r.agent === n)).length;
        return state === "active" ? <li className={line}>{`${done} of ${POST_RUN_AGENTS.length} agents finished`}</li> : null;
      }
      const top = a.findings[0];
      return (
        <>
          <li className={line}>{`${a.findings.length} finding(s)${top ? `, top severity ${top.severity}` : ""}`}</li>
          {top && <li className={line}>{top.title}</li>}
          <li className={line}>{a.analysis_method === "deterministic_fallback" ? "analyst: rule-based fallback" : `analyst: AI (${a.analysis_method})`}</li>
        </>
      );
    }
    default:
      return null;
  }
}

function StageCard({ index, state, view, events, th, startedAt, nowMs, animate, sweep, isLive }: { index: number; state: StageState; view: RunView; events: ExperimentEvent[]; th: Thresholds | null; startedAt: number | null; nowMs: number; animate: boolean; sweep: boolean; isLive: boolean }) {
  const stage = STAGES[index];
  const look = LOOK[state];
  const motion = animate ? (state === "done" ? "pl-settle" : state === "failed" ? "pl-shake" : "") : "";
  const body = view.record && state !== "pending" && state !== "skipped" ? <StageBody index={index} state={state} view={view} events={events} th={th} /> : null;
  return (
    <li
      className={`relative flex min-h-[9.5rem] flex-col rounded-lg border p-3 transition-colors duration-200 ease-out ${look.box} ${motion} ${sweep ? "pl-sweep" : ""}`}
      style={sweep ? ({ "--pl-delay": `${index * 70}ms` } as React.CSSProperties) : undefined}
      aria-current={state === "active" ? "step" : undefined}
      data-stage={stage.key}
      data-state={state}
      data-anim={motion || undefined}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <stage.icon className={`h-4 w-4 shrink-0 ${look.tone}`} aria-hidden="true" />
          <h3 className="text-sm font-semibold text-ink">
            <span className="num mr-1 text-faint">{index + 1}</span>
            {stage.title}
          </h3>
        </div>
        <span key={state} className={`flex shrink-0 items-center gap-1 text-xxs font-semibold uppercase tracking-wide ${look.tone} ${animate ? "pl-fade" : ""}`}>
          {state === "active" ? <span className={`h-2 w-2 rounded-full bg-accent ${isLive ? "pl-pulse" : ""}`} aria-hidden="true" /> : <look.icon className={`h-3 w-3 ${animate && state === "done" ? "pl-draw" : ""}`} aria-hidden="true" />}
          {look.word}
        </span>
      </div>
      {body ? <ul className="mt-2 space-y-0.5 text-xs text-dim">{body}</ul> : <p className="mt-2 text-xs text-faint">{state === "previewed" ? "previewed, not executed" : stage.what}</p>}
      {state === "active" && startedAt !== null && (
        <p className="mt-auto pt-2 text-xxs text-faint">
          running for <span className="num text-ink">{secs(nowMs / 1000 - startedAt)}</span>
        </p>
      )}
      {index % 4 !== 3 && (
        <span className="absolute -right-6 top-1/2 hidden h-0.5 w-6 -translate-y-1/2 bg-line xl:block" aria-hidden="true">
          {look.fill && <span className={`block h-full w-full origin-left ${look.fill} ${animate ? "pl-fill" : ""}`} data-testid="connector-fill" />}
        </span>
      )}
    </li>
  );
}

/** The end-state / safety banner under the second row. */
function Banner({ view, states, events, slide }: { view: RunView; states: StageState[]; events: ExperimentEvent[]; slide: boolean }) {
  const rec = view.record!;
  const dry = isDry(rec);
  const cls = slide ? "pl-slide" : "";
  if (!dry && rec.injected && (rec.rollback_error || (states[ROLLBACK] === "failed" && !rec.rollback_verified))) {
    return (
      <div className={cls}>
        <Note tone="bad" role="alert">
          <strong className="text-bad">Rollback not verified.</strong> {rec.rollback_error ?? ""} Fault injection is locked until a person resolves it.
        </Note>
      </div>
    );
  }
  if (rec.abort_reason) {
    const fired = events.find((e) => e.detail.kill_switch);
    const where = STAGES[stageOfState(fired?.state)]?.title ?? STAGES[MONITOR].title;
    return (
      <div className={cls}>
        <Note tone="warn" role="status">
          <strong className="text-ink">{operatorStop(rec.abort_reason) ? "Stopped by the operator" : "Kill switch fired"}</strong>
          {` during ${where}`}
          {rec.detected_s !== null ? ` at ${num(rec.detected_s, 1, " s")}` : ""}: {rec.abort_reason}.{dry ? "" : rec.rollback_verified ? " The fault was rolled back and verified." : " Rolling back."}
        </Note>
      </div>
    );
  }
  if (states.includes("active")) {
    const armed = !dry && [4, MONITOR, ROLLBACK].some((i) => states[i] === "active");
    return armed ? (
      <Note tone="accent" role="status">
        <strong className="text-ink">Kill switch armed.</strong> Any outage, latency above the limit, stale data or lost update stops the run and rolls the fault back.
      </Note>
    ) : (
      <Note>The kill switch arms when the fault is injected and can stop the run at any point until the rollback is verified.</Note>
    );
  }
  if (states.some((s) => s === "stopped" || s === "failed")) {
    return (
      <div className={cls}>
        <Note tone="warn" role="status">
          <strong className="text-ink">The run ended early.</strong> {rec.error ?? "See the activity log for the reason."}
        </Note>
      </div>
    );
  }
  if (dry) {
    return (
      <div className={cls}>
        <Note tone="warn">Dry run complete: every fault stage was previewed, not executed. Nothing was changed.</Note>
      </div>
    );
  }
  return (
    <div className={cls}>
      <Note tone="ok" role="status">
        <strong className="text-ok">Completed.</strong> No stop condition was met; every fault was rolled back and verified{rec.recovery_s !== null ? `, and Ditto recovered in ${num(rec.recovery_s, 1, " s")}` : ""}.
      </Note>
    </div>
  );
}

// ------------------------------------------------------------------ agents lane
const CHIP: Record<AgentChipState, { box: string; tone: string; icon: LucideIcon }> = {
  idle: { box: "border-line bg-surface opacity-70", tone: "text-faint", icon: Circle },
  queued: { box: "border-dashed border-accent/50 bg-surface", tone: "text-accent", icon: Circle },
  running: { box: "border-accent bg-accent/5 ring-2 ring-accent/30", tone: "text-accent", icon: Circle },
  completed: { box: "border-ok/40 bg-surface", tone: "text-ok", icon: Check },
  failed: { box: "border-bad bg-bad/5", tone: "text-bad", icon: TriangleAlert },
  skipped: { box: "border-dashed border-line bg-surface opacity-70", tone: "text-faint", icon: Ban },
};

function AgentDetails({ name, title, experimentId }: { name: string; title: string; experimentId: string | null }) {
  const runs = useAsync((s) => request<AgentRun[]>(`/api/agents/runs?agent=${encodeURIComponent(name)}${experimentId ? `&experiment_id=${encodeURIComponent(experimentId)}` : ""}&limit=1`, { signal: s }), [name, experimentId]);
  const run = runs.data?.[0] as (AgentRun & { input_ref?: string | null; output?: unknown }) | undefined;
  return (
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40" />
      <Dialog.Content aria-describedby={undefined} className="fixed left-1/2 top-1/2 z-50 flex max-h-[85vh] w-[min(46rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-lg border border-line bg-surface shadow-lg">
        <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
          <Dialog.Title className="text-sm font-semibold text-ink">{title}: run details</Dialog.Title>
          <Dialog.Close className="rounded-md p-1.5 text-dim hover:bg-raised hover:text-ink" aria-label="Close details">
            <X className="h-4 w-4" aria-hidden="true" />
          </Dialog.Close>
        </div>
        <div className="overflow-auto p-4 text-xs text-dim">
          {runs.loading ? (
            <Skeleton />
          ) : runs.error ? (
            <ErrorNote message={runs.error} onRetry={runs.reload} />
          ) : !run ? (
            <Empty>No stored run of this agent{experimentId ? " for this experiment" : ""}.</Empty>
          ) : (
            <div className="space-y-3">
              <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1">
                <dt className="text-faint">Status</dt>
                <dd className="text-ink">{humanize(run.status)}</dd>
                <dt className="text-faint">Input document</dt>
                <dd className="num break-all text-ink">{run.input_ref ?? "not recorded"}</dd>
                <dt className="text-faint">Started / finished</dt>
                <dd className="num text-ink">{`${run.started_at_utc} → ${run.finished_at_utc ?? "running"}`}</dd>
                <dt className="text-faint">Attempts / method</dt>
                <dd className="text-ink">{`${run.attempts} / ${run.method ?? "not reported"}`}</dd>
                <dt className="text-faint">Result</dt>
                <dd className="text-ink">{run.summary ?? "none"}</dd>
              </dl>
              {run.error && <Note tone="bad">{run.error}</Note>}
              <div>
                <div className="mb-1 font-medium text-ink">Output document (validated, read-only)</div>
                <pre className="num max-h-80 overflow-auto rounded border border-line bg-bg p-2 text-xxs text-dim">{run.output === undefined || run.output === null ? "no output" : JSON.stringify(run.output, null, 2)}</pre>
              </div>
            </div>
          )}
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  );
}

function AgentChip({ name, view, status, next, isLive }: { name: string; view: RunView; status: AgentStatus | undefined; next: boolean; isLive: boolean }) {
  const [open, setOpen] = useState(false);
  const { state, run, note } = agentChipState(name, view, status);
  const look = CHIP[state];
  const title = status?.title ?? humanize(name);
  const type = !status ? "unknown" : status.kind === "deterministic" ? "deterministic" : status.provider?.enabled ? "LLM on" : "LLM off: rules only";
  const duration = run?.duration_ms !== undefined && run?.duration_ms !== null ? (run.duration_ms < 1000 ? `${run.duration_ms} ms` : `${(run.duration_ms / 1000).toFixed(1)} s`) : null;
  const word = state === "queued" && next ? "queued: next" : state;
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button type="button" className={`w-full rounded-md border p-2.5 text-left transition-colors duration-200 ease-out hover:bg-raised ${look.box}`} data-agent={name} data-state={state} aria-label={`${title}: ${word}. Open run details`}>
          <span className="block text-xs font-semibold text-ink">{title}</span>
          <span key={state} className={`mt-1 flex items-center gap-1 text-xxs font-semibold uppercase tracking-wide ${look.tone}`}>
            {state === "running" ? <span className={`h-2 w-2 rounded-full bg-accent ${isLive ? "pl-pulse" : ""}`} aria-hidden="true" /> : <look.icon className={`h-3 w-3 shrink-0 ${isLive && state === "completed" ? "pl-draw" : ""}`} aria-hidden="true" />}
            {word}
          </span>
          <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xxs text-faint">
            <span className="rounded border border-line bg-raised px-1 py-px">{type}</span>
            {duration && <span className="num">{duration}</span>}
          </span>
          <span className="mt-1 block break-words text-xs text-dim">{run?.error ?? run?.summary ?? note ?? (state === "running" ? "working…" : state === "queued" ? "waiting for the agent before it" : "no result for this run")}</span>
        </button>
      </Dialog.Trigger>
      {open && <AgentDetails name={name} title={title} experimentId={PLAN_AGENTS.includes(name) ? null : view.record?.id ?? null} />}
    </Dialog.Root>
  );
}

function AgentsLane({ view, isLive }: { view: RunView; isLive: boolean }) {
  const live = useLive();
  const by = new Map(live.agents.map((a) => [a.name, a]));
  const states = POST_RUN_AGENTS.map((n) => agentChipState(n, view, by.get(n)).state);
  const nextQueued = states.includes("running") ? states.indexOf("queued") : -1;
  const group = (label: string, sub: string, names: string[], offset: number | null) => (
    <div>
      <div className="mb-2 text-xs">
        <span className="font-semibold text-ink">{label}</span> <span className="text-faint">{sub}</span>
      </div>
      <ol className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-1">
        {names.map((n, i) => (
          <li key={n}>
            <AgentChip name={n} view={view} status={by.get(n)} next={offset !== null && i === nextQueued} isLive={isLive} />
          </li>
        ))}
      </ol>
    </div>
  );
  return (
    <Panel title="Agents" meta="seven agents in a fixed order; they read and write validated documents and cannot execute a fault">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_2.5fr]">
        {group("Before the fault", "planning agents", PLAN_AGENTS, null)}
        <div>
          <div className="mb-2 text-xs">
            <span className="font-semibold text-ink">Analyse</span> <span className="text-faint">post-run agents, in this order</span>
          </div>
          <ol className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-5">
            {POST_RUN_AGENTS.map((n, i) => (
              <li key={n}>
                <AgentChip name={n} view={view} status={by.get(n)} next={i === nextQueued} isLive={isLive} />
              </li>
            ))}
          </ol>
        </div>
      </div>
    </Panel>
  );
}

// ------------------------------------------------------------------ sandbox strip
function Sandbox({ rec, running }: { rec: ExperimentRecord | null; running: boolean }) {
  const live = useLive();
  const services = live.services?.services ?? [];
  const hit = new Set(running ? (rec?.actions ?? []).filter((a) => ["applying", "applied", "rolling_back"].includes(a.status)).map((a) => a.target) : []);
  const targeted = new Set((rec?.actions ?? []).map((a) => a.target));
  const last: MetricRow | undefined = live.rows[live.rows.length - 1];
  const fleet = Object.values(last?.profiles ?? {});
  return (
    <Panel title="Sandbox" meta="the system under test; all twin data is synthetic">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[14rem_1fr]">
        <div className="rounded-md border border-line bg-raised p-3">
          <div className="text-xs font-semibold text-ink">Simulator</div>
          {fleet.length ? (
            <ul className="mt-1 space-y-0.5 text-xs text-dim">
              {fleet.map((b) => (
                <li key={b.title}>
                  <span className="num text-ink">{b.twins}</span> {b.title}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-xs text-faint">fleet not reported by the probe yet</p>
          )}
          <p className="mt-2 text-xxs text-faint">sends telemetry to Ditto</p>
        </div>
        <div>
          <div className="mb-2 text-xs font-semibold text-ink">Eclipse Ditto services</div>
          {services.length ? (
            <ul className="flex flex-wrap gap-2" aria-label="Ditto services">
              {services.map((s) => {
                const active = hit.has(s.service);
                return (
                  <li key={s.container} className={`rounded-md border px-2.5 py-1.5 text-xs transition-colors ${active ? "border-bad bg-bad/10" : targeted.has(s.service) ? "border-accent/50 bg-accent/5" : "border-line bg-surface"}`}>
                    <span className="font-medium text-ink">{s.service}</span>
                    <span className={`ml-2 ${active ? "font-semibold text-bad" : s.state === "running" ? "text-ok" : "text-warn"}`}>{active ? "fault active" : s.state}</span>
                    {!active && targeted.has(s.service) && <span className="ml-2 text-accent">target of this run</span>}
                  </li>
                );
              })}
            </ul>
          ) : (
            <Empty>Service states are not available (is Docker reachable?).</Empty>
          )}
          <p className="mt-3 text-xs text-dim" role="status">
            Latest probe:{" "}
            {last ? (
              <span className="num">
                read {last.direct_ok ? `${last.direct_ms.toFixed(0)} ms` : "FAILED"} · search {last.search_ok ? `${last.search_ms.toFixed(0)} ms` : "FAILED"} · staleness p95 {num(last.stale_p95_s, 1, " s")}
              </span>
            ) : (
              <span className="text-faint">no sample yet</span>
            )}
          </p>
        </div>
      </div>
    </Panel>
  );
}

// ------------------------------------------------------------------ page
export function Pipeline() {
  const { id: replayId } = useParams();
  const live = useLive();
  const now = useNow();
  const saved = useAsync((s) => (replayId ? api.experiment(replayId, s) : Promise.resolve(null)), [replayId]);
  const orch = live.orchestrator;
  const view: RunView = replayId
    ? { status: "saved", record: saved.data?.record ?? null, analysis: saved.data?.analysis ?? null, agentRuns: saved.data?.agent_runs ?? [] }
    : { status: orch?.status ?? "idle", record: orch?.record ?? null, analysis: orch?.analysis ?? null, agentRuns: orch?.agent_runs ?? [] };
  const rec = view.record;
  const { events } = useExperimentEvents(rec?.id ?? null, 1000);
  const states = stageStates(view);
  const running = !replayId && (view.status === "running" || view.status === "analyzing");
  const motionOk = !replayId && !reducedMotion();

  // Which cards changed since the last render of the SAME run. Up to three cards changing is a
  // real step (a stage finishes, an instant stage such as Inject passes, the next one starts)
  // and is animated; more than that is a catch-up (reconnect, first load) and is not.
  const sig = `${rec?.id ?? ""}:${states.join(",")}`;
  const seen = useRef<{ sig: string; id: string | null; states: StageState[]; changed: Set<number>; completed: boolean }>({ sig: "", id: null, states: [], changed: new Set(), completed: false });
  if (seen.current.sig !== sig) {
    const p = seen.current;
    const same = p.id !== null && p.id === (rec?.id ?? null);
    const diff = states.map((s, i) => (s !== p.states[i] ? i : -1)).filter((i) => i >= 0);
    const step = same && motionOk && diff.length > 0 && diff.length <= 3;
    seen.current = { sig, id: rec?.id ?? null, states, changed: new Set(step ? diff : []), completed: step && p.states.includes("active") && !states.includes("active") };
  }
  const { changed, completed } = seen.current;

  const runStart = events.length ? Date.parse(events[0].ts) / 1000 - events[0].t_s : null;
  const startedAt = (i: number): number | null => {
    if (i === ANALYSE) {
      const since = live.agents.find((a) => a.state === "running" && a.task === rec?.id)?.since;
      return since ? Date.parse(since) / 1000 : null;
    }
    const first = events.find((e) => STAGES[i].states.includes(e.state ?? ""));
    return first && runStart !== null ? runStart + first.t_s : null;
  };
  const activeIndex = states.indexOf("active");
  const announce = !rec ? "" : activeIndex >= 0 ? `Stage ${activeIndex + 1}, ${STAGES[activeIndex].title}: active` : `Run finished. ${STAGES.map((s, i) => `${s.title}: ${LOOK[states[i]].word}`).join(". ")}`;
  const dry = rec ? isDry(rec) : false;

  const cards = (from: number, to: number) => (
    <ol className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2 xl:grid-cols-4">
      {STAGES.slice(from, to).map((s, k) => {
        const i = from + k;
        return <StageCard key={s.key} index={i} state={states[i]} view={view} events={events} th={live.thresholds} startedAt={states[i] === "active" ? startedAt(i) : null} nowMs={now} animate={changed.has(i)} sweep={completed} isLive={motionOk} />;
      })}
    </ol>
  );

  return (
    <div className="space-y-4">
      <PageHeader
        title="Pipeline"
        sub="One experiment, stage by stage, as the server-side controller reports it. Every value comes from the run's own record and events."
        actions={
          rec ? (
            <>
              <Badge tone={running ? "ok" : "muted"}>{running ? "live" : replayId ? "saved run: replay" : "last run, not live"}</Badge>
              <Badge tone={dry ? "warn" : "accent"}>{dry ? "dry run: nothing is injected" : "real run"}</Badge>
            </>
          ) : undefined
        }
      />
      <p className="sr-only" aria-live="polite" data-testid="stage-announcer">
        {announce}
      </p>
      {replayId && saved.loading ? (
        <Skeleton />
      ) : replayId && (saved.error || !rec) ? (
        <ErrorNote message={saved.error ?? "This experiment could not be loaded."} onRetry={saved.reload} />
      ) : !rec ? (
        <Note role="status">
          No experiment has run since the API started. Start one from{" "}
          <Link className="text-accent hover:underline" to="/control">
            Experiment control
          </Link>
          ; this page follows it as it happens.
        </Note>
      ) : (
        <p className="text-xs text-dim">
          Run{" "}
          <Link className="num text-accent hover:underline" to={`/history/${rec.id}`}>
            {rec.id}
          </Link>{" "}
          · <span className="num">{rec.signature ?? `${rec.fault}@${rec.target}`}</span> · state <span className="font-medium text-ink">{humanize(view.status === "analyzing" ? "analyzing" : rec.state)}</span>
          {replayId && (
            <>
              {" "}
              ·{" "}
              <Link className="text-accent hover:underline" to="/pipeline">
                back to the live pipeline
              </Link>
            </>
          )}
        </p>
      )}

      <Panel title="Before the fault" meta="nothing is touched in these stages">
        {cards(0, 4)}
      </Panel>
      <Panel title="During and after" meta="the kill switch covers inject, monitor and roll back">
        {cards(4, 8)}
        {rec && (
          <div className="mt-3">
            <Banner view={view} states={states} events={events} slide={completed} />
          </div>
        )}
      </Panel>
      <AgentsLane view={view} isLive={motionOk} />
      <Sandbox rec={rec} running={running} />
    </div>
  );
}
