// Live data: one REST snapshot, then Server-Sent Events from the sequence number that
// snapshot was taken at. Framework-free so it can be unit tested.
//
// Guarantees this module provides to the UI:
//  - nothing "live" is shown before a valid snapshot has loaded
//  - events are applied at most once and never out of order (seq / boot)
//  - a lost connection is visible (conn), retried with bounded backoff + jitter, and
//    resynchronised from a fresh snapshot rather than assumed continuous
//  - metric history is capped, so memory does not grow with uptime
import type {
  AgentStatus,
  Baseline,
  ControllerStatus,
  HealthSnapshot,
  MetricRow,
  MonitoringStatus,
  OrchestratorStatus,
  ServicesView,
  Snapshot,
  StreamEvent,
  Thresholds,
} from "../api/types";

export const MAX_ROWS = 1800;
export const MAX_ALERTS = 50;
export const SILENCE_LIMIT_MS = 45000; // the server heartbeats every 15s
export const STALE_AFTER_S = 8;

export type Conn = "connecting" | "live" | "reconnecting" | "offline";

export interface Alert {
  seq: number;
  ts: number;
  level: string;
  kind?: string;
  message: string;
  experiment_id?: string;
}

export interface LiveState {
  phase: "loading" | "ready" | "error";
  conn: Conn;
  error: string | null;
  attempt: number;
  retryAt: number | null;
  seq: number;
  boot: string | null;
  lastEventAt: number | null;
  lastSnapshotAt: number | null;
  health: HealthSnapshot | null;
  monitoring: MonitoringStatus | null;
  services: ServicesView | null;
  rows: MetricRow[];
  baseline: Baseline | null;
  thresholds: Thresholds | null;
  orchestrator: OrchestratorStatus | null;
  controller: ControllerStatus | null;
  agents: AgentStatus[];
  alerts: Alert[];
  activity: number; // bumped when REST-backed views (history, learning) should refetch
}

export const initialState: LiveState = {
  phase: "loading",
  conn: "connecting",
  error: null,
  attempt: 0,
  retryAt: null,
  seq: 0,
  boot: null,
  lastEventAt: null,
  lastSnapshotAt: null,
  health: null,
  monitoring: null,
  services: null,
  rows: [],
  baseline: null,
  thresholds: null,
  orchestrator: null,
  controller: null,
  agents: [],
  alerts: [],
  activity: 0,
};

/** Insert keeping ts order, dropping exact duplicates, capped to the newest `cap`. */
export function mergeRows(rows: MetricRow[], incoming: MetricRow[], cap = MAX_ROWS): MetricRow[] {
  if (!incoming.length) return rows;
  const key = (r: MetricRow) => `${r.ts}|${r.phase ?? ""}`;
  const seen = new Set(rows.map(key));
  const fresh = incoming.filter((r) => typeof r?.ts === "number" && Number.isFinite(r.ts) && !seen.has(key(r)) && seen.add(key(r)));
  if (!fresh.length) return rows;
  const last = rows.length ? rows[rows.length - 1].ts : -Infinity;
  const out = fresh.every((r) => r.ts >= last) && isSorted(fresh) ? rows.concat(fresh) : rows.concat(fresh).sort((a, b) => a.ts - b.ts);
  return out.length > cap ? out.slice(out.length - cap) : out;
}

function isSorted(rows: MetricRow[]): boolean {
  for (let i = 1; i < rows.length; i++) if (rows[i].ts < rows[i - 1].ts) return false;
  return true;
}

export function applySnapshot(state: LiveState, snap: Snapshot, now: number): LiveState {
  const sameBoot = state.boot === snap.boot;
  return {
    ...state,
    phase: "ready",
    error: null,
    seq: snap.seq,
    boot: snap.boot,
    lastSnapshotAt: now,
    lastEventAt: now,
    health: snap.health,
    monitoring: snap.monitoring,
    services: snap.services,
    rows: mergeRows(sameBoot ? state.rows : [], snap.rows),
    baseline: snap.baseline,
    thresholds: snap.thresholds,
    orchestrator: snap.orchestrator,
    controller: snap.controller,
    agents: snap.agents,
    activity: state.activity + 1,
  };
}

export type Effect = "resync" | "refresh-status" | "refresh-agents" | null;

/** Pure reducer for one stream event. Returns the new state and any follow-up the
 *  client must perform (the stream carries notifications, REST carries the documents). */
export function applyEvent(state: LiveState, ev: StreamEvent, now: number): { state: LiveState; effect: Effect } {
  if (!ev || typeof ev.seq !== "number" || typeof ev.type !== "string") return { state, effect: null };
  if (ev.type === "resync") return { state: { ...state, lastEventAt: now }, effect: "resync" };
  if (state.boot !== null && ev.boot !== state.boot) return { state, effect: "resync" }; // another server process
  if (ev.type === "heartbeat") return { state: { ...state, lastEventAt: now }, effect: null };
  if (ev.seq <= state.seq) return { state, effect: null }; // duplicate or late: already applied
  const base: LiveState = { ...state, seq: ev.seq, lastEventAt: now };
  const d = ev.data as Record<string, unknown>;
  switch (ev.type) {
    case "metric":
      return { state: { ...base, rows: mergeRows(base.rows, [d.row as MetricRow]) }, effect: null };
    case "health":
      return { state: { ...base, health: d.health as HealthSnapshot, monitoring: d.monitoring as MonitoringStatus }, effect: null };
    case "services":
      return { state: { ...base, services: d as unknown as ServicesView }, effect: null };
    case "controller":
      return { state: { ...base, controller: d as unknown as ControllerStatus }, effect: null };
    case "alert": {
      const alert: Alert = {
        seq: ev.seq,
        ts: ev.ts,
        level: String(d.level ?? "info"),
        kind: d.kind as string | undefined,
        message: String(d.message ?? ""),
        experiment_id: d.experiment_id as string | undefined,
      };
      return { state: { ...base, alerts: [alert, ...base.alerts].slice(0, MAX_ALERTS) }, effect: "refresh-status" };
    }
    case "experiment":
    case "proposal":
      return { state: base, effect: "refresh-status" };
    case "pipeline":
      return { state: { ...base, activity: base.activity + 1 }, effect: "refresh-status" };
    case "agent":
      return { state: base, effect: "refresh-agents" };
    default:
      return { state: base, effect: null }; // unknown event type from a newer server: ignore, keep order
  }
}

/** Exponential backoff, capped at 30s, with 50-100% jitter. */
export function backoffDelay(attempt: number, rand: () => number = Math.random): number {
  const ceiling = Math.min(30000, 1000 * 2 ** Math.min(attempt, 10));
  return Math.round(ceiling * (0.5 + 0.5 * rand()));
}

export type HealthLabel = "LOADING" | "DISCONNECTED" | "UNKNOWN" | "STALE" | "DEGRADED" | "HEALTHY";

/** What the UI may claim about the target right now. Anything that is not a fresh,
 *  connected measurement is reported as such -- never as HEALTHY. */
export function healthLabel(state: LiveState, nowMs: number): HealthLabel {
  if (state.phase !== "ready" || !state.health) return state.phase === "error" ? "DISCONNECTED" : "LOADING";
  if (state.conn === "offline" || state.conn === "reconnecting") return "DISCONNECTED";
  const h = state.health;
  if (h.latest_timestamp === null || h.sample_count === 0) return "UNKNOWN";
  if (!h.fresh || nowMs / 1000 - h.latest_timestamp > Math.max(STALE_AFTER_S, h.window_s)) return "STALE";
  if (h.status === "unknown") return "UNKNOWN";
  return h.status === "healthy" ? "HEALTHY" : "DEGRADED";
}

export interface EventSourceLike {
  onmessage: ((ev: { data: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onopen: ((ev: unknown) => void) | null;
  close(): void;
}

export interface LiveDeps {
  fetchSnapshot(signal: AbortSignal): Promise<Snapshot>;
  fetchStatus(signal: AbortSignal): Promise<OrchestratorStatus>;
  fetchAgents(signal: AbortSignal): Promise<AgentStatus[]>;
  openStream(lastSeq: number, boot: string, ticket?: string | null): EventSourceLike;
  /** Optional: fetch a single-use stream ticket (token-protected servers). */
  streamTicket?: (signal: AbortSignal) => Promise<string | null>;
  describeError(e: unknown): string;
  now?: () => number;
  setTimeout?: (fn: () => void, ms: number) => unknown;
  clearTimeout?: (handle: unknown) => void;
  random?: () => number;
}

export class LiveClient {
  private state: LiveState = initialState;
  private listeners = new Set<() => void>();
  private source: EventSourceLike | null = null;
  private abort: AbortController | null = null;
  private retryTimer: unknown = null;
  private watchdog: unknown = null;
  private pending = new Map<string, unknown>();
  private running = false;
  private generation = 0;
  private deps: Required<Omit<LiveDeps, "streamTicket">> & Pick<LiveDeps, "streamTicket">;

  constructor(deps: LiveDeps) {
    this.deps = {
      now: () => Date.now(),
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
      random: Math.random,
      ...deps,
    };
  }

  getState = (): LiveState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: LiveState): void {
    if (next === this.state) return;
    this.state = next;
    this.listeners.forEach((l) => l());
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    void this.connect();
  }

  /** Releases the stream, timers and in-flight requests. Safe to call repeatedly. */
  stop(): void {
    this.running = false;
    this.generation++;
    this.closeSource();
    this.abort?.abort();
    this.abort = null;
    for (const h of [this.retryTimer, this.watchdog, ...this.pending.values()]) if (h !== null) this.deps.clearTimeout(h);
    this.retryTimer = this.watchdog = null;
    this.pending.clear();
  }

  /** Manual retry (the "Reconnect now" control). */
  reconnectNow(): void {
    if (!this.running) return;
    if (this.retryTimer !== null) this.deps.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.closeSource();
    void this.connect();
  }

  private closeSource(): void {
    if (this.source) {
      this.source.onmessage = this.source.onerror = this.source.onopen = null;
      this.source.close();
      this.source = null;
    }
  }

  private async connect(): Promise<void> {
    const gen = ++this.generation;
    this.abort?.abort();
    this.abort = new AbortController();
    this.set({ ...this.state, conn: this.state.phase === "ready" ? "reconnecting" : "connecting", retryAt: null });
    let snap: Snapshot;
    let ticket: string | null = null;
    try {
      snap = await this.deps.fetchSnapshot(this.abort.signal);
      if (this.deps.streamTicket) ticket = await this.deps.streamTicket(this.abort.signal);
    } catch (e) {
      if (gen !== this.generation || !this.running) return;
      return this.fail(this.deps.describeError(e));
    }
    if (gen !== this.generation || !this.running) return;
    this.set(applySnapshot(this.state, snap, this.deps.now()));
    this.open(gen, ticket);
  }

  private open(gen: number, ticket: string | null): void {
    this.closeSource();
    const src = this.deps.openStream(this.state.seq, this.state.boot ?? "", ticket);
    this.source = src;
    src.onopen = () => {
      if (gen !== this.generation) return;
      this.set({ ...this.state, conn: "live", attempt: 0, error: null, lastEventAt: this.deps.now() });
      this.armWatchdog(gen);
    };
    src.onmessage = (msg) => {
      if (gen !== this.generation) return;
      let ev: StreamEvent;
      try {
        ev = JSON.parse(msg.data) as StreamEvent;
      } catch {
        return; // a malformed frame is dropped; the watchdog/seq check covers a broken stream
      }
      const { state, effect } = applyEvent(this.state.conn === "live" ? this.state : { ...this.state, conn: "live", attempt: 0 }, ev, this.deps.now());
      this.set(state);
      this.armWatchdog(gen);
      if (effect === "resync") this.reconnectNow();
      else if (effect === "refresh-status") this.debounce("status", () => this.refreshStatus(gen));
      else if (effect === "refresh-agents") this.debounce("agents", () => this.refreshAgents(gen));
    };
    src.onerror = () => {
      if (gen !== this.generation) return;
      this.fail("The live stream was interrupted.");
    };
  }

  private armWatchdog(gen: number): void {
    if (this.watchdog !== null) this.deps.clearTimeout(this.watchdog);
    this.watchdog = this.deps.setTimeout(() => {
      if (gen === this.generation && this.running) this.fail("No data from the backend for 45s.");
    }, SILENCE_LIMIT_MS);
  }

  private fail(message: string): void {
    this.closeSource();
    if (this.watchdog !== null) this.deps.clearTimeout(this.watchdog);
    this.watchdog = null;
    const attempt = this.state.attempt + 1;
    const delay = backoffDelay(attempt - 1, this.deps.random);
    this.set({
      ...this.state,
      phase: this.state.phase === "ready" ? "ready" : "error",
      conn: "offline",
      error: message,
      attempt,
      retryAt: this.deps.now() + delay,
    });
    if (this.retryTimer !== null) this.deps.clearTimeout(this.retryTimer);
    this.retryTimer = this.deps.setTimeout(() => {
      this.retryTimer = null;
      if (this.running) void this.connect();
    }, delay);
  }

  private debounce(key: string, fn: () => void): void {
    if (this.pending.has(key)) return;
    this.pending.set(
      key,
      this.deps.setTimeout(() => {
        this.pending.delete(key);
        fn();
      }, 250),
    );
  }

  private async refreshStatus(gen: number): Promise<void> {
    try {
      const orchestrator = await this.deps.fetchStatus(this.abort?.signal ?? new AbortController().signal);
      if (gen === this.generation) this.set({ ...this.state, orchestrator });
    } catch {
      /* the stream's own error handling reports a dead backend */
    }
  }

  private async refreshAgents(gen: number): Promise<void> {
    try {
      const agents = await this.deps.fetchAgents(this.abort?.signal ?? new AbortController().signal);
      if (gen === this.generation) this.set({ ...this.state, agents });
    } catch {
      /* as above */
    }
  }

  /** Called by pages after a successful mutation so the result shows immediately. */
  refresh(): void {
    this.debounce("status", () => this.refreshStatus(this.generation));
  }

  setController(controller: ControllerStatus): void {
    this.set({ ...this.state, controller });
  }
}
