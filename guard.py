"""Guard: inject ONE controlled failure into Ditto and auto-revert it (kill switch).

What it does:
  1. Measures a short "before" baseline (or uses a validated baseline.json if present).
  2. Injects a fault into one allowlisted Ditto service.
  3. Every PROBE_INTERVAL_S seconds checks health (using probe.probe_once).
     If a threshold is breached -> reverts immediately (kill switch).
  4. Always reverts: on breach, at max duration, on Ctrl+C, or on any crash -- as long
     as injection was attempted, revert is attempted and its result is verified.
  5. Watches until Ditto is healthy again and records recovery time.
  6. Saves everything to experiments/<id>.json, written atomically (Day-2 AI reads these).

Lifecycle (recorded in record["state"]):
  PRECHECK -> BASELINE -> INJECTING -> MONITORING -> REVERTING -> VERIFYING_RECOVERY -> COMPLETED
  Terminal states on the unhappy paths: ABORTED, FAILED, RECOVERY_TIMEOUT.

Faults:
  kill   - docker kill <container>        revert: docker start <container> (+ identity/state check)
  delay  - add network delay (tc netem via nicolaka/netshoot) to the container.
           The helper container removes the delay itself after --ttl seconds,
           even if guard.py crashes (dead-man's switch; see README for its limits).

Safety:
  - Only services in ALLOWLIST can be targeted.
  - The container must carry docker compose labels for project COMPOSE_PROJECT and be running.
  - A real (non-dry-run) experiment asks for interactive confirmation unless --yes is passed.
  - Only one real experiment per --target may run at a time (file lock in experiments/.locks).
  - Run it ONLY on the main laptop (where Ditto runs), and tell the team first.

Examples:
  python guard.py --fault kill  --target things-search --max-duration 60
  python guard.py --fault delay --target mongodb --ms 2000 --ttl 60
  python guard.py --fault delay --target mongodb --ms 2000 --dry-run   (prints commands only)
  python guard.py --fault kill  --target things      --preflight-only (checks readiness, injects nothing)

Before the first delay test, pull the helper image once:  docker pull nicolaka/netshoot
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import math
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import httpx

import config
import probe

SCHEMA_VERSION = "1.1"   # 1.1 adds the additive `events` activity log
GUARD_VERSION = "1.2.0"

COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "docker")
ALLOWLIST = {"gateway", "things", "things-search", "policies", "connectivity", "mongodb"}
NETSHOOT_IMAGE = os.getenv("NETSHOOT_IMAGE", "nicolaka/netshoot")
NETEM_IFACE = os.getenv("NETEM_IFACE", "eth0")
EXPERIMENTS_DIR = os.getenv("EXPERIMENTS_DIR", "experiments")
LOCKS_DIR = os.path.join(EXPERIMENTS_DIR, ".locks")

DOCKER_CMD_TIMEOUT_S = float(os.getenv("DOCKER_CMD_TIMEOUT_S", "30"))
LOCK_STALE_S = float(os.getenv("LOCK_STALE_S", "7200"))
MAX_BASELINE_AGE_S_DEFAULT = float(os.getenv("MAX_BASELINE_AGE_S", "3600"))
RECOVERY_STREAK = int(os.getenv("RECOVERY_STREAK", "3"))

# Hard ceiling per probe tick (probe_once + its own HTTP timeout) so a hung health
# check can never silently stall the experiment past --max-duration.
PROBE_HARD_TIMEOUT_S = config.REQUEST_TIMEOUT_S * 2 + config.PROBE_INTERVAL_S + 2

# Lifecycle states, recorded in the saved experiment JSON for AI-readiness.
STATE_PRECHECK = "PRECHECK"
STATE_BASELINE = "BASELINE"
STATE_INJECTING = "INJECTING"
STATE_MONITORING = "MONITORING"
STATE_REVERTING = "REVERTING"
STATE_VERIFYING_RECOVERY = "VERIFYING_RECOVERY"
STATE_COMPLETED = "COMPLETED"
STATE_ABORTED = "ABORTED"
STATE_FAILED = "FAILED"
STATE_RECOVERY_TIMEOUT = "RECOVERY_TIMEOUT"


class GuardError(Exception):
    """User-facing, actionable error. Never means a fault was left unreverted."""


class RollbackError(GuardError):
    """A revert/cleanup step ran but could not be verified. Never swallowed silently."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- docker helpers
# An experiment installs a callback here so every command issued on its behalf -- by this
# module or by faults.py -- is reported to its activity log, without each caller having
# to pass one. Tasks and worker threads started by the experiment inherit it.
_command_sink: "contextvars.ContextVar" = contextvars.ContextVar("guard_command_sink", default=None)
# When true, run_cmd executes NOTHING, whatever its dry_run argument says (scenario dry runs).
_force_dry_run: "contextvars.ContextVar[bool]" = contextvars.ContextVar("guard_force_dry_run", default=False)


def _report_command(on_command, info: dict) -> None:
    cb = on_command or _command_sink.get()
    if cb is None:
        return
    try:
        cb(info)
    except Exception as e:  # noqa: BLE001 -- reporting a command must never change its outcome
        print(f"[guard] WARNING: command observer failed: {type(e).__name__}: {e}", flush=True)


async def run_cmd(args: list[str], dry_run: bool = False, check: bool = True,
                   timeout: float = DOCKER_CMD_TIMEOUT_S, on_command=None) -> str:
    """Run a subprocess off the event loop thread, with an explicit timeout.

    dry_run short-circuits BEFORE any subprocess is started -- dry-run performs
    zero Docker interaction, not even a read-only one.

    `on_command` (optional; defaults to the experiment-scoped observer, if any) is called
    once with {argv, executed, returncode, stderr, timed_out, timeout_s, duration_ms}. It
    reports what happened; it cannot alter it, and callers that pass nothing behave
    exactly as before.
    """
    if dry_run or _force_dry_run.get():
        # Labelled explicitly: an unlabelled "$ docker kill ..." line is indistinguishable
        # from a real execution when read back in a terminal or a saved log.
        print("[guard] (dry-run, NOT executed) $ " + " ".join(args), flush=True)
        _report_command(on_command, {"argv": list(args), "executed": False})
        return ""
    print("[guard] $ " + " ".join(args), flush=True)
    t0 = time.monotonic()
    try:
        r = await asyncio.to_thread(subprocess.run, args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _report_command(on_command, {"argv": list(args), "executed": True, "returncode": None, "timed_out": True,
                                     "timeout_s": timeout, "duration_ms": int((time.monotonic() - t0) * 1000)})
        msg = f"command timed out after {timeout}s: {' '.join(args)}"
        if check:
            raise GuardError(msg) from e
        print(f"[guard] WARNING (non-fatal): {msg}", flush=True)
        return ""
    except FileNotFoundError as e:
        _report_command(on_command, {"argv": list(args), "executed": False, "returncode": None,
                                     "stderr": "docker CLI not found on PATH"})
        raise GuardError("docker CLI not found on PATH -- is Docker installed and on PATH?") from e
    _report_command(on_command, {"argv": list(args), "executed": True, "returncode": r.returncode,
                                 "stderr": (r.stderr or "").strip()[:400] if r.returncode != 0 else "",
                                 "timed_out": False, "duration_ms": int((time.monotonic() - t0) * 1000)})
    if r.returncode != 0:
        if check:
            raise GuardError(f"command failed ({r.returncode}): {' '.join(args)}\n{r.stderr.strip()}")
        print(f"[guard] (non-fatal) command failed ({r.returncode}): {r.stderr.strip()}", flush=True)
    return r.stdout.strip()


async def docker_preflight() -> None:
    try:
        await run_cmd(["docker", "version", "--format", "{{.Server.Version}}"])
    except GuardError as e:
        raise GuardError(f"Docker daemon is not reachable (is it running? do you have permission?): {e}") from e


@dataclass(frozen=True)
class ContainerRef:
    name: str
    id: str


async def resolve_container(service: str, dry_run: bool) -> ContainerRef:
    """Find exactly one RUNNING container for an allowlisted service of OUR compose project."""
    if service not in ALLOWLIST:
        raise GuardError(f"'{service}' is not in the allowlist {sorted(ALLOWLIST)}")
    if dry_run:
        # Fully synthetic -- no Docker call of any kind in dry-run mode.
        return ContainerRef(name=f"{COMPOSE_PROJECT}-{service}-1", id="dry-run-fake-id")
    out = await run_cmd(["docker", "ps", "-a",
                         "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                         "--filter", f"label=com.docker.compose.service={service}",
                         "--format", "{{.Names}}\t{{.ID}}\t{{.State}}"])
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    if len(rows) != 1:
        names = [r[0] for r in rows]
        raise GuardError(f"expected exactly 1 container for service '{service}' in compose project "
                          f"'{COMPOSE_PROJECT}', found {names or 'none'}. Is Ditto running? "
                          f"Set COMPOSE_PROJECT if your project name differs.")
    name, _short_id, state = rows[0]
    if state.strip().lower() != "running":
        raise GuardError(f"container '{name}' for service '{service}' exists but is not running "
                          f"(state={state}). Start it first: docker start {name}")
    # `docker ps --format {{.ID}}` only ever returns the SHORT (12-char) id, but
    # `docker inspect -f {{.Id}}` (used later to re-verify identity before reverting)
    # returns the FULL id. Capture the full id here too so that later comparison is
    # apples-to-apples instead of always mismatching.
    full_id = await _current_container_id(name)
    if not full_id:
        raise GuardError(f"could not read a container id for '{name}' via `docker inspect`.")
    return ContainerRef(name=name, id=full_id)


async def _current_container_id(name: str) -> str | None:
    out = await run_cmd(["docker", "inspect", "-f", "{{.Id}}", name], check=False)
    return out.strip() or None


async def _wait_running(name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        out = await run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False)
        if out.strip() == "true":
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)


class KillFault:
    name = "kill"

    def __init__(self, container: ContainerRef, dry_run: bool):
        self.container, self.dry_run = container, dry_run
        self.params: dict = {}

    async def preflight(self) -> None:
        return  # resolve_container() already verified it is running

    async def inject(self) -> None:
        # Addressed by full id: a name can be re-used by a different container, an id cannot.
        await run_cmd(["docker", "kill", self.container.id], self.dry_run)

    async def revert(self) -> None:
        if self.dry_run:
            await run_cmd(["docker", "start", self.container.id], True)
            return
        current_id = await _current_container_id(self.container.name)
        if current_id is None:
            raise RollbackError(
                f"could not read the current id of '{self.container.name}' (container removed, or "
                f"Docker did not answer) -- refusing to start anything by name without confirming "
                f"it is the container that was killed. Check `docker ps -a` and start it manually.")
        if current_id != self.container.id:
            raise RollbackError(
                f"container '{self.container.name}' now has id {current_id[:12]}, not the "
                f"original {self.container.id[:12]} -- refusing to start what may be a different "
                f"container. Investigate manually (something else may have recreated it).")
        await run_cmd(["docker", "start", self.container.id])
        if not await _wait_running(self.container.id, timeout=15):
            raise RollbackError(f"'docker start' returned success but '{self.container.name}' did not "
                                 f"reach the running state within 15s; check `docker logs {self.container.name}`.")


class DelayFault:
    name = "delay"

    def __init__(self, container: ContainerRef, dry_run: bool, ms: int, ttl: int):
        if not (1 <= ms <= 30000):
            raise GuardError("--ms must be between 1 and 30000")
        if not (5 <= ttl <= 600):
            raise GuardError("--ttl must be between 5 and 600 seconds")
        self.container, self.dry_run = container, dry_run
        self.params = {"delay_ms": ms, "ttl_s": ttl, "iface": NETEM_IFACE}
        self.helper = f"chaos-delay-{uuid.uuid4().hex[:8]}"

    def _netshoot_args(self, script: str, detach: bool, name: str | None = None) -> list[str]:
        args = ["docker", "run", "--rm", "--cap-add", "NET_ADMIN",
                "--net", f"container:{self.container.name}"]
        if detach:
            args.append("-d")
        if name:
            args += ["--name", name]
        return args + [NETSHOOT_IMAGE, "sh", "-c", script]

    async def _qdisc_show(self) -> str:
        """Raises GuardError if the check itself fails -- callers must NEVER treat a
        failed/timed-out check the same as "confirmed clean". `docker image inspect`
        on a missing image famously prints `[]` (non-empty!) to stdout while still
        exiting non-zero, so this -- like the image-presence check below -- relies on
        run_cmd's check=True exit-code handling, not on guessing from stdout content."""
        return await run_cmd(self._netshoot_args(f"tc qdisc show dev {NETEM_IFACE}", detach=False),
                              self.dry_run, check=True)

    async def preflight(self) -> None:
        """Read-only safety checks, run BEFORE anything is touched."""
        if self.dry_run:
            return
        try:
            await run_cmd(["docker", "image", "inspect", NETSHOOT_IMAGE])
        except GuardError as e:
            raise GuardError(f"helper image '{NETSHOOT_IMAGE}' is not available locally.\n"
                              f"  Run: docker pull {NETSHOOT_IMAGE}\n  ({e})") from e
        try:
            existing = await self._qdisc_show()
        except GuardError as e:
            raise GuardError(f"could not read the current tc state on '{self.container.name}' to "
                              f"check for a pre-existing netem config: {e}") from e
        if "netem" in existing:
            raise GuardError(
                f"'{self.container.name}' already has a netem qdisc on {NETEM_IFACE}, possibly left "
                f"over from a crashed previous run:\n  {existing.strip()}\n"
                f"Refusing to inject on top of unknown existing tc state. Clear it manually first:\n"
                f"  docker run --rm --cap-add NET_ADMIN --net container:{self.container.name} "
                f"{NETSHOOT_IMAGE} tc qdisc del dev {NETEM_IFACE} root\n"
                f"(guard.py cannot distinguish other custom, non-netem qdiscs from kernel defaults -- "
                f"see README limitations.)")

    async def inject(self) -> None:
        ms, ttl = self.params["delay_ms"], self.params["ttl_s"]
        # Dead-man's switch: the helper removes the delay itself after `ttl`s even if
        # guard.py crashes. See README for the failure modes this does NOT cover.
        script = (f"tc qdisc replace dev {NETEM_IFACE} root netem delay {ms}ms && "
                  f"sleep {ttl}; tc qdisc del dev {NETEM_IFACE} root 2>/dev/null; true")
        await run_cmd(self._netshoot_args(script, detach=True, name=self.helper), self.dry_run)
        if self.dry_run:
            return
        await asyncio.sleep(0.5)  # give tc a moment to apply before verifying
        try:
            check = await self._qdisc_show()
        except GuardError as e:
            raise GuardError(f"could not verify the delay fault applied to "
                              f"'{self.container.name}': {e}") from e
        if "netem" not in check:
            raise GuardError(f"delay fault did not apply: tc qdisc show reports: {check.strip() or '(empty)'}")

    async def revert(self) -> None:
        await run_cmd(["docker", "rm", "-f", self.helper], self.dry_run, check=False)
        if self.dry_run:
            await run_cmd(self._netshoot_args(f"tc qdisc del dev {NETEM_IFACE} root 2>/dev/null; true",
                                               detach=False), True, check=False)
            return
        detail = "(no check succeeded)"
        for attempt in range(2):
            await run_cmd(self._netshoot_args(f"tc qdisc del dev {NETEM_IFACE} root 2>/dev/null; true",
                                               detach=False), False, check=False)
            try:
                check = await self._qdisc_show()
            except GuardError as e:
                detail = f"could not verify: {e}"  # NOT treated as clean -- fail closed, keep retrying
            else:
                if "netem" not in check:
                    return  # verified clean -- idempotent: a 2nd call also finds it clean and returns here
                detail = f"tc qdisc show still reports: {check.strip()}"
            if attempt == 0:
                await asyncio.sleep(1.0)
        raise RollbackError(
            f"could not confirm the netem delay was removed from '{self.container.name}' after 2 "
            f"cleanup attempts ({detail}).\nManually run:\n"
            f"  docker run --rm --cap-add NET_ADMIN --net container:{self.container.name} "
            f"{NETSHOOT_IMAGE} tc qdisc del dev {NETEM_IFACE} root")


# ---------------------------------------------------------------- thresholds & health logic
@dataclass(frozen=True)
class Thresholds:
    max_error_rate: float          # looser, longer-window "overall degraded" signal
    error_rate_window: int
    max_latency_factor: float      # x baseline p95
    min_latency_floor_ms: float    # ignore tiny baselines
    max_stale_s: float
    breach_window: int              # consecutive ticks for the fast outage/latency/stale signals
    recovery_latency_factor: float
    recovery_stale_s: float

    @classmethod
    def from_env(cls) -> "Thresholds":
        breach_window = int(os.getenv("BREACH_WINDOW", "3"))
        return cls(
            max_error_rate=float(os.getenv("MAX_ERROR_RATE", "0.3")),
            error_rate_window=int(os.getenv("MAX_ERROR_RATE_WINDOW", str(max(breach_window, 10)))),
            max_latency_factor=float(os.getenv("MAX_LATENCY_FACTOR", "3.0")),
            min_latency_floor_ms=float(os.getenv("MIN_LATENCY_FLOOR_MS", "200")),
            max_stale_s=float(os.getenv("MAX_STALE_S", "15")),
            breach_window=breach_window,
            recovery_latency_factor=float(os.getenv("RECOVERY_LATENCY_FACTOR", "2.0")),
            recovery_stale_s=float(os.getenv("RECOVERY_STALE_S", "5.0")),
        )

    def validate(self) -> None:
        problems = []
        if not (0 < self.max_error_rate <= 1):
            problems.append("MAX_ERROR_RATE must be in (0, 1]")
        if self.error_rate_window < self.breach_window:
            problems.append("MAX_ERROR_RATE_WINDOW must be >= BREACH_WINDOW")
        if self.max_latency_factor <= 1:
            problems.append("MAX_LATENCY_FACTOR must be > 1")
        if self.min_latency_floor_ms < 0:
            problems.append("MIN_LATENCY_FLOOR_MS must be >= 0")
        if self.max_stale_s <= 0:
            problems.append("MAX_STALE_S must be > 0")
        if self.breach_window < 1:
            problems.append("BREACH_WINDOW must be >= 1")
        if self.recovery_latency_factor <= 1:
            problems.append("RECOVERY_LATENCY_FACTOR must be > 1")
        if self.recovery_stale_s <= 0:
            problems.append("RECOVERY_STALE_S must be > 0")
        if problems:
            raise GuardError("invalid threshold configuration: " + "; ".join(problems))


def timeout_row(phase: str = "fault") -> dict:
    """Synthetic row for a probe tick that hung past PROBE_HARD_TIMEOUT_S.

    Shaped identically to probe.probe_once()'s output so every downstream consumer
    (check_breach, is_healthy, probe.summarize) treats it uniformly. `probe_timeout`
    distinguishes "the health check itself failed to respond" from "the health check
    ran and Ditto returned an error" -- both count as a failed tick, but only one is a
    confirmed application failure.
    """
    return {"ts": round(time.time(), 3), "direct_ok": False, "direct_ms": PROBE_HARD_TIMEOUT_S * 1000,
            "search_ok": False, "search_ms": PROBE_HARD_TIMEOUT_S * 1000,
            "stale_p50_s": None, "stale_p95_s": None, "stale_max_s": None,
            "lost_acked": None, "search_drift": None, "probe_timeout": True, "phase": phase}


def check_breach(window: list[dict], baseline: dict, th: Thresholds) -> tuple[bool, str]:
    """Pure function: decide whether to trip the kill switch right now.

    Signal order matters. A confirmed loss of an already-acknowledged update is a
    critical integrity violation: it fires on ANY tick, immediately, without waiting
    for `breach_window` samples -- silent data loss must never wait for a trend.

    The remaining signals need `breach_window` ticks and are each independently
    sufficient:
      - direct-read or search endpoint failing on EVERY one of the last `breach_window`
        ticks ("consecutive outage"): a single endpoint failing every tick DOES trip
        this by design -- that is a real, sustained outage of that endpoint. A single
        transient blip does not, because it won't repeat on every tick in the window.
      - a softer, longer-horizon "overall probe failure rate" over `error_rate_window`
        ticks, for noisy-but-not-always-failing patterns that never satisfy the
        consecutive check.
      - sustained latency or staleness degradation, each over `breach_window` ticks.
    """
    for row in window:
        lost = row.get("lost_acked")
        if lost:
            return True, (f"CRITICAL: {lost} car(s) have a stored value OLDER than an update Ditto "
                          f"already acknowledged (confirmed silent data loss) at ts={row.get('ts')}")

    if len(window) < th.breach_window:
        return False, ""
    recent = window[-th.breach_window:]

    if all(not r["direct_ok"] for r in recent):
        return True, f"direct-read endpoint failed on all of the last {th.breach_window} ticks"

    if all(not r["search_ok"] for r in recent):
        return True, f"search endpoint failed on all of the last {th.breach_window} ticks"

    if len(window) >= th.error_rate_window:
        wide = window[-th.error_rate_window:]
        degraded = sum(1 for r in wide if not r["direct_ok"] or not r["search_ok"])
        rate = degraded / len(wide)
        if rate > th.max_error_rate:
            return True, (f"overall probe failure rate {degraded}/{len(wide)} ({rate:.0%}) > "
                          f"{th.max_error_rate:.0%} over the last {th.error_rate_window} ticks")

    base_p95 = (baseline.get("direct_ms") or {}).get("p95")
    if base_p95:
        limit = max(base_p95 * th.max_latency_factor, th.min_latency_floor_ms)
        if all(r["direct_ok"] and r["direct_ms"] > limit for r in recent):
            return True, (f"read latency > {limit:.0f}ms ({th.max_latency_factor}x baseline p95) "
                          f"for {th.breach_window} consecutive ticks")

    if all(r.get("stale_p95_s") is not None and r["stale_p95_s"] > th.max_stale_s for r in recent):
        return True, f"twin staleness p95 > {th.max_stale_s}s for {th.breach_window} consecutive ticks"

    return False, ""


def is_healthy(row: dict, baseline: dict, th: Thresholds) -> bool:
    """A missing/None reading is never 'healthy' -- both conditions below fail closed."""
    if not (row["direct_ok"] and row["search_ok"]):
        return False
    base_p95 = (baseline.get("direct_ms") or {}).get("p95")
    if base_p95 and row["direct_ms"] > max(base_p95 * th.recovery_latency_factor, th.min_latency_floor_ms):
        return False
    stale = row.get("stale_p95_s")
    return stale is None or stale <= max(th.recovery_stale_s, config.SEND_INTERVAL_S * 3)


def load_baseline() -> dict | None:
    try:
        with open(config.BASELINE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def validate_baseline(baseline: dict | None, source: str, max_age_s: float) -> None:
    """Fail closed: never let a missing, invalid, stale, or unhealthy-when-measured
    baseline silently set the kill-switch thresholds."""
    if not baseline:
        raise GuardError("no usable baseline (missing/empty). Run "
                          "`python probe.py --duration 120 --baseline` first, or re-run guard.py with "
                          "--fresh-baseline --pre 30.")
    base_p95 = (baseline.get("direct_ms") or {}).get("p95")
    if base_p95 is None:
        raise GuardError("baseline is missing direct_ms.p95 (no successful reads were measured); "
                          "cannot safely set latency thresholds. Re-measure against a healthy Ditto.")
    derr, serr = baseline.get("direct_error_rate") or 0, baseline.get("search_error_rate") or 0
    if derr > 0.02 or serr > 0.02:
        raise GuardError(f"baseline looks unhealthy (direct_error_rate={derr}, search_error_rate={serr}); "
                          f"refusing to trust it. Re-measure the baseline while Ditto is healthy.")
    if source == "file":
        recorded_at = baseline.get("recorded_at")
        age = None if not recorded_at else time.time() - recorded_at
        if age is None or age > max_age_s:
            shown = "unknown" if age is None else f"{age / 60:.0f} min"
            raise GuardError(f"baseline.json is stale or missing a timestamp (age={shown}, "
                              f"max allowed={max_age_s / 60:.0f} min). Re-run probe.py --baseline, "
                              f"or pass --fresh-baseline to measure one now from --pre.")


# ---------------------------------------------------------------- persistence
def _sanitize(obj):
    """Replace non-finite floats with null so the record always serializes as strict JSON."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def save(record: dict) -> str:
    """Write the experiment record atomically: build in a temp file, then os.replace
    (atomic on POSIX and Windows) so a crash mid-write never leaves a corrupt/partial
    JSON file for the AI consumer to choke on."""
    os.makedirs(EXPERIMENTS_DIR, exist_ok=True)
    path = os.path.join(EXPERIMENTS_DIR, f"{record['id']}.json")
    tmp = path + f".tmp-{os.getpid()}"
    clean = _sanitize(record)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- concurrency lock
@dataclass
class Lock:
    path: str


def acquire_lock(target: str, force: bool = False) -> Lock:
    """File-based lock scoped to --target, so two experiments can't hit the same
    container concurrently. Staleness is time-based (age > LOCK_STALE_S), not a PID
    liveness check -- that's a deliberate, documented simplification that works the
    same way cross-platform (including Windows)."""
    os.makedirs(LOCKS_DIR, exist_ok=True)
    path = os.path.join(LOCKS_DIR, f"{target}.lock")
    payload = json.dumps({"pid": os.getpid(), "started_at": time.time()}).encode()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        stale = force
        if not force:
            try:
                with open(path, encoding="utf-8") as f:
                    info = json.load(f)
                stale = (time.time() - info.get("started_at", 0)) > LOCK_STALE_S
            except (OSError, ValueError):
                stale = True
        if not stale:
            raise GuardError(
                f"an experiment already appears to be running against '{target}' (lock file {path}). "
                f"If you're sure it isn't, pass --force-unlock or delete that file.")
        print(f"[guard] WARNING: removing {'forced' if force else 'stale'} lock for '{target}' ({path})",
              flush=True)
        os.remove(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    return Lock(path)


def release_lock(lock: Lock | None) -> None:
    if lock is None:
        return
    try:
        os.remove(lock.path)
    except OSError as e:
        print(f"[guard] WARNING: could not remove lock file {lock.path}: {e}", flush=True)


# ---------------------------------------------------------------- experiment
async def run_experiment(fault, target: str, container: ContainerRef, th: Thresholds,
                          max_duration: float, pre_s: float, recovery_timeout: float,
                          fresh_baseline: bool, max_baseline_age_s: float,
                          stop_event: "asyncio.Event | None" = None,
                          on_update: "Callable[[dict], None] | None" = None) -> dict:
    """Runs the full lifecycle and ALWAYS returns a record -- it never raises, even on
    Ctrl+C, a Docker failure, or an unexpected exception, so the caller can always save
    something. A revert is attempted whenever record["injected"] is True, regardless of
    what happened during injection or monitoring.

    `stop_event` (optional, default None -- behavior is unchanged from before this
    parameter existed) lets an external caller (orchestrator.py) request an early,
    graceful stop: checked once before injection (aborts cleanly, nothing to revert)
    and once per tick during monitoring (same path as a kill-switch breach: revert,
    then still verify recovery). This is NOT a second kill switch -- guard.py's own
    check_breach() is unchanged and still runs every tick regardless of stop_event.

    `on_update` (optional, default None) is called with the in-progress `record` dict
    after most probe ticks and again just before returning, so a caller polling from
    another process/thread can show live-ish progress. It must not raise; any
    exception it raises propagates like any other bug in this function -- callers
    should keep it trivial (e.g. just copying the dict somewhere)."""
    exp_id = f"exp-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    record: dict = {
        "schema_version": SCHEMA_VERSION, "guard_version": GUARD_VERSION,
        "id": exp_id, "state": STATE_PRECHECK,
        "fault": fault.name, "target": target,
        "container": {"name": container.name, "id": container.id},
        "params": fault.params, "dry_run": fault.dry_run,
        "thresholds": asdict(th),
        "baseline": None, "baseline_source": None,
        "started_at_utc": utc_now_iso(), "ended_at_utc": None,
        "started": None, "simulated": False,
        "injected": False, "injection_error": None,
        "aborted": False, "abort_reason": None, "detected_s": None,
        "reverted": False, "reverted_at": None,
        "rollback_verified": False, "rollback_error": None,
        "recovery_s": None, "recovery_outcome": None,
        "error": None, "timeline": [], "during": None,
        "python_version": sys.version.split()[0],
        "events": [], "event_seq": 0,
    }
    import activity  # local import: activity.py imports this module
    sink_token = _command_sink.set(activity.command_sink(record))
    mode = "DRY RUN: no command will be executed" if fault.dry_run else "REAL fault injection"
    activity.emit(record, "info", f"Experiment {exp_id} started ({mode}): '{fault.name}' on '{target}'.",
                  dry_run=bool(fault.dry_run), fault=fault.name, target=target, params=fault.params)
    activity.emit(record, "info",
                  (f"Target '{target}' is on the allowlist; using synthetic container '{container.name}' "
                   f"because this is a dry run (Docker is not contacted)." if fault.dry_run else
                   f"Target '{target}' is on the allowlist and resolved to container '{container.name}' "
                   f"(id {container.id[:12]}), which is running."),
                  container=container.name, container_id=container.id, synthetic=bool(fault.dry_run))

    ids = config.all_car_ids()
    try:
        async with httpx.AsyncClient(base_url=config.DITTO_URL,
                                      auth=(config.DITTO_USER, config.DITTO_PASS),
                                      timeout=config.REQUEST_TIMEOUT_S) as client:

            async def tick(phase: str) -> dict:
                t0 = time.monotonic()
                try:
                    row = await asyncio.wait_for(probe.probe_once(client, ids), timeout=PROBE_HARD_TIMEOUT_S)
                except asyncio.TimeoutError:
                    row = timeout_row(phase)
                row["phase"] = phase
                record["timeline"].append(row)
                activity.health(record, row)
                print(f"[guard] {phase:9s} direct={'OK ' if row['direct_ok'] else 'ERR'} "
                      f"{row['direct_ms']:7.1f}ms search={'OK ' if row['search_ok'] else 'ERR'} "
                      f"stale95={row['stale_p95_s']} lost={row['lost_acked']}", flush=True)
                if on_update is not None:
                    on_update(record)
                await asyncio.sleep(max(0.0, config.PROBE_INTERVAL_S - (time.monotonic() - t0)))
                return row

            # ---- BASELINE ----
            record["state"] = STATE_BASELINE
            pre_ticks = max(1, round(pre_s / config.PROBE_INTERVAL_S))
            if pre_ticks < th.breach_window:
                print(f"[guard] WARNING: --pre {pre_s}s gives only {pre_ticks} tick(s), fewer than "
                      f"breach_window={th.breach_window}; the pre-check/baseline will be statistically "
                      f"weak.", flush=True)
            pre = [await tick("before") for _ in range(pre_ticks)]
            if not all(r["direct_ok"] and r["search_ok"] for r in pre[-min(2, len(pre)):]):
                activity.emit(record, "decision", "Refusing to inject: Ditto was not healthy on the pre-check "
                                                  "ticks, so a fault would be indistinguishable from the existing problem.")
                raise GuardError("Ditto is not healthy BEFORE the experiment (direct and/or search "
                                  "failing); refusing to inject. Check `docker ps` and Ditto logs first.")
            activity.emit(record, "info", f"Pre-check passed: Ditto answered direct reads and searches on the "
                                          f"last {min(2, len(pre))} of {len(pre)} tick(s).")

            file_baseline = None if fresh_baseline else load_baseline()
            baseline, source = (file_baseline, "file") if file_baseline is not None \
                else (probe.summarize(pre), "fresh")
            validate_baseline(baseline, source, max_baseline_age_s)
            record["baseline"], record["baseline_source"] = baseline, source
            activity.thresholds(record, baseline, th, source)

            # ---- INJECTING / MONITORING / REVERTING ----
            if stop_event is not None and stop_event.is_set():
                # Stop requested before anything was touched -- nothing to revert.
                record["state"] = STATE_ABORTED
                activity.emit(record, "decision", "Manual stop requested before injection: nothing was "
                                                  "touched, so there is nothing to revert.")
                record["aborted"], record["abort_reason"] = True, "manual stop (requested before injection)"
                record["recovery_outcome"] = "skipped_manual_abort"
                manual_abort = True
            else:
                record["state"] = STATE_INJECTING
                activity.emit(record, "info", f"Running the '{fault.name}' fault's read-only preflight checks.")
                await fault.preflight()
                activity.emit(record, "info", f"Preflight passed. Injecting '{fault.name}' into '{target}'"
                                              + (" (dry run: commands are only shown)." if fault.dry_run else "."))
                record["started"] = time.time()
                started_mono = time.monotonic()
                manual_abort = False

                record["injected"] = True  # register rollback responsibility BEFORE injecting
                try:
                    try:
                        await fault.inject()
                    except Exception as e:
                        record["injection_error"] = str(e)
                        activity.emit(record, "error", f"Injection failed: {e}. The fault may be partly "
                                                       f"applied, so a revert will be attempted.")
                        raise
                    print(f"[guard] >>> fault '{fault.name}' injected into {target}", flush=True)

                    record["state"] = STATE_MONITORING
                    activity.emit(record, "info",
                                  (f"Dry run: the fault was NOT applied. Monitoring Ditto for up to {max_duration:g}s "
                                   f"exactly as a real run would." if fault.dry_run else
                                   f"Fault applied. Monitoring Ditto every {config.PROBE_INTERVAL_S:g}s for up to "
                                   f"{max_duration:g}s; any stop condition reverts immediately."))
                    window: list[dict] = []
                    while time.monotonic() - started_mono < max_duration:
                        window.append(await tick("fault"))
                        if stop_event is not None and stop_event.is_set():
                            record["aborted"], record["abort_reason"] = True, "manual stop (requested during monitoring)"
                            record["detected_s"] = round(time.monotonic() - started_mono, 2)
                            activity.emit(record, "decision", "Manual stop requested from the dashboard: "
                                                              "reverting the fault now.", reason="manual_stop")
                            print("[guard] !!! MANUAL STOP requested -> reverting", flush=True)
                            break
                        breached, reason = check_breach(window, baseline, th)
                        if breached:
                            record["aborted"], record["abort_reason"] = True, reason
                            record["detected_s"] = round(time.monotonic() - started_mono, 2)
                            activity.emit(record, "decision", f"Kill switch triggered: {reason}. Reverting the "
                                                              f"fault now.", reason=reason, kill_switch=True)
                            print(f"[guard] !!! KILL SWITCH: {reason} -> reverting", flush=True)
                            break
                        activity.emit(record, "decision", activity.within_limits(window[-1], baseline, th))
                    else:
                        activity.emit(record, "decision", f"Maximum duration of {max_duration:g}s reached without "
                                                          f"a stop condition: reverting the fault.",
                                      reason="max_duration")
                except (KeyboardInterrupt, asyncio.CancelledError) as e:
                    # On Python 3.11+ asyncio.run() delivers Ctrl+C as a task cancellation,
                    # not a KeyboardInterrupt, and an API shutdown cancels the task the same
                    # way. Both must revert and still hand back a record to save.
                    manual_abort = True
                    record["aborted"], record["abort_reason"] = True, _interrupt_reason(e)
                    activity.emit(record, "decision", f"Interrupted ({_interrupt_reason(e)}): reverting the fault.")
                except Exception as e:
                    record["error"] = record["error"] or f"{type(e).__name__}: {e}"
                    if not record["injection_error"]:
                        activity.emit(record, "error", f"Unexpected error while the fault was active: "
                                                       f"{type(e).__name__}: {e}. Reverting.")
                finally:
                    # ALWAYS attempt + verify revert because record["injected"] is True.
                    record["state"] = STATE_REVERTING
                    activity.emit(record, "info",
                                  (f"Dry run: showing the revert commands for '{fault.name}' on '{target}'."
                                   if fault.dry_run else
                                   f"Reverting '{fault.name}' on '{target}' and verifying the result."))
                    try:
                        await fault.revert()
                        record["reverted"] = True
                        record["rollback_verified"] = True
                        record["reverted_at"] = time.time()
                        activity.emit(record, "info",
                                      ("Dry run: nothing was applied, so there was nothing to revert."
                                       if fault.dry_run else
                                       f"Revert verified: '{fault.name}' is confirmed removed from '{target}'."),
                                      verified=not fault.dry_run)
                        print("[guard] <<< fault reverted (verified)", flush=True)
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        print("[guard] !!! interrupted again during rollback -- the fault may still be "
                              f"ACTIVE on '{target}'. See README 'Manual recovery' for the exact commands.",
                              flush=True)
                        record["rollback_error"] = "interrupted during rollback (second Ctrl+C)"
                        record["error"] = _join(record["error"], record["rollback_error"])
                        activity.emit(record, "error", f"Revert was interrupted: the fault may still be ACTIVE "
                                                       f"on '{target}'. Recover it manually.", verified=False)
                    except Exception as e:
                        record["rollback_error"] = str(e)
                        record["error"] = _join(record["error"], f"REVERT FAILED: {e}")
                        activity.emit(record, "error", f"Revert could NOT be verified: {e}. The fault may still "
                                                       f"be active on '{target}'; recovery is not confirmed.",
                                      verified=False)
                        print(f"[guard] REVERT FAILED, fix manually: {e}", flush=True)

            # ---- Decide what happens after revert ----
            if manual_abort:
                record["state"] = STATE_ABORTED
                record["recovery_outcome"] = record["recovery_outcome"] or "skipped_manual_abort"
            elif record["rollback_error"] or record["injection_error"]:
                record["state"] = STATE_FAILED
            else:
                record["state"] = STATE_VERIFYING_RECOVERY
                healthy_streak, rec_start_mono = 0, time.monotonic()
                recovered = False
                activity.emit(record, "info", f"Verifying recovery: {RECOVERY_STREAK} consecutive healthy ticks "
                                              f"are required within {recovery_timeout:g}s.")
                while time.monotonic() - rec_start_mono < recovery_timeout:
                    row = await tick("recovery")
                    ok_now = is_healthy(row, baseline, th)
                    healthy_streak = healthy_streak + 1 if ok_now else 0
                    activity.emit(record, "decision",
                                  f"{healthy_streak}/{RECOVERY_STREAK} healthy ticks."
                                  if ok_now else
                                  f"Not healthy yet (requests failing, latency above the recovery limit, or data "
                                  f"stale): streak reset to 0/{RECOVERY_STREAK}.",
                                  healthy_streak=healthy_streak, required=RECOVERY_STREAK)
                    if healthy_streak >= RECOVERY_STREAK:
                        record["recovery_s"] = round(
                            time.monotonic() - rec_start_mono - (RECOVERY_STREAK - 1) * config.PROBE_INTERVAL_S, 2)
                        record["recovery_outcome"] = "recovered"
                        recovered = True
                        activity.emit(record, "info", f"Recovery verified: Ditto was healthy again "
                                                      f"{record['recovery_s']}s after the revert.",
                                      recovery_s=record["recovery_s"])
                        break
                if not recovered:
                    record["recovery_s"] = None
                    record["recovery_outcome"] = "timeout"
                    record["error"] = _join(record["error"], f"not recovered within {recovery_timeout}s")
                    activity.emit(record, "error", f"Recovery NOT verified: Ditto did not return to a healthy "
                                                   f"state within {recovery_timeout:g}s of the revert.")
                record["state"] = STATE_COMPLETED if recovered else STATE_RECOVERY_TIMEOUT

            fault_rows = [r for r in record["timeline"] if r["phase"] == "fault"]
            record["during"] = probe.summarize(fault_rows) if fault_rows else None

    except (KeyboardInterrupt, asyncio.CancelledError) as e:
        record["aborted"] = True
        record["abort_reason"] = record["abort_reason"] or _interrupt_reason(e)
        record["state"] = STATE_ABORTED if record["state"] in (STATE_PRECHECK, STATE_BASELINE) else record["state"]
    except GuardError as e:
        record["error"] = str(e)
        record["state"] = STATE_ABORTED if record["state"] in (STATE_PRECHECK, STATE_BASELINE) else STATE_FAILED
        activity.emit(record, "error", f"Stopped: {e}")
        print(f"[guard] ERROR: {e}", flush=True)
    except Exception as e:
        record["error"] = _join(record["error"], f"{type(e).__name__}: {e}")
        record["state"] = STATE_FAILED
        activity.emit(record, "error", f"Unexpected error: {type(e).__name__}: {e}")
        print(f"[guard] UNEXPECTED ERROR: {e}", flush=True)
    finally:
        _command_sink.reset(sink_token)

    if fault.dry_run:
        # Nothing was touched: `injected` above only registered rollback responsibility
        # for the (no-op) dry-run fault object. Saved as-is, the record read "injected,
        # reverted, rollback verified" and was counted as a real run by the planner.
        record["simulated"] = True
        record["injected"] = record["reverted"] = record["rollback_verified"] = False
        record["reverted_at"] = None

    outcome = {STATE_COMPLETED: "completed and recovery was verified", STATE_ABORTED: "was aborted",
               STATE_FAILED: "FAILED", STATE_RECOVERY_TIMEOUT: "ended without verified recovery"}.get(
        record["state"], f"ended in state {record['state']}")
    activity.emit(record, "error" if record["state"] in (STATE_FAILED, STATE_RECOVERY_TIMEOUT) else "info",
                  f"Experiment {outcome}."
                  + (" This was a dry run: no command was executed and nothing was changed." if fault.dry_run else ""),
                  final=True, state=record["state"])
    record["ended_at_utc"] = utc_now_iso()
    if on_update is not None:
        on_update(record)
    return record


def _interrupt_reason(exc: BaseException) -> str:
    return "manual stop (Ctrl+C)" if isinstance(exc, KeyboardInterrupt)         else "cancelled (Ctrl+C or shutdown while the experiment task was running)"


def _join(a: str | None, b: str) -> str:
    return f"{a}; {b}" if a else b


# ---------------------------------------------------------------- CLI
def build_fault(container: ContainerRef, dry_run: bool, args) -> KillFault | DelayFault:
    if args.fault == "kill":
        return KillFault(container, dry_run)
    return DelayFault(container, dry_run, args.ms, args.ttl)


def confirm_or_raise(args) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise GuardError("refusing to run a real fault injection in a non-interactive session "
                          "without --yes. Pass --yes to confirm.")
    print(f"[guard] About to inject a REAL '{args.fault}' fault into service '{args.target}' "
          f"(compose project '{COMPOSE_PROJECT}'). This can disrupt anything using that service "
          f"while the experiment runs.", flush=True)
    if input("[guard] Type 'yes' to continue: ").strip().lower() != "yes":
        raise GuardError("aborted by user (confirmation not given)")


async def run_preflight(args) -> int:
    print("[guard] PREFLIGHT: checking Docker daemon...", flush=True)
    try:
        await docker_preflight()
        container = await resolve_container(args.target, dry_run=False)
        print(f"[guard] PREFLIGHT: resolved '{args.target}' -> {container.name} "
              f"({container.id[:12]}), running.", flush=True)
        fault = build_fault(container, False, args)
        await fault.preflight()
    except GuardError as e:
        print(f"[guard] PREFLIGHT FAILED: {e}")
        return 2
    print("[guard] PREFLIGHT OK: ready to run a real experiment.")
    return 0


async def run_dry_run(args) -> int:
    container = await resolve_container(args.target, dry_run=True)
    fault = build_fault(container, True, args)
    print(f"[guard] DRY RUN for fault={args.fault} target={args.target} "
          f"(container name is synthetic; no Docker/tc commands are executed)", flush=True)
    await fault.inject()
    await fault.revert()
    print("[guard] dry run only: nothing was changed")
    return 0


async def execute(args, th: Thresholds) -> dict:
    await docker_preflight()
    container = await resolve_container(args.target, dry_run=False)
    fault = build_fault(container, False, args)
    return await run_experiment(fault, args.target, container, th, args.max_duration,
                                 args.pre, args.recovery_timeout, args.fresh_baseline,
                                 args.max_baseline_age)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Inject one safe, auto-reverting fault into Ditto with a kill switch.",
        epilog=(
            "Examples:\n"
            "  python guard.py --fault kill  --target things-search --max-duration 60\n"
            "  python guard.py --fault delay --target mongodb --ms 2000 --ttl 60\n"
            "  python guard.py --fault delay --target mongodb --ms 2000 --dry-run\n"
            "  python guard.py --fault kill  --target things --preflight-only\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fault", choices=["kill", "delay"], required=True)
    ap.add_argument("--target", required=True, help=f"one of {sorted(ALLOWLIST)}")
    ap.add_argument("--ms", type=int, default=1000, help="delay in ms (delay fault)")
    ap.add_argument("--ttl", type=int, default=60, help="dead-man's-switch self-revert seconds (delay fault)")
    ap.add_argument("--max-duration", type=float, default=90, help="hard stop in seconds")
    ap.add_argument("--pre", type=float, default=10, help="seconds of health check before injecting")
    ap.add_argument("--recovery-timeout", type=float, default=180)
    ap.add_argument("--fresh-baseline", action="store_true",
                     help="ignore baseline.json and measure a short baseline from --pre instead")
    ap.add_argument("--max-baseline-age", type=float, default=MAX_BASELINE_AGE_S_DEFAULT,
                     help="reject baseline.json if older than this many seconds")
    ap.add_argument("--dry-run", action="store_true", help="print docker/tc commands, run nothing real")
    ap.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    ap.add_argument("--force-unlock", action="store_true",
                     help="remove an existing lock file for --target before starting")
    ap.add_argument("--preflight-only", action="store_true",
                     help="check Docker/container/image readiness and exit; inject nothing")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()

    th = Thresholds.from_env()
    try:
        th.validate()
    except GuardError as e:
        print(f"[guard] CONFIG ERROR: {e}")
        return 2

    if args.target not in ALLOWLIST:
        print(f"[guard] ERROR: '{args.target}' is not in the allowlist {sorted(ALLOWLIST)}")
        return 2

    if args.preflight_only:
        return asyncio.run(run_preflight(args))

    if args.dry_run:
        try:
            return asyncio.run(run_dry_run(args))
        except GuardError as e:
            print(f"[guard] ERROR: {e}")
            return 2

    try:
        confirm_or_raise(args)
    except GuardError as e:
        print(f"[guard] ERROR: {e}")
        return 2

    try:
        lock = acquire_lock(args.target, force=args.force_unlock)
    except GuardError as e:
        print(f"[guard] ERROR: {e}")
        return 2

    try:
        record = asyncio.run(execute(args, th))
    except KeyboardInterrupt:
        print("\n[guard] stopped before any fault was injected (nothing to revert)")
        return 130
    except GuardError as e:
        print(f"[guard] ERROR: {e}")
        return 2
    finally:
        release_lock(lock)

    path = save(record)
    print(f"[guard] state={record['state']} aborted={record['aborted']} reason={record['abort_reason']} "
          f"detected_s={record.get('detected_s')} recovery_s={record.get('recovery_s')}")
    print(f"[guard] saved {path}")

    if record["state"] == STATE_COMPLETED:
        return 0
    if record["state"] == STATE_ABORTED and record["reverted"] and not record["rollback_error"]:
        return 0  # kill-switch or manual abort, cleanly reverted: a successful safety demonstration
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
