#!/usr/bin/env python3
"""Run the exact flake check DAG and prebuilt runtime tests without GitHub.

This is a local adapter over the same plan, graph, build and evidence scripts
used by GitHub Actions. Nix alone schedules builds; the runtime test runner
executes the already-built binaries directly from the local store.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


LIB = Path(__file__).resolve().parent


def command(script, *args):
    subprocess.run([sys.executable, str(LIB / script), *map(str, args)],
                   check=True)


def require_clean_root():
    root = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True).strip()
    if Path(root).resolve() != Path.cwd().resolve():
        raise ValueError("run local CI from the consumer repository root")
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], text=True)
    if dirty.strip():
        raise ValueError(
            "exact-head evidence requires committed tracked files; "
            "use nix flake check --keep-going for uncommitted changes")


def run(args):
    require_clean_root()
    if not 1 <= args.timeout_seconds <= 3600:
        raise ValueError("runtime timeout must be between 1 and 3600 seconds")

    base = Path(args.output_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    print(f"CI evidence directory: {directory}", flush=True)

    config = directory / "config.json"
    raw_config = subprocess.check_output(
        ["nix", "eval", "--json", "--file", str(args.config)], text=True)
    settings = json.loads(raw_config)
    if not isinstance(settings, dict) or not isinstance(settings.get("system"), str):
        raise ValueError("CI config must specify a Nix system")
    config.write_text(json.dumps(settings, sort_keys=True) + "\n")

    plan = directory / "plan.json"
    graph = directory / "graph.json"
    build = directory / "evidence.json"
    command("flake-check-plan.py", "plan", "--config", config, "--output", plan)
    command("flake-check-dag.py", "analyze", "--plan", plan, "--output", graph)

    tests_path = directory / "test-packages.json"
    names = []
    if not args.checks_only:
        command("flake-test-packages.py", "plan", "--system",
                settings["system"], "--output", tests_path)
        tests = json.loads(tests_path.read_text())
        names = sorted(tests["tests"])

    build_args = ["build", "--plan", plan, "--graph", graph,
                  "--output", build]
    if names:
        build_args.extend(["--test-packages", tests_path,
                           "--test-root", directory / "test-package-roots"])
    command("flake-check-dag.py", *build_args)
    command("flake-check-dag.py", "verify", "--plan", plan,
            "--graph", graph, "--evidence", build)

    if names:
        results = directory / "runtime-results"
        command("flake-test-packages.py", "batch",
                "--local", "--plan", tests_path,
                "--names-json", json.dumps(names),
                "--timeout-seconds", args.timeout_seconds,
                "--results", results)
        command("flake-test-packages.py", "verify",
                "--plan", tests_path, "--results", results)

    print(f"PASS: local CI verified all checks and {len(names)} runtime tests")
    print(f"Evidence: {directory}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="ci/flake-check-config.nix",
                        help="Consumer's existing Nix CI policy file")
    parser.add_argument("--output-dir", default=".ci-local",
                        help="Root for retained, per-invocation evidence directories")
    parser.add_argument("--checks-only", action="store_true",
                        help="Skip prebuilt runtime test packages")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, OSError, subprocess.CalledProcessError,
            json.JSONDecodeError) as error:
        print(f"local CI failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
