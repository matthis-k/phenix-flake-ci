#!/usr/bin/env python3
"""Generate a check DAG from real Nix derivations and execute it once.

Nix, not this script, schedules runnable derivations and prevents duplicate
builds in a single store. This script records dependency proof and exact-head
result evidence; it never invents package or check dependencies.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# Import the existing authoritative check inventory/digest validator.
import importlib.util

module = Path(__file__).with_name("flake-check-plan.py")
spec = importlib.util.spec_from_file_location("flake_check_plan", module)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)

SCHEMA = 1
STORE = planner.STORE


def save(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(planner.canonical(payload) + "\n")


def graph_summary(plan, raw):
    """Check-level dependency closure through intermediate Nix derivations."""
    # Nix >=2.33 emits protocol v4 as {"version": 4,
    # "derivations": { "<store-hash-name>": {...} }}. Earlier versions
    # emitted the path map directly. Both contain inputDrvs.
    if isinstance(raw, dict) and "derivations" in raw:
        if not isinstance(raw["derivations"], dict):
            raise ValueError("invalid versioned derivation graph")
        raw = raw["derivations"]
    if not isinstance(raw, dict) or not raw or len(raw) > 100000:
        raise ValueError("invalid or unbounded recursive derivation graph")
    def normalize(path):
        # Nix 2.2x and 2.3x JSON formats differ: newer derivation JSON
        # sometimes uses store-relative derivation keys.
        if not isinstance(path, str):
            raise ValueError("non-string Nix derivation reference")
        full = path if path.startswith("/nix/store/") else "/nix/store/" + path
        if not STORE.fullmatch(full) or not full.endswith(".drv"):
            raise ValueError(f"invalid derivation graph node: {path!r}")
        return full

    parents = {}
    for raw_drv, attrs in raw.items():
        drv = normalize(raw_drv)
        if drv in parents:
            raise ValueError("derivation graph has duplicate normalized paths")
        if not isinstance(attrs, dict):
            raise ValueError(f"invalid derivation record: {raw_drv}")
        # Nix >= 2.33 uses JSON protocol v4, with inputs.drvs and a
        # top-level versioned derivations map. Older versions use inputDrvs.
        if "inputs" in attrs:
            inputs = attrs["inputs"]
            dependencies = inputs.get("drvs") if isinstance(inputs, dict) else None
        else:
            dependencies = attrs.get("inputDrvs")
        if not isinstance(dependencies, dict):
            raise ValueError(f"derivation lacks valid input dependencies: {raw_drv}")
        parents[drv] = set(map(normalize, dependencies))
    for drv, deps in parents.items():
        if not deps <= parents.keys():
            raise ValueError(f"incomplete recursive graph for {drv}")
    if not set(plan["checks"].values()) <= parents.keys():
        raise ValueError("Nix graph is missing declared check derivations")

    visiting, memo = set(), {}
    def ancestors(node):
        if node in visiting:
            raise ValueError("cyclic Nix derivation graph")
        if node in memo:
            return memo[node]
        visiting.add(node)
        found = set(parents[node])
        for dep in parents[node]:
            found.update(ancestors(dep))
        visiting.remove(node)
        memo[node] = frozenset(found)
        return memo[node]

    checks = plan["checks"]
    deps = {
        name: sorted(
            other for other, drv in checks.items()
            if other != name and drv in ancestors(checks[name])
        )
        for name in sorted(checks)
    }
    # Checks with identical drv paths have the same realized build but remain
    # independent named evidence entries. They are not dependency edges.
    waves = {}
    visiting_checks = set()
    def wave(name):
        if name in waves:
            return waves[name]
        if name in visiting_checks:
            raise ValueError("cyclic check dependency graph")
        visiting_checks.add(name)
        value = max((wave(dep) + 1 for dep in deps[name]), default=0)
        visiting_checks.remove(name)
        waves[name] = value
        return value
    for name in sorted(checks):
        wave(name)
    tiers = [
        sorted(name for name in checks if waves[name] == w)
        for w in range(max(waves.values()) + 1)
    ]
    return {
        "schemaVersion": SCHEMA,
        "planDigest": plan["digest"],
        "commit": plan["commit"],
        "system": plan["system"],
        "graphDigest": planner.digest({drv: sorted(deps) for drv, deps in sorted(parents.items())}),
        # This adjacency list is safe to cache and later use for automatic
        # shared-frontier selection, without reevaluating all the derivations.
        "derivationDependencies": {drv: sorted(deps) for drv, deps in sorted(parents.items())},
        "derivationCount": len(parents),
        "checkDependencies": deps,
        "checkWaves": tiers,
        "criticalCheckDepth": max(waves.values()),
    }


def analyze_command(args):
    plan = json.loads(Path(args.plan).read_text())
    planner.validate_plan(plan)
    if not args.graph:
        # Ask Nix for the actual recursive graph. This is evaluation only;
        # no store outputs are built or substituted by the planner.
        paths = sorted(set(plan["checks"].values()))
        raw = subprocess.check_output(
            ["nix", "derivation", "show", "--recursive", "--stdin"],
            input="\n".join(paths) + "\n", text=True,
        )
        graph = json.loads(raw)
    else:
        graph = json.loads(Path(args.graph).read_text())
    report = graph_summary(plan, graph)
    save(args.output, report)
    print(f"DAG: {report['derivationCount']} derivations, "
          f"{len(plan['checks'])} named checks, "
          f"{len(report['checkWaves'])} logical check waves")


def parse_nix_results(result, checks):
    """Associate --json outputs with evaluated drv identities, fail closed."""
    if not isinstance(result, list) or not result:
        raise ValueError("Nix --json returned no build results")
    expected = set(checks.values())
    realized = {}
    for item in result:
        if not isinstance(item, dict):
            raise ValueError("invalid Nix build result")
        drv = item.get("drvPath")
        if drv not in expected:
            raise ValueError(f"unexpected Nix result derivation: {drv!r}")
        outs = item.get("outputs")
        if not isinstance(outs, dict) or not outs:
            raise ValueError(f"Nix build result lacks outputs for {drv}")
        paths = []
        for output in outs.values():
            path = output.get("path") if isinstance(output, dict) else output
            if not isinstance(path, str) or not STORE.fullmatch(path):
                raise ValueError(f"invalid realized Nix output: {path!r}")
            paths.append(path)
        values = sorted(set(paths))
        if drv in realized and realized[drv] != values:
            raise ValueError(f"inconsistent repeated Nix result for {drv}")
        realized[drv] = values
    if set(realized) != expected:
        raise ValueError("Nix --json omitted required check derivations")
    return realized


def build_command(args):
    plan = json.loads(Path(args.plan).read_text())
    planner.validate_plan(plan)
    graph = json.loads(Path(args.graph).read_text())
    if (graph.get("schemaVersion") != SCHEMA
            or graph.get("planDigest") != plan["digest"]
            or graph.get("commit") != plan["commit"]
            or graph.get("system") != plan["system"]
            or set(graph.get("checkDependencies", {})) != set(plan["checks"])):
        raise ValueError("stale, invalid or incomplete DAG plan")
    commit = planner.run_command(["git", "rev-parse", "HEAD"])
    if commit != plan["commit"]:
        raise ValueError("DAG builder checkout does not match plan commit")
    checks = plan["checks"]
    targets = [f".#checks.{plan['system']}.{name}" for name in sorted(checks)]
    # Nix evaluates all outputs together, scheduling every ready dependency
    # exactly once on this runner. No check is skipped and no manual wave
    # barriers serialize independent derivations.
    raw = subprocess.check_output(
        ["nix", "build", "--no-link", "--json", "-L", *targets], text=True,
    )
    realized = parse_nix_results(json.loads(raw), checks)
    evidence = {
        "schemaVersion": SCHEMA,
        "planDigest": plan["digest"],
        "graphDigest": graph["graphDigest"],
        "commit": plan["commit"],
        "system": plan["system"],
        "mode": "native-nix-dag",
        "checks": {
            name: {"drv": drv, "status": "pass", "outputs": realized[drv]}
            for name, drv in sorted(checks.items())
        },
    }
    save(args.output, evidence)
    print(f"PASS: {len(checks)} exact-head checks, "
          f"{len(realized)} unique check derivations, one Nix build graph")


def verify_command(args):
    plan = json.loads(Path(args.plan).read_text())
    planner.validate_plan(plan)
    graph = json.loads(Path(args.graph).read_text())
    evidence = json.loads(Path(args.evidence).read_text())
    if any(evidence.get(k) != value for k, value in {
        "schemaVersion": SCHEMA, "planDigest": plan["digest"],
        "graphDigest": graph.get("graphDigest"), "commit": plan["commit"],
        "system": plan["system"], "mode": "native-nix-dag",
    }.items()):
        raise ValueError("stale DAG build evidence")
    if graph.get("planDigest") != plan["digest"]:
        raise ValueError("stale DAG dependency graph")
    checks = evidence.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(plan["checks"]):
        raise ValueError("DAG build evidence does not cover all evaluated checks")
    for name, item in checks.items():
        if (item.get("drv") != plan["checks"][name]
                or item.get("status") != "pass"
                or not isinstance(item.get("outputs"), list)
                or not item["outputs"]
                or not all(isinstance(p, str) and STORE.fullmatch(p)
                           for p in item["outputs"])):
            raise ValueError(f"invalid DAG evidence for {name}")
    print(f"PASS: {len(checks)} checked derivations, complete exact-head DAG evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    a = subs.add_parser("analyze")
    a.add_argument("--plan", required=True)
    a.add_argument("--graph", help="Supply recursive Nix JSON graph for tests")
    a.add_argument("--output", required=True)
    a.set_defaults(func=analyze_command)
    b = subs.add_parser("build")
    b.add_argument("--plan", required=True)
    b.add_argument("--graph", required=True)
    b.add_argument("--output", required=True)
    b.set_defaults(func=build_command)
    c = subs.add_parser("verify")
    c.add_argument("--plan", required=True)
    c.add_argument("--graph", required=True)
    c.add_argument("--evidence", required=True)
    c.set_defaults(func=verify_command)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, KeyError, OSError, TypeError, subprocess.CalledProcessError,
            json.JSONDecodeError) as exc:
        print(f"flake-check-dag: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
