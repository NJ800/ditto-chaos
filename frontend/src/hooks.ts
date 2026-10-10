import { useCallback, useEffect, useRef, useState } from "react";
import { api, describeError } from "./api/client";
import type { ExperimentEvent } from "./api/types";

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Load REST data. Aborts the request on unmount / dependency change, and never lets a
 *  slower earlier response overwrite a newer one. */
export function useAsync<T>(fetcher: (signal: AbortSignal) => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const ref = useRef(fetcher);
  ref.current = fetcher;

  useEffect(() => {
    const ac = new AbortController();
    setLoading(true);
    ref
      .current(ac.signal)
      .then((d) => {
        if (ac.signal.aborted) return;
        setData(d);
        setError(null);
      })
      .catch((e) => {
        if (ac.signal.aborted) return;
        setError(describeError(e));
      })
      .finally(() => {
        if (!ac.signal.aborted) setLoading(false);
      });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

export interface Action<A extends unknown[], R> {
  run: (...args: A) => Promise<R | undefined>;
  pending: boolean;
  error: string | null;
  clear: () => void;
}

/** Wrap a mutation: tracks pending, surfaces the error text, and ignores a second
 *  invocation while one is in flight (double-click protection). */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>, onDone?: (r: R) => void): Action<A, R> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const run = useCallback(
    async (...args: A) => {
      if (busy.current) return undefined;
      busy.current = true;
      setPending(true);
      setError(null);
      try {
        const r = await fn(...args);
        if (mounted.current) onDone?.(r);
        return r;
      } catch (e) {
        if (mounted.current) setError(describeError(e));
        return undefined;
      } finally {
        busy.current = false;
        if (mounted.current) setPending(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [fn],
  );
  return { run, pending, error, clear: () => setError(null) };
}

export interface ExperimentEvents {
  events: ExperimentEvent[];
  loading: boolean;
  error: string | null;
  /** The backend reported the run finished: the log is complete and polling has stopped. */
  terminal: boolean;
  dryRun: boolean;
  polling: boolean;
  state: string | null;
}

/** Activity log of one experiment. Loads everything once, then asks only for events newer
 *  than the last sequence number it has, every `pollMs`, until the backend says the run is
 *  terminal. Events are appended in sequence order and never duplicated; a failed request
 *  keeps what is already shown and is retried. Everything is cancelled on unmount. */
export function useExperimentEvents(id: string | null, pollMs = 1500): ExperimentEvents {
  const [events, setEvents] = useState<ExperimentEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [terminal, setTerminal] = useState(false);
  const [dryRun, setDryRun] = useState(false);
  const [state, setState] = useState<string | null>(null);

  useEffect(() => {
    setEvents([]);
    setLoading(true);
    setError(null);
    setTerminal(false);
    setDryRun(false);
    setState(null);
    if (!id) {
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let lastSeq = 0;
    let stopped = false;

    const tick = async () => {
      let done = false;
      try {
        const res = await api.events(id, lastSeq, ac.signal);
        if (stopped) return;
        const fresh = res.events.filter((e) => e.seq > lastSeq).sort((a, b) => a.seq - b.seq);
        if (fresh.length) {
          lastSeq = fresh[fresh.length - 1].seq;
          setEvents((prev) => prev.concat(fresh));
        }
        setDryRun(res.dry_run);
        setState(res.state);
        setError(null);
        done = res.terminal;
        setTerminal(res.terminal);
      } catch (e) {
        if (stopped || ac.signal.aborted) return;
        setError(describeError(e));
      } finally {
        if (!stopped) setLoading(false);
      }
      if (!stopped && !done) timer = setTimeout(tick, pollMs);
    };
    void tick();
    return () => {
      stopped = true;
      ac.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [id, pollMs]);

  return { events, loading, error, terminal, dryRun, polling: !terminal && !loading && Boolean(id), state };
}
