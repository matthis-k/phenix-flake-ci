#!/usr/bin/env python3
"""Regression cases for native, dependency-driven Nix CI."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

root = Path(__file__).parent
spec = importlib.util.spec_from_file_location("flake_check_dag", root / "flake-check-dag.py")
dag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dag)

SHA = "a" * 40
def drv(letter, label):
    return f"/nix/store/{letter * 32}-{label}.drv"

def output(letter, label):
    return f"/nix/store/{letter * 32}-{label}"

def mkplan():
    return dag.planner.make_plan(
        {"a": drv("a", "a"), "b": drv("b", "b"),
         "shipping": drv("c", "shipping")},
        {"schemaVersion": 1, "system": "x86_64-linux", "shardCount": 3},
        SHA,
    )

def mkgraph():
    core = drv("d", "sdk")
    aggregate = drv("e", "conformance")
    return {
        core: {"inputDrvs": {}},
        drv("a", "a"): {"inputDrvs": {core: ["out"]}},
        drv("b", "b"): {"inputDrvs": {core: ["out"]}},
        aggregate: {"inputDrvs": {drv("a", "a"): ["out"], drv("b", "b"): ["out"]}},
        drv("c", "shipping"): {"inputDrvs": {aggregate: ["out"]}},
    }

class DagTests(unittest.TestCase):
    def test_real_derivation_graph_fans_out_then_joins_for_shipping(self):
        plan = mkplan()
        report = dag.graph_summary(plan, mkgraph())
        self.assertEqual(report["derivationCount"], 5)
        self.assertEqual(report["checkWaves"], [["a", "b"], ["shipping"]])
        self.assertEqual(report["checkDependencies"]["shipping"], ["a", "b"])
        self.assertEqual(report["checkDependencies"]["a"], [])
        self.assertEqual(report, dag.graph_summary(plan, dict(reversed(list(mkgraph().items())))))

    def test_modern_nix_v3_envelope_and_store_relative_nodes(self):
        plan = mkplan()
        modern = {
            "version": 3,
            "derivations": {
                k.removeprefix("/nix/store/"): {
                    "inputDrvs": {
                        dep.removeprefix("/nix/store/"): outputs
                        for dep, outputs in obj["inputDrvs"].items()
                    }
                }
                for k, obj in mkgraph().items()
            },
        }
        old = dag.graph_summary(plan, mkgraph())
        new = dag.graph_summary(plan, modern)
        self.assertEqual(old, new)

    def test_nix_233_v4_nested_inputs_and_envelope(self):
        plan = mkplan()
        modern = {
            "version": 4,
            "derivations": {
                k.removeprefix("/nix/store/"): {
                    "version": 4,
                    "inputs": {
                        "drvs": {
                            dep.removeprefix("/nix/store/"): outputs
                            for dep, outputs in obj["inputDrvs"].items()
                        },
                        "srcs": [],
                    },
                }
                for k, obj in mkgraph().items()
            },
        }
        self.assertEqual(dag.graph_summary(plan, mkgraph()),
                         dag.graph_summary(plan, modern))

    def test_publish_requires_verified_evidence_and_copies_only_outputs(self):
        plan = mkplan()
        graph = dag.graph_summary(plan, mkgraph())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dag.save(root / "plan.json", plan)
            dag.save(root / "graph.json", graph)
            dag.save(root / "evidence.json", {
                "schemaVersion": dag.SCHEMA, "planDigest": plan["digest"],
                "graphDigest": graph["graphDigest"], "commit": plan["commit"],
                "system": plan["system"], "mode": "native-nix-dag",
                "checks": {name: {"drv": drvpath, "status": "pass", "outputs": [output("f", name)]}
                           for name, drvpath in plan["checks"].items()},
            })
            args = type("Args", (), {"plan": root / "plan.json",
                                     "graph": root / "graph.json",
                                     "evidence": root / "evidence.json",
                                     "to": "file:///tmp/verified-nix-cache"})
            with mock.patch.object(dag.subprocess, "run") as copy:
                dag.publish_command(args)
            copy.assert_called_once_with([
                "nix", "copy", "--to", args.to,
                *sorted(output("f", name) for name in plan["checks"])], check=True)
            args.to = "https://example.com/untrusted"
            with self.assertRaisesRegex(ValueError, "destination"):
                dag.publish_command(args)
            args.to = "file:///tmp/verified-nix-cache"
            broken = json.loads((root / "evidence.json").read_text())
            del broken["checks"]["shipping"]
            dag.save(root / "evidence.json", broken)
            with mock.patch.object(dag.subprocess, "run") as copy:
                with self.assertRaisesRegex(ValueError, "complete|cover"):
                    dag.publish_command(args)
                copy.assert_not_called()

    def test_incomplete_and_cyclic_nix_graph_rejected(self):
        plan = mkplan()
        graph = mkgraph()
        del graph[drv("d", "sdk")]
        with self.assertRaisesRegex(ValueError, "incomplete recursive"):
            dag.graph_summary(plan, graph)
        graph = mkgraph()
        graph[drv("a", "a")]["inputDrvs"][drv("c", "shipping")] = ["out"]
        with self.assertRaisesRegex(ValueError, "cyclic"):
            dag.graph_summary(plan, graph)

    def test_one_build_command_generates_complete_evidence(self):
        plan = mkplan()
        graph = dag.graph_summary(plan, mkgraph())
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            dag.save(directory / "plan.json", plan)
            dag.save(directory / "graph.json", graph)
            args = type("Args", (), {"plan": directory / "plan.json",
                                     "graph": directory / "graph.json",
                                     "output": directory / "evidence.json"})
            raw = [
                {"drvPath": drv("a", "a"), "outputs": {"out": output("f", "a")}},
                {"drvPath": drv("b", "b"), "outputs": {"out": output("g", "b")}},
                {"drvPath": drv("c", "shipping"), "outputs": {"out": output("h", "ship")}},
            ]
            with mock.patch.object(dag.planner, "run_command", return_value=SHA), mock.patch.object(
                    dag.subprocess, "check_output", return_value=json.dumps(raw)) as runner:
                dag.build_command(args)
            runner.assert_called_once_with(
                ["nix", "build", "--no-link", "--json", "-L",
                 ".#checks.x86_64-linux.a", ".#checks.x86_64-linux.b",
                 ".#checks.x86_64-linux.shipping"], text=True)
            proof = type("Args", (), {"plan": args.plan, "graph": args.graph,
                                      "evidence": args.output})
            dag.verify_command(proof)
            evidence = json.loads(args.output.read_text())
            evidence["checks"].pop("a")
            dag.save(args.output, evidence)
            with self.assertRaisesRegex(ValueError, "does not cover"):
                dag.verify_command(proof)

    def test_nix_results_fail_closed_on_missing_output_or_forged_drv(self):
        plan = mkplan()
        checks = plan["checks"]
        with self.assertRaisesRegex(ValueError, "omitted"):
            dag.parse_nix_results(
                [{"drvPath": drv("a", "a"), "outputs": {"out": output("e", "a")}}],
                checks)
        with self.assertRaisesRegex(ValueError, "unexpected"):
            dag.parse_nix_results(
                [{"drvPath": drv("z", "evil"), "outputs": {"out": output("e", "evil")}}],
                checks)
        with self.assertRaisesRegex(ValueError, "invalid realized"):
            dag.parse_nix_results(
                [{"drvPath": drv("a", "a"), "outputs": {"out": "/tmp/forged"}}],
                checks)

if __name__ == "__main__":
    unittest.main()
