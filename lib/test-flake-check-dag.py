#!/usr/bin/env python3
"""Regression cases for native, dependency-driven Nix CI."""
import importlib.util
import json
import tempfile
import subprocess
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

    def test_deep_chain_uses_constant_depth_iteration(self):
        # Real Nix flakes can have thousands of sequential derivations.
        # A recursive closure expander hits Python's recursion limit here.
        size = 2500
        paths = [f"/nix/store/{index + 1:032x}-node-{index}.drv"
                 for index in range(size)]
        graph = {
            path: {"inputDrvs": {paths[index - 1]: ["out"]} if index else {}}
            for index, path in enumerate(paths)
        }
        checks = {"start": paths[0], "middle": paths[1250], "end": paths[-1]}
        plan = dag.planner.make_plan(
            checks, {"schemaVersion": 1, "system": "x86_64-linux",
                     "shardCount": 3}, SHA)
        report = dag.graph_summary(plan, graph)
        self.assertEqual(report["derivationCount"], size)
        self.assertEqual(report["checkWaves"], [["start"], ["middle"], ["end"]])
        self.assertEqual(report["checkDependencies"]["end"], ["middle", "start"])
        self.assertEqual(report["criticalCheckDepth"], 2)

    def test_deep_named_check_waves_group_in_stable_order(self):
        # Many named checks in separate waves formerly rescanned the entire
        # named-check inventory once per wave. Cover both depth and ordering.
        size = 512
        paths = [f"/nix/store/{index + 1:032x}-chain-{index}.drv"
                 for index in range(size)]
        names = [f"check-{index:04d}" for index in range(size)]
        checks = dict(zip(reversed(names), reversed(paths)))
        graph = {
            path: {"inputDrvs": {paths[index - 1]: ["out"]} if index else {}}
            for index, path in enumerate(paths)
        }
        plan = dag.planner.make_plan(
            checks, {"schemaVersion": 1, "system": "x86_64-linux",
                     "shardCount": 3}, SHA)
        report = dag.graph_summary(plan, graph)
        self.assertEqual(report["criticalCheckDepth"], size - 1)
        self.assertEqual(report["checkWaves"],
                         [[name] for name in names])
        self.assertEqual(report["checkDependencies"][names[-1]],
                         names[:-1])

    def test_wide_fan_in_and_named_derivation_aliases(self):
        # Many unrelated leaves should not balloon into transitive set copies.
        leaves = [f"/nix/store/{index + 100:032x}-leaf-{index}.drv"
                  for index in range(1200)]
        core = drv("a", "check-core")
        out = drv("b", "check-output")
        graph = {node: {"inputDrvs": {}} for node in leaves}
        graph[core] = {"inputDrvs": {node: ["out"] for node in leaves}}
        graph[out] = {"inputDrvs": {core: ["out"]}}
        checks = {"core": core, "alias": core, "output": out}
        plan = dag.planner.make_plan(
            checks, {"schemaVersion": 1, "system": "x86_64-linux",
                     "shardCount": 3}, SHA)
        report = dag.graph_summary(plan, graph)
        self.assertEqual(report["checkDependencies"]["core"], [])
        self.assertEqual(report["checkDependencies"]["alias"], [])
        self.assertEqual(report["checkDependencies"]["output"], ["alias", "core"])
        self.assertEqual(report["checkWaves"], [["alias", "core"], ["output"]])

    def test_thousands_of_independent_check_roots_do_not_need_full_scan(self):
        # Width, not only depth, must scale. Independent checks have an
        # empty ancestor mask; only the join should decode two upstream bits.
        count = 4096
        paths = [f"/nix/store/{index + 1:032x}-wide-{index}.drv"
                 for index in range(count)]
        names = [f"check-{index:04d}" for index in range(count)]
        checks = dict(zip(names, paths))
        join = drv("f", "wide-join")
        checks["join"] = join
        graph = {path: {"inputDrvs": {}} for path in paths}
        graph[join] = {"inputDrvs": {
            paths[1]: ["out"], paths[-2]: ["out"]}}
        plan = dag.planner.make_plan(
            checks, {"schemaVersion": 1, "system": "x86_64-linux",
                     "shardCount": 3}, SHA)
        report = dag.graph_summary(plan, graph)
        self.assertEqual(report["derivationCount"], count + 1)
        self.assertEqual(report["checkDependencies"]["join"],
                         [names[1], names[-2]])
        self.assertEqual(report["checkWaves"], [sorted(names), ["join"]])
        self.assertTrue(all(report["checkDependencies"][name] == []
                            for name in names))

    def test_disconnected_cyclic_derivations_fail_closed(self):
        # Validate the *entire* recursive graph, not just paths reached from
        # check roots. Unexpected cyclic records must never be hidden.
        graph = mkgraph()
        hidden_a = drv("x", "hidden-a")
        hidden_b = drv("y", "hidden-b")
        graph[hidden_a] = {"inputDrvs": {hidden_b: ["out"]}}
        graph[hidden_b] = {"inputDrvs": {hidden_a: ["out"]}}
        with self.assertRaisesRegex(ValueError, "cyclic"):
            dag.graph_summary(mkplan(), graph)

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
                ["nix", "build", "--keep-going", "--no-link", "--json", "-L",
                 '.#checks.x86_64-linux."a"', '.#checks.x86_64-linux."b"',
                 '.#checks.x86_64-linux."shipping"'], text=True)
            proof = type("Args", (), {"plan": args.plan, "graph": args.graph,
                                      "evidence": args.output})
            dag.verify_command(proof)
            evidence = json.loads(args.output.read_text())
            evidence["checks"].pop("a")
            dag.save(args.output, evidence)
            with self.assertRaisesRegex(ValueError, "does not cover"):
                dag.verify_command(proof)

    def test_checks_and_test_packages_share_one_compilation_graph(self):
        import hashlib
        plan = mkplan()
        graph = dag.graph_summary(plan, mkgraph())
        test_inventory = {
            "schemaVersion": 2, "commit": SHA, "system": "x86_64-linux",
            "tests": {"rpc": {"drv": drv("r", "rpc"),
                               "out": output("r", "rpc"),
                               "exe": output("r", "rpc") + "/bin/test-rpc"}}}
        test_inventory["digest"] = hashlib.sha256(json.dumps(
            test_inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            dag.save(directory / "plan.json", plan)
            dag.save(directory / "graph.json", graph)
            dag.save(directory / "tests.json", test_inventory)
            args = type("Args", (), {"plan": directory / "plan.json",
                                     "graph": directory / "graph.json",
                                     "test_packages": directory / "tests.json",
                                     "test_root": str(directory / "roots"),
                                     "output": directory / "build.json"})
            raw = [
                {"drvPath": drv("a", "a"), "outputs": {"out": output("f", "a")}},
                {"drvPath": drv("b", "b"), "outputs": {"out": output("g", "b")}},
                {"drvPath": drv("c", "shipping"), "outputs": {"out": output("h", "ship")}},
                {"drvPath": drv("r", "rpc"), "outputs": {"out": output("r", "rpc")}},
            ]
            with mock.patch.object(dag.planner, "run_command", return_value=SHA), mock.patch.object(
                    dag.subprocess, "check_output", return_value=json.dumps(raw)) as build, mock.patch.object(
                    dag.subprocess, "run") as pinned:
                dag.build_command(args)
            build.assert_called_once_with(
                ["nix", "build", "--keep-going", "--no-link", "--json", "-L",
                 '.#checks.x86_64-linux."a"', '.#checks.x86_64-linux."b"',
                 '.#checks.x86_64-linux."shipping"',
                 '.#packages.x86_64-linux."test.rpc"'], text=True)
            pinned.assert_called_once_with(
                ["nix", "build", "--offline", "--no-substitute", "--max-jobs", "0",
                 "--option", "builders", "", "--out-link", str(directory / "roots"),
                 '.#packages.x86_64-linux."test.rpc"'], check=True)
            self.assertEqual(set(json.loads(args.output.read_text())["checks"]), set(plan["checks"]))

            test_inventory["commit"] = "b" * 40
            test_inventory["digest"] = hashlib.sha256(json.dumps(
                {k: v for k, v in test_inventory.items() if k != "digest"},
                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            dag.save(args.test_packages, test_inventory)
            with mock.patch.object(dag.planner, "run_command", return_value=SHA), mock.patch.object(
                    dag.subprocess, "check_output") as should_not_build:
                with self.assertRaisesRegex(ValueError, "exact-head"):
                    dag.build_command(args)
                should_not_build.assert_not_called()

    def test_failed_full_graph_still_requests_all_checks_and_runtime_roots(self):
        # The failure-only staged backend uses this same native DAG builder.
        # On nonzero Nix exit every independent check and executable package
        # must have been submitted, with no forged passing evidence.
        import hashlib
        plan = mkplan()
        graph = dag.graph_summary(plan, mkgraph())
        item = {"drv": drv("r", "rpc"), "out": output("r", "rpc"),
                "exe": output("r", "rpc") + "/bin/test-rpc"}
        test_plan = {"schemaVersion": 2, "commit": SHA,
                     "system": plan["system"], "tests": {"rpc": item}}
        test_plan["digest"] = hashlib.sha256(json.dumps(
            test_plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dag.save(root / "plan.json", plan)
            dag.save(root / "graph.json", graph)
            dag.save(root / "tests.json", test_plan)
            args = type("Args", (), {
                "plan": root / "plan.json", "graph": root / "graph.json",
                "test_packages": root / "tests.json",
                "test_root": root / "test-roots",
                "output": root / "recovery-evidence.json",
            })()
            with mock.patch.object(dag.planner, "run_command", return_value=SHA), \
                 mock.patch.object(dag.subprocess, "check_output",
                                   side_effect=subprocess.CalledProcessError(
                                       100, ["nix", "build"])) as build:
                with self.assertRaises(subprocess.CalledProcessError):
                    dag.build_command(args)
            invocation = build.call_args.args[0]
            self.assertIn("--keep-going", invocation)
            for name in plan["checks"]:
                self.assertIn(dag.planner.check_target(plan["system"], name), invocation)
            self.assertIn('.#packages.x86_64-linux."test.rpc"', invocation)
            self.assertFalse(args.output.exists())

    def test_failed_nix_graph_propagates_failure_after_keep_going(self):
        # Nix owns scheduling; the command is nonzero only after attempting
        # every independent root. Never synthesize passing check evidence.
        roots = {drv("a", "a"), drv("b", "b")}
        with mock.patch.object(dag.subprocess, "check_output",
                               side_effect=subprocess.CalledProcessError(100, ["nix", "build"])) as build:
            with self.assertRaises(subprocess.CalledProcessError):
                dag.realize_roots(roots, [".#checks.x86_64-linux.a",
                                          ".#checks.x86_64-linux.b"])
        arguments = build.call_args.args[0]
        self.assertIn("--keep-going", arguments)
        self.assertIn(".#checks.x86_64-linux.a", arguments)
        self.assertIn(".#checks.x86_64-linux.b", arguments)

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
