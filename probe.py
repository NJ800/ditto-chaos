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


async def run(duration: float | None, write_baseline: bool) -> None:
    os.makedirs(config.METRICS_DIR, exist_ok=True)
    out_path = os.path.join(config.METRICS_DIR, f"probe-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    ids = config.all_car_ids()
    rows: list[dict] = []
    start = time.time()
    async with httpx.AsyncClient(base_url=config.DITTO_URL,
                                 auth=(config.DITTO_USER, config.DITTO_PASS),
                                 timeout=config.REQUEST_TIMEOUT_S) as client:
      with open(out_path, "a", encoding="utf-8") as out:
        print(f"[probe] writing {out_path}", flush=True)
        try:
            while duration is None or time.time() - start < duration:
                tick = time.time()
                row = await probe_once(client, ids)
                rows.append(row)
                out.write(json.dumps(row) + "\n")
                out.flush()
                print(f"[probe] direct={'OK ' if row['direct_ok'] else 'ERR'} {row['direct_ms']:7.1f}ms | "
                      f"search={'OK ' if row['search_ok'] else 'ERR'} {row['search_ms']:7.1f}ms | "
                      f"stale p95={row['stale_p95_s']}s | lost={row['lost_acked']} | "
                      f"drift={row['search_drift']}", flush=True)
                await asyncio.sleep(max(0.0, config.PROBE_INTERVAL_S - (time.time() - tick)))
        finally:
            if rows:
                summary = summarize(rows)
                print("[probe] summary:", json.dumps(summary, indent=2), flush=True)
                if write_baseline:
                    with open(config.BASELINE_FILE, "w", encoding="utf-8") as f:
                        json.dump({"recorded_at": time.time(), "duration_s": round(time.time() - start),
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
