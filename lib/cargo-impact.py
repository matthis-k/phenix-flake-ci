"""Conservative affected-check selection from Cargo's authoritative workspace metadata.

Generated GitHub Actions workflows inline this program. It has no third-party
Python dependencies. Uncertain inputs fail open to running the declared checks.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib


def workspace_only_lock_changes(config, changed, metadata, old_lock, new_lock):
    """Prove Cargo.lock changed only records of workspace crates with changed manifests.

    Cargo.lock is normally a shared compiler input. Never narrow an external
    dependency revision, checksum, lockfile format or unaccounted package edit.
    Paths and dependency closure come from Cargo metadata, not a parallel table.
    """
    if {k: v for k, v in old_lock.items() if k != "package"} != {
        k: v for k, v in new_lock.items() if k != "package"
    }:
        return False

    def indexed(packages):
        result = {}
        for package in packages:
            key = (package["name"], package["version"], package.get("source"))
            if key in result:
                raise ValueError("ambiguous Cargo.lock package identity: " + repr(key))
            result[key] = package
        return result

    before = indexed(old_lock.get("package", []))
    after = indexed(new_lock.get("package", []))
    workspace = set(metadata["workspace_members"])
    manifests = {
        pkg["name"]: os.path.relpath(pkg["manifest_path"])
        for pkg in metadata["packages"]
        if pkg["id"] in workspace
    }
    differences = [
        key for key in before.keys() | after.keys()
        if before.get(key) != after.get(key)
    ]
    if not differences:
        return True
    for name, _, source in differences:
        if source is not None or name not in manifests or manifests[name] not in changed:
            return False
    return True


def cargo_closure(metadata, changed):
    """Changed workspace crates plus all reverse path-dependency consumers."""
    workspace = set(metadata["workspace_members"])
    packages = {p["id"]: p for p in metadata["packages"] if p["id"] in workspace}
    by_directory = {
        str(Path(pkg["manifest_path"]).parent.resolve()): ident
        for ident, pkg in packages.items()
    }
    reverse = {ident: set() for ident in packages}
    for ident, pkg in packages.items():
        for dep in pkg.get("dependencies", []):
            path = dep.get("path")
            if path is not None:
                dependency = by_directory.get(str(Path(path).resolve()))
                if dependency:
                    reverse[dependency].add(ident)
    affected = set()
    for filename in changed:
        absolute = str(Path(filename).resolve())
        matches = [
            (directory, ident)
            for directory, ident in by_directory.items()
            if absolute.startswith(directory + os.sep) or absolute == directory
        ]
        if not matches:
            raise ValueError("changed Rust path does not belong to a known workspace crate: " + filename)
        affected.add(max(matches, key=lambda pair: len(pair[0]))[1])
    pending = list(affected)
    while pending:
        for dependent in reverse[pending.pop()]:
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    return {packages[ident]["name"] for ident in affected}


def select_jobs(config, files, metadata):
    """Return job selection without conflating source changes with test targets."""
    plans = config["jobs"]
    workspace = config["workspace"].rstrip("/") + "/"
    changed = sorted(set(files))
    # Unknown or build/toolchain changes affect all checks. Only well-defined
    # documentation and known workspace crate paths can narrow the selection.
    rust = []
    for name in changed:
        if name.startswith(workspace):
            rel = name[len(workspace):]
            if rel in ("Cargo.toml", "Cargo.lock") or not rel.startswith("crates/"):
                raise ValueError("shared Rust build input: " + name)
            rust.append(name)
        elif name.startswith("spec/") and name.endswith(".md"):
            continue
        elif name in ("README.md", "DEVELOPMENT.md", "LICENSE"):
            continue
        else:
            raise ValueError("unclassified repository input: " + name)
    if not changed:
        raise ValueError("empty change set")
    affected = cargo_closure(metadata, rust) if rust else set()
    results = {}
    for job, rule in plans.items():
        if rule["kind"] != "cargo":
            raise ValueError("unknown impact adapter: " + str(rule["kind"]))
        targets = rule.get("packages")
        if targets is not None:
            package_names = {p["name"] for p in metadata["packages"]
                             if p["id"] in metadata["workspace_members"]}
            if not set(targets) <= package_names:
                raise ValueError("CI target is not a selected workspace package: " + job)
        results[job] = bool(affected) and (targets is None or bool(affected.intersection(targets)))
    return results, sorted(affected)


def run():
    config = json.loads(os.environ["PHENIX_IMPACT_CONFIG"])
    if not isinstance(config["workspace"], str) or not config["workspace"]:
        raise ValueError("workspace path required")
    base = os.environ["GITHUB_BASE_REF"]
    if not base or not all(c.isascii() and (c.isalnum() or c in "-_./") for c in base):
        raise ValueError("invalid base ref")
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=ACMRTUXBD",
         "refs/remotes/origin/" + base + "...HEAD"],
        text=True,
    ).splitlines()
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--no-deps", "--format-version", "1", "--locked", "--offline",
         "--manifest-path", config["workspace"].rstrip("/") + "/Cargo.toml"],
        text=True,
    ))
    lock_path = config["workspace"].rstrip("/") + "/Cargo.lock"
    if lock_path in changed:
        # Use the same merge base as the filename diff. Comparing against the
        # latest base tip could mistake unrelated newer commits for this PR.
        base_revision = subprocess.check_output(
            ["git", "merge-base", "refs/remotes/origin/" + base, "HEAD"],
            text=True,
        ).strip()
        previous = tomllib.loads(subprocess.check_output(
            ["git", "show", base_revision + ":" + lock_path], text=True,
        ))
        current = tomllib.loads(Path(lock_path).read_text(encoding="utf-8"))
        if workspace_only_lock_changes(config, changed, metadata, previous, current):
            print("Verified workspace-only Cargo.lock changes; deriving impact from changed manifests")
            changed.remove(lock_path)
    selected, crates = select_jobs(config, changed, metadata)
    return selected, changed, crates


def main():
    config = json.loads(os.environ["PHENIX_IMPACT_CONFIG"])
    try:
        selected, changed, crates = run()
        print("Impact source: Cargo metadata; changed paths:", json.dumps(changed))
        print("Affected crates and reverse dependents:", json.dumps(crates))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        # An error may not suppress a necessary check. No silent omissions.
        print("Impact analysis uncertain; selecting all configured jobs: " + str(error),
              file=sys.stderr)
        selected = {name: True for name in config["jobs"]}
        crates = None
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    # null means uncertain: the consumer must retain its full-workspace fallback.
    # [] is a verified documentation-only change.
    packages = json.dumps(crates, separators=(",", ":"))
    print("Selected PR jobs:", encoded)
    print("Affected PR packages:", packages)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("jobs=" + encoded + "\n")
            stream.write("packages=" + packages + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("### Dependency-derived PR check selection\n\n")
            stream.write("| Check | Run |\n| --- | --- |\n")
            for name, run_job in sorted(selected.items()):
                stream.write("| " + name + " | " + ("yes" if run_job else "no") + " |\n")


if __name__ == "__main__":
    main()
