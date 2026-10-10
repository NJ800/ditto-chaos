import { describe, expect, it } from "vitest";
import type { OrchestratorStatus, Snapshot, StreamEvent } from "../api/types";
import { orchestrator, row, snapshot } from "../test/fixtures";
import { LiveClient, MAX_ROWS, SILENCE_LIMIT_MS, applyEvent, applySnapshot, backoffDelay, healthLabel, initialState, mergeRows, type EventSourceLike } from "./stream";

const NOW = 1_800_000_000_000;
const ev = (seq: number, type: string, data: Record<string, unknown> = {}, boot = "boot-a"): StreamEvent => ({ v: 1, boot, seq, ts: NOW / 1000, type, data });
const ready = () => applySnapshot(initialState, snapshot(NOW / 1000), NOW);

describe("mergeRows", () => {
  it("keeps rows ordered by time and drops duplicates", () => {
    const a = [row(1), row(3)];
    expect(mergeRows(a, [row(2), row(3), row(4)]).map((r) => r.ts)).toEqual([1, 2, 3, 4]);
    expect(mergeRows(a, [row(3)])).toBe(a); // nothing new: same array, no re-render
  });

  it("keeps a probe row and a controller row taken at the same instant", () => {
    expect(mergeRows([row(5)], [row(5, { phase: "fault" })])).toHaveLength(2);
  });

  it("caps history so memory does not grow with uptime", () => {
    let rows = [row(0)];
    for (let i = 1; i < MAX_ROWS + 500; i++) rows = mergeRows(rows, [row(i)]);
    expect(rows).toHaveLength(MAX_ROWS);
    expect(rows[rows.length - 1].ts).toBe(MAX_ROWS + 499);
  });

  it("ignores rows without a usable timestamp", () => {
    expect(mergeRows([], [{ ...row(1), ts: Number.NaN }, row(2)]).map((r) => r.ts)).toEqual([2]);
  });
});

describe("applyEvent", () => {
  it("applies each event once and never out of order", () => {
    let s = ready();
    s = applyEvent(s, ev(11, "metric", { row: row(100) }), NOW).state;
    const again = applyEvent(s, ev(11, "metric", { row: row(101) }), NOW).state;
    const late = applyEvent(s, ev(9, "metric", { row: row(102) }), NOW).state;
    expect(again).toBe(s);
    expect(late).toBe(s);
    expect(s.seq).toBe(11);
    expect(s.rows.some((r) => r.ts === 100)).toBe(true);
  });

  it("asks for a resync on an event from another server process or an explicit resync", () => {
    const s = ready();
    expect(applyEvent(s, ev(11, "metric", { row: row(1) }, "boot-b"), NOW).effect).toBe("resync");
    expect(applyEvent(s, ev(10, "resync", { reason: "x" }), NOW).effect).toBe("resync");
  });

  it("updates health and monitoring from a health event", () => {
    const s = applyEvent(ready(), ev(11, "health", { health: { ...ready().health!, status: "degraded" }, monitoring: { ...ready().monitoring!, state: "stale" } }), NOW).state;
    expect(s.health?.status).toBe("degraded");
    expect(s.monitoring?.state).toBe("stale");
  });

  it("records alerts newest first and requests a status refresh for experiment events", () => {
    let s = ready();
    const a = applyEvent(s, ev(11, "alert", { level: "error", kind: "kill_switch", message: "latency" }), NOW);
    s = a.state;
    expect(a.effect).toBe("refresh-status");
    s = applyEvent(s, ev(12, "alert", { level: "critical", kind: "rollback_failed", message: "tc" }), NOW).state;
    expect(s.alerts.map((x) => x.kind)).toEqual(["rollback_failed", "kill_switch"]);
    expect(applyEvent(s, ev(13, "experiment", { state: "REVERTING" }), NOW).effect).toBe("refresh-status");
    expect(applyEvent(s, ev(13, "agent", { agent: "insights" }), NOW).effect).toBe("refresh-agents");
  });

  it("tolerates unknown event types and malformed events", () => {
    const s = ready();
    expect(applyEvent(s, ev(11, "something_new"), NOW).state.seq).toBe(11);
    expect(applyEvent(s, {} as StreamEvent, NOW).state).toBe(s);
  });
});

describe("backoffDelay", () => {
  it("grows exponentially, is capped at 30s, and is jittered", () => {
    expect(backoffDelay(0, () => 1)).toBe(1000);
    expect(backoffDelay(3, () => 1)).toBe(8000);
    expect(backoffDelay(20, () => 1)).toBe(30000);
    expect(backoffDelay(3, () => 0)).toBe(4000);
    for (let i = 0; i < 50; i++) expect(backoffDelay(i)).toBeLessThanOrEqual(30000);
  });
});

describe("healthLabel", () => {
  it("never reports HEALTHY for stale, unknown or disconnected data", () => {
    const s = ready();
    expect(healthLabel({ ...s, conn: "live" }, NOW)).toBe("HEALTHY");
    expect(healthLabel({ ...s, conn: "live" }, NOW + 60_000)).toBe("STALE"); // no new sample for a minute
    expect(healthLabel({ ...s, conn: "live", health: { ...s.health!, fresh: false } }, NOW)).toBe("STALE");
    expect(healthLabel({ ...s, conn: "live", health: { ...s.health!, latest_timestamp: null, sample_count: 0 } }, NOW)).toBe("UNKNOWN");
    expect(healthLabel({ ...s, conn: "offline" }, NOW)).toBe("DISCONNECTED");
    expect(healthLabel({ ...s, conn: "live", health: { ...s.health!, status: "degraded" } }, NOW)).toBe("DEGRADED");
    expect(healthLabel(initialState, NOW)).toBe("LOADING");
  });
});

// ---------------------------------------------------------------- LiveClient with fakes
class FakeSource implements EventSourceLike {
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onopen: ((ev: unknown) => void) | null = null;
  closed = false;
  constructor(public seq: number, public boot: string) {}
  close() {
    this.closed = true;
  }
  emit(e: StreamEvent) {
    this.onmessage?.({ data: JSON.stringify(e) });
  }
}

function harness(opts: { snapshots?: (Snapshot | Error)[] } = {}) {
  let now = NOW;
  const timers: { fn: () => void; at: number; id: number; live: boolean }[] = [];
  const sources: FakeSource[] = [];
  const queue = [...(opts.snapshots ?? [])];
  const fetched = { snapshot: 0, status: 0, agents: 0 };
  let status: OrchestratorStatus = orchestrator({ status: "running" });
  const client = new LiveClient({
    fetchSnapshot: async () => {
      fetched.snapshot++;
      const next = queue.length ? queue.shift()! : snapshot(now / 1000);
      if (next instanceof Error) throw next;
      return next;
    },
    fetchStatus: async () => {
      fetched.status++;
      return status;
    },
    fetchAgents: async () => {
      fetched.agents++;
      return [];
    },
    openStream: (seq, boot) => {
      const s = new FakeSource(seq, boot);
      sources.push(s);
      return s;
    },
    describeError: (e) => (e instanceof Error ? e.message : "error"),
    now: () => now,
    setTimeout: (fn, ms) => {
      const t = { fn, at: now + ms, id: timers.length, live: true };
      timers.push(t);
      return t;
    },
    clearTimeout: (h) => {
      (h as { live: boolean }).live = false;
    },
    random: () => 1,
  });
  const flush = async () => {
    for (let i = 0; i < 6; i++) await Promise.resolve();
  };
  const advance = async (ms: number) => {
    now += ms;
    for (const t of timers.filter((x) => x.live && x.at <= now)) {
      t.live = false;
      t.fn();
    }
    await flush();
  };
  return { client, sources, fetched, flush, advance, liveTimers: () => timers.filter((t) => t.live).length, setStatus: (s: OrchestratorStatus) => (status = s) };
}

describe("LiveClient", () => {
  it("loads a snapshot before opening the stream, then streams from that sequence number", async () => {
    const h = harness();
    expect(h.client.getState().phase).toBe("loading");
    h.client.start();
    expect(h.sources).toHaveLength(0); // no stream before a valid snapshot
    await h.flush();
    expect(h.client.getState().phase).toBe("ready");
    expect(h.sources[0].seq).toBe(10);
    expect(h.sources[0].boot).toBe("boot-a");
    h.sources[0].onopen?.({});
    expect(h.client.getState().conn).toBe("live");
    h.sources[0].emit(ev(11, "metric", { row: row(NOW / 1000) }));
    expect(h.client.getState().seq).toBe(11);
  });

  it("shows an error, not empty data, when the first snapshot fails, and retries with backoff", async () => {
    const h = harness({ snapshots: [new Error("Cannot reach the backend"), new Error("Cannot reach the backend")] });
    h.client.start();
    await h.flush();
    let s = h.client.getState();
    expect([s.phase, s.conn, s.error, s.attempt]).toEqual(["error", "offline", "Cannot reach the backend", 1]);
    expect(s.health).toBeNull();
    await h.advance(1000); // first retry after 1s
    s = h.client.getState();
    expect(s.attempt).toBe(2);
    expect(s.retryAt! - (NOW + 1000)).toBe(2000); // second wait doubled
    await h.advance(2000);
    expect(h.client.getState().phase).toBe("ready");
    expect(h.fetched.snapshot).toBe(3);
  });

  it("reconnects after the stream drops, resynchronising from a fresh snapshot and keeping history", async () => {
    const h = harness();
    h.client.start();
    await h.flush();
    h.sources[0].onopen?.({});
    h.sources[0].emit(ev(11, "metric", { row: row(NOW / 1000 + 1) }));
    const before = h.client.getState().rows.length;
    h.sources[0].onerror?.({});
    expect(h.client.getState().conn).toBe("offline");
    expect(h.client.getState().phase).toBe("ready"); // last known data stays visible, marked not live
    expect(h.sources[0].closed).toBe(true);
    await h.advance(1000);
    expect(h.sources).toHaveLength(2);
    expect(h.client.getState().rows.length).toBeGreaterThanOrEqual(before);
    h.sources[1].onopen?.({});
    expect(h.client.getState()).toMatchObject({ conn: "live", attempt: 0, error: null });
  });

  it("ignores events from a superseded connection", async () => {
    const h = harness();
    h.client.start();
    await h.flush();
    const old = h.sources[0];
    const handler = old.onmessage!;
    old.onerror?.({});
    await h.advance(1000);
    handler({ data: JSON.stringify(ev(99, "metric", { row: row(1) })) });
    expect(h.client.getState().seq).toBe(10);
  });

  it("re-fetches the snapshot when the server says resync", async () => {
    const h = harness();
    h.client.start();
    await h.flush();
    h.sources[0].onopen?.({});
    h.sources[0].emit(ev(10, "resync", { reason: "server restarted" }));
    await h.flush();
    expect(h.fetched.snapshot).toBe(2);
    expect(h.sources).toHaveLength(2);
  });

  it("debounces status refreshes triggered by bursts of experiment events", async () => {
    const h = harness();
    h.client.start();
    await h.flush();
    h.sources[0].onopen?.({});
    for (let i = 11; i < 20; i++) h.sources[0].emit(ev(i, "experiment", { state: "MONITORING" }));
    await h.advance(300);
    expect(h.fetched.status).toBe(1);
    expect(h.client.getState().orchestrator?.status).toBe("running");
  });

  it("treats prolonged silence as a dead stream", async () => {
    const h = harness();
    h.client.start();
    await h.flush();
    h.sources[0].onopen?.({});
    await h.advance(SILENCE_LIMIT_MS + 1);
    expect(h.client.getState().conn).not.toBe("live");
    expect(h.sources[0].closed).toBe(true);
  });

  it("stop() closes the stream and clears every timer", async () => {
    const h = harness();
    h.client.start();
    await h.flush();
    h.sources[0].onopen?.({});
    h.sources[0].emit(ev(11, "experiment", {}));
    h.client.stop();
    expect(h.sources[0].closed).toBe(true);
    expect(h.liveTimers()).toBe(0);
    await h.advance(60_000);
    expect(h.fetched.snapshot).toBe(1); // nothing keeps running after unmount
    h.client.stop(); // idempotent
  });

  it("drops a malformed frame without breaking the stream", async () => {
    const h = harness();
    h.client.start();
    await h.flush();
    h.sources[0].onopen?.({});
    h.sources[0].onmessage?.({ data: "{not json" });
    h.sources[0].emit(ev(11, "heartbeat"));
    expect(h.client.getState().conn).toBe("live");
  });
});
