#!/usr/bin/env python3
"""Discover runnable test.* packages without knowing the consumer's domain.

The consumer exposes packages.<system>."test.<name>" as ordinary Nix
derivations with a main program. Builds are scheduled through the existing
native Nix coordinator. Runtime cases run in deterministic bounded batches after compilation.
Each case retains separate evidence; no CI-specific domain semantics.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SCHEMA = 2
MAX_MATRIX_JOBS = 256  # GitHub Actions job-matrix hard limit.
DEFAULT_MAX_TEST_JOBS = 32
DEFAULT_TESTS_PER_JOB = 4
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
STORE = re.compile(r"/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._?=-]+\Z")
EXE_NAME = re.compile(r"[A-Za-z0-9_+.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
EVAL = (
    'pkgs: builtins.listToAttrs (builtins.map (name: '
    '{ inherit name; value = let pkg = builtins.getAttr name pkgs; '
    'program = (pkg.meta or {}).mainProgram or '
    '(pkg.pname or (builtins.parseDrvName pkg.name).name); in '
    '{ drv = pkg.drvPath; out = pkg.outPath; '
    'exe = "${pkg.outPath}/bin/${program}"; }; }) '
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
        if not isinstance(entry, dict) or set(entry) != {"drv", "out", "exe"}:
            raise ValueError("test package must expose derivation, output and executable paths")
        if not all(isinstance(entry[k], str) and STORE.fullmatch(entry[k])
                   for k in ("drv", "out")) or not entry["drv"].endswith(".drv"):
            raise ValueError("invalid Nix test package derivation or output path")
        exe = entry["exe"]
        if not isinstance(exe, str) or not exe.startswith(entry["out"] + "/bin/"):
            raise ValueError("test executable must be inside the planned Nix store output")
        executable_name = exe[len(entry["out"]) + len("/bin/"):]
        if executable_name in (".", "..") or not EXE_NAME.fullmatch(executable_name):
            raise ValueError("invalid Nix test executable name")
    if plan.get("digest") != digest({k: v for k, v in plan.items()
                                    if k != "digest"}):
        raise ValueError("test-package plan digest mismatch")
    return plan


def discover_inventory(system):
    raw = subprocess.check_output(
        ["nix", "eval", "--no-write-lock-file", "--json", "--apply", EVAL,
         f".#packages.{system}"], text=True)
    packages = json.loads(raw)
    if not isinstance(packages, dict):
        raise ValueError("Nix test package evaluation returned non-attributes")
    return {name.removeprefix("test."): item for name, item
            in sorted(packages.items())}


def make_batches(tests, *, max_jobs=DEFAULT_MAX_TEST_JOBS,
                 tests_per_job=DEFAULT_TESTS_PER_JOB):
    """Deterministic bounded GitHub job partition; no test is omitted."""
    if (isinstance(max_jobs, bool) or not isinstance(max_jobs, int)
            or not 1 <= max_jobs <= MAX_MATRIX_JOBS):
        raise ValueError(f"max test jobs must be between 1 and {MAX_MATRIX_JOBS}")
    if (isinstance(tests_per_job, bool) or not isinstance(tests_per_job, int)
            or tests_per_job < 1):
        raise ValueError("minimum tests per job must be positive")
    names = sorted(tests)
    if not names:
        return []
    # Keep at least the requested packing density; bound total runner count.
    count = min(max_jobs, (len(names) + tests_per_job - 1) // tests_per_job)
    batches = [[] for _ in range(count)]
    for index, name in enumerate(names):
        batches[index % count].append(name)
    return [{"id": f"batch-{index:03d}", "names": group}
            for index, group in enumerate(batches)]


def plan_command(args):
    commit = git_head()
    inventory = discover_inventory(args.system)
    plan = {"schemaVersion": SCHEMA, "commit": commit,
            "system": args.system, "tests": inventory}
    plan["digest"] = digest(plan)
    validate(plan)
    save(args.output, plan)
    batches = make_batches(inventory, max_jobs=args.max_test_jobs,
                           tests_per_job=args.tests_per_job)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("tests=" + canonical({"include": batches}) + "\n")
            stream.write(f"test_count={len(inventory)}\n")
    print(f"Discovered {len(inventory)} runnable test.* packages in "
          f"{len(batches)} execution jobs at {commit}")


def verify_head(plan, expected_commit=None):
    actual = git_head() if expected_commit is None else expected_commit
    if not isinstance(actual, str) or not SHA.fullmatch(actual) or actual != plan["commit"]:
        raise ValueError("test-package checkout or supplied commit is not the planned exact head")


def build_command(args):
    plan = validate(load(args.plan))
    verify_head(plan)
    tests = plan["tests"]
    if tests:
        roots = [target(plan["system"], name) for name in tests]
        # The symlink keeps exactly the executable test output closures live
        # until the shared coordinator's cache-saving post-job hook.
        subprocess.run(["nix", "build", "--no-write-lock-file", "-L", "--out-link", args.root,
                        *roots], check=True)
        for name, entry in tests.items():
            if not Path(entry["exe"]).is_file() or not os.access(entry["exe"], os.X_OK):
                raise ValueError(f"test package {name} has no executable at {entry['exe']}")
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


def import_closure(archive):
    with open(archive, "rb") as stream:
        subprocess.run(["nix-store", "--import"], stdin=stream, check=True)


def verify_outputs(plan, names):
    outputs = sorted({plan["tests"][name]["out"] for name in names})
    actual = subprocess.check_output(
        ["nix", "path-info", "--offline", *outputs], text=True).splitlines()
    if set(actual) != set(outputs):
        raise ValueError("one or more prebuilt test outputs are missing")


def execute_case(plan, name, path):
    entry = plan["tests"][name]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = {"schemaVersion": SCHEMA, "digest": plan["digest"],
              "commit": plan["commit"], "name": name,
              "drv": entry["drv"], "out": entry["out"], "exe": entry["exe"],
              "status": "failed"}
    runtime_dir = path.parent / "runtime-result"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_report = runtime_dir / "result.json"
    runtime_report.unlink(missing_ok=True)
    try:
        # The executable may be a generic package rather than mkTest's own
        # runner. Give every case fresh cwd and user cache/config state so
        # bundled tests cannot accidentally share relative files.
        with tempfile.TemporaryDirectory(prefix="phenix-ci-case-") as workspace:
            environment = dict(
                os.environ,
                PHENIX_TEST_OUTPUT=str(runtime_dir.resolve()),
                HOME=workspace,
                TMPDIR=workspace,
                XDG_CACHE_HOME=str(Path(workspace) / ".cache"),
                XDG_CONFIG_HOME=str(Path(workspace) / ".config"),
                XDG_DATA_HOME=str(Path(workspace) / ".local" / "share"),
            )
            # No Nix evaluator, builder or substitute resolver on testers.
            subprocess.run([entry["exe"]], cwd=workspace,
                           env=environment, check=True)
        if runtime_report.exists():
            details = load(runtime_report)
            if not isinstance(details, dict) or details.get("status") != "pass":
                raise ValueError("runtime test report is missing a passing status")
        result["status"] = "pass"
    except (ValueError, OSError, subprocess.CalledProcessError,
            json.JSONDecodeError) as error:
        result["reason"] = str(error)
        raise
    finally:
        if runtime_report.exists():
            try:
                result["runtime"] = load(runtime_report)
            except (ValueError, OSError) as error:
                result["runtimeError"] = f"cannot read runtime test report: {error}"
                result["status"] = "failed"
        save(path, result)
    print(f"PASS: runnable test package {name}")


def run_command(args):
    plan = validate(load(args.plan))
    verify_head(plan, getattr(args, "expected_commit", None))
    if args.name not in plan["tests"]:
        raise ValueError("test job not in exact-head package inventory")
    import_closure(args.closure)
    verify_outputs(plan, [args.name])
    execute_case(plan, args.name, args.output)


def batch_command(args):
    plan = validate(load(args.plan))
    verify_head(plan, getattr(args, "expected_commit", None))
    try:
        names = json.loads(args.names_json)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid JSON test batch") from error
    if (not isinstance(names, list) or not names
            or any(not isinstance(name, str) or name not in plan["tests"]
                   for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("test batch must contain unique planned package names")
    # The closure is imported and checked ONCE, regardless of batch length.
    import_closure(args.closure)
    verify_outputs(plan, names)
    failures = []
    for name in names:
        try:
            execute_case(plan, name, Path(args.results) / name / "evidence.json")
        except (ValueError, OSError, subprocess.CalledProcessError,
                json.JSONDecodeError) as error:
            failures.append(name)
            print(f"FAIL: test package {name}: {error}", file=sys.stderr)
    if failures:
        raise ValueError(f"{len(failures)} of {len(names)} test packages failed: "
                         + ", ".join(failures))
    print(f"PASS: batch executed {len(names)} independent test packages")


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
            "drv": item["drv"], "out": item["out"], "exe": item["exe"],
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
    a.add_argument("--max-test-jobs", type=int, default=DEFAULT_MAX_TEST_JOBS)
    a.add_argument("--tests-per-job", type=int, default=DEFAULT_TESTS_PER_JOB)
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
    c.add_argument("--expected-commit", help="Exact source SHA for an artifact-only runner")
    c.set_defaults(func=run_command)
    batch = subs.add_parser("batch")
    batch.add_argument("--plan", required=True)
    batch.add_argument("--names-json", required=True)
    batch.add_argument("--results", required=True)
    batch.add_argument("--closure", required=True)
    batch.add_argument("--expected-commit")
    batch.set_defaults(func=batch_command)
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
