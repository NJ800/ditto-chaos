"""Repeatable end-to-end chaos matrix against the LOCAL Ditto sandbox. Real fault injection.

Runs its own API server (port CHAOS_MATRIX_PORT, default 8002) so it can also kill and
restart it to test crash recovery. Every fault is confirmed INDEPENDENTLY with read-only
Docker queries while it is active, and the target is checked again after rollback.

Prerequisites: Ditto up (docker compose), `python simulator.py` and `python probe.py` running
with the same fleet configuration, a fresh baseline (`python main.py baseline --duration 120`),
`docker pull nicolaka/netshoot`.

    python scripts/chaos_matrix.py            # writes reports/chaos_matrix_<time>.json
    python scripts/chaos_matrix.py kill pause # only these fault cases (plus the non-fault cases)

It refuses to start when a recovery lockout is active, and stops all further destructive
cases as soon as one case fails to verify recovery.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config  # noqa: E402
import guard  # noqa: E402

PORT = int(os.getenv("CHAOS_MATRIX_PORT", "8002"))
API = f"http://127.0.0.1:{PORT}/api"
PY = sys.executable
NETWORK = {"delay": "delay 300ms", "jitter": "delay 200ms", "loss": "loss 5%", "bandwidth": "rate 1Mbit"}
CASES = [  # fault, target, params
    ("kill", "things-search", {}), ("stop", "things-search", {"grace_s": 5}),
    ("restart", "things-search", {"grace_s": 5}), ("pause", "things-search", {}),
    ("delay", "mongodb", {"delay_ms": 300}), ("jitter", "things", {"delay_ms": 200, "jitter_ms": 100}),
    ("loss", "gateway", {"loss_pct": 5}), ("bandwidth", "things", {"rate_kbit": 1000}),
    ("cpu_throttle", "things-search", {"cpu_pct": 50}),
]


# ---------------------------------------------------------------- independent Docker checks (read-only)
def docker(*args) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60).stdout.strip()


def cname(target: str) -> str:
    return f"{guard.COMPOSE_PROJECT}-{target}-1"


def state(target: str) -> dict:
    run, paused, quota, started = docker("inspect", "-f", "{{.State.Running}} {{.State.Paused}} "
                                         "{{.HostConfig.CpuQuota}} {{.State.StartedAt}}", cname(target)).split(" ")
    tc = docker("run", "--rm", "--network", f"container:{cname(target)}", guard.NETSHOOT_IMAGE,
                "tc", "qdisc", "show", "dev", guard.NETEM_IFACE) if run == "true" else ""
    return {"running": run == "true", "paused": paused == "true", "cpu_quota": int(quota),
            "started_at": started, "netem": "netem" in tc, "tc": tc}


def clean(st: dict) -> bool:
    return st["running"] and not st["paused"] and st["cpu_quota"] <= 0 and not st["netem"]


def fault_visible(fault: str, before: dict, now: dict) -> bool:
    if fault in ("kill", "stop"):
        return not now["running"]
    if fault == "restart":
        return now["started_at"] != before["started_at"]
    if fault == "pause":
        return now["paused"]
    if fault == "cpu_throttle":
        return now["cpu_quota"] > 0
    return NETWORK[fault].split()[0] in now["tc"] and now["netem"]


# ---------------------------------------------------------------- API server we control
class Server:
    def __init__(self):
        self.proc = None

    def start(self):
        env = {**os.environ, "API_PORT": str(PORT)}
        self.log = open(os.path.join(config.REPORTS_DIR, f"chaos_matrix_api_{PORT}.log"), "a")
        self.proc = subprocess.Popen([PY, "main.py", "serve"], cwd=ROOT, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                if httpx.get(f"{API}/livez", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise SystemExit("API server did not start")

    def kill(self):
        self.proc.kill()      # hard kill: no shutdown handlers, simulates a crash
        self.proc.wait(30)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(30)


def wait(c, until=("done", "error"), state_=None, limit=420):
    t0 = time.time()
    while time.time() - t0 < limit:
        st = c.get(f"{API}/orchestrator/status").json()
        if st["status"] in until or (state_ and (st.get("record") or {}).get("state") == state_):
            return st
        time.sleep(0.4)
    raise TimeoutError("experiment did not finish")


def scenario_body(actions, **kw):
    return {"name": "chaos matrix", "actions": actions, "max_duration_s": kw.get("max_duration_s", 30),
            "pre_s": 6, "recovery_timeout_s": 180}


def run_case(c, name, actions, *, observe=None, stop_after=None, crash=None, server=None):
    """One real experiment with independent verification. Returns a result dict."""
    targets = sorted({a["target"] for a in actions})
    before = {t: state(t) for t in targets}
    res = {"case": name, "actions": actions, "before_clean": all(clean(s) for s in before.values())}
    body = {"authorize": True, "scenario": scenario_body(actions), "request_id": f"matrix-{name}-{time.time()}"}
    r = c.post(f"{API}/scenarios/run", json=body)
    res["start_http"] = r.status_code
    if r.status_code != 200:
        res["error"] = r.text[:300]
        return res
    seen = {}
    t0 = time.time()
    while time.time() - t0 < 120:   # independent confirmation while the fault is active
        st = c.get(f"{API}/orchestrator/status").json()
        rec = st.get("record") or {}
        if rec.get("state") in (guard.STATE_MONITORING,) or any(a.get("status") == "applied" for a in rec.get("actions") or []):
            for a in actions:
                if not seen.get(a["fault"] + a["target"]):
                    seen[a["fault"] + a["target"]] = fault_visible(a["fault"], before[a["target"]], state(a["target"]))
            if all(seen.get(a["fault"] + a["target"]) for a in actions):
                break
        if st["status"] in ("done", "error"):
            break
        time.sleep(0.3)
    res["fault_confirmed_independently"] = {k: bool(v) for k, v in seen.items()}
    if crash:
        res["crash"] = crash(c, server)
        return res
    if stop_after is not None:
        time.sleep(stop_after)
        res["stop_http"] = c.post(f"{API}/stop").status_code
    st = wait(c)
    rec = st["record"]
    after = {t: state(t) for t in targets}
    res.update({"experiment_id": rec["id"], "state": rec["state"], "abort_reason": rec.get("abort_reason"),
                "rollback_verified": rec.get("rollback_verified"), "recovery_outcome": rec.get("recovery_outcome"),
                "recovery_s": rec.get("recovery_s"), "detected_s": rec.get("detected_s"),
                "after_clean": all(clean(s) for s in after.values()),
                "during_direct_p95_ms": ((rec.get("during") or {}).get("direct_ms") or {}).get("p95"),
                "during_search_error_rate": (rec.get("during") or {}).get("search_error_rate"),
                "affected_profiles": (rec.get("profile_impact") or {}).get("affected_profiles"),
                "saved": os.path.isfile(os.path.join(ROOT, config.EXPERIMENTS_DIR, f"{rec['id']}.json")),
                "event_kinds": sorted({e["kind"] for e in rec.get("events") or []})})
    c.post(f"{API}/orchestrator/reset")
    return res


def passed(res) -> bool:
    return (res.get("start_http") == 200 and all(res.get("fault_confirmed_independently", {}).values())
            and res.get("rollback_verified") and res.get("after_clean") and res.get("saved"))


def main(only: set[str]):
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    results, server = [], Server()
    server.start()
    try:
        with httpx.Client(timeout=60) as c:
            if c.get(f"{API}/recovery").json().get("lockout"):
                raise SystemExit("a recovery lockout is active: resolve it before running the matrix")
            c.post(f"{API}/controller/mode", json={"mode": "manual"})

            # --- non-destructive cases
            dry = c.post(f"{API}/scenarios/run", json={"dry_run": True, "authorize": False, "request_id": "matrix-dry",
                                                       "scenario": scenario_body([{"fault": "kill", "target": "things"}])})
            st = wait(c)
            results.append({"case": "healthy dry run (no injection)", "start_http": dry.status_code,
                            "state": st["record"]["state"], "injected": st["record"]["injected"],
                            "target_clean": clean(state("things")), "pass": dry.status_code == 200
                            and not st["record"]["injected"] and clean(state("things"))})
            c.post(f"{API}/orchestrator/reset")
            rej = {
                "unknown target": c.post(f"{API}/scenarios/run", json={"authorize": True, "scenario": scenario_body(
                    [{"fault": "kill", "target": "nginx"}])}).status_code,
                "out-of-range parameter": c.post(f"{API}/scenarios/run", json={"authorize": True, "scenario": scenario_body(
                    [{"fault": "delay", "target": "mongodb", "params": {"delay_ms": 999999}}])}).status_code,
                "missing authorization": c.post(f"{API}/scenarios/run", json={"scenario": scenario_body(
                    [{"fault": "kill", "target": "things"}])}).status_code,
                "cross-site origin": c.post(f"{API}/stop", headers={"Origin": "https://evil.example"}).status_code,
            }
            results.append({"case": "invalid requests rejected", "http": rej,
                            "pass": all(code in (400, 403, 422) for code in rej.values())})

            # --- one real experiment per supported fault
            stop_destructive = False
            for fault, target, params in CASES:
                if only and fault not in only:
                    continue
                res = run_case(c, f"{fault}@{target}", [{"fault": fault, "target": target, "params": params}])
                res["pass"] = passed(res) and res.get("recovery_outcome") == "recovered"
                results.append(res)
                print(json.dumps(res), flush=True)
                if not res.get("after_clean") or not res.get("rollback_verified"):
                    stop_destructive = True
                    print("[matrix] rollback/cleanup not verified -- stopping destructive cases", flush=True)
                    break
                time.sleep(15)

            if not stop_destructive and not only:
                # multi-fault
                res = run_case(c, "delay@mongodb + kill@things-search",
                               [{"fault": "delay", "target": "mongodb", "params": {"delay_ms": 300}},
                                {"fault": "kill", "target": "things-search", "params": {}}])
                res["pass"] = passed(res)
                results.append(res); print(json.dumps(res), flush=True); time.sleep(15)

                # emergency stop + concurrent request while active
                def concurrent_then_stop(cl, _srv):
                    dup = cl.post(f"{API}/scenarios/run", json={"authorize": True, "scenario": scenario_body(
                        [{"fault": "kill", "target": "things"}])}).status_code
                    stop = cl.post(f"{API}/stop").status_code
                    st_ = wait(cl)
                    after = state("gateway")
                    cl.post(f"{API}/orchestrator/reset")
                    return {"concurrent_http": dup, "stop_http": stop, "state": st_["record"]["state"],
                            "abort_reason": st_["record"]["abort_reason"],
                            "rollback_verified": st_["record"]["rollback_verified"], "after_clean": clean(after)}
                res = run_case(c, "emergency stop + concurrent request", [{"fault": "loss", "target": "gateway",
                                                                            "params": {"loss_pct": 3}}],
                               crash=concurrent_then_stop)
                cr = res["crash"]
                res["pass"] = (cr["concurrent_http"] == 409 and cr["stop_http"] == 200 and cr["rollback_verified"]
                               and cr["after_clean"] and "emergency stop" in (cr["abort_reason"] or ""))
                results.append(res); print(json.dumps(res), flush=True); time.sleep(15)

                # missing / unhealthy target: preflight must refuse, nothing injected
                docker("stop", cname("connectivity"))
                try:
                    r = c.post(f"{API}/scenarios/run", json={"authorize": True, "scenario": scenario_body(
                        [{"fault": "pause", "target": "connectivity"}])})
                    st = wait(c) if r.status_code == 200 else None
                    rec = (st or {}).get("record") or {}
                    results.append({"case": "unhealthy target refused at preflight", "start_http": r.status_code,
                                    "state": rec.get("state"), "injected": rec.get("injected"),
                                    "error": (rec.get("error") or r.text)[:200],
                                    "pass": not rec.get("injected") and rec.get("state") != "COMPLETED"})
                    c.post(f"{API}/orchestrator/reset")
                finally:
                    docker("start", cname("connectivity"))
                print(json.dumps(results[-1]), flush=True); time.sleep(20)

                # crash of the API process with a fault active -> journal-based recovery on restart
                def crash_and_restart(cl, srv):
                    srv.kill()
                    still = state("things-search")
                    srv.start()
                    time.sleep(3)
                    rec_ = httpx.get(f"{API}/recovery", timeout=30).json()
                    after = state("things-search")
                    ready = httpx.get(f"{API}/readyz", timeout=30).json()
                    return {"fault_active_after_crash": still["paused"], "after_restart_clean": clean(after),
                            "lockout": rec_.get("lockout"), "startup_recovery": ready.get("startup_recovery")}
                res = run_case(c, "API crash mid-fault, recovery on restart",
                               [{"fault": "pause", "target": "things-search", "params": {}}],
                               crash=crash_and_restart, server=server)
                cr = res["crash"]
                res["pass"] = cr["fault_active_after_crash"] and cr["after_restart_clean"] and not cr["lockout"]
                results.append(res); print(json.dumps(res), flush=True)
                with httpx.Client(timeout=60) as c2:
                    c2.post(f"{API}/controller/mode", json={"mode": "manual"})
                    time.sleep(30)
                    # repeat execution after everything above: no leaked locks or state
                    res = run_case(c2, "repeat kill@things-search (state leakage)",
                                   [{"fault": "kill", "target": "things-search", "params": {}}])
                    res["pass"] = passed(res)
                    results.append(res); print(json.dumps(res), flush=True)
                    c2.post(f"{API}/controller/mode", json={"mode": "recommend"})
    finally:
        server.stop()
    out = os.path.join(ROOT, config.REPORTS_DIR, f"chaos_matrix_{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    ok = sum(1 for r in results if r.get("pass"))
    print(f"\n[matrix] {ok}/{len(results)} cases passed; details in {out}", flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(set(sys.argv[1:])))
