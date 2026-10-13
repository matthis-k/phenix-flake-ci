#!/usr/bin/env python3
"""Unit tests for generic Nix sharding and exact-head evidence."""
import importlib.util
import json
import tempfile
import unittest
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
