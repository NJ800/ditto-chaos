"""Dependency Discovery Agent: maps the REAL running Ditto deployment.

Deliberately simple and deterministic -- no AI involved. It:
  1. Asks Docker (read-only `docker ps`) which containers are actually running in
     COMPOSE_PROJECT right now -- this part is OBSERVED fact, not a guess.
  2. Overlays Eclipse Ditto's documented service architecture (gateway routes to
     things/things-search/policies/connectivity; those four persist to mongodb) --
     this part is KNOWN-ARCHITECTURE, i.e. "true per Ditto's own docs", not
     independently confirmed here by watching live traffic.
  3. Applies any manual overrides from overrides.yaml, if that file exists --
     MANUAL-OVERRIDE, the highest-confidence source since a human asserted it.
  4. Computes fan-in/fan-out from the resulting edge list (never hardcoded).

Every edge in the output is tagged with exactly one of these three sources, so a
reader can see what's actually verified versus merely documented versus asserted by
a teammate.

Run:  python main.py map
   or python dependency_mapper.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

import config
import guard  # only for COMPOSE_PROJECT and ALLOWLIST constants -- read-only use

# Eclipse Ditto's documented architecture (https://eclipse.dev/ditto/architecture-overview.html):
# the gateway is the single HTTP/WS entry point and routes to the backend services below it;
# things/things-search/policies/connectivity each persist their own state to MongoDB.
KNOWN_ARCHITECTURE_EDGES: list[tuple[str, str, str]] = [
    ("gateway", "things", "gateway routes thing CRUD requests to the things service"),
    ("gateway", "things-search", "gateway routes search queries to the things-search service"),
    ("gateway", "policies", "gateway routes policy requests to the policies service"),
    ("gateway", "connectivity", "gateway routes connection-management requests to connectivity"),
    ("things", "mongodb", "things persists digital-twin state to MongoDB"),
    ("things-search", "mongodb", "things-search persists its search index to MongoDB"),
    ("policies", "mongodb", "policies persists authorization policies to MongoDB"),
    ("connectivity", "mongodb", "connectivity persists connection configuration to MongoDB"),
    ("things-search", "things", "things-search consumes change events from things to stay in sync"),
]

# Services this specific demo's docker-compose.yml is documented to add (README.md) that
# aren't part of Ditto's own core backend -- not on the simulated fleet's data path.
NON_CORE_SERVICES = {"nginx", "swagger-ui", "ditto-ui"}


def _docker_ps(compose_project: str) -> list[dict]:
    """Read-only: list every container in this compose project, running or not."""
    r = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={compose_project}",
         "--format", "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label \"com.docker.compose.service\"}}"],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"docker ps failed: {r.stderr.strip()}")
    out = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        name, image, state, service = (line.split("\t") + ["", "", "", ""])[:4]
        out.append({"container": name, "image": image, "state": state.strip().lower(),
                    "service": service or name})
    return out


def _load_overrides(path: str = "overrides.yaml") -> list[dict]:
    """Optional manual dependency overrides. Minimal line-based parser (no PyYAML
    dependency) supporting exactly:
        edges:
          - from: serviceA
            to: serviceB
            note: why this edge exists
    Returns [] if the file doesn't exist or can't be parsed -- this is optional
    input, never required for the mapper to run."""
    if not os.path.isfile(path):
        return []
    try:
        import re
        edges = []
        current: dict = {}
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip()
                m = re.match(r"^\s*-\s*from:\s*(\S+)", line)
                if m:
                    if current:
                        edges.append(current)
                    current = {"from": m.group(1), "source": "manual-override"}
                    continue
                m = re.match(r"^\s*to:\s*(\S+)", line)
                if m and current:
                    current["to"] = m.group(1)
                    continue
                m = re.match(r"^\s*note:\s*(.+)", line)
                if m and current:
                    current["note"] = m.group(1).strip().strip('"').strip("'")
            if current:
                edges.append(current)
        return [e for e in edges if "from" in e and "to" in e]
    except OSError:
        return []


def build_dependency_map(compose_project: str | None = None) -> dict:
    compose_project = compose_project or guard.COMPOSE_PROJECT
    try:
        observed_containers = _docker_ps(compose_project)
        docker_error = None
    except (RuntimeError, OSError) as e:
        observed_containers = []
        docker_error = str(e)

    observed_services = {c["service"] for c in observed_containers}
    services = []
    for c in observed_containers:
        services.append({
            "name": c["service"], "container": c["container"], "image": c["image"],
            "state": c["state"], "running": c["state"] == "running",
            "role": "core-ditto-backend" if c["service"] not in NON_CORE_SERVICES else "support/ui",
            "in_fault_allowlist": c["service"] in guard.ALLOWLIST,
        })

    edges = []
    for src, dst, note in KNOWN_ARCHITECTURE_EDGES:
        edges.append({
            "from": src, "to": dst, "source": "known-architecture", "note": note,
            "both_ends_observed_running": src in observed_services and dst in observed_services,
        })
    for e in _load_overrides():
        edges.append({"from": e["from"], "to": e["to"], "source": e.get("source", "manual-override"),
                      "note": e.get("note", ""),
                      "both_ends_observed_running": e["from"] in observed_services and e["to"] in observed_services})

    fan_in: dict[str, int] = {}
    fan_out: dict[str, int] = {}
    for e in edges:
        fan_out[e["from"]] = fan_out.get(e["from"], 0) + 1
        fan_in[e["to"]] = fan_in.get(e["to"], 0) + 1

    recommendations = []
    for svc in sorted(observed_services):
        risk = "low"
        reasons = []
        fi = fan_in.get(svc, 0)
        if fi >= 3:
            risk = "high"
            reasons.append(f"{fi} other services depend on it (highest fan-in in this deployment)")
        elif fi >= 1:
            risk = "medium"
            reasons.append(f"{fi} other service(s) depend on it")
        else:
            reasons.append("nothing in the known architecture depends on it directly")
        if svc not in guard.ALLOWLIST:
            reasons.append("NOT in guard.py's ALLOWLIST -- guard.py will refuse to target it")
        recommendations.append({
            "service": svc, "fan_in": fi, "fan_out": fan_out.get(svc, 0),
            "calculated_risk": risk, "allowlisted_for_faults": svc in guard.ALLOWLIST,
            "reasoning": "; ".join(reasons),
        })

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "compose_project": compose_project,
        "docker_query_error": docker_error,
        "services": services,
        "edges": edges,
        "fan_in": fan_in,
        "fan_out": fan_out,
        "fault_target_recommendations": recommendations,
    }


def to_mermaid(dep_map: dict) -> str:
    lines = ["flowchart TD"]
    for s in dep_map["services"]:
        shape = f'["{s["name"]}"]' if s["role"] == "core-ditto-backend" else f'("{s["name"]}")'
        lines.append(f'    {_mid(s["name"])}{shape}')
    for e in dep_map["edges"]:
        style = "-->" if e["source"] == "known-architecture" else "-.->"
        label = "" if e["source"] == "known-architecture" else f"|{e['source']}|"
        lines.append(f'    {_mid(e["from"])} {style}{label} {_mid(e["to"])}')
    return "\n".join(lines)


def _mid(name: str) -> str:
    return name.replace("-", "_")


def to_markdown(dep_map: dict) -> str:
    lines = [
        "# Dependency map (auto-generated, deterministic -- no AI)\n",
        f"Generated: {dep_map['generated_at_utc']}  \n"
        f"Compose project: `{dep_map['compose_project']}`\n",
    ]
    if dep_map["docker_query_error"]:
        lines.append(f"> **Could not query Docker:** {dep_map['docker_query_error']}\n"
                      f"> The edges below are from Ditto's known architecture only -- not confirmed "
                      f"against what's actually running.\n")
    lines.append("## Observed containers\n")
    lines.append("| service | container | image | state | role | fault-allowlisted |")
    lines.append("|---|---|---|---|---|---|")
    for s in dep_map["services"]:
        lines.append(f"| {s['name']} | {s['container']} | {s['image']} | {s['state']} | "
                      f"{s['role']} | {'yes' if s['in_fault_allowlist'] else 'no'} |")
    lines.append("\n## Dependency edges\n")
    lines.append("| from | to | source | both ends observed running | note |")
    lines.append("|---|---|---|---|---|")
    for e in dep_map["edges"]:
        lines.append(f"| {e['from']} | {e['to']} | {e['source']} | "
                      f"{'yes' if e['both_ends_observed_running'] else 'no'} | {e['note']} |")
    lines.append("\n## Fault-target recommendations (risk calculated from fan-in, not asserted)\n")
    lines.append("| service | fan-in | fan-out | calculated risk | allowlisted | reasoning |")
    lines.append("|---|---|---|---|---|---|")
    for r in dep_map["fault_target_recommendations"]:
        lines.append(f"| {r['service']} | {r['fan_in']} | {r['fan_out']} | {r['calculated_risk']} | "
                      f"{'yes' if r['allowlisted_for_faults'] else 'no'} | {r['reasoning']} |")
    lines.append("\n## Diagram\n\n```mermaid\n" + to_mermaid(dep_map) + "\n```\n")
    lines.append("Solid arrows = known Ditto architecture (from Ditto's own docs). "
                  "Dashed/labeled arrows = manual overrides from overrides.yaml, if any.\n")
    return "\n".join(lines)


def save(dep_map: dict, out_dir: str | None = None) -> tuple[str, str]:
    out_dir = out_dir or config.DEPENDENCY_MAP_DIR
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "dependency_map.json")
    md_path = os.path.join(out_dir, "dependency_map.md")
    tmp = json_path + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dep_map, f, indent=2)
    os.replace(tmp, json_path)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(to_markdown(dep_map))
    return json_path, md_path


if __name__ == "__main__":
    m = build_dependency_map()
    jp, mp = save(m)
    print(f"[mapper] wrote {jp}")
    print(f"[mapper] wrote {mp}")
    if m["docker_query_error"]:
        print(f"[mapper] WARNING: {m['docker_query_error']}")
