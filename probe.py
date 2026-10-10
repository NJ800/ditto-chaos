"""Health checker: measures how healthy Ditto is, every PROBE_INTERVAL_S seconds.

Per tick it records:
  direct_ok / direct_ms  - read all twins via things service
  search_ok / search_ms  - fleet query via things-search service
  stale_p50/p95/max_s    - seconds since the stored update was sent by the car
  lost_acked             - cars whose stored seq is OLDER than a seq Ditto already acknowledged
                           (silent data loss; should always be 0)
  search_drift           - cars missing from search or > SEARCH_DRIFT_TOLERANCE updates behind

Lines go to metrics/probe-<time>.jsonl. The simulator must run on the same machine
for lost_acked (it reads state/sent.json); otherwise that field is null.

This module also offers a small, standalone kill-switch API for anything that just
wants a yes/no safety answer without running its own fault-injection loop:
  current_health(window_s=10)   -- "what does Ditto look like right now?"
  check_breach(health, baseline) -- "should an experiment stop right now?"
guard.py (the actual experiment controller) does NOT use these -- it has its own,
more detailed check_breach() that looks at consecutive probe ticks it drives itself,
in-process, rather than a time-windowed summary read back from a file. Both exist
for a reason; see the two functions' docstrings and the project README for why they
aren't (yet) merged into one.

Run:  python probe.py                         (until Ctrl+C)
      python probe.py --duration 600 --baseline   (10 min, then writes baseline.json)
"""
import argparse
import asyncio
import json
import math
import os
import time

import httpx

import config

FIELDS = "thingId,features/battery/properties"


def percentile(values: list[float], p: float):
    if not values:
        return None
    s = sorted(values)
    k = max(0, math.ceil(p / 100 * len(s)) - 1)
    return s[k]


def read_acked() -> dict | None:
    try:
        with open(config.SENT_FILE, encoding="utf-8") as f:
            return {cid: v["acked_seq"] for cid, v in json.load(f)["cars"].items()}
    except (OSError, ValueError, KeyError):
        return None


def seq_by_id(items: list) -> dict:
    out = {}
    for t in items:
        props = t.get("features", {}).get("battery", {}).get("properties", {})
        if "seq" in props:
            out[t["thingId"]] = (props["seq"], props.get("sentAt"))
    return out


async def timed_get(client, path, params):
    t0 = time.perf_counter()
    try:
        r = await client.get(path, params=params)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code == 200:
            return True, ms, r.json()
        return False, ms, None
    except (httpx.HTTPError, ValueError):
        return False, (time.perf_counter() - t0) * 1000, None


async def probe_once(client, ids: list[str]) -> dict:
    acked = read_acked()  # read BEFORE querying Ditto, so every acked seq must already be stored
    direct_task = timed_get(client, "/api/2/things", {"ids": ",".join(ids), "fields": FIELDS})
    search_task = timed_get(client, "/api/2/search/things",
                            {"filter": f'like(thingId,"{config.NAMESPACE}:car-*")',
                             "option": "size(200)", "fields": FIELDS})
    (d_ok, d_ms, d_body), (s_ok, s_ms, s_body) = await asyncio.gather(direct_task, search_task)
    now = time.time()

    row = {"ts": round(now, 3), "direct_ok": d_ok, "direct_ms": round(d_ms, 1),
           "search_ok": s_ok, "search_ms": round(s_ms, 1),
           "stale_p50_s": None, "stale_p95_s": None, "stale_max_s": None,
           "lost_acked": None, "search_drift": None}

    direct = seq_by_id(d_body) if d_ok and isinstance(d_body, list) else None
    if direct:
        stale = [now - sent for _, sent in direct.values() if sent is not None]
        row["stale_p50_s"] = round(percentile(stale, 50), 2) if stale else None
        row["stale_p95_s"] = round(percentile(stale, 95), 2) if stale else None
        row["stale_max_s"] = round(max(stale), 2) if stale else None
        if acked is not None:
            row["lost_acked"] = sum(1 for cid, a in acked.items()
                                    if cid in direct and direct[cid][0] < a)
    if direct and s_ok and isinstance(s_body, dict):
        search = seq_by_id(s_body.get("items", []))
        row["search_drift"] = sum(1 for cid, (seq, _) in direct.items()
                                  if cid not in search
                                  or seq - search[cid][0] > config.SEARCH_DRIFT_TOLERANCE)
    return row


def summarize(rows: list[dict]) -> dict:
    def col(name):
        return [r[name] for r in rows if r.get(name) is not None]

    n = len(rows)
    d_ms = [r["direct_ms"] for r in rows if r["direct_ok"]]
    s_ms = [r["search_ms"] for r in rows if r["search_ok"]]
    return {
        "samples": n,
        "direct_error_rate": round(1 - len(d_ms) / n, 4) if n else None,
        "search_error_rate": round(1 - len(s_ms) / n, 4) if n else None,
        "direct_ms": {p: percentile(d_ms, q) for p, q in (("p50", 50), ("p95", 95), ("p99", 99))},
        "search_ms": {p: percentile(s_ms, q) for p, q in (("p50", 50), ("p95", 95), ("p99", 99))},
        "stale_p95_s": percentile(col("stale_p95_s"), 95),
        "lost_acked_max": max(col("lost_acked"), default=None),
        "search_drift_max": max(col("search_drift"), default=None),
    }


def load_baseline() -> dict | None:
    try:
        with open(config.BASELINE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# How recent must the newest sample be to trust it at all? If probe.py itself has
# stalled, crashed, or was never started, metrics/*.jsonl stops growing -- without
# this, an old file could look "healthy" forever just because the last real reading
# happened to be good.
FRESHNESS_MAX_AGE_S = 3 * config.PROBE_INTERVAL_S


def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _num_or_none(x):
    """Treat anything that isn't a real, finite number (None, NaN, a string from a
    corrupted record, ...) as missing. Safer than letting a bad value silently pass
    (or fail with a crash) a float comparison deep inside the kill switch."""
    return x if _is_finite_number(x) else None


def _tail_lines(path: str, max_lines: int, chunk_size: int = 65536) -> list[str]:
    """Read roughly the last `max_lines` lines of a file without loading the whole
    file into memory (or off disk) -- metrics/*.jsonl can grow large over a
    long-running demo, and current_health() may be called every couple of seconds."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        remaining = f.tell()
        chunks: list[bytes] = []
        newline_count = 0
        while remaining > 0 and newline_count <= max_lines:
            take = min(chunk_size, remaining)
            remaining -= take
            f.seek(remaining)
            chunks.append(f.read(take))
            newline_count += chunks[-1].count(b"\n")
        data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:] if max_lines else lines


def current_health(window_s: float = 10, metrics_dir: str | None = None) -> dict:
    """What does Ditto look like in roughly the last `window_s` seconds?

    Reads only the MOST RECENTLY WRITTEN metrics/probe-*.jsonl file (by mtime, not
    filename), and only its TAIL -- bounded by `window_s`, not the whole file -- then
    summarizes just the rows whose "ts" falls inside the window.

    Deliberately mtime, not "lexicographically last filename": a short-lived run
    (e.g. `main.py baseline`, which also calls probe.run() and writes its own
    probe-<timestamp>.jsonl) can easily start AFTER a long-running standalone
    `python probe.py` but finish first. Its filename would still sort last and
    silently shadow the actually-active stream forever once it stops growing --
    this was a real bug, caught live when a dashboard kept reporting stale metrics
    while a perfectly healthy probe.py was still running, because an unrelated
    `main.py baseline` run earlier that session had left a newer-NAMED but
    already-finished file sitting in the same directory.

    Always returns a dict with every key below present (never None as a whole, and
    never a bare empty/zero in place of "I don't have this data"):
        window_s            the requested window, seconds (as given)
        sample_count         how many probe ticks fell inside the window
        error_rate            max(direct_error_rate, search_error_rate) -- the worse
                             of the two; this is what check_breach()'s error-rate
                             rule compares against. None if neither is known.
        direct_error_rate     fraction of direct-read ticks that failed (0..1), or None
        search_error_rate     fraction of search ticks that failed (0..1), or None
        direct_p95_ms         p95 latency over SUCCESSFUL direct reads only, or None
                             if there were zero successful direct reads in the window
        search_p95_ms         same, for search
        stale_p95_s           p95 twin staleness (seconds), or None if no twin in the
                             window had a usable sentAt
        lost_acked             max "cars with a lost update" count seen in the window,
                             or None -- None means "the simulator's state/sent.json
                             wasn't available for any tick in the window", i.e.
                             UNKNOWN, never silently treated as a confirmed zero
        search_drift            max search-index drift seen in the window, or None
                             (same None-vs-zero rule as lost_acked)
        latest_timestamp       unix time of the newest row actually used, or None
        fresh                   True only if latest_timestamp is within
                             FRESHNESS_MAX_AGE_S of "now" -- False means probe.py may
                             have stalled, crashed, or never run recently
        status                  "unknown" (no data, or not fresh), "degraded" (fresh
                             data but at least one error or a lost update happened),
                             or "healthy" (fresh, zero errors, zero lost updates this
                             window). This is a plain DATA-QUALITY label, not a safety
                             decision -- check_breach() is the only function that
                             decides whether to stop an experiment; don't duplicate its
                             thresholds by branching on `status` instead.

    With only a handful of samples -- a 10s window at the default 2s probe interval is
    ~5 ticks -- p95 is really closer to "the single worst observation" than a
    statistically solid percentile. Treat it as a rough signal until you have dozens
    of samples (e.g. a longer window, or a slower-changing metric like staleness).
    """
    metrics_dir = metrics_dir or config.METRICS_DIR
    empty = {
        "window_s": window_s, "sample_count": 0, "error_rate": None,
        "direct_error_rate": None, "search_error_rate": None,
        "direct_p95_ms": None, "search_p95_ms": None, "stale_p95_s": None,
        "lost_acked": None, "search_drift": None, "latest_timestamp": None,
        "fresh": False, "status": "unknown",
    }
    try:
        files = [f for f in os.listdir(metrics_dir) if f.startswith("probe-") and f.endswith(".jsonl")]
    except OSError:
        return empty
    if not files:
        return empty

    def _mtime_or_never(name: str) -> float:
        try:
            return os.path.getmtime(os.path.join(metrics_dir, name))
        except OSError:
            return -1.0  # deleted/inaccessible between listdir() and here -- sort it last, don't crash
    path = os.path.join(metrics_dir, max(files, key=_mtime_or_never))
    max_lines = max(20, int(window_s / config.PROBE_INTERVAL_S) + 10)  # generous margin for jitter
    try:
        tail = _tail_lines(path, max_lines)
    except OSError:
        return empty

    now = time.time()
    cutoff = now - window_s
    rows = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue  # half-written last line (probe.py mid-write), or genuinely corrupt -- skip
        ts = r.get("ts")
        if not _is_finite_number(ts):
            continue  # no usable timestamp -- can't place it in the window, so drop it
        if ts >= cutoff:
            rows.append(r)
    if not rows:
        return empty

    # max(), not "the last line", in case of any out-of-order ts within the tail.
    latest_ts = max(r["ts"] for r in rows)
    summary = summarize(rows)
    direct_err, search_err = summary["direct_error_rate"], summary["search_error_rate"]
    known_errs = [e for e in (direct_err, search_err) if e is not None]
    combined_err = max(known_errs) if known_errs else None

    lost, drift = summary["lost_acked_max"], summary["search_drift_max"]
    fresh = (now - latest_ts) <= FRESHNESS_MAX_AGE_S
    if not fresh:
        status = "unknown"
    elif (combined_err or 0) > 0 or (lost or 0) > 0:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "window_s": window_s, "sample_count": summary["samples"], "error_rate": combined_err,
        "direct_error_rate": direct_err, "search_error_rate": search_err,
        "direct_p95_ms": summary["direct_ms"]["p95"], "search_p95_ms": summary["search_ms"]["p95"],
        "stale_p95_s": summary["stale_p95_s"], "lost_acked": lost, "search_drift": drift,
        "latest_timestamp": latest_ts, "fresh": fresh, "status": status,
    }


def check_breach(health: dict | None, baseline: dict | None = None) -> tuple[bool, str]:
    """Should the experiment stop right now? Pure, deterministic, easy to unit test.

    Evaluates ONE rolling summary (typically current_health()'s return value) -- it
    has no memory between calls and doesn't look at a sequence of past windows. Call
    it again next tick to react to new data.

    `baseline` is the dict from baseline.json (needs direct_ms.p95 / search_ms.p95),
    or None if you don't have one yet.

    Checked in this fixed order -- the first confirmed match wins, and NOTHING here
    waits for a second opinion once it's confirmed:
      1. Missing, stale, or unusable monitoring data -> breach. "I can't see Ditto's
         health" must never be read as "Ditto must be fine" (see current_health()'s
         `fresh`/`status`/`sample_count` fields).
      2. A CONFIRMED lost acknowledged update (lost_acked > 0, not None) -> breach,
         before anything else -- silent data loss doesn't wait for an averaged rate.
      3. Overall error rate (health["error_rate"]) > config.MAX_ERROR_RATE.
      4. Direct-read p95 > max(config.MAX_LATENCY_FACTOR x baseline direct p95,
         config.MIN_LATENCY_FLOOR_MS).
      5. Search p95 > the same kind of limit, using the baseline's SEARCH p95
         (evaluated independently of direct-read latency -- one can be fine while the
         other is badly degraded).
      6. Twin staleness p95 (seconds) > config.MAX_STALE_S.
    Each latency check is SKIPPED (not failed, not assumed) if the baseline number it
    needs isn't available -- there's nothing to safely compare against, and inventing
    a baseline would be worse than not checking at all.

    All comparisons are strict ">" -- a reading sitting exactly ON a threshold passes,
    it does not breach. Non-finite/invalid numbers (None, NaN, wrong type) are treated
    as "missing" for that one signal rather than crashing or silently comparing as
    false -- this function must never raise on malformed input, since a crash here
    would take down the kill switch at the worst possible moment.
    """
    if not health or not health.get("fresh") or not health.get("sample_count") \
            or health.get("status") == "unknown":
        return True, "no recent/fresh health data (is probe.py running?)"

    lost = _num_or_none(health.get("lost_acked"))
    if lost is not None and lost > 0:
        return True, f"{lost:g} car(s) have lost acknowledged updates (confirmed silent data loss)"

    err = _num_or_none(health.get("error_rate"))
    if err is not None and err > config.MAX_ERROR_RATE:
        d_err, s_err = health.get("direct_error_rate"), health.get("search_error_rate")
        fmt = lambda v: "n/a" if v is None else f"{v:.1%}"  # noqa: E731
        return True, (f"error rate {err:.1%} > {config.MAX_ERROR_RATE:.1%} "
                      f"(direct={fmt(d_err)}, search={fmt(s_err)})")

    base = baseline or {}
    base_direct_p95 = _num_or_none((base.get("direct_ms") or {}).get("p95"))
    direct_p95 = _num_or_none(health.get("direct_p95_ms"))
    if base_direct_p95 and direct_p95 is not None:
        limit = max(base_direct_p95 * config.MAX_LATENCY_FACTOR, config.MIN_LATENCY_FLOOR_MS)
        if direct_p95 > limit:
            return True, (f"direct-read p95 {direct_p95:.0f}ms > {limit:.0f}ms "
                          f"({config.MAX_LATENCY_FACTOR}x baseline {base_direct_p95:.0f}ms, "
                          f"floor {config.MIN_LATENCY_FLOOR_MS:.0f}ms)")

    base_search_p95 = _num_or_none((base.get("search_ms") or {}).get("p95"))
    search_p95 = _num_or_none(health.get("search_p95_ms"))
    if base_search_p95 and search_p95 is not None:
        limit = max(base_search_p95 * config.MAX_LATENCY_FACTOR, config.MIN_LATENCY_FLOOR_MS)
        if search_p95 > limit:
            return True, (f"search p95 {search_p95:.0f}ms > {limit:.0f}ms "
                          f"({config.MAX_LATENCY_FACTOR}x baseline {base_search_p95:.0f}ms, "
                          f"floor {config.MIN_LATENCY_FLOOR_MS:.0f}ms)")

    stale = _num_or_none(health.get("stale_p95_s"))
    if stale is not None and stale > config.MAX_STALE_S:
        return True, f"twin staleness p95 {stale:.1f}s > {config.MAX_STALE_S:.0f}s"

    return False, ""


async def run(duration: float | None, write_baseline: bool) -> None:
    os.makedirs(config.METRICS_DIR, exist_ok=True)
    out_path = os.path.join(config.METRICS_DIR, f"probe-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    ids = config.all_car_ids()
    rows: list[dict] = []
    start_mono = time.monotonic()
    async with httpx.AsyncClient(base_url=config.DITTO_URL,
                                 auth=(config.DITTO_USER, config.DITTO_PASS),
                                 timeout=config.REQUEST_TIMEOUT_S) as client:
      with open(out_path, "a", encoding="utf-8") as out:
        print(f"[probe] writing {out_path}", flush=True)
        try:
            while duration is None or time.monotonic() - start_mono < duration:
                tick = time.monotonic()
                row = await probe_once(client, ids)
                rows.append(row)
                out.write(json.dumps(row) + "\n")
                out.flush()
                print(f"[probe] direct={'OK ' if row['direct_ok'] else 'ERR'} {row['direct_ms']:7.1f}ms | "
                      f"search={'OK ' if row['search_ok'] else 'ERR'} {row['search_ms']:7.1f}ms | "
                      f"stale p95={row['stale_p95_s']}s | lost={row['lost_acked']} | "
                      f"drift={row['search_drift']}", flush=True)
                await asyncio.sleep(max(0.0, config.PROBE_INTERVAL_S - (time.monotonic() - tick)))
        finally:
            if rows:
                summary = summarize(rows)
                print("[probe] summary:", json.dumps(summary, indent=2), flush=True)
                if write_baseline:
                    with open(config.BASELINE_FILE, "w", encoding="utf-8") as f:
                        json.dump({"recorded_at": time.time(),
                                   "duration_s": round(time.monotonic() - start_mono),
                                   "num_cars": config.NUM_CARS, **summary}, f, indent=2)
                    print(f"[probe] baseline written to {config.BASELINE_FILE}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=None, help="seconds to run (default: until Ctrl+C)")
    ap.add_argument("--baseline", action="store_true", help="write baseline.json at the end")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.duration, args.baseline))
    except KeyboardInterrupt:
        print("\n[probe] stopped")
