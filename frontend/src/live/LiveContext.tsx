import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import { api, describeError, streamTicket, streamUrl } from "../api/client";
import { LiveClient, type EventSourceLike, type LiveState } from "./stream";

const Ctx = createContext<LiveClient | null>(null);

export function createLiveClient(): LiveClient {
  return new LiveClient({
    fetchSnapshot: (signal) => api.snapshot(signal),
    fetchStatus: (signal) => api.status(signal),
    fetchAgents: async (signal) => (await api.agents(signal)).agents,
    openStream: (seq, boot, ticket) => new EventSource(streamUrl(seq, boot, ticket)) as unknown as EventSourceLike,
    streamTicket: (signal) => streamTicket(signal),
    describeError,
  });
}

export function LiveProvider({ children, client }: { children: ReactNode; client?: LiveClient }) {
  const live = useMemo(() => client ?? createLiveClient(), [client]);
  useEffect(() => {
    live.start();
    return () => live.stop(); // closes the EventSource and every timer on unmount
  }, [live]);
  return <Ctx.Provider value={live}>{children}</Ctx.Provider>;
}

export function useLiveClient(): LiveClient {
  const c = useContext(Ctx);
  if (!c) throw new Error("useLive must be used inside <LiveProvider>");
  return c;
}

export function useLive(): LiveState {
  const c = useLiveClient();
  return useSyncExternalStore(c.subscribe, c.getState, c.getState);
}

/** Wall clock that ticks once a second, so "stale" and "x seconds ago" keep moving
 *  even when no event arrives -- which is exactly when they matter. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
