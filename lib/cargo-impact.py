"""Conservative affected-check selection from Cargo's authoritative workspace metadata.

Generated GitHub Actions workflows inline this program. It has no third-party
Python dependencies. Uncertain inputs fail open to running the declared checks.
"""
import json
import os
from pathlib import Path
import re
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
    manifests = {}
    for pkg in metadata["packages"]:
        if pkg["id"] not in workspace:
            continue
        key = (pkg["name"], pkg["version"])
        if key in manifests:
            # Metadata itself is ambiguous; no name-only fallbacks.
            return False
        manifests[key] = os.path.relpath(pkg["manifest_path"])
    differences = [
        key for key in before.keys() | after.keys()
        if before.get(key) != after.get(key)
    ]
    if not differences:
        return True
    for name, version, source in differences:
        package = (name, version)
        if source is not None or package not in manifests or manifests[package] not in changed:
            return False
    return True


def workspace_member_only_change(config, changed, old_manifest, new_manifest):
    """Accept adding workspace members only when their own manifests are in the diff.

    All workspace dependency, profile, patch, resolver and feature changes are
    shared compiler inputs and continue to invalidate the full CI selection.
    """
    old = dict(old_manifest)
    new = dict(new_manifest)
    old_workspace = dict(old.pop("workspace", {}))
    new_workspace = dict(new.pop("workspace", {}))
    if old != new:
        return False
    previous_members = old_workspace.pop("members", None)
    next_members = new_workspace.pop("members", None)
    if old_workspace != new_workspace:
        return False
    if not isinstance(previous_members, list) or not isinstance(next_members, list):
        return False
    if len(previous_members) != len(set(previous_members)) or len(next_members) != len(set(next_members)):
        return False
    old_set, new_set = set(previous_members), set(next_members)
    # Removing or globbing workspace members has too many invalidation paths
    # to infer from current Cargo metadata alone.
    if not old_set <= new_set:
        return False
    for member in new_set - old_set:
        parts = Path(member).parts
        if len(parts) != 2 or parts[0] != "crates" or parts[1] in (".", ".."):
            return False
        manifest = config["workspace"].rstrip("/") + "/" + member + "/Cargo.toml"
        if manifest not in changed:
            return False
    return True


def verified_generated_shard_edits(config, changed, metadata, base_revision):
    """Prove generated CI edits only mirror one declared package shard list.

    This is opt-in per consumer. A reviewer supplies file/attribute locations,
    not dependency edges: all changed packages still come from Cargo metadata.
    Arbitrary Nix/workflow edits, untracked package moves and parse ambiguity
    force the ordinary full-CI fallback.
    """
    witness = config.get("verifiedShardChange")
    if not isinstance(witness, dict):
        return set()
    if any(not isinstance(witness.get(key), str) or not witness[key]
           for key in ("source", "list", "job", "workflow")):
        return set()
    source_path, workflow_path = witness["source"], witness["workflow"]
    if source_path not in changed or workflow_path not in changed:
        return set()

    def previous(path):
        return subprocess.check_output(
            ["git", "show", base_revision + ":" + path], text=True,
        )

    def nix_list(contents, name):
        lines = contents.splitlines(keepends=True)
        start = re.compile(r"[ \t]*" + re.escape(name) + r"[ \t]*=[ \t]*\[[ \t]*\r?\n?")
        matching = [i for i, line in enumerate(lines) if start.fullmatch(line)]
        if len(matching) != 1:
            raise ValueError("ambiguous Nix shard list: " + name)
        begin = matching[0]
        entries = []
        for end in range(begin + 1, len(lines)):
            if re.fullmatch(r"[ \t]*\];[ \t]*\r?\n?", lines[end]):
                if len(entries) != len(set(entries)):
                    raise ValueError("duplicate Nix CI shard package")
                return (entries, "".join(lines[:begin]) + "<verified-shard-list>\n"
                        + "".join(lines[end + 1:]))
            match = re.fullmatch(r'[ \t]*"([A-Za-z0-9_-]+)"[ \t]*\r?\n?', lines[end])
            if match is None:
                raise ValueError("non-literal Nix shard list item")
            entries.append(match.group(1))
        raise ValueError("unterminated Nix shard list")

    def yaml_impact(contents):
        pattern = re.compile(r"^([ \t]*PHENIX_IMPACT_CONFIG:[ \t]*)(.+)$", re.MULTILINE)
        found = list(pattern.finditer(contents))
        if len(found) != 1:
            raise ValueError("unexpected generated impact YAML structure")
        encoded = json.loads(found[0].group(2))
        if not isinstance(encoded, str):
            raise ValueError("generated impact env is not a JSON string")
        config_value = json.loads(encoded)
        normalized = pattern.sub(r"\g<1><verified-impact-config>", contents)
        return config_value, normalized

    old_packages, old_source = nix_list(previous(source_path), witness["list"])
    new_packages, new_source = nix_list(
        Path(source_path).read_text(encoding="utf-8"), witness["list"],
    )
    if old_source != new_source:
        return set()
    old_config, old_workflow = yaml_impact(previous(workflow_path))
    new_config, new_workflow = yaml_impact(
        Path(workflow_path).read_text(encoding="utf-8"),
    )
    if old_workflow != new_workflow or new_config != config:
        return set()
    job = witness["job"]
    if (old_config["jobs"][job]["packages"] != old_packages
            or new_config["jobs"][job]["packages"] != new_packages):
        return set()
    # Only this job's target list and the opt-in witness may differ. No
    # workflow logic, unrelated CI target or toolchain input is exempted.
    old_config.pop("verifiedShardChange", None)
    new_config.pop("verifiedShardChange", None)
    old_config["jobs"][job]["packages"] = list(new_packages)
    if old_config != new_config:
        return set()

    workspace_members = set(metadata["workspace_members"])
    manifests = {
        pkg["name"]: os.path.relpath(pkg["manifest_path"])
        for pkg in metadata["packages"]
        if pkg["id"] in workspace_members
    }
    for name in set(old_packages) ^ set(new_packages):
        if name not in manifests or manifests[name] not in changed:
            return set()
    return {source_path, workflow_path}


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
    workspace = config["workspace"].rstrip("/")
    lock_path = workspace + "/Cargo.lock"
    manifest_path = workspace + "/Cargo.toml"
    if lock_path in changed or manifest_path in changed:
        # Diff against the exact merge-base commit, not the later base tip.
        base_revision = subprocess.check_output(
            ["git", "merge-base", "refs/remotes/origin/" + base, "HEAD"],
            text=True,
        ).strip()
        def previous_toml(path):
            return tomllib.loads(subprocess.check_output(
                ["git", "show", base_revision + ":" + path], text=True,
            ))
        if lock_path in changed and workspace_only_lock_changes(
            config, changed, metadata, previous_toml(lock_path),
            tomllib.loads(Path(lock_path).read_text(encoding="utf-8")),
        ):
            print("Verified workspace-only Cargo.lock changes; deriving impact from changed manifests")
            changed.remove(lock_path)
        if manifest_path in changed and workspace_member_only_change(
            config, changed, previous_toml(manifest_path),
            tomllib.loads(Path(manifest_path).read_text(encoding="utf-8")),
        ):
            print("Verified Cargo workspace member additions; deriving impact from added crate manifests")
            changed.remove(manifest_path)
    if config.get("verifiedShardChange") is not None:
        if "base_revision" not in locals():
            base_revision = subprocess.check_output(
                ["git", "merge-base", "refs/remotes/origin/" + base, "HEAD"],
                text=True,
            ).strip()
        reviewed = verified_generated_shard_edits(config, changed, metadata, base_revision)
        if reviewed:
            print("Verified generated CI target-list sync:", sorted(reviewed))
            changed = [name for name in changed if name not in reviewed]
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
