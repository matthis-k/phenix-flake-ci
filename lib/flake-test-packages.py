#!/usr/bin/env python3
"""Discover runnable test.* packages without knowing the consumer's domain.

The consumer exposes packages.<system>."test.<name>" as ordinary Nix
derivations with a main program. Builds are scheduled through the existing
native Nix coordinator. Each test runs as a separate GitHub job only after
its already-built output is restored; no CI-specific domain semantics.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SCHEMA = 1
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
STORE = re.compile(r"/nix/store/[a-z0-9]{32}-[^\s]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
EVAL = (
    'pkgs: builtins.listToAttrs (builtins.map (name: '
    '{ inherit name; value = let pkg = builtins.getAttr name pkgs; in '
    '{ drv = pkg.drvPath; out = pkg.outPath; }; }) '
    '(builtins.filter (name: '
    'builtins.match "test[.][A-Za-z0-9][A-Za-z0-9_-]*" name != null) '
    '(builtins.attrNames pkgs)))'
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def git_head():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical(value) + "\n")


def target(system, name):
    if not NAME.fullmatch(name):
        raise ValueError("invalid test package name")
    return f'.#packages.{system}."test.{name}"'


def validate(plan):
    if not isinstance(plan, dict) or plan.get("schemaVersion") != SCHEMA:
        raise ValueError("invalid test-package plan schema")
    if not isinstance(plan.get("commit"), str) or not SHA.fullmatch(plan["commit"]):
        raise ValueError("test-package plan has invalid commit")
    if not isinstance(plan.get("system"), str) or not re.fullmatch(
            r"[a-z0-9_-]+", plan["system"]):
        raise ValueError("invalid test-package Nix system")
    tests = plan.get("tests")
    if not isinstance(tests, dict):
        raise ValueError("test-package inventory must be an attribute set")
    for name, entry in tests.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("invalid test package name")
        if not isinstance(entry, dict) or set(entry) != {"drv", "out"}:
            raise ValueError("test package must expose derivation and output paths")
        if not all(isinstance(entry[k], str) and STORE.fullmatch(entry[k])
                   for k in ("drv", "out")) or not entry["drv"].endswith(".drv"):
            raise ValueError("invalid Nix test package derivation or output path")
    if plan.get("digest") != digest({k: v for k, v in plan.items()
                                    if k != "digest"}):
        raise ValueError("test-package plan digest mismatch")
    return plan


def discover_inventory(system):
    raw = subprocess.check_output(
        ["nix", "eval", "--json", "--apply", EVAL,
         f".#packages.{system}"], text=True)
    packages = json.loads(raw)
    if not isinstance(packages, dict):
        raise ValueError("Nix test package evaluation returned non-attributes")
    return {name.removeprefix("test."): item for name, item
            in sorted(packages.items())}


def plan_command(args):
    commit = git_head()
    inventory = discover_inventory(args.system)
    plan = {"schemaVersion": SCHEMA, "commit": commit,
            "system": args.system, "tests": inventory}
    plan["digest"] = digest(plan)
    validate(plan)
    save(args.output, plan)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("tests=" + canonical({
                "include": [{"name": name} for name in sorted(inventory)]
            }) + "\n")
            stream.write(f"test_count={len(inventory)}\n")
    print(f"Discovered {len(inventory)} runnable test.* packages at {commit}")


def verify_head(plan):
    if git_head() != plan["commit"]:
        raise ValueError("test-package checkout is not the planned exact head")


def build_command(args):
    plan = validate(load(args.plan))
    verify_head(plan)
    tests = plan["tests"]
    if tests:
        roots = [target(plan["system"], name) for name in tests]
        # The symlink keeps exactly the executable test output closures live
        # until the shared coordinator's cache-saving post-job hook.
        subprocess.run(["nix", "build", "-L", "--out-link", args.root,
                        *roots], check=True)
        for name, entry in tests.items():
            resolved = subprocess.check_output(
                ["nix", "eval", "--raw", "--apply", "p: p.outPath",
                 target(plan["system"], name)], text=True).strip()
            if resolved != entry["out"]:
                raise ValueError(f"test package {name} no longer matches its derivation")
        # The Nix build exit status proves every root was realized.
        expected = sorted(set(item["out"] for item in tests.values()))
        actual = subprocess.check_output(
            ["nix", "path-info", "--offline", *expected], text=True).splitlines()
        if set(actual) != set(expected):
            raise ValueError("built test package closure is incomplete")
    print(f"Built {len(tests)} executable test packages through the native scheduler")


def export_command(args):
    plan = validate(load(args.plan))
    verify_head(plan)
    roots = sorted({entry["out"] for entry in plan["tests"].values()})
    if not roots:
        return
    paths = subprocess.check_output(
        ["nix", "path-info", "--offline", "--recursive", *roots],
        text=True).splitlines()
    if not set(roots).issubset(paths):
        raise ValueError("test export is missing planned roots")
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as stream:
        subprocess.run(["nix-store", "--export", *sorted(set(paths))],
                       stdout=stream, check=True)


def run_command(args):
    plan = validate(load(args.plan))
    verify_head(plan)
    if args.name not in plan["tests"]:
        raise ValueError("test job not in exact-head package inventory")
    entry = plan["tests"][args.name]
    with open(args.closure, "rb") as stream:
        subprocess.run(["nix-store", "--import"], stdin=stream, check=True)
    # Deliberately fail rather than rebuilding on a test runner.
    paths = subprocess.check_output(
        ["nix", "path-info", "--offline", entry["out"]], text=True).splitlines()
    if paths != [entry["out"]]:
        raise ValueError("test executable missing from restored Nix store")
    result = {"schemaVersion": SCHEMA, "digest": plan["digest"],
              "commit": plan["commit"], "name": args.name,
              "drv": entry["drv"], "out": entry["out"], "status": "failed"}
    try:
        subprocess.run(["nix", "run", "--offline", "--no-substitute", "--max-jobs", "0", "--no-write-lock-file",
                        target(plan["system"], args.name)], check=True)
        result["status"] = "pass"
    finally:
        save(args.output, result)
    print(f"PASS: runnable test package {args.name}")


def verify_command(args):
    plan = validate(load(args.plan))
    expected = set(plan["tests"])
    paths = sorted(Path(args.results).rglob("evidence.json"))
    if len(paths) != len(expected):
        raise ValueError(f"expected {len(expected)} test results, found {len(paths)}")
    seen = set()
    for path in paths:
        value = load(path)
        name = value.get("name")
        if name not in expected or name in seen:
            raise ValueError("unknown or repeated test package evidence")
        seen.add(name)
        item = plan["tests"][name]
        if any(value.get(key) != expected_value for key, expected_value in {
            "schemaVersion": SCHEMA, "digest": plan["digest"],
            "commit": plan["commit"], "name": name, "status": "pass",
            "drv": item["drv"], "out": item["out"],
        }.items()):
            raise ValueError("failed, stale or forged test-package evidence")
    if seen != expected:
        raise ValueError("incomplete runnable test package evidence")
    print(f"PASS: {len(expected)} independently executed runnable test packages")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    a = subs.add_parser("plan")
    a.add_argument("--system", required=True)
    a.add_argument("--output", required=True)
    a.set_defaults(func=plan_command)
    b = subs.add_parser("build")
    b.add_argument("--plan", required=True)
    b.add_argument("--root", required=True)
    b.set_defaults(func=build_command)
    c = subs.add_parser("run")
    c.add_argument("--plan", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--output", required=True)
    c.add_argument("--closure", required=True)
    c.set_defaults(func=run_command)
    d = subs.add_parser("verify")
    d.add_argument("--plan", required=True)
    d.add_argument("--results", required=True)
    d.set_defaults(func=verify_command)
    e = subs.add_parser("export")
    e.add_argument("--plan", required=True)
    e.add_argument("--output", required=True)
    e.set_defaults(func=export_command)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, KeyError, OSError, TypeError, subprocess.CalledProcessError,
            json.JSONDecodeError) as exc:
        print(f"flake-test-packages: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
