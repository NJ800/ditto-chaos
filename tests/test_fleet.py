"""Multi-product fleet: profile schemas, generator consistency, configuration, Ditto
payloads, profile-aware monitoring and per-profile experiment impact.

All Ditto HTTP here is MOCKED (httpx.MockTransport). Live validation against a real
Ditto is in tests/integration/test_live_fleet.py (opt-in, DITTO_CHAOS_LIVE_DITTO=1).
"""
import asyncio
import json
import time

import httpx
import pytest

import config
import fleet
import probe
import profiles
import simulator

NOW = 1_800_000_000.0
MIXED = {"SIM_FLEET": "ev:6,industrial_machine:4,smart_home:8,energy_system:3,environmental_sensor:3,"
                      "connected_appliance:6", "SIM_SEED": "42"}


def mk(env: dict) -> fleet.Fleet:
    return fleet.load(env=env)


def run_twin(spec, fl, ticks, start=NOW):
    """Drive one twin for `ticks` updates in simulated time; return every features payload."""
    tw = simulator.Twin(spec, fl, now=start)
    return tw, [tw.next_features(now=start + (i + 1) * spec.interval_s) for i in range(ticks)]


def as_thing(fl, spec, feats):
    return {"thingId": spec.id, **fleet.thing_payload(fl, spec, feats)}


# ---------------------------------------------------------------- registry / schema
def test_registry_has_the_six_required_categories():
    assert set(profiles.REGISTRY) == {"ev", "industrial_machine", "smart_home", "energy_system",
                                      "environmental_sensor", "connected_appliance"}
    for p in profiles.REGISTRY.values():
        d = p.describe()
        assert d["synthetic"] is True and d["kinds"] and d["features"]
        assert p.heartbeat_feature in d["features"]
        assert {"seq", "sentAt"} <= set(d["features"][p.heartbeat_feature]["properties"])


def test_profiles_have_genuinely_different_schemas():
    feats = {k: frozenset(p.features) for k, p in profiles.REGISTRY.items()}
    assert len(set(feats.values())) == len(feats)
    assert "battery" in feats["ev"] and "battery" not in feats["industrial_machine"]
    assert "vibration" in feats["industrial_machine"]
    assert "location" not in feats["energy_system"] | feats["smart_home"] | feats["connected_appliance"]


def test_not_applicable_signals_are_explicit():
    m = profiles.REGISTRY["industrial_machine"]
    assert not m.signal_applicable("battery_pct") and not m.signal_applicable("humidity_pct")
    assert m.describe()["signals"]["battery_pct"] == {"applicable": False}
    assert profiles.REGISTRY["ev"].signal_applicable("battery_pct")
    assert not profiles.REGISTRY["ev"].signal_applicable("humidity_pct")


@pytest.mark.parametrize("key", list(profiles.REGISTRY))
def test_every_profile_generates_valid_consistent_twins_over_time(key):
    """Long simulated runs of every device kind must pass the profile's own schema,
    range and consistency rules on every single update (including monotonic rules)."""
    fl = mk({"SIM_FLEET": f"{key}:8", "SIM_SEED": "7"})
    for spec in fl.twins:
        _, payloads = run_twin(spec, fl, 400)
        prev = None
        for i, feats in enumerate(payloads):
            thing = as_thing(fl, spec, feats)
            res = profiles.validate_twin(profiles.REGISTRY[key], spec.kind, thing, prev,
                                         NOW + (i + 1) * spec.interval_s + 1)
            assert res["violations"] == [] and res["missing_features"] == [] and res["unexpected_features"] == [], \
                (spec.id, i, res)
            prev = thing["features"]


def test_ev_values_stay_in_range_and_charging_is_stationary():
    fl = mk({"SIM_FLEET": "ev:5", "SIM_SEED": "3"})
    for spec in fl.twins:
        _, payloads = run_twin(spec, fl, 1500)
        for f in payloads:
            b, m = f["battery"]["properties"], f["motion"]["properties"]
            assert 0 <= b["level"] <= 100 and 0 <= m["speedKmh"] <= 120
            if b["charging"]:
                assert m["speedKmh"] == 0 and b["chargingPowerKw"] > 0


def test_energy_power_balance_holds():
    fl = mk({"SIM_FLEET": "energy:2", "SIM_SEED": "1"})
    for spec in fl.twins:
        _, payloads = run_twin(spec, fl, 300)
        for f in payloads:
            g, load = f["grid"]["properties"], f["consumption"]["properties"]["loadW"]
            gen, st = f["solar"]["properties"]["generationW"], f["storage"]["properties"]["powerW"]
            assert abs(g["powerW"] - (load - gen + st)) <= 50


def test_validators_detect_injected_problems():
    fl = mk({"SIM_FLEET": "industrial_machine:1,smart_home:4", "SIM_SEED": "1"})
    m = fl.twins[0]
    _, (feats,) = run_twin(m, fl, 1)
    bad = json.loads(json.dumps(feats))
    bad["vibration"]["properties"]["zone"] = "A"
    bad["vibration"]["properties"]["rmsMmS"] = 9.0           # zone D, inconsistent
    bad["motor"]["properties"]["temperatureC"] = 900         # out of range
    del bad["diagnostics"]
    res = profiles.validate_twin(profiles.REGISTRY["industrial_machine"], m.kind, as_thing(fl, m, bad), None, NOW + 10)
    assert "machine.vibration_zone" in res["violations"]
    assert "motor.temperatureC:invalid" in res["violations"]
    assert res["missing_features"] == ["diagnostics"]
    light = next(t for t in fl.twins if t.kind == "smart_light")
    _, (lf,) = run_twin(light, fl, 1)
    lf["lighting"]["properties"].update(on=False, powerW=8.0)
    res = profiles.validate_twin(profiles.REGISTRY["smart_home"], "smart_light", as_thing(fl, light, lf), None, NOW + 10)
    assert "home.light_power_consistent" in res["violations"]


def test_monotonic_rule_catches_regression():
    fl = mk({"SIM_FLEET": "connected_appliance:1", "SIM_SEED": "1"})
    spec = fl.twins[0]
    _, (a, b) = run_twin(spec, fl, 2)
    b["power"]["properties"]["energyKwh"] = a["power"]["properties"]["energyKwh"] - 5
    res = profiles.validate_twin(profiles.REGISTRY["connected_appliance"], spec.kind, as_thing(fl, spec, b),
                                 a, NOW + 10)
    assert "appliance.energy_monotonic" in res["violations"]


def test_aqi_helpers_follow_epa_breakpoints():
    assert profiles.aqi_from_pm25(0) == 0 and profiles.aqi_from_pm25(9.0) == 50
    assert profiles.aqi_from_pm25(35.4) == 100 and profiles.aqi_category(151) == "unhealthy"
    assert profiles.vibration_zone(1.0) == "A" and profiles.vibration_zone(8) == "D"


# ---------------------------------------------------------------- configuration / plan
def test_legacy_car_only_fleet_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "NUM_CARS", 100)
    monkeypatch.setattr(config, "SEND_INTERVAL_S", 2.0)
    fl = mk({})
    assert fl.legacy and fl.counts() == {"ev": 100}
    assert fl.ids == config.all_car_ids()
    assert fl.ids[0] == f"{config.NAMESPACE}:car-001" and fl.intervals == {"ev": 2.0}
    assert fleet.search_filter(fl) == f'like(thingId,"{config.NAMESPACE}:car-*")'


def test_mixed_fleet_ids_are_unique_and_profile_prefixed():
    fl = mk(MIXED)
    assert len(fl.ids) == len(set(fl.ids)) == 30
    assert fl.counts() == {"ev": 6, "industrial_machine": 4, "smart_home": 8, "energy_system": 3,
                           "environmental_sensor": 3, "connected_appliance": 6}
    assert f"{config.NAMESPACE}:machine-004" in fl.ids and f"{config.NAMESPACE}:home-008" in fl.ids
    kinds = {t.kind for t in fl.twins if t.profile == "smart_home"}
    assert kinds == {"climate_sensor", "thermostat", "smart_light", "contact_sensor"}
    assert fleet.search_filter(fl).startswith("or(")


def test_aliases_intervals_and_params_from_env():
    fl = mk({"SIM_FLEET": "car:2,machine:2,home:4", "SIM_INTERVALS": "home:6",
             "SIM_PROFILE_PARAMS": json.dumps({"machine": {"rated_rpm": 2950}})})
    assert fl.intervals == {"ev": 2.0, "industrial_machine": 2.0, "smart_home": 6.0}
    assert fl.params["industrial_machine"].rated_rpm == 2950


def test_config_file(tmp_path):
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps({"fleet": {"ev": 3, "environment": {"count": 2, "interval_s": 4,
                                                                   "id_prefix": "air", "params": {"base_pm25": 30}}},
                                "seed": 9, "max_twins": 10, "fleet_tag": "lab-1"}))
    fl = mk({"SIM_CONFIG_FILE": str(path)})
    assert fl.config.seed == 9 and fl.config.fleet_tag == "lab-1" and fl.source.startswith("file:")
    assert f"{config.NAMESPACE}:air-002" in fl.ids and fl.params["environmental_sensor"].base_pm25 == 30


@pytest.mark.parametrize("env,needle", [
    ({"SIM_FLEET": "toaster:3"}, "unknown product profile"),
    ({"SIM_FLEET": "ev:150,machine:60"}, "max_twins"),
    ({"SIM_FLEET": "ev:5", "SIM_MAX_TWINS": "500"}, "max_twins"),
    ({"SIM_FLEET": "ev:5", "SIM_INTERVALS": "ev:30"}, "interval_s"),
    ({"SIM_FLEET": "ev:200", "SIM_INTERVALS": "ev:1"}, "update rate"),
    ({"SIM_FLEET": "ev:0"}, "empty"),
    ({"SIM_FLEET": "ev:x"}, "not a number"),
    ({"SIM_FLEET": "ev:2,car:2"}, "configured twice"),
    ({"SIM_FLEET": "ev:2", "SIM_PROFILE_PARAMS": '{"ev": {"battery_capacity_kwh": 5000}}'}, "battery_capacity_kwh"),
    ({"SIM_FLEET": "ev:2", "SIM_PROFILE_PARAMS": '{"ev": {"warp_drive": 1}}'}, "warp_drive"),
])
def test_invalid_configuration_is_rejected_with_a_reason(env, needle):
    with pytest.raises(fleet.FleetConfigError) as e:
        mk(env)
    assert needle in str(e.value)


def test_duplicate_prefixes_rejected(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"fleet": {"ev": {"count": 1, "id_prefix": "dev"},
                                          "machine": {"count": 1, "id_prefix": "dev"}}}))
    with pytest.raises(fleet.FleetConfigError, match="unique"):
        mk({"SIM_CONFIG_FILE": str(path)})


def test_fixed_seed_is_reproducible_and_independent_of_fleet_order():
    a, b = mk(MIXED), mk({**MIXED, "SIM_FLEET": "connected_appliance:6,ev:6"})
    for spec in [t for t in a.twins if t.profile in ("ev", "connected_appliance")]:
        other = next(t for t in b.twins if t.id == spec.id)
        _, pa = run_twin(spec, a, 50)
        _, pb = run_twin(other, b, 50)
        assert pa == pb
    _, p1 = run_twin(a.twins[0], a, 5)
    c = mk({**MIXED, "SIM_SEED": "43"})
    _, p2 = run_twin(c.twins[0], c, 5)
    assert p1 != p2


def test_seq_is_monotonic_and_sent_at_follows_time():
    fl = mk(MIXED)
    for spec in fl.twins:
        _, payloads = run_twin(spec, fl, 5)
        hb = profiles.REGISTRY[spec.profile].heartbeat_feature
        seqs = [p[hb]["properties"]["seq"] for p in payloads]
        sent = [p[hb]["properties"]["sentAt"] for p in payloads]
        assert seqs == [1, 2, 3, 4, 5] and sent == sorted(sent)


# ---------------------------------------------------------------- Ditto payloads (mocked HTTP)
class FakeDitto:
    """In-memory Ditto things/search API for httpx.MockTransport."""

    def __init__(self):
        self.things: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_direct = False
        self.search_lag: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if request.method == "PUT" and path.startswith("/api/2/things/"):
            rest = path[len("/api/2/things/"):]
            body = json.loads(request.content)
            if rest.endswith("/features"):
                tid = rest[:-len("/features")]
                if tid not in self.things:
                    return httpx.Response(404)
                self.things[tid]["features"] = body
                return httpx.Response(204)
            created = rest not in self.things
            self.things[rest] = {"thingId": rest, **body}
            return httpx.Response(201 if created else 204)
        if request.method == "GET" and path == "/api/2/things":
            if self.fail_direct:
                return httpx.Response(503)
            ids = request.url.params["ids"].split(",")
            return httpx.Response(200, json=[self.things[i] for i in ids if i in self.things])
        if request.method == "GET" and path == "/api/2/search/things":
            items = []
            for tid, t in self.things.items():
                item = json.loads(json.dumps({"thingId": tid, "features": t.get("features", {})}))
                for feat in item["features"].values():
                    props = feat.get("properties", {})
                    if "seq" in props:
                        props["seq"] -= self.search_lag.get(tid, 0)
                items.append(item)
            return httpx.Response(200, json={"items": items})
        return httpx.Response(400)


def client_for(ditto):
    return httpx.AsyncClient(transport=httpx.MockTransport(ditto), base_url="http://ditto")


def test_create_and_update_things_against_mocked_ditto():
    fl = mk(MIXED)
    ditto = FakeDitto()

    async def go():
        async with client_for(ditto) as c:
            twins = [simulator.Twin(s, fl) for s in fl.twins]
            await asyncio.gather(*(simulator.create_twin(c, t, fl, asyncio.Semaphore(5)) for t in twins))
            r = await c.put(f"/api/2/things/{twins[0].id}/features", json=twins[0].next_features())
            return twins, r.status_code
    twins, code = asyncio.run(go())
    assert code == 204 and all(t.acked_seq == 1 for t in twins)
    assert set(ditto.things) == set(fl.ids)
    machine = ditto.things[f"{config.NAMESPACE}:machine-001"]
    a = machine["attributes"]
    assert a["productType"] == "industrial_machine" and a["model"] == "IM-500 Motor-Pump"
    assert a["simulator"]["synthetic"] is True and a["simulator"]["profile"] == "industrial_machine"
    assert a["simulator"]["notice"] == profiles.SYNTHETIC_NOTICE
    assert machine["definition"] == "org.dittochaos.sim:industrial-machine:1.0.0"
    assert machine["features"]["vibration"]["definition"] == ["org.dittochaos.sim:industrial-machine-vibration:1.0.0"]
    assert machine["features"]["telemetry"]["properties"]["synthetic"] is True
    car = ditto.things[f"{config.NAMESPACE}:car-001"]
    assert car["attributes"]["model"] == "EV-X" and car["attributes"]["fleet"] == "hackathon"
    assert "seq" in car["features"]["battery"]["properties"]      # legacy heartbeat location kept
    assert "battery" not in machine["features"]


def test_create_twin_raises_with_ditto_error_text():
    fl = mk({"SIM_FLEET": "ev:1"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="no")),
                                     base_url="http://x") as c:
            await simulator.create_twin(c, simulator.Twin(fl.twins[0], fl), fl, asyncio.Semaphore(1))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        asyncio.run(go())


def test_sent_state_records_profile_and_fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SENT_FILE", str(tmp_path / "sent.json"))
    fl = mk(MIXED)
    twins = [simulator.Twin(s, fl) for s in fl.twins[:3]]
    simulator.write_state(twins, fl)
    data = json.loads((tmp_path / "sent.json").read_text())
    assert data["fleet"]["signature"] == fl.signature
    assert data["twins"][twins[0].id]["profile"] == "ev"
    assert probe.read_acked() == {t.id: 0 for t in twins}


def test_read_acked_accepts_the_car_only_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SENT_FILE", str(tmp_path / "sent.json"))
    (tmp_path / "sent.json").write_text(json.dumps({"cars": {"ns:car-001": {"acked_seq": 4, "acked_at": 1}}}))
    assert probe.read_acked() == {"ns:car-001": 4}


# ---------------------------------------------------------------- profile-aware monitoring
@pytest.fixture
def mixed(monkeypatch, tmp_path):
    fl = mk(MIXED)
    monkeypatch.setattr(fleet, "_current", fl)
    monkeypatch.setattr(config, "SENT_FILE", str(tmp_path / "sent.json"))
    fleet.reset_quality_cache()
    monkeypatch.setattr(probe, "_tick", 0)
    ditto = FakeDitto()
    now = time.time()
    twins = {}
    for s in fl.twins:
        tw = simulator.Twin(s, fl, now=now - 1)
        ditto.things[s.id] = {"thingId": s.id, **fleet.thing_payload(fl, s, tw.next_features(now=now - 1))}
        twins[s.id] = tw
    simulator.write_state(list(twins.values()), fl)
    for tw in twins.values():
        tw.acked_seq = tw.seq
    simulator.write_state(list(twins.values()), fl)
    return fl, ditto, twins


def probe_now(ditto, ids=None):
    async def go():
        async with client_for(ditto) as c:
            return await probe.probe_once(c, ids)
    return asyncio.run(go())


def test_probe_reports_system_and_profile_metrics_separately(mixed):
    fl, ditto, _ = mixed
    row = probe_now(ditto)
    assert row["direct_ok"] and row["search_ok"]
    assert row["lost_acked"] == 0 and row["search_drift"] == 0 and row["stale_p95_s"] is not None
    assert set(row["profiles"]) == set(fl.counts())
    for key, b in row["profiles"].items():
        assert b["measured"] and b["observed"] == b["twins"] == fl.counts()[key]
        assert b["fresh"] == b["twins"] and b["invalid"] == 0 and b["lost_acked"] == 0 and b["search_drift"] == 0
        assert b["freshness_limit_s"] == round(3 * fl.intervals[key] + config.PROBE_INTERVAL_S, 2)
    assert row["fleet"]["simulator_signature_match"] is True
    m = row["profiles"]["industrial_machine"]["signals"]
    assert m["battery_pct"] == {"applicable": False} and m["temperature_c"]["applicable"] and m["temperature_c"]["n"] == 4
    assert row["profiles"]["energy_system"]["signals"]["humidity_pct"] == {"applicable": False}
    assert set(probe.last_twin_status) == set(fl.ids)


def test_probe_isolates_problems_to_the_affected_profile(mixed):
    fl, ditto, twins = mixed
    machine = f"{config.NAMESPACE}:machine-002"
    ditto.things[machine]["features"]["telemetry"]["properties"]["sentAt"] = time.time() - 120   # stale
    env = f"{config.NAMESPACE}:envsensor-001"
    ditto.things[env]["features"]["airQuality"]["properties"]["aqi"] = 400                         # inconsistent
    car = f"{config.NAMESPACE}:car-003"
    twins[car].acked_seq = 99                                                                       # lost update
    simulator.write_state(list(twins.values()), fl)
    ditto.search_lag[f"{config.NAMESPACE}:home-001"] = 5                                            # search drift
    row = probe_now(ditto)
    p = row["profiles"]
    assert p["industrial_machine"]["stale"] == 1 and p["industrial_machine"]["fresh"] == 3
    assert p["environmental_sensor"]["invalid"] == 1
    assert p["environmental_sensor"]["violations"].get("env.aqi_matches_pm25") == 1
    assert p["ev"]["lost_acked"] == 1 and row["lost_acked"] == 1
    assert p["smart_home"]["search_drift"] == 1 and row["search_drift"] == 1
    for unaffected in ("energy_system", "connected_appliance"):
        assert p[unaffected]["fresh"] == p[unaffected]["twins"] and p[unaffected]["invalid"] == 0
    assert probe.last_twin_status[machine]["fresh"] is False


def test_failed_direct_read_reports_profiles_as_not_measured(mixed):
    _, ditto, _ = mixed
    ditto.fail_direct = True
    row = probe_now(ditto)
    assert row["direct_ok"] is False and row["stale_p95_s"] is None
    for b in row["profiles"].values():
        assert b["measured"] is False and b["fresh"] is None and b["valid_ratio"] is None


def test_missing_twin_is_counted_as_missing_not_invalid(mixed):
    _, ditto, _ = mixed
    del ditto.things[f"{config.NAMESPACE}:appliance-002"]
    b = probe_now(ditto)["profiles"]["connected_appliance"]
    assert b["missing"] == 1 and b["observed"] == 5 and b["invalid"] == 0 and b["stale"] == 1


def test_large_fleet_direct_read_is_chunked(monkeypatch, tmp_path):
    fl = mk({"SIM_FLEET": "ev:120,machine:30", "SIM_SEED": "1"})
    monkeypatch.setattr(fleet, "_current", fl)
    monkeypatch.setattr(config, "SENT_FILE", str(tmp_path / "none.json"))
    ditto = FakeDitto()
    for s in fl.twins:
        ditto.things[s.id] = {"thingId": s.id, **fleet.thing_payload(fl, s, simulator.Twin(s, fl).next_features())}
    row = probe_now(ditto)
    assert sum(1 for m, p in ditto.requests if p == "/api/2/things") == 4     # 2 heartbeat + 2 full-check chunks
    assert row["direct_ok"] and row["profiles"]["ev"]["observed"] == 120 and row["lost_acked"] is None


# ---------------------------------------------------------------- per-profile experiment impact
def _row(phase, **per):
    base = {"measured": True, "twins": 4, "title": "x", "freshness_limit_s": 8, "fresh_ratio": 1.0,
            "stale_p95_s": 2.0, "valid_ratio": 1.0, "lost_acked": 0, "search_drift": 0, "missing": 0}
    return {"phase": phase, "profiles": {k: ({**base, **v} if v is not None else {**base, "measured": False,
                                                                                    "fresh_ratio": None,
                                                                                    "stale_p95_s": None})
                                         for k, v in per.items()}}


def test_profile_impact_separates_affected_and_unaffected_profiles():
    tl = ([_row("before", ev={}, industrial_machine={})] * 3
          + [_row("fault", ev={"fresh_ratio": 0.2, "stale_p95_s": 20.0}, industrial_machine={})] * 3
          + [_row("recovery", ev={}, industrial_machine={})] * 3)
    imp = fleet.profile_impact(tl, ["industrial_machine"])
    assert imp["affected_profiles"] == ["ev"] and imp["unaffected_profiles"] == ["industrial_machine"]
    assert imp["profiles"]["ev"]["symptoms"] == ["freshness_loss", "staleness"]
    assert imp["profiles"]["ev"]["recovered"] is True and imp["profiles"]["ev"]["in_scope"] is False
    assert imp["profiles"]["industrial_machine"]["in_scope"] is True
    assert imp["system_wide_symptoms"] == []


def test_profile_impact_marks_unobservable_profiles_and_non_recovery():
    tl = ([_row("before", ev={}, smart_home={})] * 2 + [_row("fault", ev=None, smart_home=None)] * 3
          + [_row("recovery", ev={"fresh_ratio": 0.1}, smart_home={})] * 2)
    imp = fleet.profile_impact(tl)
    assert imp["not_measured_profiles"] == ["ev", "smart_home"] and imp["affected_profiles"] == []
    assert imp["profiles"]["ev"]["recovered"] is False and imp["profiles"]["smart_home"]["recovered"] is True


def test_profile_impact_flags_symptoms_shared_by_every_profile():
    lossy = {"fresh_ratio": 0.0, "stale_p95_s": 30.0}
    tl = [_row("before", ev={}, smart_home={})] * 2 + [_row("fault", ev=lossy, smart_home=lossy)] * 2
    imp = fleet.profile_impact(tl)
    assert imp["system_wide_symptoms"] == ["freshness_loss", "staleness"]
    assert imp["profiles"]["ev"]["recovered"] is None          # recovery was not measured at all


def test_legacy_record_without_profile_data_has_no_impact_block():
    assert fleet.profile_impact([{"phase": "fault", "direct_ok": True}]) is None
