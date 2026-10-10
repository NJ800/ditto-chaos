"""Fleet configuration, twin plan and per-profile monitoring.

One deterministic plan -- which twins exist, their ids, product profile, device kind and
update interval -- computed from configuration and shared by simulator.py (creates and
drives the twins), probe.py (reads and validates them), the API and the agents.

Configuration, highest precedence first:
  SIM_CONFIG_FILE   path to a JSON file (see README "Simulator configuration"):
                    {"fleet": {"ev": {"count": 30}, "industrial_machine": 20, ...},
                     "seed": 7, "max_twins": 200, "fleet_tag": "hackathon"}
  SIM_FLEET         "ev:30,industrial_machine:20,smart_home:50" (aliases like machine,
                    home, energy, environment, appliance work too)
  SIM_INTERVALS     "ev:2,smart_home:4"  per-profile update interval (s)
  SIM_PROFILE_PARAMS JSON object {"ev": {"battery_capacity_kwh": 60}}
  SIM_SEED, SIM_MAX_TWINS, SIM_FLEET_TAG
  (none of these)   legacy car-only workflow: NUM_CARS electric vehicles named
                    <NAMESPACE>:car-NNN, updating every SEND_INTERVAL_S -- identical ids,
                    feature names and update cadence to the car-only simulator.

Monitoring is split in two, on purpose:
  * system-level health (direct/search availability, latency, error rate, staleness,
    lost acknowledged updates, search drift) -- probe.py's top-level row fields; the kill
    switch reads ONLY these, exactly as before multi-product support.
  * product data quality -- row["profiles"][<profile>], computed here: per-profile
    freshness against that profile's own update interval, lost updates, search drift,
    schema/consistency validity, operational states, and cross-profile signals that are
    reported as NOT APPLICABLE (never zero) where a profile has no such feature.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import config
import profiles

HARD_MAX_TWINS = 200              # one Ditto search page (size(200)); also bounds probe request size
MAX_UPDATES_PER_S = 100.0         # aggregate simulator write rate ceiling
MIN_INTERVAL_S = 0.5
DIRECT_READ_CHUNK = 100           # ids per direct-read request (keeps URLs well under proxy limits)
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class FleetConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("invalid simulator fleet configuration: " + "; ".join(errors))
        self.errors = errors


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=0, le=HARD_MAX_TWINS)
    interval_s: float | None = Field(None, gt=0)
    id_prefix: str | None = None
    params: dict = Field(default_factory=dict)


class FleetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fleet: dict[str, ProfileConfig]
    seed: int | None = None
    max_twins: int = Field(HARD_MAX_TWINS, ge=1, le=HARD_MAX_TWINS)
    fleet_tag: str = "hackathon"


@dataclass(frozen=True)
class TwinSpec:
    id: str
    profile: str
    kind: str
    index: int
    interval_s: float


@dataclass
class Fleet:
    config: FleetConfig
    source: str
    legacy: bool
    twins: list[TwinSpec]
    params: dict            # profile key -> validated Params instance
    intervals: dict         # profile key -> seconds
    prefixes: dict          # profile key -> id prefix

    @property
    def ids(self) -> list[str]:
        return [t.id for t in self.twins]

    def by_id(self) -> dict[str, TwinSpec]:
        return {t.id: t for t in self.twins}

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.twins:
            out[t.profile] = out.get(t.profile, 0) + 1
        return out

    @property
    def signature(self) -> str:
        blob = json.dumps({"twins": [(t.id, t.profile, t.kind, t.interval_s) for t in self.twins],
                           "params": {k: v.model_dump() for k, v in self.params.items()},
                           "seed": self.config.seed, "tag": self.config.fleet_tag}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def freshness_limit_s(self, profile: str) -> float:
        """A twin is stale once its newest stored update is older than 3 of its own update
        intervals plus one probe interval -- per profile, never a car-derived constant."""
        return round(3 * self.intervals[profile] + config.PROBE_INTERVAL_S, 2)


# ---------------------------------------------------------------- loading
def _parse_pairs(raw: str, what: str, errors: list[str], cast=float) -> dict[str, float]:
    out = {}
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        name, sep, value = part.partition(":")
        if not sep:
            errors.append(f"{what}: '{part}' is not name:value")
            continue
        try:
            out[name.strip()] = cast(value.strip())
        except ValueError:
            errors.append(f"{what}: '{value.strip()}' is not a number (in '{part}')")
    return out


def _raw_config(env) -> tuple[dict, str, bool, list[str]]:
    errors: list[str] = []
    path = env.get("SIM_CONFIG_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            raise FleetConfigError([f"SIM_CONFIG_FILE {path}: {e}"])
        if not isinstance(raw, dict):
            raise FleetConfigError([f"SIM_CONFIG_FILE {path}: top level must be an object"])
        raw["fleet"] = {k: ({"count": v} if isinstance(v, int) and not isinstance(v, bool) else v)
                        for k, v in (raw.get("fleet") or {}).items()}
        source, legacy = f"file:{path}", False
    elif env.get("SIM_FLEET", "").strip():
        counts = _parse_pairs(env["SIM_FLEET"], "SIM_FLEET", errors, cast=int)
        raw = {"fleet": {k: {"count": v} for k, v in counts.items()}}
        source, legacy = "env:SIM_FLEET", False
    else:
        raw = {"fleet": {"ev": {"count": config.NUM_CARS, "interval_s": config.SEND_INTERVAL_S}}}
        source, legacy = "legacy:NUM_CARS", True
    if not legacy:
        for name, sec in _parse_pairs(env.get("SIM_INTERVALS", ""), "SIM_INTERVALS", errors).items():
            raw["fleet"].setdefault(name, {"count": 0})["interval_s"] = sec
        if env.get("SIM_PROFILE_PARAMS", "").strip():
            try:
                extra = json.loads(env["SIM_PROFILE_PARAMS"])
                if not isinstance(extra, dict):
                    raise ValueError("must be a JSON object")
                for name, prm in extra.items():
                    raw["fleet"].setdefault(name, {"count": 0}).setdefault("params", {}).update(prm or {})
            except (ValueError, AttributeError, TypeError) as e:
                errors.append(f"SIM_PROFILE_PARAMS: {e}")
    for key, field in (("SIM_SEED", "seed"), ("SIM_MAX_TWINS", "max_twins")):
        if env.get(key, "").strip():
            try:
                raw[field] = int(env[key])
            except ValueError:
                errors.append(f"{key} must be an integer")
    if env.get("SIM_FLEET_TAG", "").strip():
        raw["fleet_tag"] = env["SIM_FLEET_TAG"].strip()
    return raw, source, legacy, errors


def load(env=None) -> Fleet:
    """Parse, validate and plan. Raises FleetConfigError listing EVERY problem found."""
    env = os.environ if env is None else env
    raw, source, legacy, errors = _raw_config(env)
    # resolve aliases before pydantic sees the keys
    resolved: dict[str, dict] = {}
    for name, pc in (raw.get("fleet") or {}).items():
        try:
            key = profiles.get(name).key
        except KeyError as e:
            errors.append(str(e).strip("'\""))
            continue
        if key in resolved:
            errors.append(f"profile '{key}' is configured twice (as '{name}')")
            continue
        resolved[key] = pc
    raw["fleet"] = resolved
    try:
        cfg = FleetConfig(**raw)
    except ValidationError as e:
        raise FleetConfigError(errors + [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                                         for err in e.errors()])
    params, intervals, prefixes = {}, {}, {}
    max_interval = config.MAX_STALE_S / 2
    for key, pc in cfg.fleet.items():
        prof = profiles.REGISTRY[key]
        try:
            params[key] = prof.params(pc.params)
        except ValidationError as e:
            errors += [f"{key}.params.{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        iv = pc.interval_s if pc.interval_s is not None else prof.default_interval_s
        if not MIN_INTERVAL_S <= iv <= max_interval:
            errors.append(f"{key}.interval_s={iv:g} must be between {MIN_INTERVAL_S:g} and {max_interval:g}s "
                          f"(half of MAX_STALE_S, so a healthy fleet never trips the staleness kill switch)")
        intervals[key] = iv
        prefix = pc.id_prefix or prof.id_prefix
        if not _PREFIX_RE.match(prefix):
            errors.append(f"{key}.id_prefix '{prefix}' must match {_PREFIX_RE.pattern}")
        prefixes[key] = prefix
    dup = {p for p in prefixes.values() if list(prefixes.values()).count(p) > 1}
    if dup:
        errors.append(f"id prefixes must be unique per profile (duplicated: {', '.join(sorted(dup))})")
    if any(prefixes[a] != prefixes[b] and (prefixes[a].startswith(prefixes[b] + "-"))
           for a in prefixes for b in prefixes):
        errors.append("one id prefix must not extend another (search filters would overlap)")
    total = sum(pc.count for pc in cfg.fleet.values())
    if total < 1:
        errors.append("the fleet is empty: configure at least one twin")
    if total > cfg.max_twins:
        errors.append(f"{total} twins configured but max_twins is {cfg.max_twins} (hard limit {HARD_MAX_TWINS})")
    rate = sum(pc.count / intervals[k] for k, pc in cfg.fleet.items() if intervals.get(k))
    if rate > MAX_UPDATES_PER_S:
        errors.append(f"aggregate update rate {rate:.0f}/s exceeds {MAX_UPDATES_PER_S:g}/s; "
                      f"lower counts or raise intervals")
    if not _TAG_RE.match(cfg.fleet_tag):
        errors.append(f"fleet_tag must match {_TAG_RE.pattern}")
    if errors:
        raise FleetConfigError(errors)
    twins = [TwinSpec(id=f"{config.NAMESPACE}:{prefixes[k]}-{i:03d}", profile=k,
                      kind=profiles.REGISTRY[k].kind_for(i), index=i, interval_s=intervals[k])
             for k, pc in cfg.fleet.items() for i in range(1, pc.count + 1)]
    return Fleet(config=cfg, source=source, legacy=legacy, twins=twins, params=params,
                 intervals={k: v for k, v in intervals.items() if cfg.fleet[k].count},
                 prefixes={k: v for k, v in prefixes.items() if cfg.fleet[k].count})


_current: Fleet | None = None


def current() -> Fleet:
    """The process-wide fleet, loaded once. reset() forces a reload (tests)."""
    global _current
    if _current is None:
        _current = load()
    return _current


def reset() -> None:
    global _current
    _current = None


def twin_rng(seed: int | None, twin_id: str) -> random.Random:
    """Per-twin generator: reproducible for a fixed seed, independent of fleet order."""
    if seed is None:
        return random.Random()
    return random.Random(int(hashlib.sha256(f"{seed}:{twin_id}".encode()).hexdigest()[:16], 16))


# ---------------------------------------------------------------- Ditto payloads
def thing_payload(fl: Fleet, t: TwinSpec, features: dict) -> dict:
    """Full Thing for PUT /api/2/things/{id}: a thing definition, identity/metadata
    attributes (product type, model, profile, synthetic notice) and the features."""
    prof, p = profiles.REGISTRY[t.profile], fl.params[t.profile]
    attrs = {"fleet": fl.config.fleet_tag, "model": prof.models[t.kind], "productType": prof.product_type,
             "category": prof.title, "deviceKind": t.kind, "manufacturer": "Synthetic (ditto-chaos simulator)",
             "serialNumber": f"SIM-{fl.prefixes[t.profile].upper()}-{t.index:05d}",
             "simulator": {"profile": prof.key, "profileVersion": prof.version, "synthetic": True,
                           "notice": profiles.SYNTHETIC_NOTICE, "index": t.index, "seed": fl.config.seed,
                           "updateIntervalS": t.interval_s, "fleetSignature": fl.signature}}
    attrs.update(prof.attributes(t.kind, p))
    return {"definition": prof.thing_definition(), "attributes": attrs, "features": features}


def features_payload(prof: profiles.Profile, kind: str, rendered: dict, seq: int, now: float) -> dict:
    """PUT /api/2/things/{id}/features body. seq/sentAt go into the profile's heartbeat
    feature; every feature carries its definition identifier."""
    feats = {name: dict(props) for name, props in rendered.items()}
    hb = feats.setdefault(prof.heartbeat_feature, {})
    hb["seq"], hb["sentAt"] = seq, now
    if prof.heartbeat_feature == "telemetry":
        hb["synthetic"] = True
    return {name: {"definition": [prof.definition(name)], "properties": props} for name, props in feats.items()}


# ---------------------------------------------------------------- probe support
def search_filter(fl: Fleet) -> str:
    prefixes = sorted(set(fl.prefixes.values()))
    likes = [f'like(thingId,"{config.NAMESPACE}:{p}-*")' for p in prefixes]
    return likes[0] if len(likes) == 1 else f"or({','.join(likes)})"


def search_fields(fl: Fleet) -> str:
    hbs = sorted({profiles.REGISTRY[k].heartbeat_feature for k in fl.prefixes})
    return ",".join(["thingId"] + [f"features/{h}/properties/seq" for h in hbs])


def heartbeat(thing: dict, prof: profiles.Profile | None) -> tuple:
    """(seq, sentAt) from the twin's heartbeat feature, or (None, None)."""
    feats = thing.get("features") if isinstance(thing.get("features"), dict) else {}
    names = [prof.heartbeat_feature] if prof else ["battery", "telemetry"]
    for n in names:
        f = feats.get(n)
        props = f.get("properties") if isinstance(f, dict) else None
        if isinstance(props, dict) and "seq" in props:
            return props.get("seq"), props.get("sentAt")
    return None, None


def _pct(values: list[float], p: float):
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


_prev_features: dict[str, dict] = {}     # twin id -> features at the previous full check (monotonic rules)
_quality: dict[str, dict] = {}            # twin id -> {"res", "signals", "at"} from the latest full check


def reset_quality_cache() -> None:
    _prev_features.clear()
    _quality.clear()


def heartbeat_fields(fl: Fleet) -> str:
    """The only fields the per-tick direct read needs: seq/sentAt of each heartbeat feature
    (the same small read the car-only probe made)."""
    hbs = sorted({profiles.REGISTRY[k].heartbeat_feature for k in fl.prefixes})
    return ",".join(["thingId"] + [f"features/{h}/properties/{p}" for h in hbs for p in profiles.HEARTBEAT_PROPS])


def profile_metrics(fl: Fleet, things: list | None, search_seqs: dict | None, acked: dict | None,
                    now: float, ids: list[str] | None = None, full: list | None = None) -> tuple[dict, dict]:
    """Per-profile data-quality block for one probe tick, plus per-twin status.

    `things` is the per-tick direct-read body (None when the read failed -> every metric is
    None, i.e. not measured); only heartbeats are needed from it. `full` is a complete read
    of the twins (attributes + features) when this tick runs the schema/consistency check;
    otherwise validity and signals come from each twin's latest full check, and a twin never
    checked yet counts as not validated (neither valid nor invalid).
    `search_seqs` maps id -> seq from search (None when search failed).
    """
    full_by_id = {t.get("thingId"): t for t in (full or []) if isinstance(t, dict)} if full is not None else None
    wanted = set(ids) if ids is not None else None
    specs = [t for t in fl.twins if wanted is None or t.id in wanted]
    stored = {t.get("thingId"): t for t in (things or []) if isinstance(t, dict)} if things is not None else None
    out, twin_status = {}, {}
    for key in fl.prefixes:
        prof = profiles.REGISTRY[key]
        mine = [t for t in specs if t.profile == key]
        if not mine:
            continue
        limit = fl.freshness_limit_s(key)
        block = {"title": prof.title, "product_type": prof.product_type, "twins": len(mine),
                 "interval_s": fl.intervals[key], "freshness_limit_s": limit, "measured": stored is not None,
                 "observed": None, "missing": None, "fresh": None, "stale": None, "fresh_ratio": None,
                 "stale_p95_s": None, "stale_max_s": None, "latest_sent_at": None, "lost_acked": None,
                 "search_drift": None, "valid": None, "invalid": None, "valid_ratio": None,
                 "violations": {}, "missing_features": 0, "operational": {}, "signals": {}}
        for sig in profiles.SIGNALS:
            block["signals"][sig] = {"applicable": prof.signal_applicable(sig)}
        if stored is None:
            out[key] = block
            continue
        ages, fresh, observed, valid, checked, latest, oldest_check = [], 0, 0, 0, 0, None, None
        sig_vals: dict[str, list[float]] = {s: [] for s in profiles.SIGNALS if prof.signal_applicable(s)}
        lost = 0 if acked is not None else None
        drift = 0 if search_seqs is not None else None
        for t in mine:
            thing = stored.get(t.id)
            if thing is None:
                # absent from the direct read: counted under `missing`, not as search drift
                twin_status[t.id] = {"profile": key, "kind": t.kind, "present": False, "fresh": False}
                continue
            observed += 1
            seq, sent = heartbeat(thing, prof)
            age = round(now - sent, 2) if _num(sent) is not None else None
            is_fresh = age is not None and age <= limit
            if age is not None:
                ages.append(age)
                latest = sent if latest is None or sent > latest else latest
            fresh += int(is_fresh)
            if lost is not None and t.id in acked and _num(seq) is not None and seq < acked[t.id]:
                lost += 1
            if drift is not None and _num(seq) is not None:
                s_seq = search_seqs.get(t.id)
                if s_seq is None or seq - s_seq > config.SEARCH_DRIFT_TOLERANCE:
                    drift += 1
            ft = full_by_id.get(t.id) if full_by_id is not None else None
            if ft is not None:
                feats = ft.get("features") if isinstance(ft.get("features"), dict) else {}
                res = profiles.validate_twin(prof, t.kind, ft, _prev_features.get(t.id), now)
                _prev_features[t.id] = feats
                view = profiles.View(feats)
                _quality[t.id] = {"res": res, "at": now,
                                  "signals": {s: _num(view(prof.signal_path(s, t.kind)))
                                              for s in sig_vals if prof.signal_path(s, t.kind)}}
            q = _quality.get(t.id)
            ok = None
            if q is not None:
                res = q["res"]
                checked += 1
                ok = not res["violations"] and not res["missing_features"]
                valid += int(ok)
                oldest_check = q["at"] if oldest_check is None else min(oldest_check, q["at"])
                for v in res["violations"]:
                    block["violations"][v] = block["violations"].get(v, 0) + 1
                block["missing_features"] += len(res["missing_features"])
                op = res["operational"] or "unknown"
                block["operational"][op] = block["operational"].get(op, 0) + 1
                for sig, val in q["signals"].items():
                    if val is not None:
                        sig_vals[sig].append(val)
            twin_status[t.id] = {"profile": key, "kind": t.kind, "present": True, "seq": seq, "sent_at": sent,
                                 "age_s": age, "fresh": is_fresh, "valid": ok,
                                 "violations": q["res"]["violations"][:8] if q else [],
                                 "missing_features": q["res"]["missing_features"] if q else [],
                                 "operational": q["res"]["operational"] if q else None,
                                 "validated_at": q["at"] if q else None}
        block.update(observed=observed, missing=len(mine) - observed, fresh=fresh, stale=len(mine) - fresh,
                     fresh_ratio=round(fresh / len(mine), 4), valid=valid if checked else None,
                     invalid=checked - valid if checked else None,
                     valid_ratio=round(valid / checked, 4) if checked else None, validated=checked,
                     validation_age_s=round(now - oldest_check, 1) if oldest_check is not None else None,
                     stale_p95_s=round(_pct(ages, 95), 2) if ages else None,
                     stale_max_s=round(max(ages), 2) if ages else None, latest_sent_at=latest,
                     lost_acked=lost, search_drift=drift)
        for sig, vals in sig_vals.items():
            # applicable to the profile, but this device kind mix may still have no value this tick
            block["signals"][sig].update({"n": len(vals), "min": round(min(vals), 2) if vals else None,
                                          "max": round(max(vals), 2) if vals else None,
                                          "mean": round(sum(vals) / len(vals), 2) if vals else None})
        out[key] = block
    return out, twin_status


def summarize_profiles(rows: list[dict]) -> dict:
    """Window summary per profile (same None-means-not-measured rule as probe.summarize)."""
    keys: list[str] = []
    for r in rows:
        for k in (r.get("profiles") or {}):
            if k not in keys:
                keys.append(k)
    out = {}
    for k in keys:
        blocks = [(r.get("profiles") or {}).get(k) for r in rows]
        measured = [b for b in blocks if b and b.get("measured")]

        def col(name):
            return [b[name] for b in measured if _num(b.get(name)) is not None]
        first = next((b for b in blocks if b), {})
        out[k] = {"title": first.get("title"), "twins": first.get("twins"), "samples": len(rows),
                  "measured_samples": len(measured),
                  "freshness_limit_s": first.get("freshness_limit_s"),
                  "stale_p95_s": _pct(col("stale_p95_s"), 95),
                  "fresh_ratio_min": min(col("fresh_ratio"), default=None),
                  "fresh_ratio_mean": round(sum(col("fresh_ratio")) / len(col("fresh_ratio")), 4)
                  if col("fresh_ratio") else None,
                  "valid_ratio_min": min(col("valid_ratio"), default=None),
                  "lost_acked_max": max(col("lost_acked"), default=None),
                  "search_drift_max": max(col("search_drift"), default=None),
                  "missing_max": max(col("missing"), default=None)}
    return out


# Per-profile impact thresholds (measured deltas between the before window and the fault window).
IMPACT_FRESH_DROP = 0.2       # fresh twin ratio falls by 20 percentage points or more
IMPACT_STALE_DELTA_S = 5.0    # p95 staleness rises by 5 s or more
IMPACT_VALID_DROP = 0.2


def profile_impact(timeline: list[dict], scope: list[str] | None = None) -> dict | None:
    """Impact of an experiment on each product profile, from the record's own probe ticks.

    Every profile in the fleet is reported, whether or not it was in the observation scope:
    a fault on a shared service is not limited to the profiles someone chose to watch.
    Returns None for records without per-profile data (legacy car-only records)."""
    rows = [r for r in timeline or [] if isinstance(r, dict)]
    if not any(r.get("profiles") for r in rows):
        return None
    phases = {ph: [r for r in rows if r.get("phase") == ph] for ph in ("before", "fault", "recovery")}
    sums = {ph: summarize_profiles(rs) if rs else {} for ph, rs in phases.items()}
    keys = list(dict.fromkeys([k for s in sums.values() for k in s]))
    out = {}
    for k in keys:
        b, f, rc = sums["before"].get(k) or {}, sums["fault"].get(k) or {}, sums["recovery"].get(k) or {}
        symptoms, notes = [], []
        if not f.get("measured_samples"):
            status = "not_measured"
            notes.append("no direct-read data for this profile while the fault was active "
                         "(the system-level read failed), so its twin data could not be inspected")
        else:
            def drop(name):
                x, y = b.get(name), f.get(name)
                return None if x is None or y is None else round(x - y, 4)
            fd, vd = drop("fresh_ratio_mean"), drop("valid_ratio_min")
            sd = (None if b.get("stale_p95_s") is None or f.get("stale_p95_s") is None
                  else round(f["stale_p95_s"] - b["stale_p95_s"], 2))
            if fd is not None and fd >= IMPACT_FRESH_DROP:
                symptoms.append("freshness_loss")
            if sd is not None and sd >= IMPACT_STALE_DELTA_S:
                symptoms.append("staleness")
            if (f.get("lost_acked_max") or 0) > 0:
                symptoms.append("data_loss")
            if (f.get("search_drift_max") or 0) > (b.get("search_drift_max") or 0):
                symptoms.append("search_drift")
            if vd is not None and vd >= IMPACT_VALID_DROP:
                symptoms.append("data_quality")
            status = "affected" if symptoms else "no_measured_impact"
        rec_rows = [((r.get("profiles") or {}).get(k) or {}) for r in phases["recovery"]]
        tail = [x for x in rec_rows if x.get("measured")][-2:]
        if not phases["recovery"]:
            recovered = None
        elif len(tail) < 2:
            recovered = False
            notes.append("fewer than two measured recovery ticks for this profile")
        else:
            base_fresh = b.get("fresh_ratio_mean") or 0
            recovered = all((x.get("fresh_ratio") or 0) >= base_fresh - 0.1 and not (x.get("lost_acked") or 0)
                            for x in tail)
        out[k] = {"title": b.get("title") or f.get("title"), "twins": b.get("twins") or f.get("twins"),
                  "in_scope": scope is None or k in scope, "status": status, "symptoms": symptoms,
                  "before": b or None, "fault": f or None, "recovery": rc or None,
                  "recovered": recovered, "notes": notes}
    affected = [k for k, v in out.items() if v["status"] == "affected"]
    measured = [k for k, v in out.items() if v["status"] != "not_measured"]
    common = sorted(set.intersection(*(set(out[k]["symptoms"]) for k in measured))) if measured else []
    return {"profiles": out, "scope": scope, "affected_profiles": affected,
            "unaffected_profiles": [k for k in measured if k not in affected],
            "not_measured_profiles": [k for k in out if k not in measured],
            "system_wide_symptoms": common if len(measured) > 1 else [],
            "basis": "per-profile probe ticks recorded in this experiment's timeline"}


# ---------------------------------------------------------------- descriptions
def record_metadata(fl: Fleet | None = None) -> dict | None:
    """What gets stored in every new experiment record (`record["fleet"]`)."""
    try:
        fl = fl or current()
    except FleetConfigError as e:
        return {"error": str(e)}
    return {"signature": fl.signature, "source": fl.source, "legacy_car_only": fl.legacy,
            "total_twins": len(fl.twins), "seed": fl.config.seed, "synthetic": True,
            "profiles": {k: {"title": profiles.REGISTRY[k].title, "product_type": profiles.REGISTRY[k].product_type,
                             "count": n, "interval_s": fl.intervals[k], "id_prefix": fl.prefixes[k],
                             "kinds": sorted({t.kind for t in fl.twins if t.profile == k})}
                         for k, n in fl.counts().items()}}


def describe(fl: Fleet | None = None) -> dict:
    fl = fl or current()
    return {**record_metadata(fl), "namespace": config.NAMESPACE, "fleet_tag": fl.config.fleet_tag,
            "max_twins": fl.config.max_twins, "hard_max_twins": HARD_MAX_TWINS,
            "signals": profiles.SIGNALS, "synthetic_notice": profiles.SYNTHETIC_NOTICE,
            "registry": {k: p.describe() for k, p in profiles.REGISTRY.items()},
            "enabled": list(fl.prefixes),
            "params": {k: v.model_dump() for k, v in fl.params.items()}}


def health_fields() -> dict:
    """Fleet fields merged into every health payload. `num_cars` keeps its car-only
    meaning (number of EV twins) so older dashboards and scripts stay correct."""
    try:
        fl = current()
    except FleetConfigError as e:
        return {"num_cars": 0, "num_twins": None, "fleet": {"error": str(e)}}
    return {"num_cars": fl.counts().get("ev", 0), "num_twins": len(fl.twins),
            "fleet": {"signature": fl.signature, "profiles": fl.counts(), "legacy_car_only": fl.legacy,
                      "source": fl.source, "synthetic": True}}


def slim_row(row: dict) -> dict:
    """Probe row for charts and the live stream: per-profile metrics kept, the bulky
    per-check violation maps and signal ranges dropped (the latest full block is served by
    GET /api/fleet)."""
    prof = row.get("profiles")
    if not isinstance(prof, dict):
        return row
    slim = {}
    for k, b in prof.items():
        if not isinstance(b, dict):
            continue
        sigs = {s: ({"applicable": True, "mean": v.get("mean")} if v.get("applicable") else {"applicable": False})
                for s, v in (b.get("signals") or {}).items() if isinstance(v, dict)}
        slim[k] = {**{x: y for x, y in b.items() if x not in ("violations", "signals")},
                   "violation_count": sum((b.get("violations") or {}).values()), "signals": sigs}
    return {**row, "profiles": slim}
