#!/usr/bin/env python3
"""Unit tests for generic Nix sharding and exact-head evidence."""
import importlib.util
import os
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "flake_check_plan", Path(__file__).with_name("flake-check-plan.py"))
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)
SHA = "a" * 40
CONFIG = {"schemaVersion": 1, "system": "x86_64-linux", "shardCount": 3,
          "checkHints": {"*shipping*": {"isolate": True, "weight": 100}}}


def checks(*names):
    return {n: "/nix/store/" + ("b" * 32) + "-" + n + ".drv" for n in names}


class TestFlakeChecks(unittest.TestCase):
    def test_new_check_enters_exact_inventory(self):
        a = planner.make_plan(checks("a", "b"), CONFIG, SHA)
        b = planner.make_plan(checks("a", "b", "new"), CONFIG, SHA)
        self.assertEqual(set(b["checks"]) - set(a["checks"]), {"new"})
        self.assertEqual(sum(map(lambda x: len(x["checks"]), b["shards"])), 3)
        self.assertNotEqual(a["digest"], b["digest"])

    def test_deterministic_shards_and_shipping_isolation(self):
        names = ["m1-shipping", "c", "b", "a", "z", "foo"]
        plan = planner.make_plan(checks(*names), CONFIG, SHA)
        self.assertEqual(plan["shards"][0]["checks"], ["m1-shipping"])
        self.assertEqual(plan, planner.make_plan(checks(*reversed(names)), CONFIG, SHA))
        planner.validate_plan(plan)

    def test_affinity_colocates_checks_with_shared_nix_builds(self):
        cfg = {**CONFIG, "checkHints": {
            "*shipping*": {"isolate": True, "weight": 100},
            "b2-*": {"affinity": "state", "weight": 3},
            "b3-*": {"affinity": "state", "weight": 2},
            "f*": {"affinity": "frontend"},
        }}
        checkset = checks("m1-shipping", "b2-session", "b2-sqlite",
                          "b3-memory", "f1-frontends", "f2-acp", "z")
        plan = planner.make_plan(checkset, cfg, SHA)
        assignments = {name: shard["id"] for shard in plan["shards"]
                       for name in shard["checks"]}
        self.assertEqual(len(assignments), len(checkset))
        self.assertEqual({assignments[k] for k in
                          ("b2-session", "b2-sqlite", "b3-memory")},
                         {assignments["b2-session"]})
        self.assertEqual(assignments["f1-frontends"], assignments["f2-acp"])
        self.assertEqual(plan["shards"][0]["checks"], ["m1-shipping"])
        self.assertEqual(plan, planner.make_plan(dict(reversed(list(checkset.items()))),
                                                  cfg, SHA))
        planner.validate_plan(plan)

    def test_affinity_rejects_malformed_and_conflicting_hints(self):
        for hint in ({"affinity": ""}, {"affinity": "../../bad"},
                     {"affinity": "state", "isolate": True}):
            cfg = {**CONFIG, "checkHints": {"a": hint}}
            with self.subTest(hint=hint), self.assertRaises(ValueError):
                planner.make_plan(checks("a"), cfg, SHA)

    def test_no_silent_missing_or_duplicate_checks(self):
        plan = planner.make_plan(checks("a", "b", "c"), CONFIG, SHA)
        plan["shards"][0]["checks"].append("a")
        plan["digest"] = planner.digest({k: v for k, v in plan.items() if k != "digest"})
        with self.assertRaisesRegex(ValueError, "partition"):
            planner.validate_plan(plan)

    def test_invalid_inputs_fail_closed(self):
        for invalid in [
            checks("../bad"),
            {"valid": "not a derivation"},
            {},
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                planner.make_plan(invalid, CONFIG, SHA)
        with self.assertRaisesRegex(ValueError, "not enough shards"):
            planner.make_plan(checks("a-shipping", "b-shipping", "c"),
                              {**CONFIG, "shardCount": 2}, SHA)

    def evidence(self, plan, shard):
        return {"schemaVersion": 1, "digest": plan["digest"],
                "commit": plan["commit"], "system": plan["system"],
                "shard": shard["id"], "status": "pass",
                "checks": {name: {"drv": plan["checks"][name], "status": "pass",
                                  "outputs": ["/nix/store/" + "c" * 32 + "-" + name]}
                           for name in shard["checks"]}}

    def test_evidence_gate_requires_all_exact_head_outputs(self):
        plan = planner.make_plan(checks("a", "b", "c", "m1-shipping"), CONFIG, SHA)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner.write_json(root / "plan.json", plan)
            for shard in plan["shards"]:
                planner.write_json(root / shard["id"] / "evidence.json",
                                   self.evidence(plan, shard))
            args = type("Args", (), {"plan": root / "plan.json", "directory": root})
            planner.verify_command(args)
            one = root / plan["shards"][0]["id"] / "evidence.json"
            data = json.loads(one.read_text())
            data["commit"] = "d" * 40
            planner.write_json(one, data)
            with self.assertRaisesRegex(ValueError, "stale"):
                planner.verify_command(args)
            data["commit"] = SHA
            planner.write_json(one, data)
            one.unlink()
            with self.assertRaisesRegex(ValueError, "expected"):
                planner.verify_command(args)


    def test_cache_key_uses_declared_inputs_without_source_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                Path("Cargo.lock").write_text("version one")
                cfg = {**CONFIG, "cache": {"keyPrefix": "rust", "keyFiles": ["Cargo.lock"]}}
                first = planner.make_plan(checks("a"), cfg, SHA)
                self.assertTrue(first["cachePrefix"].startswith("rust-x86_64-linux-"))
                changed_commit = planner.make_plan(checks("a"), cfg, "d" * 40)
                self.assertEqual(first["cachePrefix"], changed_commit["cachePrefix"])
                Path("Cargo.lock").write_text("version two")
                after = planner.make_plan(checks("a"), cfg, SHA)
                self.assertNotEqual(first["cachePrefix"], after["cachePrefix"])
            finally:
                os.chdir(previous)

    def test_prebuild_artifacts_are_required_and_authenticated(self):
        cfg = {**CONFIG, "prebuildTargets": [".#kernel"]}
        plan = planner.make_plan(checks("a"), cfg, SHA)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner.write_json(root / "plan.json", plan)
            planner.write_json(root / "s0" / "evidence.json",
                               self.evidence(plan, plan["shards"][0]))
            # Single-check inventory means one shard despite shardCount=3.
            pre = {"schemaVersion": 1, "digest": plan["digest"], "commit": SHA,
                   "outputs": {".#kernel": ["/nix/store/" + "d" * 32 + "-kernel"]}}
            planner.write_json(root / "prebuild.json", pre)
            args = type("Args", (), {"plan": root / "plan.json",
                                    "directory": root, "prebuild": root / "prebuild.json"})
            planner.verify_command(args)
            pre["outputs"] = {}
            planner.write_json(root / "prebuild.json", pre)
            with self.assertRaisesRegex(ValueError, "missing"):
                planner.verify_command(args)


    def test_prebuild_uses_one_concurrent_nix_invocation(self):
        config = {**CONFIG, "prebuildTargets": [".#kernel", ".#shared-plugin"]}
        plan = planner.make_plan(checks("a"), config, SHA)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner.write_json(root / "plan.json", plan)
            args = type("Args", (), {"plan": root / "plan.json", "output": root / "prebuild.json"})
            def fake_command(argv):
                if argv[0] == "git":
                    return SHA
                return "/nix/store/" + "c" * 32 + "-example-output"
            with mock.patch.object(planner.subprocess, "run") as build, mock.patch.object(
                    planner, "run_command", side_effect=fake_command) as command:
                planner.prebuild_command(args)
            build.assert_called_once_with(
                ["nix", "build", "--no-link", "-L", ".#kernel", ".#shared-plugin"],
                check=True)
            self.assertEqual(command.call_count, 3)
            evidence = json.loads((root / "prebuild.json").read_text())
            self.assertEqual(set(evidence["outputs"]), set(config["prebuildTargets"]))

    def test_prebuild_must_be_locally_restored_not_rebuilt_by_shard(self):
        cfg = {**CONFIG, "prebuildTargets": [".#shared"]}
        plan = planner.make_plan(checks("a"), cfg, SHA)
        output = "/nix/store/" + "d" * 32 + "-shared"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner.write_json(root / "plan.json", plan)
            planner.write_json(root / "prebuild.json", {
                "schemaVersion": 1, "digest": plan["digest"], "commit": SHA,
                "outputs": {".#shared": [output]},
            })
            args = type("Args", (), {
                "plan": root / "plan.json", "prebuild": root / "prebuild.json"})
            with mock.patch.object(planner, "run_command", return_value=output) as cmd:
                planner.prebuild_local_command(args)
            cmd.assert_called_once_with(["nix", "path-info", "--offline", output])
            with mock.patch.object(planner, "run_command", return_value=""):
                with self.assertRaisesRegex(ValueError, "missing from shard"):
                    planner.prebuild_local_command(args)
            evidence = json.loads((root / "prebuild.json").read_text())
            evidence["digest"] = "forged"
            planner.write_json(root / "prebuild.json", evidence)
            with self.assertRaisesRegex(ValueError, "stale or mismatched"):
                planner.prebuild_local_command(args)

    def test_skipped_and_forged_passes_fail(self):
        plan = planner.make_plan(checks("a"), CONFIG, SHA)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner.write_json(root / "plan.json", plan)
            item = self.evidence(plan, plan["shards"][0])
            item["checks"]["a"]["drv"] = "forged"
            planner.write_json(root / "shard" / "evidence.json", item)
            args = type("Args", (), {"plan": root / "plan.json", "directory": root})
            with self.assertRaisesRegex(ValueError, "invalid result"):
                planner.verify_command(args)
            item["checks"]["a"]["drv"] = plan["checks"]["a"]
            item["status"] = "failed"
            planner.write_json(root / "shard" / "evidence.json", item)
            with self.assertRaisesRegex(ValueError, "failed"):
                planner.verify_command(args)


if __name__ == "__main__":
    unittest.main()
