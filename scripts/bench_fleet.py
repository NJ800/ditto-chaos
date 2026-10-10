"""Reproducible load benchmark: simulator + probe at several fleet sizes against the LOCAL Ditto.

Starts the simulator and probe for each fleet (own METRICS_DIR/STATE_DIR under the output
directory, so nothing of yours is touched), warms up 30 s, samples `docker stats` and the
probe's rows for 60 s, stops them, and prints one JSON line per level. Do not run it while
the regular simulator/probe drive the same twins.

    python scripts/bench_fleet.py                                   # default levels
    python scripts/bench_fleet.py "ev:10,machine:8,home:12" "ev:100"   # your own levels
"""
import json
import os
import re
import statistics
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "reports", f"bench-{time.strftime('%Y%m%d-%H%M%S')}")
os.makedirs(OUT, exist_ok=True)
PY = sys.executable
LEVELS = [(spec, spec) for spec in sys.argv[1:]] or [
    ("30 twins / 3 products", "ev:10,machine:8,home:12"), ("60 twins / 3 products", "ev:20,machine:16,home:24"),
    ("100 EVs", "ev:100"), ("120 twins / 6 products", "ev:30,machine:20,home:24,energy:16,environment:14,appliance:16")]
WARM, SAMPLE, EVERY = 30, 60, 10


def stats():
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
                          "docker-things-1", "docker-gateway-1", "docker-things-search-1", "docker-mongodb-1"],
                         capture_output=True, text=True, timeout=60).stdout
    return {l.split("\t")[0]: (float(l.split("\t")[1].rstrip("%")), l.split("\t")[2]) for l in out.splitlines() if l}


results = []
for label, spec in LEVELS:
    env = {**os.environ, "SIM_FLEET": spec, "SIM_SEED": "7", "PYTHONUNBUFFERED": "1",
           "METRICS_DIR": os.path.join(OUT, "metrics"), "STATE_DIR": os.path.join(OUT, "state")}
    sim_log, probe_log = open(os.path.join(OUT, f"sim-{spec.replace(':', '_').replace(',', '-')}.log"), "w"), open(os.path.join(OUT, f"probe-{spec.replace(':', '_').replace(',', '-')}.log"), "w")
    sim = subprocess.Popen([PY, "simulator.py"], cwd=REPO, env=env, stdout=sim_log, stderr=subprocess.STDOUT)
    prb = subprocess.Popen([PY, "probe.py"], cwd=REPO, env=env, stdout=probe_log, stderr=subprocess.STDOUT)
    time.sleep(WARM)
    t0, samples = time.time(), []
    while time.time() - t0 < SAMPLE:
        samples.append(stats())
        time.sleep(EVERY)
    sim.terminate(); prb.terminate(); sim.wait(15); prb.wait(15)
    sim_log.close(); probe_log.close()
    rows = []
    mdir = os.path.join(OUT, "metrics")
    newest = max((os.path.join(mdir, f) for f in os.listdir(mdir) if f.startswith("probe-")), key=os.path.getmtime)
    for line in open(newest, encoding="utf-8"):
        r = json.loads(line)
        if r["ts"] >= t0:
            rows.append(r)
    simtxt = open(os.path.join(OUT, f"sim-{spec.replace(':', '_').replace(',', '-')}.log")).read()
    last = re.findall(r"sent=(\d+) ok=(\d+) failed=(\d+)", simtxt)
    ok = [r for r in rows if r["direct_ok"]]
    d = sorted(r["direct_ms"] for r in ok)
    res = {"level": label, "fleet": spec, "probe_ticks": len(rows),
           "direct_error_rate": round(1 - len(ok) / len(rows), 3) if rows else None,
           "direct_p50_ms": d[len(d) // 2] if d else None, "direct_p95_ms": d[int(len(d) * .95) - 1] if d else None,
           "stale_p95_s_median": statistics.median([r["stale_p95_s"] for r in ok if r["stale_p95_s"] is not None]) if ok else None,
           "fresh_ratio_min": min((b["fresh_ratio"] for r in ok for b in r["profiles"].values() if b["fresh_ratio"] is not None), default=None),
           "lost_acked_max": max((r["lost_acked"] or 0) for r in rows) if rows else None,
           "sim_sent_ok_failed": last[-1] if last else None,
           "things_cpu_mean": round(statistics.mean(s["docker-things-1"][0] for s in samples), 1),
           "gateway_cpu_mean": round(statistics.mean(s["docker-gateway-1"][0] for s in samples), 1),
           "search_cpu_mean": round(statistics.mean(s["docker-things-search-1"][0] for s in samples), 1),
           "things_mem_last": samples[-1]["docker-things-1"][1]}
    results.append(res)
    print(json.dumps(res), flush=True)
    time.sleep(20)
json.dump(results, open(os.path.join(OUT, "bench.json"), "w"), indent=1)
