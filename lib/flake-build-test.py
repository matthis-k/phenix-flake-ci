#!/usr/bin/env python3
"""Four build owners -> verified Nix-store join -> test-only check fan-out.

Nix is the sole source of derivation/check relationships. No packages/checks
are selected by name. Compilation and pure build-dependency derivations are
realized ahead of independent check roots; check outputs are never executed
during the build phase. Test-containing derivations remain in test phases.
"""
import argparse
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).parent

def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

staged = module("staged_for_build_test", "flake-dag-stages.py")
planner = staged.planner
dag = staged.dag
SCHEMA = 1
WORKERS = 4

def load(path):
    return json.loads(Path(path).read_text())

def save(path, value):
    staged.save(path, value)

def build_plan(plan, graph):
    # Reuses the shipping inventory, causal dependency graph and check waves.
    stages = staged.partition(plan, graph)
    parents = {node: set(deps) for node, deps in graph["derivationDependencies"].items()}
    check_drvs = set(plan["checks"].values())
    if not check_drvs <= set(parents):
        raise ValueError("missing check roots")
    if any(not deps <= set(parents) for deps in parents.values()):
        raise ValueError("incomplete derived Nix dependency graph")

    @lru_cache(None)
    def depends_on_check(node):
        return node in check_drvs or any(depends_on_check(dep) for dep in parents[node])

    @lru_cache(None)
    def closure(node):
        return frozenset([node]).union(*(closure(dep) for dep in parents[node]))

    # Maximal nodes on the cut between pure build inputs and check-dependent
    # computations. Nothing in this cut depends on a test/check derivation.
    cut = sorted({dep for node, deps in parents.items() if depends_on_check(node)
                  for dep in deps if not depends_on_check(dep)})
    if not cut:
        raise ValueError("no independent compilation frontier: cannot stage builds")
    if any(depends_on_check(node) for node in cut):
        raise ValueError("build phase would execute a check")
    # These are disjoint Nix drv identities. Nix's own DAG can still share
    # transitive inputs between workers; publish overlap as diagnostic.
    workers = [{"id": f"build-{i + 1:02d}", "roots": [], "weight": 0}
               for i in range(WORKERS)]
    covered = [set() for _ in workers]
    for node in sorted(cut, key=lambda n: (-len(closure(n)), n)):
        dependencies = set(closure(node))
        index = min(range(WORKERS),
                    key=lambda i: (len(covered[i] | dependencies), workers[i]["weight"], i))
        workers[index]["roots"].append(node)
        covered[index].update(dependencies)
        workers[index]["weight"] = len(covered[index])
    for worker in workers:
        worker["roots"].sort()
    # A fully connected build graph may not provide four independent workload
    # groups. Empty owners still pass through the same strict join contract.
    owners = {root: worker["id"] for worker in workers for root in worker["roots"]}
    if len(owners) != len(cut) or set(owners) != set(cut):
        raise ValueError("duplicate or lost build derivation owner")
    all_closure = set().union(*covered)
    repeated = sum(len(c) for c in covered) - len(all_closure)
    # Every derivation required by two or more build owners is a single
    # shared-store prebuild obligation, not independent duplicated work.
    counts = {node: sum(node in paths for paths in covered) for node in all_closure}
    common = {node for node, count in counts.items() if count > 1}
    # Maximal shared nodes cover the entire shared dependency closure.
    ancestors_of_common = set().union(*(set(closure(n)) - {n} for n in common))
    shared_frontier = sorted(common - ancestors_of_common)
    common_closure = set().union(*(set(closure(n)) for n in shared_frontier))
    if common_closure != common:
        raise ValueError("shared build frontier failed to cover all cross-owner dependencies")
    residual = [nodes - common_closure for nodes in covered]
    residual_overlap = sum(len(nodes) for nodes in residual) - len(set().union(*residual))
    if residual_overlap:
        raise ValueError("shared frontier still permits redundant transitive builds")
    result = {
        "schemaVersion": SCHEMA, "commit": plan["commit"],
        "planDigest": plan["digest"], "graphDigest": graph["graphDigest"],
        "stagedDigest": stages["digest"], "workers": workers,
        "roots": cut, "commonRoots": shared_frontier,
        "estimatedOverlapDrvs": repeated,
        "estimatedOverlapAfterCommon": residual_overlap,
        "estimatedSharedDrvs": len(common_closure),
        "estimatedUniqueDrvs": len(all_closure),
    }
    result["digest"] = planner.digest(result)
    return result

def validated(plan_path, graph_path, build_plan_path):
    plan, graph, specified = load(plan_path), load(graph_path), load(build_plan_path)
    expected = build_plan(plan, graph)
    if specified != expected:
        raise ValueError("stale, altered or mismatched build phase plan")
    return plan, graph, specified

def exact_head(plan):
    if planner.run_command(["git", "rev-parse", "HEAD"]) != plan["commit"]:
        raise ValueError("build/test checkout does not match exact commit")

def materialize_exact_head_derivations(plan):
    """Re-instantiate the checked-out flake's full Nix derivation graph.

    Nix store caches carry outputs but unbuilt .drv paths from a separate
    planning job may be absent from this runner's Nix store. Calling
    `nix build /nix/store/<drv>.drv^*` first can then fail with
    "don't know how to build these paths". Exact-head flake evaluation
    registers the derivations without executing the Nix checks. Reject
    any changed evaluation rather than building an unexpected derivation.
    """
    observed = planner.inventory(".", plan["system"])
    if observed != plan["checks"]:
        missing = sorted(set(plan["checks"]) - set(observed))
        changed = sorted(name for name in observed.keys() & plan["checks"].keys()
                         if observed[name] != plan["checks"][name])
        unexpected = sorted(set(observed) - set(plan["checks"]))
        raise ValueError(
            f"exact-head derivation inventory changed during worker re-evaluation: "
            f"missing={missing}, changed={changed}, unexpected={unexpected}")
    print(f"Instantiated {len(observed)} exact-head check roots and their "
          "transitive Nix derivations without executing any check")


def plan_cmd(args):
    result = build_plan(load(args.plan), load(args.graph))
    save(args.output, result)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("builders=" + planner.canonical({
                "include": [{"id": worker["id"]} for worker in result["workers"]]
            }) + "\n")
            output.write("build_digest=" + result["digest"] + "\n")
    print(f"Build frontier: {len(result['roots'])} unique roots on {WORKERS} builders; "
          f"{len(result['commonRoots'])} graph-derived common roots protecting "
          f"{result['estimatedSharedDrvs']} shared derivations; "
          f"overlap before={result['estimatedOverlapDrvs']}, "
          f"after={result['estimatedOverlapAfterCommon']}")

def outputs_from_nix(drvs):
    if not drvs:
        return {}
    raw = subprocess.check_output(
        ["nix", "build", "--no-link", "--json", "-L",
         *(node + "^*" for node in sorted(drvs))], text=True)
    return dag.parse_nix_results(json.loads(raw), {str(i): x for i, x in enumerate(drvs)})

def common_cmd(args):
    plan, graph, staging = validated(args.plan, args.graph, args.build_plan)
    exact_head(plan)
    materialize_exact_head_derivations(plan)
    realized = outputs_from_nix(staging["commonRoots"])
    roots = Path(args.gc_roots_dir)
    roots.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(sorted({p for group in realized.values() for p in group})):
        subprocess.run(["nix-store", "--realise", "--add-root",
                        str(roots / f"common-{index:04d}"), path],
                       stdout=subprocess.DEVNULL, check=True)
    record = {
        "schemaVersion": SCHEMA, "role": "common", "commit": plan["commit"],
        "planDigest": plan["digest"], "graphDigest": graph["graphDigest"],
        "buildDigest": staging["digest"], "outputs": realized,
    }
    save(args.output, record)
    print(f"Prebuilt {len(realized)} graph-derived common roots; "
          "GC-protected for the shared cache writer")

def common_outputs(plan, graph, staging, common_file):
    record = load(common_file)
    for key, expected in {
        "schemaVersion": SCHEMA, "role": "common", "commit": plan["commit"],
        "planDigest": plan["digest"], "graphDigest": graph["graphDigest"],
        "buildDigest": staging["digest"],
    }.items():
        if record.get(key) != expected:
            raise ValueError("stale or mismatched shared build evidence")
    mapping = record.get("outputs")
    if not isinstance(mapping, dict) or set(mapping) != set(staging["commonRoots"]):
        raise ValueError("missing graph-derived shared build outputs")
    if any(not isinstance(paths, list) or not paths
           or any(not planner.STORE.fullmatch(p) for p in paths)
           for paths in mapping.values()):
        raise ValueError("invalid graph-derived shared store paths")
    return sorted({p for paths in mapping.values() for p in paths})

def common_verify_cmd(args):
    plan, graph, staging = validated(args.plan, args.graph, args.build_plan)
    exact_head(plan)
    paths = common_outputs(plan, graph, staging, args.common)
    if paths:
        actual = planner.run_command(["nix", "path-info", "--offline", *paths]).splitlines()
        if set(actual) != set(paths):
            raise ValueError("common frontier did not restore on this runner")
    print(f"PASS: {len(paths)} common output paths available before build/test execution")

def worker_cmd(args):
    plan, graph, staging = validated(args.plan, args.graph, args.build_plan)
    exact_head(plan)
    materialize_exact_head_derivations(plan)
    worker = next((w for w in staging["workers"] if w["id"] == args.worker), None)
    if worker is None:
        raise ValueError("unregistered build worker")
    realized = outputs_from_nix(worker["roots"])
    shared = sorted(set(staged.shared_outputs(args.prebuild)
                        + common_outputs(plan, graph, staging, args.common)))
    archive = Path(args.archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    incremental = staged.export_incremental_archive(
        {drv: {"outputs": paths} for drv, paths in realized.items()}, shared, archive)
    record = {
        "schemaVersion": SCHEMA, "role": worker["id"], "commit": plan["commit"],
        "planDigest": plan["digest"], "graphDigest": graph["graphDigest"],
        "buildDigest": staging["digest"], "outputs": realized, "exportedPaths": incremental,
    }
    save(args.output, record)
    print(f"{worker['id']}: {len(realized)} pure build roots, "
          f"{len(incremental)} exported store paths")

def verify_worker(plan, graph, staging, worker, item):
    expected = {
        "schemaVersion": SCHEMA, "role": worker["id"], "commit": plan["commit"],
        "planDigest": plan["digest"], "graphDigest": graph["graphDigest"],
        "buildDigest": staging["digest"],
    }
    if any(item.get(k) != v for k, v in expected.items()):
        raise ValueError("stale or wrong build worker evidence")
    realized = item.get("outputs")
    if not isinstance(realized, dict) or set(realized) != set(worker["roots"]):
        raise ValueError("missing or duplicate build derivation evidence")
    if any(not isinstance(paths, list) or not paths
           or not all(planner.STORE.fullmatch(p) for p in paths)
           for paths in realized.values()):
        raise ValueError("invalid built outputs")
    exported = item.get("exportedPaths")
    if not isinstance(exported, list) or len(exported) != len(set(exported)):
        raise ValueError("invalid build archive inventory")

def join_cmd(args):
    plan, graph, staging = validated(args.plan, args.graph, args.build_plan)
    exact_head(plan)
    files = sorted(Path(args.builders).rglob("evidence.json"))
    if len(files) != WORKERS:
        raise ValueError("missing one of four builder evidences")
    common = common_outputs(plan, graph, staging, args.common)
    seen, outputs, archive_paths = set(), {}, []
    for path in files:
        item = load(path)
        worker = next((w for w in staging["workers"] if w["id"] == item.get("role")), None)
        if worker is None or worker["id"] in seen:
            raise ValueError("unknown or repeated builder")
        verify_worker(plan, graph, staging, worker, item)
        seen.add(worker["id"])
        nar = path.with_name("store.nar")
        if not nar.is_file():
            raise ValueError("missing build archive")
        if nar.stat().st_size:
            with nar.open("rb") as stream:
                subprocess.run(["nix-store", "--import"], stdin=stream,
                               stdout=subprocess.DEVNULL, check=True)
        elif item["exportedPaths"]:
            raise ValueError("empty archive with declared paths")
        outputs.update(item["outputs"])
        archive_paths.extend(item["exportedPaths"])
    if set(outputs) != set(staging["roots"]):
        raise ValueError("joined build frontier incomplete")
    realized = sorted({p for paths in outputs.values() for p in paths})
    if realized:
        actual = planner.run_command(["nix", "path-info", "--offline", *realized]).splitlines()
        if set(actual) != set(realized):
            raise ValueError("missing pure-build closure after join")
    extra = staged.export_incremental_archive(
        {drv: {"outputs": paths} for drv, paths in outputs.items()},
        sorted(set(staged.shared_outputs(args.prebuild) + common)), args.archive)
    report = {
        "schemaVersion": SCHEMA, "role": "build-join", "commit": plan["commit"],
        "planDigest": plan["digest"], "graphDigest": graph["graphDigest"],
        "buildDigest": staging["digest"], "outputs": outputs,
        "exportedPaths": extra,
        "overlappingArchiveEntries": len(archive_paths) - len(set(archive_paths)),
    }
    save(args.output, report)
    print(f"PASS: joined {WORKERS} builders / {len(outputs)} build roots; "
          f"{report['overlappingArchiveEntries']} repeated archive path references")

def install_cmd(args):
    plan, graph, staging = validated(args.plan, args.graph, args.build_plan)
    exact_head(plan)
    # Joined builder archives deliberately omit already shared paths.
    # Their complete closures are required locally before any imports.
    shared = common_outputs(plan, graph, staging, args.common)
    if shared:
        actual = planner.run_command(["nix", "path-info", "--offline", *shared]).splitlines()
        if set(actual) != set(shared):
            raise ValueError("shared build frontier not restored to test runner")
    evidence = load(args.evidence)
    if any(evidence.get(k) != v for k, v in {
        "schemaVersion": SCHEMA, "role": "build-join", "commit": plan["commit"],
        "planDigest": plan["digest"], "graphDigest": graph["graphDigest"],
        "buildDigest": staging["digest"],
    }.items()) or set(evidence.get("outputs", {})) != set(staging["roots"]):
        raise ValueError("stale or missing compiled build outputs")
    if not Path(args.archive).is_file():
        raise ValueError("missing joined Nix store archive")
    if Path(args.archive).stat().st_size:
        with open(args.archive, "rb") as stream:
            subprocess.run(["nix-store", "--import"], stdin=stream,
                           stdout=subprocess.DEVNULL, check=True)
    roots = sorted({p for paths in evidence["outputs"].values() for p in paths})
    if any(not planner.STORE.fullmatch(p) for p in roots):
        raise ValueError("invalid joined output paths")
    actual = planner.run_command(["nix", "path-info", "--offline", *roots]).splitlines()
    if set(actual) != set(roots):
        raise ValueError("missing joined build dependencies on test runner")
    print(f"PASS: {len(roots)} joined pure-build outputs available for tests")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="operation", required=True)
    for name, handler in (("plan", plan_cmd), ("common", common_cmd),
                          ("common-verify", common_verify_cmd), ("worker", worker_cmd),
                          ("join", join_cmd), ("install", install_cmd)):
        cmd = subs.add_parser(name)
        cmd.set_defaults(handler=handler)
        cmd.add_argument("--plan", required=True)
        cmd.add_argument("--graph", required=True)
        cmd.add_argument("--build-plan")
        if name == "plan":
            cmd.add_argument("--output", required=True)
        elif name == "common":
            cmd.add_argument("--output", required=True)
            cmd.add_argument("--gc-roots-dir", required=True)
        elif name == "common-verify":
            cmd.add_argument("--common", required=True)
        elif name == "worker":
            cmd.add_argument("--worker", required=True)
            cmd.add_argument("--prebuild", required=True)
            cmd.add_argument("--common", required=True)
            cmd.add_argument("--archive", required=True)
            cmd.add_argument("--output", required=True)
        elif name == "join":
            cmd.add_argument("--builders", required=True)
            cmd.add_argument("--prebuild", required=True)
            cmd.add_argument("--common", required=True)
            cmd.add_argument("--archive", required=True)
            cmd.add_argument("--output", required=True)
        else:
            cmd.add_argument("--evidence", required=True)
            cmd.add_argument("--common", required=True)
            cmd.add_argument("--archive", required=True)
    args = parser.parse_args()
    try:
        args.handler(args)
    except (ValueError, KeyError, OSError, TypeError, subprocess.CalledProcessError,
            json.JSONDecodeError, RecursionError) as exc:
        print(f"flake-build-test: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

if __name__ == "__main__":
    main()
