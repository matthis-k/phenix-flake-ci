#!/usr/bin/env python3
"""Run selected, already-compiled Cargo test executables. Never invokes Cargo."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def choose(manifest, bin_dir, packages, lib, tests):
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("missing compiled Cargo test inventory")
    selected = []
    available_packages = set()
    available_tests = set()
    available_lib = False
    seen_binaries = set()
    base = Path(bin_dir).resolve()
    for record in manifest:
        if not isinstance(record, dict):
            raise ValueError("invalid Cargo test artifact")
        package, target = record.get("package"), record.get("target")
        kinds, relative = record.get("targetKinds"), record.get("binary")
        workdir = record.get("workdir")
        if (not isinstance(package, str) or not package
                or not isinstance(target, str) or not target
                or not isinstance(kinds, list) or not kinds
                or not all(isinstance(kind, str) for kind in kinds)
                or not isinstance(relative, str) or not relative
                or not isinstance(workdir, str) or not workdir):
            raise ValueError("incomplete Cargo test artifact identity")
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or path in seen_binaries:
            raise ValueError("unsafe or repeated Cargo test executable path")
        seen_binaries.add(path)
        available_packages.add(package)
        if packages and package not in packages:
            continue
        if "test" in kinds:
            available_tests.add(target)
        if "lib" in kinds:
            available_lib = True
        if tests or lib:
            if not (("lib" in kinds and lib) or ("test" in kinds and target in tests)):
                continue
        if not path.is_file():
            raise ValueError(f"compiled Cargo test executable is missing: {relative}")
        selected.append((package, target, path, workdir))
    if packages - available_packages:
        raise ValueError(f"unknown Cargo packages: {sorted(packages - available_packages)}")
    if tests - available_tests:
        raise ValueError(f"unknown integration test targets: {sorted(tests - available_tests)}")
    if lib and not available_lib:
        raise ValueError("requested lib test binaries do not exist")
    if not selected:
        raise ValueError("no compiled Cargo tests match this check")
    return selected


def execute(manifest_file, bin_dir, cwd, packages, lib, tests, args):
    document = json.loads(Path(manifest_file).read_text())
    binaries = choose(document, bin_dir, set(packages), lib, set(tests))
    source = Path(cwd).resolve()
    if not source.is_dir():
        raise ValueError("immutable Cargo source directory is absent")
    failures = []
    for package, target, binary, manifest_dir in binaries:
        workdir = Path(manifest_dir).resolve()
        if not workdir.is_dir() or not workdir.is_relative_to(source):
            raise ValueError("Cargo test package working directory escapes immutable source")
        print(f"cargo-test-check: {package}/{target}", flush=True)
        result = subprocess.run([str(binary), *args], cwd=workdir, check=False)
        if result.returncode:
            failures.append((package, target, result.returncode))
    if failures:
        raise ValueError(f"{len(failures)}/{len(binaries)} Cargo test binaries failed: {failures}")
    print(f"PASS: {len(binaries)} precompiled Cargo test binaries; zero Cargo invocations")


def main():
    argv = sys.argv[1:]
    if "--" in argv:
        pos = argv.index("--")
        args = argv[pos + 1:]
        argv = argv[:pos]
    else:
        args = []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--bin-dir", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument("--lib", action="store_true")
    parser.add_argument("--test", action="append", default=[])
    options, other_args = parser.parse_known_args(argv)
    try:
        execute(options.manifest, options.bin_dir, options.cwd,
                options.package, options.lib, options.test, other_args + args)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f"cargo-test-check: {error}\n")


if __name__ == "__main__":
    main()
