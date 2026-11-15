#!/usr/bin/env python3
"""Unit tests for bounded DAG worker fan-out and final shipping join."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

folder = Path(__file__).parent
spec = importlib.util.spec_from_file_location("dag_stages", folder / "flake-dag-stages.py")
stages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stages)

SHA = "a" * 40
def drv(c, name):
    return "/nix/store/" + c * 32 + "-" + name + ".drv"
def store(c, name):
    return "/nix/store/" + c * 32 + "-" + name

def sample():
    plan = stages.planner.make_plan({
        "model": drv("m", "model"), "tools": drv("t", "tools"),
        "integration": drv("i", "integration"), "shipping": drv("s", "shipping"),
    }, {
        "schemaVersion": 1, "system": "x86_64-linux", "shardCount": 3,
        "checkHints": {"shipping": {"isolate": True, "weight": 50}},
    }, SHA)
    graph = {
        "schemaVersion": 1, "planDigest": plan["digest"],
        "commit": SHA, "system": plan["system"], "graphDigest": "g" * 64,
        "checkDependencies": {
            "model": [], "tools": [], "integration": ["model", "tools"],
            "shipping": ["model", "tools", "integration"],
        },
    }
    return plan, graph

class StagedDagTests(unittest.TestCase):
    def test_only_ready_roots_fan_out_and_final_checks_join(self):
        plan, graph = sample()
        manifest = stages.partition(plan, graph)
        self.assertEqual(manifest["initialChecks"], ["model", "tools"])
        self.assertEqual(manifest["finalChecks"], ["integration", "shipping"])
        self.assertEqual(
            {n for shard in manifest["shards"] for n in shard["checks"]},
            {"model", "tools"})
        self.assertEqual(len(manifest["shards"]), 2)
        self.assertEqual(manifest, stages.partition(plan, graph))

    def test_stale_graph_or_unknown_dependency_rejected(self):
        plan, graph = sample()
        graph["planDigest"] = "forged"
        with self.assertRaisesRegex(ValueError, "invalid or stale"):
            stages.partition(plan, graph)
        graph["planDigest"] = plan["digest"]
        graph["checkDependencies"]["shipping"].append("missing-check")
        with self.assertRaisesRegex(ValueError, "invalid derived"):
            stages.partition(plan, graph)

    def test_each_worker_submits_all_roots_in_one_native_nix_invocation(self):
        plan, _ = sample()
        selected = ["model", "tools"]
        json_output = [
            {"drvPath": plan["checks"][name],
             "outputs": {"out": store("x", name)}}
            for name in selected
        ]
        with mock.patch.object(stages.subprocess, "check_output",
                               return_value=json.dumps(json_output)) as nix:
            realized = stages.build_selected(plan, selected)
        self.assertEqual(set(realized), set(selected))
        nix.assert_called_once_with(
            ["nix", "build", "--no-link", "--json", "-L",
             ".#checks.x86_64-linux.model", ".#checks.x86_64-linux.tools"],
            text=True)

    def test_join_compiles_final_checks_and_runtime_packages_in_one_graph(self):
        plan, _ = sample()
        tests = {"schemaVersion": 1, "commit": SHA, "system": plan["system"],
                 "tests": {"rpc": {"drv": drv("r", "rpc"), "out": store("r", "rpc")}}}
        tests["digest"] = stages.packages.digest(tests)
        outputs = [
            {"drvPath": plan["checks"]["integration"],
             "outputs": {"out": store("x", "integration")}},
            {"drvPath": drv("r", "rpc"),
             "outputs": {"out": store("r", "rpc")}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "roots"
            with mock.patch.object(stages.subprocess, "check_output",
                                   return_value=json.dumps(outputs)) as built, mock.patch.object(
                    stages.subprocess, "run") as pinned:
                result = stages.build_selected(
                    plan, ["integration"], test_plan=tests, test_root=str(root))
            self.assertEqual(set(result), {"integration"})
            built.assert_called_once_with(
                ["nix", "build", "--no-link", "--json", "-L",
                 ".#checks.x86_64-linux.integration",
                 '.#packages.x86_64-linux."test.rpc"'],
                text=True)
            pinned.assert_called_once()
            self.assertIn("--no-substitute", pinned.call_args.args[0])
            self.assertIn(str(root), pinned.call_args.args[0])

    def test_stale_runtime_package_plan_fails_before_any_build(self):
        plan, _ = sample()
        tests = {"schemaVersion": 1, "commit": "b" * 40, "system": plan["system"],
                 "tests": {"rpc": {"drv": drv("r", "rpc"), "out": store("r", "rpc")}}}
        tests["digest"] = stages.packages.digest(tests)
        with mock.patch.object(stages.subprocess, "check_output") as no_build:
            with self.assertRaisesRegex(ValueError, "exact-head"):
                stages.build_selected(
                    plan, ["integration"], test_plan=tests, test_root="/tmp/roots")
            no_build.assert_not_called()

    def test_stale_runtime_manifest_blocks_join_before_import(self):
        plan, graph = sample()
        manifest = stages.partition(plan, graph)
        test_plan = {"schemaVersion": 1, "commit": "b" * 40,
                     "system": plan["system"], "tests": {}}
        test_plan["digest"] = stages.packages.digest(test_plan)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, value in (("plan.json", plan), ("graph.json", graph),
                                    ("stages.json", manifest), ("tests.json", test_plan)):
                stages.save(root / filename, value)
            args = type("Args", (), {
                "plan": root / "plan.json", "graph": root / "graph.json",
                "stages": root / "stages.json", "test_packages": root / "tests.json",
                "leaves": root / "absent-leaves", "test_root": root / "roots",
                "output": root / "evidence.json",
            })()
            with mock.patch.object(stages, "ensure_commit"), mock.patch.object(
                    stages.subprocess, "run") as no_import:
                with self.assertRaisesRegex(ValueError, "exact-head"):
                    stages.join_command(args)
                no_import.assert_not_called()

    def test_join_verifier_binds_runtime_manifest_digest(self):
        plan, graph = sample()
        manifest = stages.partition(plan, graph)
        test_plan = {"schemaVersion": 1, "commit": SHA,
                     "system": plan["system"], "tests": {}}
        test_plan["digest"] = stages.packages.digest(test_plan)
        check_evidence = {
            name: {"drv": drvpath, "status": "pass", "outputs": [store("x", name)]}
            for name, drvpath in plan["checks"].items()
        }
        proof = stages.evidence(plan, manifest, "join", check_evidence)
        proof["testPlanDigest"] = test_plan["digest"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename, value in (("plan.json", plan), ("graph.json", graph),
                                    ("stages.json", manifest), ("tests.json", test_plan),
                                    ("evidence.json", proof)):
                stages.save(root / filename, value)
            args = type("Args", (), {
                "plan": root / "plan.json", "graph": root / "graph.json",
                "stages": root / "stages.json", "test_packages": root / "tests.json",
                "evidence": root / "evidence.json",
            })()
            stages.verify_command(args)
            proof["testPlanDigest"] = "0" * 64
            stages.save(args.evidence, proof)
            with self.assertRaisesRegex(ValueError, "runnable test plan evidence"):
                stages.verify_command(args)

    def test_export_closure_subtracts_shared_paths(self):
        checks = {"model": {"outputs": [store("a", "model")]}}
        shared = [store("b", "dependency")]
        def fake_run(args):
            if args[:3] != ["nix-store", "--query", "--requisites"]:
                raise ValueError(args)
            return ("\n".join(sorted({store("a", "model"), store("b", "dependency")}))
                    if store("a", "model") in args else store("b", "dependency"))
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "outputs.nar"
            with mock.patch.object(stages.planner, "run_command", side_effect=fake_run), mock.patch.object(
                    stages.subprocess, "run") as exported:
                paths = stages.export_incremental_archive(checks, shared, file)
            self.assertEqual(paths, [store("a", "model")])
            self.assertEqual(exported.call_count, 1)
            self.assertEqual(exported.call_args.args[0],
                             ["nix-store", "--export", store("a", "model")])
            self.assertTrue(file.exists())

    def test_all_leaf_evidence_must_match_exact_checkout_plan(self):
        plan, graph = sample()
        manifest = stages.partition(plan, graph)
        leaf = manifest["shards"][0]
        item = stages.evidence(plan, manifest, leaf["id"], {
            name: {"drv": plan["checks"][name], "status": "pass",
                   "outputs": [store("x", name)]}
            for name in leaf["checks"]
        })
        stages.verify_leaf(plan, manifest, item)
        item["checks"].pop(next(iter(item["checks"])))
        with self.assertRaisesRegex(ValueError, "missing or duplicate"):
            stages.verify_leaf(plan, manifest, item)

if __name__ == "__main__":
    unittest.main()
