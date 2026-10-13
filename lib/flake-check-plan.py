#!/usr/bin/env python3
"""Exact-head Nix check inventory, deterministic shards and fail-closed evidence.

The consumer flake defines checks; this tool only schedules their derivations.
No Cargo or language-specific assumptions belong in this module.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
CHECK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+.-]*$")
STORE = re.compile(r"^/nix/store/[a-z0-9]{32}-[^\s]+$")


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(data):
    return hashlib.sha256(canonical(data).encode()).hexdigest()


def run_command(args):
    return subprocess.check_output(args, text=True).strip()


def inventory(flake, system):
    # Query the actual Nix check set. This is not a second list of check names.
    expr = "checks: builtins.mapAttrs (_: drv: drv.drvPath) checks"
    raw = run_command(["nix", "eval", "--json", "--apply", expr,
                       f"{flake}#checks.{system}"])
    return json.loads(raw)


def validate_inventory(checks):
    if not isinstance(checks, dict) or not checks:
        raise ValueError("Nix inventory must be a non-empty check attribute set")
    for name, drv in checks.items():
        if not isinstance(name, str) or not CHECK.fullmatch(name):
            raise ValueError(f"invalid Nix check name {name!r}")
        if not isinstance(drv, str) or not drv.endswith(".drv") or not drv.startswith("/nix/store/"):
            raise ValueError(f"invalid derivation for {name!r}: {drv!r}")


def hint_for(name, hints):
    result = {}
    for pattern in sorted(hints):
        if fnmatch.fnmatchcase(name, pattern):
            result.update(hints[pattern])
    return result


def make_plan(checks, config, commit):
    validate_inventory(checks)
    if not isinstance(config, dict) or config.get("schemaVersion") != SCHEMA:
        raise ValueError("unsupported check configuration schema")
    system = config.get("system")
    if not isinstance(system, str) or not re.fullmatch(r"[a-z0-9_-]+", system):
        raise ValueError("invalid Nix system")
    count = config.get("shardCount")
    if type(count) is not int or count < 1 or count > 64:
        raise ValueError("shardCount must be between 1 and 64")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit must be the 40-character checked-out Git SHA")
    hints = config.get("checkHints", {})
    if not isinstance(hints, dict):
        raise ValueError("checkHints must be an attribute set")
    for pattern, hint in hints.items():
        if not isinstance(pattern, str) or not isinstance(hint, dict):
            raise ValueError("each check hint must be an attribute set")
        if set(hint) - {"weight", "isolate"}:
            raise ValueError("unknown check hint fields")
        if "weight" in hint and (type(hint["weight"]) is not int or hint["weight"] < 1):
            raise ValueError("check weight must be a positive integer")
        if "isolate" in hint and type(hint["isolate"]) is not bool:
            raise ValueError("check isolate flag must be boolean")

    # Isolated checks reserve one whole worker, typically for shipping checks.
    weighted = [(name, hint_for(name, hints)) for name in sorted(checks)]
    isolated = [(name, h) for name, h in weighted if h.get("isolate", False)]
    rest = [(name, h) for name, h in weighted if not h.get("isolate", False)]
    if len(isolated) > count or (rest and len(isolated) == count):
        raise ValueError("not enough shards for isolated checks")
    shards = [{"id": f"shard-{i:02d}", "checks": [], "weight": 0}
              for i in range(min(count, len(isolated) + len(rest)))]
    for slot, (name, h) in zip(shards, isolated):
        slot["checks"] = [name]
        slot["weight"] = h.get("weight", 1)
    available = shards[len(isolated):]
    # Longest-processing-time-first, fixed tie-breaking and integer weights.
    for name, h in sorted(rest, key=lambda pair: (-pair[1].get("weight", 1), pair[0])):
        target = min(available, key=lambda slot: (slot["weight"], slot["id"]))
        target["checks"].append(name)
        target["weight"] += h.get("weight", 1)
    shards = [s for s in shards if s["checks"]]
    for shard in shards:
        shard["checks"].sort()
    result = {
        "schemaVersion": SCHEMA,
        "commit": commit,
        "system": system,
        "checks": dict(sorted(checks.items())),
        "shards": shards,
    }
    result["digest"] = digest(result)
    return result


def validate_plan(plan):
    checksum = plan.get("digest")
    if checksum != digest({k: v for k, v in plan.items() if k != "digest"}):
        raise ValueError("plan digest mismatch")
    validate_inventory(plan.get("checks"))
    seen = []
    ids = set()
    for shard in plan["shards"]:
        if shard["id"] in ids or not shard["checks"]:
            raise ValueError("duplicate or empty shard")
        ids.add(shard["id"])
        seen.extend(shard["checks"])
    if len(seen) != len(set(seen)) or set(seen) != set(plan["checks"]):
        raise ValueError("shards must partition the entire evaluated inventory")


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(canonical(data) + "\n")


def plan_command(args):
    config = json.loads(Path(args.config).read_text())
    system = config["system"]
    checks = json.loads(Path(args.inventory).read_text()) if args.inventory else inventory(args.flake, system)
    commit = args.commit or run_command(["git", "rev-parse", "HEAD"])
    plan = make_plan(checks, config, commit)
    validate_plan(plan)
    write_json(args.output, plan)
    if os.getenv("GITHUB_OUTPUT"):
        matrix = {"include": [{"id": shard["id"]} for shard in plan["shards"]]}
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as output:
            output.write("matrix=" + canonical(matrix) + "\n")
            output.write("digest=" + plan["digest"] + "\n")
    print(f"{len(plan['checks'])} Nix checks in {len(plan['shards'])} shards; {plan['digest']}")


def build_command(args):
    plan = json.loads(Path(args.plan).read_text())
    validate_plan(plan)
    if run_command(["git", "rev-parse", "HEAD"]) != plan["commit"]:
        raise ValueError("checkout does not match plan commit")
    shard = next((s for s in plan["shards"] if s["id"] == args.shard), None)
    if shard is None:
        raise ValueError("shard is missing from plan")
    evidence = {"schemaVersion": SCHEMA, "digest": plan["digest"],
                "commit": plan["commit"], "system": plan["system"],
                "shard": args.shard, "status": "failed", "checks": {}}
    try:
        for name in shard["checks"]:
            target = f".#checks.{plan['system']}.{name}"
            paths = run_command(["nix", "build", "--no-link", "--print-out-paths", "-L", target]).splitlines()
            if not paths or not all(STORE.fullmatch(path) for path in paths):
                raise ValueError(f"{name} returned no valid store paths")
            evidence["checks"][name] = {
                "drv": plan["checks"][name],
                "outputs": paths,
                "status": "pass",
            }
        evidence["status"] = "pass"
    finally:
        write_json(args.output, evidence)



def prebuild_command(args):
    config = json.loads(Path(args.config).read_text())
    if config.get("schemaVersion") != SCHEMA:
        raise ValueError("unsupported prebuild configuration")
    targets = config.get("prebuildTargets", [])
    if not isinstance(targets, list) or not all(isinstance(x, str) and x.startswith(".#") for x in targets):
        raise ValueError("prebuildTargets must be local flake installables")
    commit = run_command(["git", "rev-parse", "HEAD"])
    results = {}
    for target in targets:
        outputs = run_command(["nix", "build", "--no-link", "--print-out-paths", "-L", target]).splitlines()
        if not outputs or not all(STORE.fullmatch(path) for path in outputs):
            raise ValueError(f"prebuild target {target!r} returned invalid store paths")
        results[target] = outputs
    write_json(args.output, {"schemaVersion": SCHEMA, "commit": commit, "outputs": results})
    print(f"Prebuilt {len(results)} shared Nix targets at {commit}")


def verify_command(args):
    plan = json.loads(Path(args.plan).read_text())
    validate_plan(plan)
    expected = {s["id"]: set(s["checks"]) for s in plan["shards"]}
    paths = sorted(Path(args.directory).rglob("evidence.json"))
    if len(paths) != len(expected):
        raise ValueError(f"expected {len(expected)} shard evidence files; received {len(paths)}")
    seen = set()
    for path in paths:
        evidence = json.loads(path.read_text())
        sid = evidence.get("shard")
        if sid not in expected or sid in seen:
            raise ValueError(f"unexpected or duplicate shard {sid!r}")
        seen.add(sid)
        if any(evidence.get(k) != plan[k] for k in ("commit", "system")) or evidence.get("digest") != plan["digest"]:
            raise ValueError(f"stale or mismatched evidence from {sid}")
        if evidence.get("schemaVersion") != SCHEMA or evidence.get("status") != "pass":
            raise ValueError(f"failed or unsupported shard {sid}")
        checks = evidence.get("checks", {})
        if not isinstance(checks, dict) or set(checks) != expected[sid]:
            raise ValueError(f"incomplete check evidence in {sid}")
        for name, item in checks.items():
            if (item.get("drv") != plan["checks"][name] or item.get("status") != "pass"
                    or not isinstance(item.get("outputs"), list) or not item["outputs"]
                    or not all(isinstance(x, str) and STORE.fullmatch(x) for x in item["outputs"])):
                raise ValueError(f"invalid result for {name}")
    print(f"PASS: {len(plan['checks'])} exact-head Nix checks across {len(seen)} shards")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("plan")
    p.add_argument("--config", required=True)
    p.add_argument("--inventory", help="Use a supplied Nix check map, for tests")
    p.add_argument("--flake", default=".")
    p.add_argument("--commit")
    p.add_argument("--output", required=True)
    p.set_defaults(func=plan_command)
    p = subs.add_parser("build")
    p.add_argument("--plan", required=True)
    p.add_argument("--shard", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=build_command)
    p = subs.add_parser("prebuild")
    p.add_argument("--config", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=prebuild_command)
    p = subs.add_parser("verify")
    p.add_argument("--plan", required=True)
    p.add_argument("--directory", required=True)
    p.set_defaults(func=verify_command)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as exc:
        print(f"flake-check-plan: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
