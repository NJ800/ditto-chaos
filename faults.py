"""Fault registry: every fault the scenario engine (scenario.py) can inject.

Each fault is one small class with the same interface -- validated/bounded params,
read-only preflight, apply, actual-state inspection, idempotent rollback that VERIFIES
the result, and the recovery info needed to clean up after a process restart. Nothing
here decides whether a fault is ALLOWED to run; that is scenario.validate() and the
controller's policy. Nothing here takes free-form commands either: the only Docker
invocations that exist are the fixed argument lists written out below.

guard.py's CLI keeps its own two Day-1 classes (KillFault/DelayFault) so that file's
behaviour and tests are untouched. This registry reuses guard.py's helpers
(run_cmd, ContainerRef, error types) rather than duplicating them.

Verification status is recorded per fault in `FaultSpec.live_verified` and reported by
the API as-is. A fault that has only been exercised against mocked Docker says so.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

import guard

DEFAULT_QDISCS = {"noqueue", "fq_codel", "pfifo_fast", "pfifo", "mq", "fq"}
START_WAIT_S = 20.0


@dataclass(frozen=True)
class ParamSpec:
    name: str
    minimum: float
    maximum: float
    default: float
    unit: str
    description: str


@dataclass(frozen=True)
class FaultSpec:
    name: str
    title: str
    description: str
    category: str               # "process" | "network" | "resource"
    conflict_group: str         # two faults of the same group cannot share a target
    params: tuple[ParamSpec, ...] = ()
    expected_symptoms: tuple[str, ...] = ()
    self_expiring: bool = False  # has a dead-man's switch independent of this process
    supported: bool = True
    unsupported_reason: str | None = None
    # True = apply/inspect/rollback/verify pass against a REAL Docker daemon in
    # tests/integration/test_real_docker.py (a throwaway container, never the Ditto services).
    live_verified: bool = True
    verification_note: str = ("Mechanism verified against a real Docker daemon on a throwaway container "
                              "(tests/integration, Docker Desktop/WSL2, 2026-10-10). Not yet run against "
                              "the Ditto services themselves in this version; re-run that suite on your machine.")

    def to_dict(self) -> dict:
        return {
            "name": self.name, "title": self.title, "description": self.description,
            "category": self.category, "conflict_group": self.conflict_group,
            "params": [vars(p) for p in self.params],
            "expected_symptoms": list(self.expected_symptoms),
            "self_expiring": self.self_expiring, "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
            "live_verified": self.live_verified, "verification_note": self.verification_note,
        }


_TTL = ParamSpec("ttl_s", 5, 600, 60, "s",
                 "Dead-man's switch: the helper container removes the rule itself after this long.")


def validate_params(spec: FaultSpec, params: dict | None) -> dict:
    """Strict: unknown keys, non-numbers and out-of-range values are rejected, never
    clamped -- a caller that wants clamping (the AI planner) does it explicitly first."""
    params = dict(params or {})
    known = {p.name: p for p in spec.params}
    unknown = sorted(set(params) - set(known))
    if unknown:
        raise guard.GuardError(f"fault '{spec.name}' does not take parameter(s) {unknown}; "
                               f"it accepts {sorted(known) or 'no parameters'}")
    out = {}
    for name, p in known.items():
        value = params.get(name, p.default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise guard.GuardError(f"{spec.name}.{name} must be a number, got {value!r}")
        if not (p.minimum <= value <= p.maximum):
            raise guard.GuardError(f"{spec.name}.{name}={value:g} is outside the allowed range "
                                   f"{p.minimum:g}..{p.maximum:g} {p.unit}")
        out[name] = int(value) if float(value).is_integer() else float(value)
    return out


# ---------------------------------------------------------------- container state helpers
async def inspect_container(container_id: str) -> dict:
    """{'exists', 'running', 'paused'}. Raises GuardError when Docker could not answer --
    "I could not find out" is never reported as "not running" or "gone"."""
    try:
        out = await guard.run_cmd(["docker", "inspect", "-f",
                                   "{{.State.Running}}\t{{.State.Paused}}", container_id])
    except guard.GuardError as e:
        if "no such" in str(e).lower():
            return {"exists": False, "running": False, "paused": False}
        raise
    parts = out.strip().split("\t")
    if len(parts) != 2:
        raise guard.GuardError(f"unexpected `docker inspect` output for {container_id[:12]}: {out!r}")
    return {"exists": True, "running": parts[0] == "true", "paused": parts[1] == "true"}


async def service_container(service: str) -> dict | None:
    """The compose project's current container for a service, whatever its state."""
    out = await guard.run_cmd(["docker", "ps", "-a", "--no-trunc",
                               "--filter", f"label=com.docker.compose.project={guard.COMPOSE_PROJECT}",
                               "--filter", f"label=com.docker.compose.service={service}",
                               "--format", "{{.Names}}\t{{.ID}}\t{{.State}}"])
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 3:
        return None
    return {"name": rows[0][0], "id": rows[0][1], "state": rows[0][2].strip().lower()}


# ---------------------------------------------------------------- base class
class Fault:
    spec: FaultSpec

    def __init__(self, service: str, container: guard.ContainerRef, params: dict | None = None,
                 state: dict | None = None):
        self.service = service
        self.container = container
        self.params = validate_params(self.spec, params)
        self.name = self.spec.name
        self.dry_run = False          # guard.run_experiment() reads this attribute
        self.state: dict = dict(state or {})

    # ---- description (pure, used by previews) ----
    def intensity(self) -> float:
        """0..1 contribution to a scenario's cumulative-intensity budget."""
        return 1.0

    def planned_commands(self) -> list[str]:
        return []

    def manual_recovery(self) -> list[str]:
        return [f"docker start {self.container.name}"]

    def recovery_info(self) -> dict:
        """Everything needed to rebuild this object and roll it back from a journal file."""
        return {"fault": self.name, "service": self.service,
                "container": {"name": self.container.name, "id": self.container.id},
                "params": self.params, "state": self.state}

    # ---- lifecycle ----
    async def preflight(self) -> None:
        return

    async def apply(self) -> None:
        raise NotImplementedError

    async def inspect(self) -> dict:
        """{'active': True|False|None, 'detail': str}. None = could not determine."""
        raise NotImplementedError

    async def rollback(self) -> None:
        """Idempotent. Returns only once the clean state has been OBSERVED; raises
        RollbackError otherwise."""
        raise NotImplementedError

    async def safe_inspect(self) -> dict:
        try:
            return await self.inspect()
        except guard.GuardError as e:
            return {"active": None, "detail": f"could not inspect: {e}"}

    async def verify_clean(self) -> tuple[bool, str]:
        """(verified, detail) from the OBSERVED state after rollback(). Unknown is not clean."""
        if self.state.get("container_replaced_by"):
            return True, (f"original container is gone; the service is running under replacement "
                          f"{self.state['container_replaced_by'][:12]}")
        if self.state.get("cleared_because"):
            return True, self.state["cleared_because"]
        obs = await self.safe_inspect()
        return obs["active"] is False, obs["detail"]

    # guard.run_experiment() compatibility
    async def inject(self) -> None:
        await self.apply()

    async def revert(self) -> None:
        await self.rollback()


# ---------------------------------------------------------------- process-state faults
class _ProcessFault(Fault):
    async def preflight(self) -> None:
        st = await inspect_container(self.container.id)
        if not st["exists"] or not st["running"] or st["paused"]:
            raise guard.GuardError(f"'{self.container.name}' is not in a clean running state "
                                   f"({st}); refusing to inject '{self.name}'.")

    async def inspect(self) -> dict:
        st = await inspect_container(self.container.id)
        if not st["exists"]:
            return {"active": None, "detail": "original container no longer exists"}
        down = (not st["running"]) or st["paused"]
        return {"active": down, "detail": f"running={st['running']} paused={st['paused']}"}

    async def rollback(self) -> None:
        cid, name = self.container.id, self.container.name
        try:
            st = await inspect_container(cid)
        except guard.GuardError as e:
            raise guard.RollbackError(f"could not read the state of '{name}': {e}") from e

        if not st["exists"]:
            # The container we acted on is gone. Never start something else by name; only
            # OBSERVE whether the service is already back under a replacement container.
            try:
                current = await service_container(self.service)
            except guard.GuardError as e:
                raise guard.RollbackError(f"'{name}' no longer exists and the service's current "
                                          f"container could not be queried: {e}") from e
            if current and current["state"] == "running":
                self.state["container_replaced_by"] = current["id"]
                return
            raise guard.RollbackError(
                f"the original container {cid[:12]} for '{self.service}' no longer exists and no "
                f"running replacement was found. Bring the service back with docker compose.")

        if st["paused"]:
            await self._cmd(["docker", "unpause", cid], "unpause")
        elif not st["running"]:
            await self._cmd(["docker", "start", cid], "start")

        deadline = time.monotonic() + START_WAIT_S
        last = "unknown"
        while True:
            try:
                st = await inspect_container(cid)
                last = f"running={st['running']} paused={st['paused']}"
                if st["exists"] and st["running"] and not st["paused"]:
                    return
            except guard.GuardError as e:
                last = f"inspect failed: {e}"
            if time.monotonic() >= deadline:
                raise guard.RollbackError(f"'{name}' did not reach a running state within "
                                          f"{START_WAIT_S:.0f}s ({last}); check `docker logs {name}`.")
            await asyncio.sleep(0.5)

    async def _cmd(self, args: list[str], what: str) -> None:
        try:
            await guard.run_cmd(args)
        except guard.GuardError as e:
            # Not fatal on its own: the loop above decides from the OBSERVED state, because
            # a command that timed out may still have taken effect.
            print(f"[faults] {what} on '{self.container.name}' reported an error: {e}", flush=True)


class KillFault(_ProcessFault):
    spec = FaultSpec(
        name="kill", title="Force kill",
        description="SIGKILL the container (docker kill), then start the same container again.",
        category="process", conflict_group="process",
        expected_symptoms=("requests routed to the service fail until it is back",
                           "dependants may time out or serve stale data"))

    def planned_commands(self) -> list[str]:
        return [f"docker kill {self.container.name}", f"(rollback) docker start {self.container.name}"]

    async def apply(self) -> None:
        await guard.run_cmd(["docker", "kill", self.container.id])


class StopFault(_ProcessFault):
    spec = FaultSpec(
        name="stop", title="Graceful shutdown",
        description="SIGTERM with a grace period (docker stop), then start the same container again.",
        category="process", conflict_group="process",
        params=(ParamSpec("grace_s", 1, 30, 10, "s", "Seconds Docker waits before escalating to SIGKILL."),),
        expected_symptoms=("in-flight work may be flushed before exit, unlike a kill",
                           "requests fail until the service is back"))

    def planned_commands(self) -> list[str]:
        return [f"docker stop -t {self.params['grace_s']} {self.container.name}",
                f"(rollback) docker start {self.container.name}"]

    async def apply(self) -> None:
        grace = self.params["grace_s"]
        await guard.run_cmd(["docker", "stop", "-t", str(grace), self.container.id],
                            timeout=guard.DOCKER_CMD_TIMEOUT_S + grace)


class RestartFault(_ProcessFault):
    spec = FaultSpec(
        name="restart", title="Restart",
        description="docker restart: the service goes down and comes straight back.",
        category="process", conflict_group="process", self_expiring=True,
        params=(ParamSpec("grace_s", 1, 30, 10, "s", "Seconds Docker waits before escalating to SIGKILL."),),
        expected_symptoms=("a short burst of failed requests", "measurable cold-start recovery time"))

    def intensity(self) -> float:
        return 0.6

    def planned_commands(self) -> list[str]:
        return [f"docker restart -t {self.params['grace_s']} {self.container.name}",
                "(rollback) verify running, start if not"]

    async def apply(self) -> None:
        grace = self.params["grace_s"]
        await guard.run_cmd(["docker", "restart", "-t", str(grace), self.container.id],
                            timeout=guard.DOCKER_CMD_TIMEOUT_S + grace)


class PauseFault(_ProcessFault):
    spec = FaultSpec(
        name="pause", title="Freeze (dependency unavailable)",
        description="docker pause: the process is frozen, so it holds its connections but answers "
                    "nothing -- a hung dependency rather than a dead one. Reversed with docker unpause.",
        category="process", conflict_group="process",
        expected_symptoms=("callers hang until their own timeouts fire, rather than failing fast",
                           "queues and connection pools upstream may fill"))

    def planned_commands(self) -> list[str]:
        return [f"docker pause {self.container.name}", f"(rollback) docker unpause {self.container.name}"]

    def manual_recovery(self) -> list[str]:
        return [f"docker unpause {self.container.name}"]

    async def apply(self) -> None:
        await guard.run_cmd(["docker", "pause", self.container.id])


# ---------------------------------------------------------------- network (tc netem) faults
class _NetemFault(Fault):
    """One root netem qdisc on the target's interface, installed from a helper container
    that shares the target's network namespace and removes the rule itself after ttl_s."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Chosen up front so the recovery journal knows the helper's name BEFORE it is started.
        self.state.setdefault("helper", f"chaos-{self.name}-{uuid.uuid4().hex[:8]}")

    def netem(self) -> str:
        raise NotImplementedError

    def _args(self, script: str, detach: bool, name: str | None = None) -> list[str]:
        args = ["docker", "run", "--rm", "--cap-add", "NET_ADMIN", "--net", f"container:{self.container.id}"]
        if detach:
            args.append("-d")
        if name:
            args += ["--name", name]
        return args + [guard.NETSHOOT_IMAGE, "sh", "-c", script]

    def _del_script(self) -> str:
        return f"tc qdisc del dev {guard.NETEM_IFACE} root 2>/dev/null; true"

    def planned_commands(self) -> list[str]:
        return [f"docker run --rm -d --cap-add NET_ADMIN --net container:{self.container.name} "
                f"{guard.NETSHOOT_IMAGE} tc qdisc replace dev {guard.NETEM_IFACE} root netem {self.netem()} "
                f"(self-removes after {self.params['ttl_s']}s)",
                f"(rollback) tc qdisc del dev {guard.NETEM_IFACE} root, then verify no netem remains"]

    def manual_recovery(self) -> list[str]:
        return [f"docker run --rm --cap-add NET_ADMIN --net container:{self.container.name} "
                f"{guard.NETSHOOT_IMAGE} tc qdisc del dev {guard.NETEM_IFACE} root"]

    async def _qdisc_show(self) -> str:
        return await guard.run_cmd(self._args(f"tc qdisc show dev {guard.NETEM_IFACE}", detach=False))

    async def preflight(self) -> None:
        st = await inspect_container(self.container.id)
        if not st["exists"] or not st["running"] or st["paused"]:
            raise guard.GuardError(f"'{self.container.name}' is not running ({st}); a network fault "
                                   f"needs a live network namespace.")
        try:
            await guard.run_cmd(["docker", "image", "inspect", guard.NETSHOOT_IMAGE])
        except guard.GuardError as e:
            raise guard.GuardError(f"helper image '{guard.NETSHOOT_IMAGE}' is not available locally. "
                                   f"Run: docker pull {guard.NETSHOOT_IMAGE}  ({e})") from e
        try:
            existing = await self._qdisc_show()
        except guard.GuardError as e:
            raise guard.GuardError(f"could not read the current tc state of '{self.container.name}': {e}") from e
        kinds = [line.split()[1] for line in existing.splitlines()
                 if line.startswith("qdisc ") and len(line.split()) > 1]
        foreign = sorted(set(kinds) - DEFAULT_QDISCS)
        if foreign:
            raise guard.GuardError(
                f"'{self.container.name}' already has non-default qdisc(s) {foreign} on "
                f"{guard.NETEM_IFACE}:\n  {existing.strip()}\nRefusing to replace traffic-control "
                f"rules this tool did not create. Clear them first:\n  {self.manual_recovery()[0]}")
        self.state["original_qdisc"] = existing.strip()

    async def apply(self) -> None:
        helper = self.state["helper"]
        iface, ttl = guard.NETEM_IFACE, self.params["ttl_s"]
        script = (f"tc qdisc replace dev {iface} root netem {self.netem()} && "
                  f"sleep {ttl}; {self._del_script()}")
        await guard.run_cmd(self._args(script, detach=True, name=helper))
        await asyncio.sleep(0.5)
        check = await self._qdisc_show()
        if "netem" not in check:
            raise guard.GuardError(f"{self.name} fault did not apply: tc reports "
                                   f"{check.strip() or '(empty)'}")

    async def inspect(self) -> dict:
        out = await self._qdisc_show()
        return {"active": "netem" in out, "detail": out.strip()}

    async def rollback(self) -> None:
        helper = self.state.get("helper")
        if helper:
            await guard.run_cmd(["docker", "rm", "-f", helper], check=False)
        try:
            st = await inspect_container(self.container.id)
        except guard.GuardError as e:
            raise guard.RollbackError(f"could not read the state of '{self.container.name}': {e}") from e
        if not st["exists"] or not st["running"]:
            # No live network namespace -> no qdisc can exist in it. Observed, not assumed.
            self.state["cleared_because"] = "target container is not running; its netns is gone"
            return
        detail = "(no check succeeded)"
        for attempt in range(3):
            await guard.run_cmd(self._args(self._del_script(), detach=False), check=False)
            try:
                check = await self._qdisc_show()
            except guard.GuardError as e:
                detail = f"could not verify: {e}"
            else:
                if "netem" not in check:
                    return
                detail = f"tc still reports: {check.strip()}"
            if attempt < 2:
                await asyncio.sleep(1.0)
        raise guard.RollbackError(
            f"could not confirm the netem rule was removed from '{self.container.name}' after 3 "
            f"attempts ({detail}). Run manually:\n  {self.manual_recovery()[0]}")


class DelayFault(_NetemFault):
    spec = FaultSpec(
        name="delay", title="Network latency",
        description="Fixed added delay on every packet leaving the target (tc netem delay).",
        category="network", conflict_group="netem", self_expiring=True,
        params=(ParamSpec("delay_ms", 1, 5000, 500, "ms", "Added one-way delay."), _TTL),
        expected_symptoms=("latency of calls through the target rises by roughly the delay",
                           "callers with short timeouts start erroring"))

    def intensity(self) -> float:
        return min(1.0, self.params["delay_ms"] / 5000)

    def netem(self) -> str:
        return f"delay {self.params['delay_ms']}ms"


class JitterFault(_NetemFault):
    spec = FaultSpec(
        name="jitter", title="Network jitter",
        description="Delay that varies per packet: delay_ms +/- jitter_ms (tc netem delay D J).",
        category="network", conflict_group="netem", self_expiring=True,
        params=(ParamSpec("delay_ms", 1, 5000, 200, "ms", "Mean added delay."),
                ParamSpec("jitter_ms", 1, 2000, 100, "ms", "Variation around the mean; must not exceed delay_ms."),
                _TTL),
        expected_symptoms=("latency percentiles spread apart (p95/p99 rise more than p50)",
                           "possible packet reordering"))

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.params["jitter_ms"] > self.params["delay_ms"]:
            raise guard.GuardError("jitter.jitter_ms must not exceed jitter.delay_ms")

    def intensity(self) -> float:
        return min(1.0, (self.params["delay_ms"] + self.params["jitter_ms"]) / 5000)

    def netem(self) -> str:
        return f"delay {self.params['delay_ms']}ms {self.params['jitter_ms']}ms"


class LossFault(_NetemFault):
    spec = FaultSpec(
        name="loss", title="Packet loss",
        description="Randomly drop a percentage of packets leaving the target (tc netem loss). "
                    "100% is a full network blackhole of that one container.",
        category="network", conflict_group="netem", self_expiring=True,
        params=(ParamSpec("loss_pct", 1, 100, 10, "%", "Share of packets dropped."), _TTL),
        expected_symptoms=("TCP retransmits show up as latency spikes", "timeouts at higher loss rates"))

    def intensity(self) -> float:
        return min(1.0, self.params["loss_pct"] / 50)

    def netem(self) -> str:
        return f"loss {self.params['loss_pct']}%"


class BandwidthFault(_NetemFault):
    spec = FaultSpec(
        name="bandwidth", title="Bandwidth limit",
        description="Cap the target's egress rate (tc netem rate) to exercise slow links and backpressure.",
        category="network", conflict_group="netem", self_expiring=True,
        params=(ParamSpec("rate_kbit", 8, 100000, 256, "kbit/s", "Egress rate limit."), _TTL),
        expected_symptoms=("large responses slow down first", "queues build up behind the target"))

    def intensity(self) -> float:
        return max(0.1, min(1.0, 64 / self.params["rate_kbit"]))

    def netem(self) -> str:
        return f"rate {self.params['rate_kbit']}kbit"


# ---------------------------------------------------------------- resource faults
class CpuThrottleFault(Fault):
    spec = FaultSpec(
        name="cpu_throttle", title="CPU limit",
        description="Temporarily cap the container's CPU with a CFS quota (docker update --cpu-quota), "
                    "then restore the original quota.",
        category="resource", conflict_group="cpu",
        params=(ParamSpec("cpu_pct", 5, 90, 25, "% of one core", "CPU the container may use while throttled."),),
        expected_symptoms=("request latency rises under load", "GC pauses and timeouts in JVM services"))
    PERIOD = 100000

    def intensity(self) -> float:
        return min(1.0, (100 - self.params["cpu_pct"]) / 100)

    def planned_commands(self) -> list[str]:
        return [f"docker update --cpu-period {self.PERIOD} --cpu-quota {self._quota()} {self.container.name}",
                f"(rollback) docker update --cpu-quota <original> {self.container.name}, then verify"]

    def manual_recovery(self) -> list[str]:
        orig = self.state.get("original_quota") or -1
        return [f"docker update --cpu-quota {orig} {self.container.name}"]

    def _quota(self) -> int:
        return int(self.PERIOD * self.params["cpu_pct"] / 100)

    async def _read(self) -> tuple[int, int, int]:
        out = await guard.run_cmd(["docker", "inspect", "-f",
                                   "{{.HostConfig.NanoCpus}}\t{{.HostConfig.CpuQuota}}\t{{.HostConfig.CpuPeriod}}",
                                   self.container.id])
        try:
            nano, quota, period = (int(x) for x in out.strip().split("\t"))
        except ValueError as e:
            raise guard.GuardError(f"unexpected CPU settings output for '{self.container.name}': {out!r}") from e
        return nano, quota, period

    async def preflight(self) -> None:
        nano, quota, period = await self._read()
        if nano:
            raise guard.GuardError(f"'{self.container.name}' is configured with --cpus (NanoCpus={nano}); "
                                   f"Docker refuses to mix that with a CPU quota. Not supported for this target.")
        self.state["original_quota"], self.state["original_period"] = quota, period

    async def apply(self) -> None:
        await guard.run_cmd(["docker", "update", "--cpu-period", str(self.PERIOD),
                             "--cpu-quota", str(self._quota()), self.container.id])
        _, quota, _ = await self._read()
        if quota != self._quota():
            raise guard.GuardError(f"cpu_throttle did not apply: CpuQuota is {quota}, expected {self._quota()}")

    async def inspect(self) -> dict:
        _, quota, period = await self._read()
        return {"active": quota == self._quota(), "detail": f"CpuQuota={quota} CpuPeriod={period}"}

    async def rollback(self) -> None:
        if "original_quota" not in self.state:
            return  # preflight never completed, so apply() never ran
        orig = self.state["original_quota"]
        restore = orig if orig > 0 else -1
        try:
            await guard.run_cmd(["docker", "update", "--cpu-quota", str(restore), self.container.id], check=False)
            _, quota, _ = await self._read()
        except guard.GuardError as e:
            raise guard.RollbackError(f"could not verify the CPU quota of '{self.container.name}': {e}") from e
        unlimited = orig <= 0 and quota <= 0
        if not (unlimited or quota == orig):
            raise guard.RollbackError(
                f"CPU quota on '{self.container.name}' is {quota}, expected the original {orig}. "
                f"Run manually:\n  {self.manual_recovery()[0]}")


# ---------------------------------------------------------------- registry
REGISTRY: dict[str, type[Fault]] = {
    cls.spec.name: cls for cls in (KillFault, StopFault, RestartFault, PauseFault,
                                   DelayFault, JitterFault, LossFault, BandwidthFault, CpuThrottleFault)
}

# Requested fault types that are deliberately NOT implemented, with the precise reason.
# They are listed by the API as unsupported; nothing can select or run them.
UNSUPPORTED: dict[str, FaultSpec] = {s.name: s for s in (
    FaultSpec("cpu_stress", "CPU stress (load generator)", "Burn CPU inside the target's cgroup.",
              "resource", "cpu", supported=False, live_verified=False, verification_note="Not implemented.",
              unsupported_reason="Needs a load generator running inside the target's own cgroup. The Ditto "
                                 "images ship no stress tool, and a separate helper container only competes "
                                 "for host CPU in general -- it would not be pressure on the chosen target. "
                                 "Use cpu_throttle for bounded CPU pressure on one container."),
    FaultSpec("memory_pressure", "Memory pressure", "Consume or cap the target's memory.",
              "resource", "memory", supported=False, live_verified=False, verification_note="Not implemented.",
              unsupported_reason="Lowering a JVM container's memory limit below its live heap makes the "
                                 "kernel OOM-kill it; that is not a bounded, reversible fault and its "
                                 "effect on Docker Desktop's shared VM cannot be contained from here."),
    FaultSpec("request_failure", "Request failure / timeout injection",
              "Fail or stall a share of HTTP requests.", "network", "proxy", supported=False,
              live_verified=False, verification_note="Not implemented.",
              unsupported_reason="Needs a fault-injecting proxy in the request path. The Ditto compose "
                                 "stack has none, and adding one changes the system under test. "
                                 "loss/delay/pause cover the reachable part of this behaviour."),
)}


def get_spec(name: str) -> FaultSpec | None:
    if name in REGISTRY:
        return REGISTRY[name].spec
    return UNSUPPORTED.get(name)


def catalog() -> list[dict]:
    return [cls.spec.to_dict() for cls in REGISTRY.values()] + [s.to_dict() for s in UNSUPPORTED.values()]


def build(name: str, service: str, container: guard.ContainerRef, params: dict | None = None,
          state: dict | None = None) -> Fault:
    cls = REGISTRY.get(name)
    if cls is None:
        spec = UNSUPPORTED.get(name)
        reason = f": {spec.unsupported_reason}" if spec else ""
        raise guard.GuardError(f"fault type '{name}' is not supported{reason}")
    return cls(service, container, params, state)


def from_recovery_info(info: dict) -> Fault:
    c = info["container"]
    return build(info["fault"], info["service"], guard.ContainerRef(c["name"], c["id"]),
                 info.get("params"), info.get("state"))


async def environment_report() -> dict:
    """Read-only look at the Docker host, so the API can say which faults can run HERE."""
    report = {"docker_available": False, "os": None, "cgroup_version": None,
              "netshoot_image_present": None, "error": None}
    try:
        out = await guard.run_cmd(["docker", "info", "--format",
                                   "{{.OSType}}\t{{.OperatingSystem}}\t{{.CgroupVersion}}"], timeout=10)
        parts = out.split("\t")
        report.update(docker_available=True, os=" / ".join(parts[:2]),
                      cgroup_version=parts[2] if len(parts) > 2 else None)
    except guard.GuardError as e:
        report["error"] = str(e).splitlines()[0][:300]
        return report
    try:
        await guard.run_cmd(["docker", "image", "inspect", "--format", "{{.Id}}", guard.NETSHOOT_IMAGE], timeout=10)
        report["netshoot_image_present"] = True
    except guard.GuardError:
        report["netshoot_image_present"] = False
    return report
