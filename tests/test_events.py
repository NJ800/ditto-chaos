"""Event bus (ordering, replay, resync, slow clients) and the server-side monitor."""
import asyncio
import json
import os
import time

import config
import monitor as mon
from events import EventBus


async def take(gen, n, timeout=1.0):
    out = []
    try:
        for _ in range(n):
            out.append(await asyncio.wait_for(gen.__anext__(), timeout))
    finally:
        await gen.aclose()
    return out


# ---------------------------------------------------------------- bus
async def test_events_are_sequenced_and_delivered_in_order():
    bus = EventBus()
    gen = bus.stream()
    first = asyncio.ensure_future(take(gen, 3))
    await asyncio.sleep(0.01)
    for i in range(3):
        bus.publish("metric", {"i": i})
    events = await first
    assert [e["seq"] for e in events] == [1, 2, 3] and [e["data"]["i"] for e in events] == [0, 1, 2]
    assert all(e["v"] == 1 and e["boot"] == bus.boot and e["ts"] for e in events)
    assert bus.subscriber_count() == 0          # closing the generator unsubscribed it


async def test_reconnect_replays_only_what_was_missed_without_duplicates():
    bus = EventBus()
    for i in range(5):
        bus.publish("metric", {"i": i})
    gen = bus.stream(last_seq=3, boot=bus.boot)
    task = asyncio.ensure_future(take(gen, 3))
    await asyncio.sleep(0.01)
    bus.publish("metric", {"i": 5})
    assert [e["seq"] for e in await task] == [4, 5, 6]


async def test_gap_larger_than_the_buffer_tells_the_client_to_resync():
    bus = EventBus(history=3)
    for i in range(10):
        bus.publish("metric", {"i": i})
    events = await take(bus.stream(last_seq=2, boot=bus.boot), 1)
    assert events[0]["type"] == "resync" and "replay buffer" in events[0]["data"]["reason"]


async def test_server_restart_is_detected_by_boot_id():
    bus = EventBus()
    bus.publish("metric", {})
    events = await take(bus.stream(last_seq=40, boot="some-older-process"), 1)
    assert events[0]["type"] == "resync" and events[0]["data"]["reason"] == "server restarted"


async def test_slow_client_gets_resync_and_never_blocks_the_publisher():
    bus = EventBus(queue_size=5)
    gen = bus.stream()
    first = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.01)
    t0 = time.perf_counter()
    for i in range(500):                     # far more than the client's queue holds
        bus.publish("metric", {"i": i})
    assert time.perf_counter() - t0 < 0.5    # publishing did not wait for anyone
    told = await first
    assert told["type"] == "resync" and bus.dropped_overflows == 1   # told to re-fetch, not fed a partial stream
    bus.publish("metric", {"i": "after"})
    nxt = await asyncio.wait_for(gen.__anext__(), 1)
    await gen.aclose()
    assert nxt["data"] == {"i": "after"} and nxt["seq"] == 501


async def test_heartbeat_when_idle():
    bus = EventBus()
    events = await take(bus.stream(heartbeat_s=0.02), 1)
    assert events[0]["type"] == "heartbeat"


async def test_publish_from_another_thread_is_safe():
    bus = EventBus()
    gen = bus.stream()
    task = asyncio.ensure_future(take(gen, 20, timeout=2))
    await asyncio.sleep(0.01)
    await asyncio.gather(*[asyncio.to_thread(bus.publish, "metric", {"i": i}) for i in range(20)])
    seqs = [e["seq"] for e in await task]
    assert seqs == sorted(seqs) and len(set(seqs)) == 20


# ---------------------------------------------------------------- monitor
def _write(path, rows, mtime=None):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write((json.dumps(r) if not isinstance(r, str) else r) + "\n")
    if mtime:
        os.utime(path, (mtime, mtime))


def test_read_rows_uses_most_recently_written_file_and_skips_garbage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "METRICS_DIR", str(tmp_path))
    now = time.time()
    # A later-NAMED file that finished long ago must not shadow the active stream.
    _write(tmp_path / "probe-20990101-000000.jsonl", [{"ts": now - 5000, "direct_ok": True}], mtime=now - 5000)
    _write(tmp_path / "probe-20200101-000000.jsonl",
           [{"ts": now - 4, "direct_ok": True}, "{half written", "[]", {"ts": "bad"}, {"ts": now - 2, "direct_ok": False}])
    rows = mon.read_rows(60)
    assert [r["direct_ok"] for r in rows] == [True, False]
    assert mon.read_rows(60, since_ts=now - 3) == rows[1:]
    assert mon.read_rows(1) == []


def test_read_rows_downsamples_to_a_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "METRICS_DIR", str(tmp_path))
    now = time.time()
    _write(tmp_path / "probe-1.jsonl", [{"ts": now - 1000 + i} for i in range(900)])
    assert len(mon.read_rows(2000, max_points=100)) == 100


def test_monitor_reports_its_own_health_separately_from_the_target(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "METRICS_DIR", str(tmp_path))
    m = mon.Monitor()
    assert m.status()["state"] == "down"                 # loop never ticked
    m.tick()
    st = m.status()
    assert st["loop_running"] is True and st["state"] == "stale" and st["probe_fresh"] is False
    _write(tmp_path / "probe-1.jsonl", [{"ts": time.time(), "direct_ok": True, "search_ok": True,
                                         "direct_ms": 5.0, "search_ms": 5.0}])
    m.tick()
    assert m.status()["state"] == "ok"
    snap = m.snapshot()
    assert snap["health"]["status"] == "healthy" and snap["monitoring"]["state"] == "ok"
    assert snap["services"]["services"] is None           # Docker not queried: unknown, not "all down"


def test_monitor_tick_publishes_each_probe_row_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "METRICS_DIR", str(tmp_path))
    published = []
    monkeypatch.setattr(mon.bus, "publish", lambda t, d: published.append((t, d)))
    m = mon.Monitor()
    path = tmp_path / "probe-1.jsonl"
    now = time.time()
    _write(path, [{"ts": now - 1, "direct_ok": True, "search_ok": True, "direct_ms": 1.0, "search_ms": 1.0}])
    m.tick()
    m.tick()
    with open(path, "a") as f:
        f.write(json.dumps({"ts": now, "direct_ok": True, "search_ok": True, "direct_ms": 1.0, "search_ms": 1.0}) + "\n")
    m.tick()
    assert [d["row"]["ts"] for t, d in published if t == "metric"] == [now - 1, now]
    assert sum(1 for t, _ in published if t == "health") == 3


def test_collect_services_reports_unavailable_docker_as_unknown(monkeypatch):
    def no_docker(*a, **kw):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(mon.subprocess, "run", no_docker)
    out = mon.collect_services()
    assert out == {"available": False, "error": out["error"], "services": None} and "docker" in out["error"]


def test_collect_services_parses_states_and_usage(monkeypatch):
    class R:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def fake(args, **kw):
        if args[1] == "ps":
            return R("things\tdocker-things-1\trunning\tUp 2 hours\nmongodb\tdocker-mongodb-1\texited\tExited (137)\n")
        return R("docker-things-1\t12.50%\t512MiB / 2GiB\t25.00%\n")
    monkeypatch.setattr(mon.subprocess, "run", fake)
    svc = {s["service"]: s for s in mon.collect_services()["services"]}
    assert svc["things"]["cpu_pct"] == 12.5 and svc["things"]["mem_pct"] == 25.0
    assert svc["mongodb"]["state"] == "exited" and svc["mongodb"]["cpu_pct"] is None


async def test_monitor_loop_survives_a_failing_iteration_and_stops_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "METRICS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PROBE_INTERVAL_S", 0.01)
    monkeypatch.setattr(mon, "collect_services", lambda: {"available": False, "error": "x", "services": None})
    m = mon.Monitor()
    calls = {"n": 0}
    real = m.tick

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("bad iteration")
        real()
    m.tick = flaky
    m.start()
    await asyncio.sleep(0.15)
    assert calls["n"] > 3 and m.status()["loop_running"]
    await m.stop()
    assert m._task is None
