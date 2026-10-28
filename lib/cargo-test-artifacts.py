#!/usr/bin/env python3
"""Package Cargo's compiled test executables without running Cargo at test time."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


def executables(lines):
    selected = {}
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # Cargo emits informational lines that are not compiler artifacts.
            continue
        if not isinstance(record, dict) or record.get("reason") != "compiler-artifact":
            continue
        executable = record.get("executable")
        profile = record.get("profile", {})
        if not isinstance(executable, str) or not executable or not isinstance(profile, dict):
            continue
        if profile.get("test") is not True:
            continue
        package = record.get("package_id")
        target = record.get("target")
        if not isinstance(package, str) or not isinstance(target, dict):
            raise ValueError("Cargo test artifact lacks its package or target identity")
        name = target.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Cargo test artifact has no target name")
        identity = (package, name, executable)
        selected[identity] = Path(executable)
    if not selected:
        raise ValueError("Cargo produced no compiled test executables")
    return sorted(selected.items())


def install(input_file, output, metadata_file):
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("test executable output must be empty")
    metadata = json.loads(metadata_file.read_text())
    if not isinstance(metadata, dict) or not isinstance(metadata.get("packages"), list):
        raise ValueError("invalid Cargo package metadata")
    workspace = set(metadata.get("workspace_members", []))
    names = {}
    for entry in metadata["packages"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid Cargo metadata package")
        if entry.get("id") in workspace:
            names[entry["id"]] = entry["name"]
    selected = executables(input_file.read_text().splitlines())
    manifest = []
    for index, ((package, name, source), binary) in enumerate(selected):
        crate = names.get(package)
        if not isinstance(crate, str) or not crate or not all(
                c.isalnum() or c in "_-" for c in crate):
            raise ValueError("Cargo test package is not a recognized workspace member")
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(f"missing compiled Cargo test binary: {source}")
        suffix = hashlib.sha256((package + "\\0" + name).encode()).hexdigest()[:12]
        group = output / crate
        group.mkdir(exist_ok=True)
        destination = group / f"{index:04d}-{suffix}"
        shutil.copy2(binary, destination)
        destination.chmod(0o555)
        manifest.append({"package": crate, "packageId": package,
                         "target": name, "binary": f"{crate}/{destination.name}"})
    (output.parent / "cargo-tests.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\\n")
    print(f"Packaged {len(manifest)} already-compiled Cargo test executables "
          f"from {len(set(item['package'] for item in manifest))} workspace crates")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        install(args.input, args.output, args.metadata)
    except (ValueError, OSError, json.JSONDecodeError) as err:
        parser.exit(1, f"cargo-test-artifacts: {err}\n")


if __name__ == "__main__":
    main()
