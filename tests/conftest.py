"""Shared fixtures for the v2 suites (faults, scenarios, agents, store, controller, API).

`sandbox` isolates every file the platform writes into a temp dir and replaces Docker
with FakeDocker, an in-memory model of containers / tc rules / CPU quotas that answers
the exact argument lists faults.py issues. Nothing in these suites shells out.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import guard  # noqa: E402
import probe  # noqa: E402

REAL_RUN_CMD = guard.run_cmd   # captured before any fixture replaces it

MUTATING = ("kill", "stop", "restart", "pause", "unpause", "start", "update", "rm")

OK_ROW = {"ts": 0, "direct_ok": True, "search_ok": True, "direct_ms": 10.0, "search_ms": 10.0,
          "stale_p50_s": 1.0, "stale_p95_s": 1.0, "stale_max_s": 1.0, "lost_acked": 0, "search_drift": 0}
BASELINE = {"recorded_at": None, "direct_ms": {"p50": 8, "p95": 20, "p99": 25},
            "search_ms": {"p50": 8, "p95": 20, "p99": 25}, "direct_error_rate": 0.0,
            "search_error_rate": 0.0, "stale_p95_s": 1.0, "lost_acked_max": 0, "search_drift_max": 0}
DEP_MAP = {
    "edges": [{"from": "gateway", "to": "things"}, {"from": "gateway", "to": "things-search"},
              {"from": "things", "to": "mongodb"}, {"from": "things-search", "to": "mongodb"}],
    "fan_in": {"things": 1, "things-search": 1, "mongodb": 2, "gateway": 0},
    "fan_out": {"gateway": 2, "things": 1, "things-search": 1},
    "fault_target_recommendations": [
        {"service": s, "fan_in": f, "calculated_risk": r, "allowlisted_for_faults": True, "reasoning": "x"}
        for s, f, r in (("mongodb", 2, "high"), ("things", 1, "medium"), ("things-search", 1, "medium"),
                        ("gateway", 0, "low"), ("policies", 0, "low"), ("connectivity", 0, "low"))],
}


class FakeDocker:
    """Answers guard.run_cmd() for the commands faults.py/guard.py issue."""

    def __init__(self):
        self.containers = {s: {"id": f"id-{s}-" + "0" * 20, "name": f"docker-{s}-1", "running": True,
                               "paused": False, "netem": None, "quota": 0, "nano": 0}
                           for s in guard.ALLOWLIST}
        self.calls: list[list[str]] = []
        self.fail: dict[str, Exception | str] = {}   # substring of the joined command -> error
        self.fail_once: dict[str, str] = {}
        self.image_present = True
        self.daemon_up = True
        self.helpers: set[str] = set()
        self.block_netem_delete = False   # simulate a tc rule that cannot be removed
        self.timeout_on: str | None = None
        self.previewed: list[list[str]] = []

    def as_subprocess(self, args, **kw):
        """Stand-in for subprocess.run, so the REAL guard.run_cmd can drive this fake."""
        import subprocess
        self.calls.append(list(args))
        joined = " ".join(args)
        if self.timeout_on and self.timeout_on in joined:
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        for key, err in self.fail.items():
            if key in joined:
                return subprocess.CompletedProcess(args, 1, "", str(err))
        if not self.daemon_up:
            return subprocess.CompletedProcess(args, 1, "", "Cannot connect to the Docker daemon")
        try:
            return subprocess.CompletedProcess(args, 0, self._run(args, joined), "")
        except guard.GuardError as e:
            return subprocess.CompletedProcess(args, 1, "", str(e))

    # -- helpers for tests
    def by_id(self, cid):
        return next((c for c in self.containers.values() if c["id"] == cid or c["name"] == cid), None)

    def mutating_calls(self):
        out = []
        for c in self.calls:
            joined = " ".join(c)
            if c[1] in MUTATING or "qdisc replace" in joined or "qdisc del" in joined:
                out.append(c)
        return out

    async def __call__(self, args, dry_run=False, check=True, timeout=None):
        # Same contract as the real run_cmd: a dry run (explicit, or forced for a scenario
        # dry run) reaches the daemon with nothing.
        if dry_run or guard._force_dry_run.get():
            self.previewed.append(list(args))
            return ""
        self.calls.append(list(args))
        joined = " ".join(args)
        for key in list(self.fail_once):
            if key in joined:
                msg = self.fail_once.pop(key)
                if check:
                    raise guard.GuardError(msg)
                return ""
        for key, err in self.fail.items():
            if key in joined:
                if check:
                    raise guard.GuardError(str(err))
                return ""
        if not self.daemon_up:
            if check:
                raise guard.GuardError("command failed (1): docker ...\nCannot connect to the Docker daemon")
            return ""
        try:
            return self._run(args, joined)
        except guard.GuardError:
            if check:
                raise
            return ""

    def _run(self, args, joined):
        cmd = args[1]
        if cmd in ("version", "info"):
            return "29.7.0" if cmd == "version" else "linux\tDocker Desktop\t2"
        if cmd == "image":
            if not self.image_present:
                raise guard.GuardError("command failed (1): No such image")
            return "sha256:abc"
        if cmd == "ps":
            svc = next(a.split("service=")[1] for a in args if "compose.service=" in a)
            c = self.containers.get(svc)
            if c is None:
                return ""
            state = "running" if c["running"] else "exited"
            cid = c["id"] if "--no-trunc" in args else c["id"][:12]
            return f"{c['name']}\t{cid}\t{state}"
        if cmd == "inspect":
            c = self.by_id(args[-1])
            if c is None:
                raise guard.GuardError(f"command failed (1): Error: No such object: {args[-1]}")
            fmt = args[3]
            if fmt == "{{.Id}}":
                return c["id"]
            if "NanoCpus" in fmt:
                return f"{c['nano']}\t{c['quota']}\t100000"
            if "Paused" in fmt:
                return f"{'true' if c['running'] else 'false'}\t{'true' if c['paused'] else 'false'}"
            return "true" if c["running"] else "false"
        if cmd in ("kill", "stop"):
            self.by_id(args[-1])["running"] = False
            return ""
        if cmd == "start":
            c = self.by_id(args[-1])
            if c is None:
                raise guard.GuardError("command failed (1): No such container")
            c["running"] = True
            return ""
        if cmd == "restart":
            self.by_id(args[-1])["running"] = True
            return ""
        if cmd == "pause":
            self.by_id(args[-1])["paused"] = True
            return ""
        if cmd == "unpause":
            self.by_id(args[-1])["paused"] = False
            return ""
        if cmd == "update":
            c = self.by_id(args[-1])
            c["quota"] = int(args[args.index("--cpu-quota") + 1])
            return ""
        if cmd == "rm":
            self.helpers.discard(args[-1])
            return ""
        if cmd == "run":   # netshoot helper sharing the target's netns
            target = next(a.split("container:")[1] for a in args if a.startswith("container:"))
            c = self.by_id(target)
            if c is None or not c["running"]:
                raise guard.GuardError("command failed (125): cannot join network of a non running container")
            script = args[-1]
            if "qdisc replace" in script:
                c["netem"] = script.split("netem ")[1].split(" &&")[0]
                if "--name" in args:
                    self.helpers.add(args[args.index("--name") + 1])
                return "helperid"
            if "qdisc del" in script:
                if not self.block_netem_delete:
                    c["netem"] = None
                return ""
            if "qdisc show" in script:
                if c["netem"]:
                    return f"qdisc netem 8001: root refcnt 2 limit 1000 {c['netem']}"
                return "qdisc noqueue 0: root refcnt 2"
        raise AssertionError(f"FakeDocker: unexpected command {args}")


def make_probe(rows=(), default=None):
    it = iter(rows)
    default = default if default is not None else OK_ROW

    async def fake(client, ids):
        try:
            return dict(next(it))
        except StopIteration:
            return dict(default)
    return fake


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """No test under tests/ may write the repository's real knowledge DB, journal or lockout,
    or depend on the fleet configured in the developer's .env (default: car-only fleet)."""
    import fleet
    import store
    monkeypatch.setattr(fleet, "_current", fleet.load(env={}))
    fleet.reset_quality_cache()
    monkeypatch.setattr(config, "KNOWLEDGE_DB", str(tmp_path / "state" / "k.db"))
    monkeypatch.setattr(config, "JOURNAL_DIR", str(tmp_path / "state" / "journal"))
    monkeypatch.setattr(config, "LOCKOUT_FILE", str(tmp_path / "state" / "recovery_required.json"))
    store._initialized.clear()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated dirs + FakeDocker + a healthy fake probe + fast ticks."""
    import faults
    import orchestrator
    import scenario
    import store
    from agents import coordinator
    from controller import controller

    for name, value in (("EXPERIMENTS_DIR", tmp_path / "experiments"), ("REPORTS_DIR", tmp_path / "reports"),
                        ("METRICS_DIR", tmp_path / "metrics"), ("KNOWLEDGE_DB", tmp_path / "state" / "k.db"),
                        ("JOURNAL_DIR", tmp_path / "state" / "journal"),
                        ("LOCKOUT_FILE", tmp_path / "state" / "recovery_required.json"),
                        ("BASELINE_FILE", tmp_path / "no-baseline.json")):
        monkeypatch.setattr(config, name, str(value))
    monkeypatch.setattr(config, "AI_ENABLED", False)
    monkeypatch.setattr(config, "API_TOKEN", "")
    monkeypatch.setattr(config, "PROBE_INTERVAL_S", 1.0)
    monkeypatch.setattr(config, "ROLLBACK_STEP_TIMEOUT_S", 5.0)
    monkeypatch.setattr(guard, "LOCKS_DIR", str(tmp_path / "experiments" / ".locks"))
    monkeypatch.setattr(guard, "RECOVERY_STREAK", 2)
    monkeypatch.setattr(faults, "START_WAIT_S", 0.3)
    store._initialized.clear()

    docker = FakeDocker()
    monkeypatch.setattr(guard, "run_cmd", docker)
    monkeypatch.setattr(probe, "probe_once", make_probe())
    monkeypatch.setattr(probe, "current_health", lambda window_s=10, metrics_dir=None: {
        "status": "healthy", "fresh": True, "sample_count": 5})
    monkeypatch.setattr(orchestrator, "_load_or_build_dependency_map", lambda: DEP_MAP)

    # Virtual time: every sleep returns immediately and advances the scenario clock by the
    # requested delay, so a 10s scenario with 1s ticks runs in milliseconds.
    real_sleep = asyncio.sleep
    now = {"t": 1000.0}

    async def fast_sleep(delay, *a, **kw):
        now["t"] += max(0.0, delay)
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(scenario, "clock", lambda: now["t"])

    orchestrator.reset()
    orchestrator._recent_requests.clear()
    controller.mode = "manual"
    controller.loop.update(running=False, executed=0, cycles=0, stopped_reason=None)
    for live in coordinator._live.values():
        live.update(state="idle", task=None, last=None, failures=0, runs=0)
    yield docker
    orchestrator.reset()


def auth(actor="tester"):
    import scenario
    return scenario.Authorization(kind="operator", actor=actor)


def scn(*actions, **kw):
    import scenario
    kw.setdefault("max_duration_s", 10)
    kw.setdefault("pre_s", 2)
    kw.setdefault("recovery_timeout_s", 20)
    return scenario.Scenario(actions=[{"fault": f, "target": t, **(extra[0] if extra else {})}
                                      for f, t, *extra in actions], **kw)
