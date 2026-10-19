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


def cache_prefix(config):
    cache = config.get("cache", {})
    if not isinstance(cache, dict):
        raise ValueError("cache policy must be an attribute set")
    prefix = cache.get("keyPrefix", "flake-checks")
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
        raise ValueError("invalid cache key prefix")
    files = cache.get("keyFiles", [])
    if not isinstance(files, list) or not all(isinstance(x, str) for x in files):
        raise ValueError("cache.keyFiles must be a list of paths")
    identities = []
    for filename in files:
        candidate = Path(filename)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.is_file():
            raise ValueError(f"invalid or absent cache key input: {filename}")
        identities.append([filename, hashlib.sha256(candidate.read_bytes()).hexdigest()])
    identity = digest({"keyFiles": sorted(identities), "strategy": config.get("strategy", "generic"),
                       "options": config.get("strategyOptions", {})})
    return f"{prefix}-{config['system']}-{identity[:20]}"


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
        if set(hint) - {"weight", "isolate", "affinity"}:
            raise ValueError("unknown check hint fields")
        if "weight" in hint and (type(hint["weight"]) is not int or hint["weight"] < 1):
            raise ValueError("check weight must be a positive integer")
        if "isolate" in hint and type(hint["isolate"]) is not bool:
            raise ValueError("check isolate flag must be boolean")
        if "affinity" in hint and (not isinstance(hint["affinity"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", hint["affinity"])):
            raise ValueError("check affinity must be a non-empty stable identifier")

    # Isolated checks reserve one worker, typically for shipping. Non-isolated
    # affinity groups run together on the same worker, so shared Nix derivations
    # do not need to be built/restored separately in other shards. Affinity is a
    # scheduling hint; the check inventory is still discovered from the flake.
    weighted = [(name, hint_for(name, hints)) for name in sorted(checks)]
    for name, h in weighted:
        if h.get("isolate", False) and "affinity" in h:
            raise ValueError(f"check {name!r} cannot be isolated and affinity-grouped")
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
    units = {}
    for name, hint in rest:
        # Unique singleton keys cannot collide with named affinity groups.
        key = ("affinity", hint["affinity"]) if "affinity" in hint else ("check", name)
        unit = units.setdefault(key, {"checks": [], "weight": 0})
        unit["checks"].append(name)
        unit["weight"] += hint.get("weight", 1)
    # Longest-processing-time-first over indivisible groups, deterministic ties.
    for key, unit in sorted(units.items(), key=lambda pair: (-pair[1]["weight"], pair[0])):
        target = min(available, key=lambda slot: (slot["weight"], slot["id"]))
        target["checks"].extend(unit["checks"])
        target["weight"] += unit["weight"]
    shards = [s for s in shards if s["checks"]]
    for shard in shards:
        shard["checks"].sort()
    targets = config.get("prebuildTargets", [])
    if not isinstance(targets, list) or not all(isinstance(x, str) and x.startswith(".#") for x in targets):
        raise ValueError("prebuildTargets must contain only local flake installables")
    result = {
        "schemaVersion": SCHEMA,
        "commit": commit,
        "system": system,
        "checks": dict(sorted(checks.items())),
        "shards": shards,
        "prebuildTargets": targets,
        "cachePrefix": cache_prefix(config),
        "configDigest": digest(config),
    }
    result["digest"] = digest(result)
    return result


def validate_plan(plan):
    checksum = plan.get("digest")
    if plan.get("schemaVersion") != SCHEMA or checksum != digest({k: v for k, v in plan.items() if k != "digest"}):
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
            output.write("cache_prefix=" + plan["cachePrefix"] + "\n")
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
        # One Nix invocation exposes the complete shard dependency DAG to the
        # native scheduler. Invoking Nix once per check needlessly serialized
        # otherwise independent builds and multiplied evaluation overhead.
        # --json reports realized outputs bound to exact evaluated drv paths;
        # reuse the same strict parser as the full-DAG backend.
        targets = [f".#checks.{plan['system']}.{name}" for name in shard["checks"]]
        raw = subprocess.check_output(
            ["nix", "build", "--no-link", "--json", "-L", *targets], text=True,
        )
        results = json.loads(raw)
        expected = {plan["checks"][name] for name in shard["checks"]}
        realized = {}
        if not isinstance(results, list) or not results:
            raise ValueError("shard produced no Nix build results")
        for entry in results:
            if not isinstance(entry, dict):
                raise ValueError("invalid Nix build result")
            drv = entry.get("drvPath")
            outputs = entry.get("outputs")
            if drv not in expected or not isinstance(outputs, dict) or not outputs:
                raise ValueError("unexpected or missing shard derivation")
            paths = []
            for output in outputs.values():
                path = output.get("path") if isinstance(output, dict) else output
                if not isinstance(path, str) or not STORE.fullmatch(path):
                    raise ValueError("invalid Nix output path")
                paths.append(path)
            paths = sorted(set(paths))
            if drv in realized and realized[drv] != paths:
                raise ValueError("conflicting realized outputs")
            realized[drv] = paths
        if set(realized) != expected:
            raise ValueError("shard omitted an evaluated check derivation")
        for name in shard["checks"]:
            evidence["checks"][name] = {
                "drv": plan["checks"][name],
                "outputs": realized[plan["checks"][name]],
                "status": "pass",
            }
        evidence["status"] = "pass"
    finally:
        write_json(args.output, evidence)



def prebuild_command(args):
    plan = json.loads(Path(args.plan).read_text())
    validate_plan(plan)
    commit = run_command(["git", "rev-parse", "HEAD"])
    if commit != plan["commit"]:
        raise ValueError("prebuild checkout does not match the plan")
    targets = plan["prebuildTargets"]
    # One Nix invocation schedules independent derivations concurrently.
    # Previously one Nix build per target serialized otherwise independent
    # packages on the critical path of all shards.
    if targets:
        # The cache writer runs Nix GC before uploading its store archive.
        # Keep an explicit GC root for every built output until that upload
        # finishes; --no-link lets GC discard the declared prebuild hotset.
        # Nix creates numbered out-links for multiple installables.
        root = Path(args.output).parent / "shared-prebuild-root"
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["nix", "build", "--out-link", str(root), "-L", *targets], check=True)
    results = {}
    for target in targets:
        # Read realized store identity only after the combined build. This
        # evaluation does not recompile successfully realized derivations.
        outputs = run_command(["nix", "build", "--no-link", "--print-out-paths", target]).splitlines()
        if not outputs or not all(STORE.fullmatch(path) for path in outputs):
            raise ValueError(f"prebuild target {target!r} returned invalid store paths")
        results[target] = outputs
    write_json(args.output, {"schemaVersion": SCHEMA, "digest": plan["digest"],
                             "commit": commit, "outputs": results})
    print(f"Prebuilt {len(results)} shared Nix targets at {commit}")


def prebuild_local_command(args):
    """Require every shared output to have reached this runner's local store.

    A successful prebuild on a different runner does not prove the cache restored
    here. Without this check the shard silently recompiles missing hotset
    dependencies, defeating shared-build performance and hiding cache eviction.
    """
    plan = json.loads(Path(args.plan).read_text())
    validate_plan(plan)
    prebuild = json.loads(Path(args.prebuild).read_text())
    if (prebuild.get("schemaVersion") != SCHEMA
            or prebuild.get("commit") != plan["commit"]
            or prebuild.get("digest") != plan["digest"]):
        raise ValueError("prebuild evidence is stale or mismatched")
    declared = prebuild.get("outputs", {})
    if not isinstance(declared, dict) or set(declared) != set(plan["prebuildTargets"]):
        raise ValueError("prebuild evidence is missing declared outputs")
    outputs = []
    for target in plan["prebuildTargets"]:
        paths = declared[target]
        if not isinstance(paths, list) or not paths or not all(
                isinstance(path, str) and STORE.fullmatch(path) for path in paths):
            raise ValueError(f"invalid prebuild outputs for {target}")
        outputs.extend(paths)
    if outputs:
        # path-info does not build nor substitute; offline makes that guarantee
        # explicit. One Nix query verifies the whole shared closure frontier.
        actual = run_command(["nix", "path-info", "--offline", *sorted(set(outputs))]).splitlines()
        if set(actual) != set(outputs):
            raise ValueError("prebuilt outputs missing from shard Nix store")
    print(f"PASS: {len(set(outputs))} shared outputs restored locally without recompilation")


def verify_command(args):
    plan = json.loads(Path(args.plan).read_text())
    validate_plan(plan)
    expected = {s["id"]: set(s["checks"]) for s in plan["shards"]}
    paths = sorted(Path(args.directory).rglob("evidence.json"))
    if len(paths) != len(expected):
        raise ValueError(f"expected {len(expected)} shard evidence files; received {len(paths)}")
    if getattr(args, "prebuild", None):
        prebuild = json.loads(Path(args.prebuild).read_text())
        if (prebuild.get("schemaVersion") != SCHEMA
                or prebuild.get("commit") != plan["commit"]
                or prebuild.get("digest") != plan["digest"]):
            raise ValueError("prebuild evidence is stale or mismatched")
        outputs = prebuild.get("outputs", {})
        if not isinstance(outputs, dict) or set(outputs) != set(plan["prebuildTargets"]):
            raise ValueError("prebuild evidence is missing declared outputs")
        for target, output_paths in outputs.items():
            if not isinstance(output_paths, list) or not output_paths or not all(
                    isinstance(x, str) and STORE.fullmatch(x) for x in output_paths):
                raise ValueError(f"invalid prebuild outputs for {target}")
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
    p.add_argument("--plan", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=prebuild_command)
    p = subs.add_parser("prebuild-local")
    p.add_argument("--plan", required=True)
    p.add_argument("--prebuild", required=True)
    p.set_defaults(func=prebuild_local_command)
    p = subs.add_parser("verify")
    p.add_argument("--plan", required=True)
    p.add_argument("--directory", required=True)
    p.add_argument("--prebuild", help="Require matching shared build evidence")
    p.set_defaults(func=verify_command)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as exc:
        print(f"flake-check-plan: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
