import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { FaultAction, FaultCatalog, FaultSpec, Mode, PreflightResult, Proposal, SandboxChecks, Scenario, ValidationReport } from "../api/types";
import { ActivityLog } from "../components/ActivityLog";
import { ActionsTable, PlanReport } from "../components/experiment";
import { Badge, Button, Empty, ErrorNote, KV, List, Note, PageHeader, Panel, Skeleton, StateBadge } from "../components/ui";
import { useAction, useAsync } from "../hooks";
import { dateTime, humanize, num } from "../lib/format";
import { useLive, useLiveClient } from "../live/LiveContext";

const MODE_HELP: Record<Mode, string> = {
  observe: "Read-only. Nothing can be injected by anyone.",
  recommend: "Agents propose. An operator may authorize a proposed scenario; custom scenarios are refused.",
  manual: "An operator may author and authorize any scenario that passes the policy.",
  autonomous_sandbox: "The controller runs proposed scenarios on its own, inside the sandbox policy, while every sandbox check passes.",
};
const requestId = () => `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

function ModePanel() {
  const live = useLive();
  const client = useLiveClient();
  const ctl = live.controller!;
  const set = useAction((m: Mode) => api.setMode(m), (c) => client.setController(c));
  const [checks, setChecks] = useState<SandboxChecks | null>(null);
  const check = useAction(() => api.sandbox(), setChecks);
  const start = useAction(() => api.startAutonomous(), (c) => client.setController(c));
  const stop = useAction((withExperiment: boolean) => api.stopAutonomous(withExperiment), (c) => client.setController(c));
  const loop = ctl.loop;
  return (
    <Panel title="Operating mode" actions={<Badge tone={ctl.mode === "autonomous_sandbox" ? "warn" : "accent"}>{humanize(ctl.mode)}</Badge>}>
      <fieldset disabled={set.pending} className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-4">
        <legend className="sr-only">Operating mode</legend>
        {ctl.modes.map((m) => {
          const blocked = m === "autonomous_sandbox" && !ctl.sandbox_enabled_in_config;
          return (
            <label key={m} className={`flex cursor-pointer gap-2 rounded border p-2 text-xs ${ctl.mode === m ? "border-accent bg-raised" : "border-line"} ${blocked ? "cursor-not-allowed opacity-50" : ""}`}>
              <input type="radio" name="mode" className="mt-0.5" checked={ctl.mode === m} disabled={blocked} onChange={() => set.run(m)} />
              <span>
                <span className="font-medium text-ink">{humanize(m)}</span>
                <span className="mt-0.5 block text-xxs text-faint">{blocked ? "Disabled: set AUTONOMY_SANDBOX_ENABLED=true in .env and restart the API." : MODE_HELP[m]}</span>
              </span>
            </label>
          );
        })}
      </fieldset>
      {set.error && <div className="mt-2"><ErrorNote message={set.error} /></div>}

      {ctl.mode === "autonomous_sandbox" && (
        <div className="mt-3 grid grid-cols-1 gap-3 border-t border-line pt-3 xl:grid-cols-2">
          <div>
            <KV
              items={[
                ["Loop", loop.running ? <Badge tone="accent">{loop.phase}</Badge> : <Badge>stopped</Badge>],
                ["Experiments run", `${loop.executed} of ${ctl.policy.max_experiments} (cycle ${loop.cycles})`],
                ["Last decision", loop.last_decision ? `${loop.last_decision.signature} · ${loop.last_decision.method} · rank #${loop.last_decision.rank}` : null],
                ["Last experiment", loop.last_experiment_id ? <Link className="num text-accent hover:underline" to={`/history/${loop.last_experiment_id}`}>{loop.last_experiment_id}</Link> : null],
                ["Stopped because", loop.stopped_reason],
                ["Allowed faults", ctl.policy.allowed_faults.join(", ")],
                ["Allowed targets", ctl.policy.allowed_targets.join(", ")],
                ["Per-scenario limits", `${ctl.policy.max_actions} action(s), ${ctl.policy.max_duration_s}s, cooldown ${ctl.policy.cooldown_s}s`],
              ]}
            />
            <div className="mt-3 flex flex-wrap gap-2">
              {!loop.running ? (
                <Button variant="primary" busy={start.pending} onClick={() => start.run()}>
                  Start autonomous loop
                </Button>
              ) : (
                <>
                  <Button busy={stop.pending} onClick={() => stop.run(false)}>
                    Stop after current experiment
                  </Button>
                  <Button variant="danger" busy={stop.pending} onClick={() => stop.run(true)}>
                    Stop loop and experiment now
                  </Button>
                </>
              )}
              <Button busy={check.pending} onClick={() => check.run()}>
                Run sandbox checks
              </Button>
            </div>
            {(start.error || stop.error || check.error) && <div className="mt-2"><ErrorNote message={(start.error || stop.error || check.error)!} /></div>}
          </div>
          <div>
            {checks ? (
              <ul className="space-y-1 text-xs" aria-label="Sandbox checks">
                {checks.checks.map((c) => (
                  <li key={c.name} className="grid grid-cols-[4.5rem_1fr] gap-2">
                    <Badge tone={c.ok ? "ok" : "bad"}>{c.ok ? "pass" : "fail"}</Badge>
                    <span>
                      <span className="text-ink">{c.name}</span> <span className="text-faint">{c.detail}</span>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>Sandbox checks have not been run from this page. The loop runs them itself before every experiment.</Empty>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}

function ActiveRun() {
  const live = useLive();
  const client = useLiveClient();
  const orch = live.orchestrator!;
  const rec = orch.record;
  const stop = useAction(() => api.stop(), () => client.refresh());
  const reset = useAction(() => api.reset(), () => client.refresh());
  if (!rec) return null;
  const running = orch.status === "running";
  const row = rec.timeline.length ? rec.timeline[rec.timeline.length - 1] : null;
  const dry = rec.dry_run || rec.simulated;
  return (
    <Panel
      title={dry ? (running ? "Running dry run" : "Last dry run") : running ? "Running experiment" : orch.status === "analyzing" ? "Agents analysing the run" : "Last experiment"}
      meta={<Link className="num text-accent hover:underline" to={`/history/${rec.id}`}>{rec.id}</Link>}
      actions={
        running ? (
          <Button variant="danger" busy={stop.pending} disabled={orch.stop_requested} onClick={() => stop.run()}>
            {orch.stop_requested ? "Stop requested" : "Emergency stop"}
          </Button>
        ) : orch.status === "done" || orch.status === "error" ? (
          <Button busy={reset.pending} onClick={() => reset.run()}>
            Dismiss
          </Button>
        ) : null
      }
    >
      {dry && <div className="mb-3"><Note tone="warn">Dry run: the lifecycle and health checks are real, but no Docker command is executed and nothing is changed.</Note></div>}
      <div className="mb-3 flex items-center gap-2 text-xs text-faint">
        State <StateBadge state={orch.status === "analyzing" ? "analyzing" : rec.state} />
      </div>
      <div className="mb-4">
        <ActivityLog key={rec.id} experimentId={rec.id} currentState={rec.state} />
      </div>
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
        <KV
          items={[
            ["Scenario", <span className="num">{rec.signature ?? `${rec.fault}@${rec.target}`}</span>],
            ["Authorized by", rec.authorization ? `${rec.authorization.actor} (${humanize(rec.authorization.kind)})` : dry ? "not needed for a dry run" : null],
            ["Recovery state", <StateBadge state={orch.recovery_state} />],
            ["Stop reason", rec.abort_reason],
            ["Detected after", num(rec.detected_s, 1, " s")],
            ["Recovered after", dry ? "not applicable (dry run)" : rec.recovery_outcome ? `${humanize(rec.recovery_outcome)}${rec.recovery_s !== null ? ` in ${num(rec.recovery_s, 1, " s")}` : ""}` : null],
            ["Latest probe", row ? `direct ${row.direct_ok ? `${row.direct_ms.toFixed(0)} ms` : "FAILED"} · search ${row.search_ok ? `${row.search_ms.toFixed(0)} ms` : "FAILED"}` : null],
          ]}
        />
        <div className="xl:col-span-2">
          <ActionsTable actions={rec.actions} />
        </div>
      </div>
      {(rec.error || orch.error) && <div className="mt-3"><Note tone={rec.state === "RECOVERY_REQUIRED" ? "bad" : "warn"}>{rec.error || orch.error}</Note></div>}
      {stop.error && <div className="mt-2"><ErrorNote message={stop.error} /></div>}
      {reset.error && <div className="mt-2"><ErrorNote message={reset.error} /></div>}
      {orch.agent_runs && (
        <div className="mt-3 flex flex-wrap gap-2 border-t border-line pt-3 text-xs">
          {orch.agent_runs.map((r) => (
            <span key={r.id} title={r.error ?? r.summary ?? ""} className="flex items-center gap-1.5">
              <span className="text-dim">{r.title}</span> <StateBadge state={r.status} />
            </span>
          ))}
        </div>
      )}
    </Panel>
  );
}

function AuthorizeRun({ label, disabledReason, run, pending, error }: { label: string; disabledReason: string | null; run: () => void; pending: boolean; error: string | null }) {
  const [ack, setAck] = useState(false);
  return (
    <div className="mt-3 border-t border-line pt-3">
      <label className="flex items-start gap-2 text-xs text-dim">
        <input type="checkbox" className="mt-0.5" checked={ack} onChange={(e) => setAck(e.target.checked)} disabled={Boolean(disabledReason)} />
        <span>I am an operator and I authorize REAL fault injection for this scenario against the local Ditto sandbox.</span>
      </label>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <Button
          variant="danger"
          busy={pending}
          disabled={!ack || Boolean(disabledReason)}
          onClick={() => {
            run();
            setAck(false);
          }}
        >
          {label}
        </Button>
        {disabledReason && <span className="text-xs text-warn">{disabledReason}</span>}
      </div>
      {error && <div className="mt-2"><ErrorNote message={error} /></div>}
    </div>
  );
}

function useBlockReason(custom: boolean): string | null {
  const live = useLive();
  const ctl = live.controller!;
  const st = live.orchestrator?.status;
  if (live.conn !== "live") return "Not connected to the backend.";
  if (ctl.lockout) return "Recovery is required; fault injection is disabled.";
  if (st === "running" || st === "analyzing") return "An experiment is already in progress.";
  if (ctl.mode === "observe") return "Observe mode: nothing can be injected.";
  if (ctl.mode === "recommend" && custom) return "Recommend mode only runs proposed scenarios. Switch to manual mode for a custom one.";
  return null;
}

function ProposalPanel() {
  const live = useLive();
  const client = useLiveClient();
  const [proposal, setProposal] = useState<Proposal | null>(live.orchestrator?.scenario_proposal ?? null);
  const plan = useAction(() => api.plan(), setProposal);
  const run = useAction(() => api.runScenario({ authorize: true, proposal_id: proposal!.proposal_id, request_id: requestId() }), () => client.refresh());
  const block = useBlockReason(false);
  return (
    <Panel
      title="Agent proposal"
      meta={proposal ? `generated ${dateTime(proposal.generated_at_utc)}` : "fault-injection agent"}
      actions={
        <Button busy={plan.pending} onClick={() => plan.run()}>
          {proposal ? "Propose again" : "Generate proposal"}
        </Button>
      }
    >
      {plan.error && <ErrorNote message={plan.error} />}
      {!proposal && !plan.error && <Empty>No proposal yet. Generating one is read-only: it ranks candidates from stored history and never starts anything.</Empty>}
      {proposal && (
        <div className="space-y-3 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="num text-sm text-ink">{proposal.validation.signature}</span>
            <Badge tone={proposal.method === "deterministic_fallback" ? "muted" : "accent"}>{proposal.method === "deterministic_fallback" ? "rule-based selection" : `model: ${proposal.method}`}</Badge>
            <Badge>rank #{proposal.ranking.rank} · score {proposal.ranking.score}</Badge>
            <Badge tone={proposal.confidence.level === "none" ? "warn" : "ok"}>
              evidence: {proposal.confidence.basis === "none" ? "none" : `${proposal.confidence.agreement} runs (${proposal.confidence.basis})`}
            </Badge>
          </div>
          {proposal.fallback_note && <Note tone="warn">{proposal.fallback_note}</Note>}
          {proposal.store_degraded && <Note tone="warn">Knowledge store unavailable; ranked from experiment files only. {proposal.store_degraded}</Note>}
          <KV
            items={[
              ["Objective", proposal.objective],
              ["Hypothesis", proposal.hypothesis],
              ["Why this one", proposal.target_reason],
              ["Expected impact", proposal.expected_impact],
              ["Expected symptoms", <List items={proposal.expected_symptoms} />],
              ["Dependencies", proposal.dependencies.join(", ")],
              ["Safety risks", <List items={proposal.safety_risks} />],
              ["Stops automatically when", <List items={proposal.stop_conditions} />],
              ["Learning value", proposal.learning_value],
              ["Alternatives", <List items={proposal.alternatives.slice(0, 4).map((a) => `#${a.rank} ${a.signature} (${a.blocked ? "blocked" : `score ${a.score}`})`)} />],
            ]}
          />
          <PlanReport report={proposal.validation} />
          <AuthorizeRun label="Run proposed scenario" disabledReason={block} run={() => run.run()} pending={run.pending} error={run.error} />
        </div>
      )}
    </Panel>
  );
}

const newAction = (catalog: FaultCatalog): FaultAction => ({ fault: catalog.faults.find((f) => f.supported)?.name ?? "", target: catalog.targets[0] ?? "", params: {}, start_offset_s: 0, duration_s: null });

function ActionEditor({ a, spec, catalog, onChange, onRemove, index, removable }: { a: FaultAction; spec: FaultSpec | undefined; catalog: FaultCatalog; onChange: (a: FaultAction) => void; onRemove: () => void; index: number; removable: boolean }) {
  const numField = (label: string, value: number | null, min: number, max: number, set: (v: number | null) => void, optional = false) => (
    <div>
      <label className="label" htmlFor={`${label}-${index}`}>
        {label}
      </label>
      <input
        id={`${label}-${index}`}
        type="number"
        className="field w-28"
        min={min}
        max={max}
        value={value ?? ""}
        placeholder={optional ? "until end" : undefined}
        onChange={(e) => set(e.target.value === "" ? null : Number(e.target.value))}
      />
    </div>
  );
  return (
    <div className="rounded border border-line bg-raised p-3">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="label" htmlFor={`fault-${index}`}>
            Fault {index + 1}
          </label>
          <select id={`fault-${index}`} className="field w-52" value={a.fault} onChange={(e) => onChange({ ...a, fault: e.target.value, params: {} })}>
            {catalog.faults.map((f) => (
              <option key={f.name} value={f.name} disabled={!f.supported}>
                {f.title}
                {f.supported ? "" : " (not supported)"}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor={`target-${index}`}>
            Target
          </label>
          <select id={`target-${index}`} className="field w-40" value={a.target} onChange={(e) => onChange({ ...a, target: e.target.value })}>
            {catalog.targets.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </div>
        {spec?.params
          .filter((p) => p.name !== "ttl_s")
          .map((p) => (
            <div key={p.name}>
              <label className="label" htmlFor={`${p.name}-${index}`} title={p.description}>
                {p.name.replace(/_/g, " ")} ({p.minimum}–{p.maximum} {p.unit})
              </label>
              <input
                id={`${p.name}-${index}`}
                type="number"
                className="field w-36"
                min={p.minimum}
                max={p.maximum}
                value={a.params[p.name] ?? p.default}
                onChange={(e) => onChange({ ...a, params: { ...a.params, [p.name]: Number(e.target.value) } })}
              />
            </div>
          ))}
        {numField("start at (s)", a.start_offset_s, 0, 300, (v) => onChange({ ...a, start_offset_s: v ?? 0 }))}
        {numField("for (s)", a.duration_s, 2, 600, (v) => onChange({ ...a, duration_s: v }), true)}
        <Button onClick={onRemove} disabled={!removable} aria-label={`Remove fault ${index + 1}`}>
          Remove
        </Button>
      </div>
      {spec && <p className="mt-2 text-xxs text-faint">{spec.description}</p>}
    </div>
  );
}

function Builder({ catalog }: { catalog: FaultCatalog }) {
  const client = useLiveClient();
  const [actions, setActions] = useState<FaultAction[]>(() => [newAction(catalog)]);
  const [maxDuration, setMaxDuration] = useState(60);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [pre, setPre] = useState<PreflightResult | null>(null);
  const scenario: Scenario = { name: "operator scenario", actions, max_duration_s: maxDuration };
  const edit = (next: FaultAction[]) => {
    setActions(next);
    setReport(null); // a preview only describes the scenario it was computed for
    setPre(null);
  };
  const validate = useAction(() => api.validate(scenario), setReport);
  const preflight = useAction(() => api.preflight(scenario), (r) => {
    setPre(r);
    setReport(r.validation);
  });
  const run = useAction(() => api.runScenario({ authorize: true, scenario, request_id: requestId() }), () => client.refresh());
  const dryRun = useAction(() => api.runScenario({ authorize: false, dry_run: true, scenario, request_id: requestId() }), () => client.refresh());
  const live = useLive();
  const busy = live.orchestrator?.status === "running" || live.orchestrator?.status === "analyzing";
  const block = useBlockReason(true);
  const notPreviewed = !report ? "Preview the scenario first." : !report.ok ? "The policy refused this scenario." : null;
  const specs = Object.fromEntries(catalog.faults.map((f) => [f.name, f]));

  return (
    <Panel title="Scenario builder" meta={`limits: ${catalog.limits.max_affected_services} services, ${catalog.limits.max_active_faults} concurrent faults, intensity ${catalog.limits.max_cumulative_intensity}`}>
      <div className="space-y-2">
        {actions.map((a, i) => (
          <ActionEditor key={i} index={i} a={a} spec={specs[a.fault]} catalog={catalog} removable={actions.length > 1} onChange={(n) => edit(actions.map((x, j) => (j === i ? n : x)))} onRemove={() => edit(actions.filter((_, j) => j !== i))} />
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <Button onClick={() => edit([...actions, newAction(catalog)])} disabled={actions.length >= 8}>
          Add fault
        </Button>
        <div>
          <label className="label" htmlFor="max-duration">
            Scenario max duration (s)
          </label>
          <input
            id="max-duration"
            type="number"
            className="field w-32"
            min={10}
            max={catalog.limits.max_duration_s}
            value={maxDuration}
            onChange={(e) => {
              setMaxDuration(Number(e.target.value));
              setReport(null);
              setPre(null);
            }}
          />
        </div>
        <Button variant="primary" busy={validate.pending} onClick={() => validate.run()}>
          Preview plan
        </Button>
        <Button busy={preflight.pending} onClick={() => preflight.run()}>
          Preflight (read-only)
        </Button>
        <Button busy={dryRun.pending} disabled={busy} onClick={() => dryRun.run()} title={busy ? "An experiment is already in progress." : undefined}>
          Dry run (no injection)
        </Button>
      </div>
      <p className="mt-2 text-xxs text-faint">
        Preview, preflight and dry run never inject anything. Preview is a pure policy check; preflight only inspects the containers; a dry run walks the whole lifecycle with real health checks and lists every command it would run, without executing any.
      </p>
      {(validate.error || preflight.error || dryRun.error) && <div className="mt-2"><ErrorNote message={(validate.error || preflight.error || dryRun.error)!} /></div>}
      {catalog.environment && !catalog.environment.docker_available && <div className="mt-2"><Note tone="warn">Docker is not reachable: {catalog.environment.error}</Note></div>}
      {catalog.environment?.netshoot_image_present === false && <div className="mt-2"><Note tone="warn">Network faults need the helper image: run `docker pull nicolaka/netshoot`.</Note></div>}

      {report && (
        <div className="mt-3 space-y-3 border-t border-line pt-3">
          <div className="flex items-center gap-2 text-xs">
            <Badge tone={report.ok ? "ok" : "bad"}>{report.ok ? "policy check passed" : "refused"}</Badge>
            <span className="text-faint">Preview only. Nothing has been executed.</span>
          </div>
          <PlanReport report={report} />
          {report.prediction && <Note>{report.prediction.statement}</Note>}
          {pre && (
            <Note tone={pre.ok ? "ok" : "bad"}>
              <div className="mb-1 font-medium text-ink">Preflight {pre.ok ? "passed" : "failed"}</div>
              {pre.error && <div>{pre.error}</div>}
              <List items={pre.results.map((r) => `${r.id} ${r.fault} on ${r.target}: ${r.ok ? "ready" : r.detail}`)} empty="" />
            </Note>
          )}
        </div>
      )}
      <AuthorizeRun label="Run this scenario" disabledReason={block ?? notPreviewed} run={() => run.run()} pending={run.pending} error={run.error} />
    </Panel>
  );
}

export function Control() {
  const live = useLive();
  const catalog = useAsync((s) => api.faults(s));
  return (
    <div className="space-y-4">
      <PageHeader title="Experiment control" sub="Every run is validated by the policy engine and executed by the server-side controller, which stops and rolls back on its own. Closing this page does not affect a running experiment." />
      {live.controller && <ModePanel />}
      {live.orchestrator?.record && <ActiveRun />}
      <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
        <ProposalPanel />
        {catalog.loading && !catalog.data ? (
          <Panel title="Scenario builder">
            <Skeleton />
          </Panel>
        ) : catalog.error || !catalog.data ? (
          <Panel title="Scenario builder">
            <ErrorNote message={catalog.error ?? "Fault catalog unavailable."} onRetry={catalog.reload} />
          </Panel>
        ) : (
          <Builder catalog={catalog.data} />
        )}
      </div>
      {catalog.data && (
        <Panel title="Fault types" flush>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-xs">
              <thead>
                <tr>
                  {["Fault", "Category", "Parameters", "Status", "Verification"].map((h) => (
                    <th key={h} className="th">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {catalog.data.faults.map((f) => (
                  <tr key={f.name}>
                    <td className="td">
                      <span className="text-ink">{f.title}</span> <span className="num text-faint">{f.name}</span>
                      <div className="text-xxs text-faint">{f.description}</div>
                    </td>
                    <td className="td">{f.category}</td>
                    <td className="td num text-dim">{f.params.map((p) => `${p.name} ${p.minimum}–${p.maximum}`).join(", ") || "—"}</td>
                    <td className="td">{f.supported ? <Badge tone="ok">available</Badge> : <Badge tone="muted">not supported</Badge>}</td>
                    <td className="td max-w-md text-xxs text-faint">{f.supported ? f.verification_note : f.unsupported_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </div>
  );
}
