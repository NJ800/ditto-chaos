import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export type PollState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
};

// Simple polling hook: Day 2 scope is "polling at a reasonable interval", not a
// streaming/websocket layer -- see ARCHITECTURE.md for why that's enough for now.
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number, deps: unknown[] = []): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const tick = useCallback(async () => {
    try {
      const result = await fetcherRef.current();
      setData(result);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not reach the backend API. Is `python main.py serve` running?");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    tick();
    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);

  return { data, error, loading, refresh: tick };
}
