"""Server-side monitor: turns probe.py's metrics files and Docker's own view of the
compose project into live events, independent of any browser being connected.

It reports two different things and never mixes them:
  - the TARGET's health (probe.current_health(): is Ditto answering?)
  - the MONITORING pipeline's health (is this loop ticking, is probe.py still writing,
    can Docker be queried?)
A failure to observe is reported as unknown/stale -- never as healthy and never as down.

This loop is not the safety controller. A running scenario probes Ditto itself, in
process (scenario.py), so stopping an experiment does not depend on this task, on
probe.py, or on a dashboard connection.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time

import config
import guard
import probe
from events import bus

SERVICES_INTERVAL_S = 10.0
_sleep = asyncio.sleep   # bound once: the monitor's pacing is wall-clock, independent of scenario time
DOCKER_QUERY_TIMEOUT_S = 12.0


def latest_metrics_file(metrics_dir: str | None = None) -> str | None:
    """Most recently WRITTEN probe file (mtime, not name -- see probe.current_health)."""
    metrics_dir = metrics_dir or config.METRICS_DIR
    try:
        files = [f for f in os.listdir(metrics_dir) if f.startswith("probe-") and f.endswith(".jsonl")]
    except OSError:
        return None

    def mtime(name: str) -> float:
        try:
            return os.path.getmtime(os.path.join(metrics_dir, name))
        except OSError:
            return -1.0
    return os.path.join(metrics_dir, max(files, key=mtime)) if files else None


def read_rows(window_s: float, max_points: int = 400, since_ts: float | None = None) -> list[dict]:
    """Probe rows from the last `window_s` seconds (optionally only those after since_ts),
    oldest first, down-sampled to max_points. Malformed lines are skipped, not guessed."""
    path = latest_metrics_file()
    if path is None:
        return []
    max_lines = min(20000, max(50, int(window_s / max(config.PROBE_INTERVAL_S, 0.1)) + 20))
    try:
        lines = probe._tail_lines(path, max_lines)
    except OSError:
        return []
    cutoff = max(time.time() - window_s, since_ts if since_ts is not None else float("-inf"))
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = r.get("ts") if isinstance(r, dict) else None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > cutoff:
            rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    if len(rows) > max_points:
        step = len(rows) / max_points
        rows = [rows[int(i * step)] for i in range(max_points)]
    return rows


def _docker(args: list[str]) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=DOCKER_QUERY_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[0][:300] if (r.stderr or r.stdout)
                           else f"docker exited {r.returncode}")
    return r.stdout


def _pct(text: str) -> float | None:
    try:
        return round(float(text.strip().rstrip("%")), 2)
    except ValueError:
        return None


def collect_services() -> dict:
    """Blocking (run it in a thread). {'available', 'error', 'services': [...] | None}."""
    try:
        ps = _docker(["ps", "-a", "--filter", f"label=com.docker.compose.project={guard.COMPOSE_PROJECT}",
                      "--format", '{{.Label "com.docker.compose.service"}}\t{{.Names}}\t{{.State}}\t{{.Status}}'])
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"[:300], "services": None}
    services = {}
    for line in ps.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[0]:
            continue
        services[parts[1]] = {"service": parts[0], "container": parts[1], "state": parts[2].strip().lower(),
                              "status_text": parts[3], "allowlisted": parts[0] in guard.ALLOWLIST,
                              "cpu_pct": None, "mem_pct": None, "mem_usage": None}
    stats_error = None
    running = [name for name, s in services.items() if s["state"] == "running"]
    if running:
        try:
            out = _docker(["stats", "--no-stream", "--format",
                           "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}", *running])
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) == 4 and parts[0] in services:
                    services[parts[0]].update(cpu_pct=_pct(parts[1]), mem_usage=parts[2].strip(),
                                              mem_pct=_pct(parts[3]))
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
            stats_error = f"{type(e).__name__}: {e}"[:300]   # states are still valid; usage is unknown
    return {"available": True, "error": stats_error,
            "services": sorted(services.values(), key=lambda s: s["service"])}


class Monitor:
    def __init__(self) -> None:
        self.started_at: float | None = None
        self.last_loop_ts: float | None = None
        self.last_row_ts: float | None = None
        self.last_error: str | None = None
        self.restarts = 0
        self.services: dict = {"available": None, "error": "not queried yet", "services": None}
        self.services_ts: float | None = None
        self._task: asyncio.Task | None = None
        self._services_task: asyncio.Task | None = None

    # ---- status (monitoring pipeline, not the target) ----
    def status(self) -> dict:
        now = time.time()
        loop_age = None if self.last_loop_ts is None else round(now - self.last_loop_ts, 1)
        loop_ok = loop_age is not None and loop_age <= max(10.0, config.PROBE_INTERVAL_S * 4)
        probe_age = None if self.last_row_ts is None else round(now - self.last_row_ts, 1)
        probe_fresh = probe_age is not None and probe_age <= probe.FRESHNESS_MAX_AGE_S
        if not loop_ok:
            state = "down"
        elif not probe_fresh:
            state = "stale"
        else:
            state = "ok"
        return {"state": state, "loop_running": loop_ok, "loop_age_s": loop_age,
                "probe_fresh": probe_fresh, "probe_last_row_ts": self.last_row_ts, "probe_age_s": probe_age,
                "docker_available": self.services.get("available"),
                "docker_error": self.services.get("error"), "services_ts": self.services_ts,
                "last_error": self.last_error, "restarts": self.restarts,
                "stream": bus.stats()}

    def snapshot(self, window_s: float = 600) -> dict:
        return {"health": {**probe.current_health(), "num_cars": config.NUM_CARS, "ditto_url": config.DITTO_URL},
                "monitoring": self.status(), "services": self.services,
                "rows": read_rows(window_s)}

    # ---- loop ----
    async def _refresh_services(self) -> None:
        self.services = await asyncio.to_thread(collect_services)
        self.services_ts = time.time()
        bus.publish("services", {**self.services, "ts": self.services_ts})

    def tick(self) -> None:
        """One iteration, synchronous and cheap (file tail + summary)."""
        since = self.last_row_ts if self.last_row_ts is not None else time.time() - config.PROBE_INTERVAL_S * 3
        for row in read_rows(120, max_points=60, since_ts=since):
            self.last_row_ts = row["ts"]
            bus.publish("metric", {"row": row, "source": "probe"})
        self.last_loop_ts = time.time()
        health = {**probe.current_health(), "num_cars": config.NUM_CARS, "ditto_url": config.DITTO_URL}
        bus.publish("health", {"health": health, "monitoring": self.status()})

    async def _loop(self) -> None:
        self.started_at = time.time()
        last_services = 0.0
        while True:
            try:
                self.tick()
                if time.monotonic() - last_services >= SERVICES_INTERVAL_S and (
                        self._services_task is None or self._services_task.done()):
                    last_services = time.monotonic()
                    # Its own task: a slow `docker stats` must not delay the metric loop.
                    self._services_task = asyncio.create_task(self._refresh_services())
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- the loop must survive any single bad iteration
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                print(f"[monitor] iteration failed: {self.last_error}", flush=True)
            await _sleep(config.PROBE_INTERVAL_S)

    async def _supervise(self) -> None:
        while True:
            try:
                await self._loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.restarts += 1
                self.last_error = f"monitor loop crashed and was restarted: {type(e).__name__}: {e}"[:300]
                print(f"[monitor] {self.last_error}", flush=True)
                await _sleep(min(30.0, 2.0 ** min(self.restarts, 4)))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._supervise(), name="monitor")

    async def stop(self) -> None:
        for task in (self._task, self._services_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = self._services_task = None


monitor = Monitor()
