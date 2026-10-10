// Step-by-step activity log of one experiment, live or saved. Every row is an event the
// backend controller recorded; nothing here is generated or animated by the frontend.
import { useEffect, useMemo, useRef, useState } from "react";
import type { EventKind, ExperimentEvent } from "../api/types";
import { useExperimentEvents } from "../hooks";
import { humanize } from "../lib/format";
import { Badge, Button, Note, Skeleton, StateBadge } from "./ui";

export const LIFECYCLE: { state: string; help: string }[] = [
  { state: "PRECHECK", help: "Checks authorization, the policy limits, the recovery lockout and the locks before anything else happens." },
  { state: "PREFLIGHT", help: "Resolves each target container and runs every fault's read-only checks. If one fails, nothing is injected." },
  { state: "BASELINE", help: "Measures Ditto while it is healthy and fixes the stop limits that the run will be judged against." },
  { state: "INJECTING", help: "Applies the faults. Each command is listed with whether it actually ran." },
  { state: "MONITORING", help: "Probes Ditto every tick and decides, each time, whether to continue or stop." },
  { state: "REVERTING", help: "Removes every applied fault and verifies, from the observed state, that it is gone." },
  { state: "VERIFYING_RECOVERY", help: "Waits for consecutive healthy ticks to confirm that Ditto has recovered." },
];
const TERMINAL = ["COMPLETED", "ABORTED", "FAILED", "RECOVERY_TIMEOUT", "RECOVERY_REQUIRED"];

const KIND: Record<EventKind, { label: string; text: string; bar: string }> = {
  info: { label: "INFO", text: "text-dim", bar: "border-l-line" },
  command: { label: "CMD", text: "text-accent", bar: "border-l-accent" },
  health: { label: "HEALTH", text: "text-ok", bar: "border-l-ok" },
  decision: { label: "DECISION", text: "text-ink", bar: "border-l-ink" },
  warning: { label: "WARNING", text: "text-warn", bar: "border-l-warn" },
  error: { label: "ERROR", text: "text-bad", bar: "border-l-bad" },
};

function metric(label: string, value: number | null | undefined, unit: string, ok?: boolean) {
  const known = typeof value === "number";
  return (
    <span key={label} className={`num whitespace-nowrap rounded border px-1.5 py-0.5 text-xxs ${ok === false ? "border-bad/60 text-bad" : known ? "border-line text-dim" : "border-dashed border-line text-faint"}`}>
      {label} {ok === false ? "FAILED" : known ? `${value}${unit}` : "unavailable"}
    </span>
  );
}

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
  };
  return (
    <button type="button" onClick={copy} aria-label="Copy command" className="h-6 shrink-0 rounded border border-line bg-raised px-2 text-xxs text-dim hover:text-ink">
      {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "Copy"}
    </button>
  );
}

function CommandBody({ ev }: { ev: ExperimentEvent }) {
  const d = ev.detail;
  const command = d.command ?? ev.message;
  return (
    <div className="mt-1 space-y-1">
      <div className="flex items-start gap-2">
        <code className="num min-w-0 flex-1 overflow-x-auto whitespace-pre-wrap break-all rounded border border-line bg-bg px-2 py-1 text-xxs text-ink">{command}</code>
        <CopyButton text={command} />
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xxs">
        {d.executed ? <Badge tone={d.timed_out || (d.returncode ?? 0) !== 0 ? "bad" : "ok"}>EXECUTED</Badge> : <Badge tone="warn">DRY RUN — NOT EXECUTED</Badge>}
        {d.executed && d.timed_out && <span className="text-bad">timed out; effect unknown</span>}
        {d.executed && !d.timed_out && <span className="num text-dim">exit code {d.returncode ?? "unknown"}</span>}
        {d.read_only && <span className="text-faint">read-only</span>}
        {typeof d.duration_ms === "number" && <span className="num text-faint">{d.duration_ms} ms</span>}
      </div>
      {d.stderr && <pre className="num overflow-x-auto whitespace-pre-wrap rounded border border-bad/40 bg-bg px-2 py-1 text-xxs text-bad">{d.stderr}</pre>}
    </div>
  );
}

function HealthBody({ ev }: { ev: ExperimentEvent }) {
  const d = ev.detail;
  if (d.probe_timeout) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1.5">
      {metric("direct", d.direct_ms, " ms", d.direct_ok)}
      {metric("search", d.search_ms, " ms", d.search_ok)}
      {metric("staleness p95", d.stale_p95_s, " s")}
      {metric("lost updates", d.lost_acked, "")}
      {metric("search drift", d.search_drift, "")}
    </div>
  );
}

function Row({ ev }: { ev: ExperimentEvent }) {
  const [open, setOpen] = useState(false);
  const k = KIND[ev.kind] ?? KIND.info;
  const hasDetail = ev.kind !== "command" && ev.kind !== "health" && Object.keys(ev.detail ?? {}).length > 0;
  const text = ev.kind === "command" ? (ev.detail.executed ? "Command run" : "Command preview") : ev.message;
  return (
    <li className={`border-l-2 ${k.bar} px-2 py-1.5 ${ev.kind === "decision" ? "bg-raised" : ""} ${ev.kind === "error" ? "bg-bad/10" : ""}`} data-kind={ev.kind} data-seq={ev.seq}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs">
        <span className="num w-14 shrink-0 text-right text-faint">+{ev.t_s.toFixed(1)}s</span>
        <span className="w-28 shrink-0 text-xxs uppercase tracking-wide text-faint">{humanize(ev.state)}</span>
        <span className={`w-16 shrink-0 text-xxs font-semibold ${k.text}`}>{k.label}</span>
        <span className={`min-w-0 flex-1 ${ev.kind === "decision" ? "font-medium text-ink" : ev.kind === "error" ? "text-ink" : "text-dim"}`}>{text}</span>
        {hasDetail && (
          <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)} className="text-xxs text-accent hover:underline">
            {open ? "Hide details" : "Details"}
          </button>
        )}
      </div>
      <div className="sm:pl-[16.5rem]">
        {ev.kind === "command" && <CommandBody ev={ev} />}
        {ev.kind === "health" && <HealthBody ev={ev} />}
        {hasDetail && open && <pre className="num mt-1 max-h-60 overflow-auto rounded border border-line bg-bg px-2 py-1 text-xxs text-dim">{JSON.stringify(ev.detail, null, 2)}</pre>}
      </div>
    </li>
  );
}

export function ActivityLog({ experimentId, currentState, pollMs = 1500 }: { experimentId: string; currentState?: string | null; pollMs?: number }) {
  const { events, loading, error, terminal, dryRun, polling, state } = useExperimentEvents(experimentId, pollMs);
  const [filter, setFilter] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  const sawLive = useRef(false);
  const now = currentState ?? state;

  useEffect(() => {
    setFilter(null);
    setFollow(true);
    sawLive.current = false;
  }, [experimentId]);

  // A run that is still going is followed from the newest event. A saved record is a
  // replay: it opens at the first event, and "Jump to latest" goes to the end.
  useEffect(() => {
    if (loading) return;
    if (!terminal) sawLive.current = true;
    else if (!sawLive.current) setFollow(false);
  }, [loading, terminal]);

  const counts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const e of events) if (e.state) m[e.state] = (m[e.state] ?? 0) + 1;
    return m;
  }, [events]);
  const shown = useMemo(() => (filter ? events.filter((e) => (filter === "END" ? TERMINAL.includes(e.state ?? "") : e.state === filter)) : events), [events, filter]);
  const endCount = TERMINAL.reduce((n, s) => n + (counts[s] ?? 0), 0);

  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [shown.length, follow]);

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
  };
  const jump = () => {
    setFollow(true);
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  };
  const help = filter === "END" ? "The final outcome of the run." : LIFECYCLE.find((s) => s.state === filter)?.help;

  return (
    <section aria-label="Activity log">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-dim">Activity log</h3>
          {dryRun && <Badge tone="warn">dry run: nothing is executed</Badge>}
          <span className="text-xxs text-faint" role="status">
            {polling ? "live, updating" : terminal ? `${events.length} event(s), complete` : ""}
          </span>
        </div>
        {filter && (
          <Button className="h-6 px-2" onClick={() => setFilter(null)}>
            Show all events
          </Button>
        )}
      </div>

      <ol className="mb-2 flex flex-wrap gap-1" aria-label="Lifecycle steps (select one to filter the log)">
        {LIFECYCLE.map((s) => {
          const n = counts[s.state] ?? 0;
          const selected = filter === s.state;
          return (
            <li key={s.state}>
              <button
                type="button"
                aria-pressed={selected}
                aria-current={now === s.state ? "step" : undefined}
                disabled={n === 0}
                onClick={() => setFilter(selected ? null : s.state)}
                title={s.help}
                className={`rounded border px-2 py-1 text-xxs uppercase tracking-wide disabled:cursor-default disabled:opacity-40 ${selected ? "border-accent bg-accent/15 text-ink" : now === s.state ? "border-accent text-ink" : "border-line text-dim hover:text-ink"}`}
              >
                {humanize(s.state)} <span className="num text-faint">{n}</span>
              </button>
            </li>
          );
        })}
        <li>
          <button type="button" aria-pressed={filter === "END"} disabled={endCount === 0} onClick={() => setFilter(filter === "END" ? null : "END")} className={`rounded border px-2 py-1 text-xxs uppercase tracking-wide disabled:opacity-40 ${filter === "END" ? "border-accent bg-accent/15 text-ink" : "border-line text-dim hover:text-ink"}`}>
            outcome <span className="num text-faint">{endCount}</span>
          </button>
        </li>
        {now && TERMINAL.includes(now) && (
          <li className="px-1 py-0.5">
            <StateBadge state={now} />
          </li>
        )}
      </ol>
      {filter && help && (
        <p className="mb-2 text-xs text-dim">
          <span className="text-ink">{filter === "END" ? "Outcome" : humanize(filter)}:</span> {help} Showing {shown.length} of {events.length} events.
        </p>
      )}

      {error && (
        <div className="mb-2">
          <Note tone="warn" role="status">
            Could not update the activity log: {error} {events.length > 0 ? "The events below are the last ones received; retrying." : "Retrying."}
          </Note>
        </div>
      )}

      {loading && events.length === 0 ? (
        <Skeleton rows={4} />
      ) : events.length === 0 ? (
        <p className="rounded border border-line bg-raised px-3 py-4 text-center text-xs text-faint">
          {terminal ? "This record has no activity log (it was saved before the log existed)." : "No events recorded yet."}
        </p>
      ) : (
        <div className="relative">
          <div ref={box} onScroll={onScroll} role="log" aria-live="polite" aria-label="Experiment events" tabIndex={0} className="max-h-[28rem] overflow-y-auto rounded border border-line bg-surface">
            <ol className="divide-y divide-line/60">
              {shown.map((ev) => (
                <Row key={ev.seq} ev={ev} />
              ))}
            </ol>
          </div>
          {!follow && (
            <div className="absolute bottom-2 right-3">
              <Button variant="primary" className="h-7 shadow" onClick={jump}>
                Jump to latest
              </Button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
