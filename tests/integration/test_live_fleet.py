"""LIVE Ditto validation of the multi-product simulator. Opt-in.

Creates one twin of every device kind of every registered profile in a private namespace
(org.dittochaos.livetest), updates them, reads them back through the real things and
search services, runs the real probe over them, and deletes the twins and their policies.
It never touches the simulator's own twins.

Run (PowerShell), with Ditto running at DITTO_URL:
    $env:DITTO_CHAOS_LIVE_DITTO = "1"
    python -m pytest tests/integration/test_live_fleet.py -q

Everything in tests/test_fleet.py uses a MOCKED Ditto; this file is the live check.
"""
import asyncio
import os
import time

import httpx
import pytest

import config
import fleet
import probe
import profiles
import simulator

pytestmark = pytest.mark.skipif(os.getenv("DITTO_CHAOS_LIVE_DITTO") != "1",
                                reason="set DITTO_CHAOS_LIVE_DITTO=1 to run against a live Ditto")
NAMESPACE = "org.dittochaos.livetest"


@pytest.fixture
def live_fleet(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "NAMESPACE", NAMESPACE)
    monkeypatch.setattr(config, "SENT_FILE", str(tmp_path / "sent.json"))
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    counts = ",".join(f"{k}:{len(p.kinds)}" for k, p in profiles.REGISTRY.items())
    fl = fleet.load(env={"SIM_FLEET": counts, "SIM_SEED": "11", "SIM_FLEET_TAG": "livetest"})
    monkeypatch.setattr(fleet, "_current", fl)
    fleet.reset_quality_cache()
    monkeypatch.setattr(probe, "_tick", 0)
    yield fl
    with httpx.Client(base_url=config.DITTO_URL, auth=(config.DITTO_USER, config.DITTO_PASS), timeout=10) as c:
        for tid in fl.ids:
            c.delete(f"/api/2/things/{tid}")
            c.delete(f"/api/2/policies/{tid}")


def test_live_mixed_fleet_round_trip(live_fleet):
    fl = live_fleet

    async def go():
        async with httpx.AsyncClient(base_url=config.DITTO_URL, auth=(config.DITTO_USER, config.DITTO_PASS),
                                     timeout=config.REQUEST_TIMEOUT_S) as c:
            twins = [simulator.Twin(s, fl) for s in fl.twins]
            await asyncio.gather(*(simulator.create_twin(c, t, fl, asyncio.Semaphore(5)) for t in twins))
            for _ in range(3):
                for t in twins:
                    r = await c.put(f"/api/2/things/{t.id}/features", json=t.next_features())
                    assert r.status_code == 204, (t.id, r.status_code, r.text[:300])
                    t.acked_seq = t.seq
                await asyncio.sleep(0.5)
            simulator.write_state(twins, fl)
            stored = (await c.get("/api/2/things", params={"ids": ",".join(fl.ids)})).json()
            deadline, row = time.time() + 30, None
            while time.time() < deadline:   # search is eventually consistent
                row = await probe.probe_once(c)
                if row["search_drift"] == 0:
                    break
                await asyncio.sleep(1)
            return twins, stored, row
    twins, stored, row = asyncio.run(go())

    assert len(stored) == len(fl.ids)
    by_id = {t["thingId"]: t for t in stored}
    for spec in fl.twins:
        thing = by_id[spec.id]
        prof = profiles.REGISTRY[spec.profile]
        assert thing["definition"] == prof.thing_definition()
        assert thing["attributes"]["simulator"]["synthetic"] is True
        assert set(thing["features"]) == set(prof.feature_names(spec.kind))
        for name, feat in thing["features"].items():
            assert feat["definition"] == [prof.definition(name)]
        res = profiles.validate_twin(prof, spec.kind, thing, None, time.time())
        assert res["violations"] == [] and res["missing_features"] == [], (spec.id, res)
        hb = thing["features"][prof.heartbeat_feature]["properties"]
        assert hb["seq"] == 4                                   # create + 3 updates, all acknowledged

    assert row["direct_ok"] and row["search_ok"], row
    assert row["lost_acked"] == 0 and row["search_drift"] == 0
    for key, b in row["profiles"].items():
        assert b["observed"] == b["twins"] == len(profiles.REGISTRY[key].kinds), (key, b)
        assert b["invalid"] == 0 and b["lost_acked"] == 0 and b["fresh"] == b["twins"], (key, b)
