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
