#!/usr/bin/env python3
"""Optional GitHub-native Nix store snapshot via GHCR, with final-gate promotion.

No third-party account, external cache server, PAT, or alternate test inventory.
A candidate is uploaded after exact-head Nix checks; only final acceptance
can promote it to the shared tag after all runnable test packages pass.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SYSTEM = re.compile(r"[a-z0-9_+-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
STORE = re.compile(r"/nix/store/[0-9a-z]{32}-[^\s/]+\Z")
FORMAT = "phenix.ghcr-nix-cache.v1"


def identity(repository, system):
    if not REPO.fullmatch(repository) or not SYSTEM.fullmatch(system):
        raise ValueError("invalid repository or Nix system for GHCR cache")
    return ("ghcr.io/" + repository.lower().replace("/", "/") + "-nix-cache",
            "shared-" + system + "-v1")


def run(*args, **kw):
    return subprocess.run(list(args), check=True, **kw)


def login():
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise ValueError("GH_TOKEN must be the repository-scoped GitHub Actions token")
    run("docker", "login", "ghcr.io", "-u", os.environ.get("GITHUB_ACTOR", "github-actions"),
        "--password-stdin", input=token, text=True, stdout=subprocess.DEVNULL)


def validate_plan(plan):
    if not isinstance(plan, dict) or not SHA.fullmatch(plan.get("commit", "")):
        raise ValueError("invalid exact-head plan")
    if not SYSTEM.fullmatch(plan.get("system", "")):
        raise ValueError("invalid plan system")
    if not re.fullmatch(r"[0-9a-f]{64}", plan.get("digest", "")):
        raise ValueError("invalid plan digest")
    return plan


def resolve_inputs(args):
    plan = validate_plan(json.loads(Path(args.plan).read_text()))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    image, stable = identity(repo, plan["system"])
    return plan, repo, image, stable


def restore(args):
    plan, repo, image, stable = resolve_inputs(args)
    login()
    ref = image + ":" + stable
    pull = subprocess.run(["docker", "pull", ref], text=True, capture_output=True)
    if pull.returncode:
        error = (pull.stdout + pull.stderr).lower()
        if any(marker in error for marker in ("manifest unknown", "name unknown", "not found", "404")):
            print("GHCR Nix cache: no compatible snapshot; building from source")
            return
        raise RuntimeError("GHCR Nix cache pull failed (not a cache miss): " + error[-1200:])
    with tempfile.TemporaryDirectory(prefix="phenix-ghcr-restore-") as temp:
        container = run("docker", "create", ref, text=True, capture_output=True).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise ValueError("invalid docker container identity")
        archive = Path(temp) / "closure.nar.zst"
        try:
            run("docker", "cp", container + ":/cache/closure.nar.zst", str(archive))
        finally:
            run("docker", "rm", "-f", container, stdout=subprocess.DEVNULL)
        with subprocess.Popen(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE) as unpack:
            imported = subprocess.run(["nix-store", "--import"], stdin=unpack.stdout, check=False)
            unpack.stdout.close()
            unpack_rc = unpack.wait()
        if imported.returncode or unpack_rc:
            raise RuntimeError("GHCR Nix archive import failed")
    print("GHCR Nix cache: restored verified prior snapshot")


def stage(args):
    plan, repo, image, stable = resolve_inputs(args)
    run(sys.executable, str(Path(__file__).with_name("flake-check-dag.py")),
        "verify", "--plan", args.plan, "--graph", args.graph, "--evidence", args.evidence)
    evidence = json.loads(Path(args.evidence).read_text())
    outputs = sorted({path for check in evidence["checks"].values() for path in check["outputs"]})
    if not outputs or any(not STORE.fullmatch(path) for path in outputs):
        raise ValueError("invalid or empty exact-head Nix output inventory")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isdecimal() or not attempt.isdecimal():
        raise ValueError("missing GitHub Actions run identity")
    tag = "candidate-" + run_id + "-" + attempt
    login()
    with tempfile.TemporaryDirectory(prefix="phenix-ghcr-stage-") as temp:
        temp_dir = Path(temp)
        paths = sorted(set(run("nix-store", "--query", "--requisites", *outputs,
                               capture_output=True, text=True).stdout.splitlines()))
        if not paths or any(not STORE.fullmatch(path) for path in paths):
            raise ValueError("invalid Nix requisite path")
        archive = temp_dir / "closure.nar.zst"
        with archive.open("wb") as target:
            with subprocess.Popen(["nix-store", "--export", *paths], stdout=subprocess.PIPE) as source:
                compressed = subprocess.run(["zstd", "-T2", "-3", "-c"], stdin=source.stdout,
                                            stdout=target, check=False)
                source.stdout.close()
                exported = source.wait()
        if compressed.returncode or exported:
            raise RuntimeError("Nix closure export/compression failed")
        # Building FROM scratch preserves the Nix export as an inert OCI layer.
        # It is never executed as a container. All cache use is via nix-store --import.
        (temp_dir / "Dockerfile").write_text(
            "FROM scratch\n"
            + "LABEL org.opencontainers.image.source=https://github.com/" + repo + "\n"
            + "LABEL org.opencontainers.image.revision=" + plan["commit"] + "\n"
            + "COPY closure.nar.zst /cache/closure.nar.zst\n"
        )
        ref = image + ":" + tag
        run("docker", "build", "--tag", ref, str(temp_dir))
        push = run("docker", "push", ref, capture_output=True, text=True)
        hits = re.findall(r"digest: (sha256:[0-9a-f]{64})", push.stdout + push.stderr)
        if not hits:
            raise RuntimeError("GHCR did not return a pushed OCI image digest")
        digest = hits[-1]
    metadata = dict(format=FORMAT, repository=repo, image=image, system=plan["system"],
                    commit=plan["commit"], plan_digest=plan["digest"], run_id=run_id,
                    attempt=attempt, candidate=tag, digest=digest, stable=stable,
                    output_count=len(outputs))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metadata, sort_keys=True) + "\n")
    print("GHCR Nix cache: staged exact-head closure; awaiting final test acceptance")


def promote(args):
    plan, repo, image, stable = resolve_inputs(args)
    metadata = json.loads(Path(args.metadata).read_text())
    if (metadata.get("format") != FORMAT or metadata.get("repository") != repo
            or metadata.get("image") != image or metadata.get("system") != plan["system"]
            or metadata.get("stable") != stable or metadata.get("commit") != plan["commit"]
            or metadata.get("plan_digest") != plan["digest"]
            or metadata.get("run_id") != os.environ.get("GITHUB_RUN_ID")
            or metadata.get("attempt") != os.environ.get("GITHUB_RUN_ATTEMPT")
            or not DIGEST.fullmatch(metadata.get("digest", ""))):
        raise ValueError("candidate does not belong to this accepted exact-head CI run")
    if metadata.get("candidate") != "candidate-" + metadata["run_id"] + "-" + metadata["attempt"]:
        raise ValueError("candidate tag mismatch")
    login()
    # Remote re-tagging reuses GHCR layers; it does not download/re-upload the
    # multi-GB candidate. The shared tag is never written before final tests.
    run("docker", "buildx", "imagetools", "create", "--tag", image + ":" + stable,
        image + "@" + metadata["digest"])
    print("GHCR Nix cache: final accepted run promoted as shared snapshot")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation, handler in (("restore", restore), ("stage", stage), ("promote", promote)):
        cmd = commands.add_parser(operation)
        cmd.add_argument("--plan", required=True)
        if operation == "stage":
            cmd.add_argument("--graph", required=True)
            cmd.add_argument("--evidence", required=True)
            cmd.add_argument("--output", required=True)
        if operation == "promote":
            cmd.add_argument("--metadata", required=True)
        cmd.set_defaults(handler=handler)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
