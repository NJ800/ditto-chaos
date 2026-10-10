"""Fleet simulator: a configurable, mixed fleet of synthetic digital twins in Ditto.

What the fleet contains -- which product profiles, how many twins of each, update
intervals, seed, per-profile parameters -- comes from fleet.py (env vars or a JSON file,
see README "Simulator configuration"). With no fleet configuration it runs the original
car-only workflow: NUM_CARS electric vehicles <NAMESPACE>:car-NNN every SEND_INTERVAL_S.

Each twin is created once with PUT /api/2/things/{id} (thing definition, identity and
profile attributes, features with definitions), then updated every interval with
PUT /api/2/things/{id}/features. Every update carries, in the profile's heartbeat feature:
  seq     - increasing counter per twin (lets the probe detect lost updates)
  sentAt  - unix time the update was sent (lets the probe measure staleness)

Every second it writes state/sent.json with the last seq Ditto ACKNOWLEDGED for each
twin. The probe compares that with what Ditto actually stores.

All telemetry is synthetic (see profiles.py); twins say so in attributes.simulator.

Run:  python simulator.py          (Ctrl+C to stop)
      SIM_FLEET=ev:30,machine:20,home:50 python simulator.py
"""
import asyncio
import json
import os
import random
import sys
import time

import httpx

import config
import fleet
import profiles

stats = {"sent": 0, "ok": 0, "failed": 0}
profile_stats: dict[str, dict] = {}


class Twin:
    """One simulated device: its plan entry, private RNG and evolving state."""

    def __init__(self, spec: fleet.TwinSpec, fl: fleet.Fleet, now: float | None = None):
        self.spec, self.id, self.profile = spec, spec.id, spec.profile
        self.prof = profiles.REGISTRY[spec.profile]
        self.params = fl.params[spec.profile]
        self.rng = fleet.twin_rng(fl.config.seed, spec.id)
        self.state = self.prof.init_state(spec.kind, self.rng, self.params, time.time() if now is None else now)
        self.seq = 0
        self.acked_seq = 0
        self.acked_at = 0.0
        self.last_step: float | None = None

    def next_features(self, now: float | None = None) -> dict:
        """Advance the device by the time since its last update and render a full
        features payload (with seq/sentAt in the heartbeat feature)."""
        now = time.time() if now is None else now
        dt = self.spec.interval_s if self.last_step is None else max(0.0, now - self.last_step)
        self.last_step = now
        self.prof.step(self.spec.kind, self.state, self.rng, dt, self.params, now)
        self.seq += 1
        rendered = self.prof.render(self.spec.kind, self.state, now, self.params)
        return fleet.features_payload(self.prof, self.spec.kind, rendered, self.seq, now)


async def create_twin(client: httpx.AsyncClient, twin: Twin, fl: fleet.Fleet, sem: asyncio.Semaphore) -> None:
    body = fleet.thing_payload(fl, twin.spec, twin.next_features())
    for attempt in range(3):   # Ditto can be slow right after a (re)start: retry timeouts and 5xx
        try:
            async with sem:
                r = await client.put(f"/api/2/things/{twin.id}", json=body)
            if r.status_code < 500:
                break
        except httpx.TimeoutException:
            if attempt == 2:
                raise
        await asyncio.sleep(2 * (attempt + 1))
    if r.status_code not in (201, 204):
        raise RuntimeError(f"Creating {twin.id} failed: HTTP {r.status_code} {r.text[:300]}")
    twin.acked_seq, twin.acked_at = twin.seq, time.time()


async def drive(client: httpx.AsyncClient, twin: Twin) -> None:
    interval = twin.spec.interval_s
    await asyncio.sleep(random.uniform(0, interval))  # spread load evenly
    while True:
        features = twin.next_features()
        stats["sent"] += 1
        profile_stats.setdefault(twin.profile, {"sent": 0, "ok": 0, "failed": 0})["sent"] += 1
        try:
            r = await client.put(f"/api/2/things/{twin.id}/features", json=features)
            if r.status_code in (201, 204):
                twin.acked_seq, twin.acked_at = twin.seq, time.time()
                stats["ok"] += 1
                profile_stats[twin.profile]["ok"] += 1
            else:
                stats["failed"] += 1
                profile_stats[twin.profile]["failed"] += 1
        except httpx.HTTPError:
            stats["failed"] += 1
            profile_stats[twin.profile]["failed"] += 1
        await asyncio.sleep(interval)


def write_state(twins: list, fl: fleet.Fleet | None = None) -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    data = {"written_at": time.time(),
            "stats": dict(stats), "stats_by_profile": {k: dict(v) for k, v in profile_stats.items()},
            "fleet": {"signature": fl.signature, "counts": fl.counts(), "source": fl.source} if fl else None,
            "twins": {t.id: {"acked_seq": t.acked_seq, "acked_at": t.acked_at,
                             "profile": getattr(t, "profile", None)} for t in twins}}
    tmp = config.SENT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    # os.replace() is atomic on POSIX and Windows -- but on Windows specifically it
    # can raise a transient PermissionError (WinError 5) if another process (probe.py,
    # reading this same file every PROBE_INTERVAL_S) happens to have it open at that
    # exact instant: Python's plain open() doesn't request share-delete access, so a
    # concurrent rename-over-it is briefly denied. This is a real race, observed
    # crashing the simulator after a few minutes of normal operation on Windows -- not
    # hypothetical. A short bounded retry is the standard mitigation (the reader's
    # handle is only ever open for the few microseconds read_acked()'s `with` block
    # takes, so a brief retry reliably wins the race without masking a real failure).
    for attempt in range(5):
        try:
            os.replace(tmp, config.SENT_FILE)  # atomic: probe never reads a half-written file
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


async def reporter(twins: list[Twin], fl: fleet.Fleet) -> None:
    tick = 0
    while True:
        await asyncio.sleep(1)
        write_state(twins, fl)
        tick += 1
        if tick % 10 == 0:
            per = " ".join(f"{k}={v['ok']}/{v['sent']}" for k, v in profile_stats.items())
            print(f"[sim] sent={stats['sent']} ok={stats['ok']} failed={stats['failed']} | {per}", flush=True)


async def main() -> None:
    try:
        fl = fleet.current()
    except fleet.FleetConfigError as e:
        print(f"[sim] {e}", file=sys.stderr)
        for err in e.errors:
            print(f"[sim]   - {err}", file=sys.stderr)
        raise SystemExit(2)
    twins = [Twin(spec, fl) for spec in fl.twins]
    mix = ", ".join(f"{n} {profiles.REGISTRY[k].title.lower()} (every {fl.intervals[k]:g}s)"
                    for k, n in fl.counts().items())
    async with httpx.AsyncClient(base_url=config.DITTO_URL,
                                 auth=(config.DITTO_USER, config.DITTO_PASS),
                                 timeout=config.REQUEST_TIMEOUT_S,
                                 limits=httpx.Limits(max_connections=50)) as client:
        print(f"[sim] fleet {fl.signature} ({fl.source}): {mix}", flush=True)
        print(f"[sim] SYNTHETIC data: {profiles.SYNTHETIC_NOTICE}", flush=True)
        print(f"[sim] creating {len(twins)} twins at {config.DITTO_URL} ...", flush=True)
        sem = asyncio.Semaphore(10)
        await asyncio.gather(*(create_twin(client, t, fl, sem) for t in twins))
        write_state(twins, fl)
        print("[sim] driving (Ctrl+C to stop)", flush=True)
        try:
            await asyncio.gather(reporter(twins, fl), *(drive(client, t) for t in twins))
        finally:
            write_state(twins, fl)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[sim] stopped")
