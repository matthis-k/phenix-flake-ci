#!/usr/bin/env python3
"""Behavioral proof of generic test package discovery and strict evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "flake_test_packages", Path(__file__).with_name("flake-test-packages.py"))
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

D = "/nix/store/" + "a" * 32
C = "a" * 40

def test_item(name):
    return {"drv": D + f"-test-{name}.drv", "out": D + f"-test-{name}"}

def test_plan(**entries):
    result = {"schemaVersion": 1, "commit": C,
              "system": "x86_64-linux", "tests": entries}
    result["digest"] = subject.digest(result)
    return result


class TestPackageTests(unittest.TestCase):
    def test_build_then_test_workflow_retains_dag_and_gates_package_fanout(self):
        source = (Path(__file__).resolve().parents[1] / ".github/workflows/flake-build-then-test.yml").read_text()
        plan = source.split("  plan:\n", 1)[1].split("  shared:\n", 1)[0]
        join = source.split("  join-builds:\n", 1)[1].split("  test:\n", 1)[0]
        runnable = source.split("  runnable-test:\n", 1)[1].split("  verify:\n", 1)[0]
        verify = source.split("  verify:\n", 1)[1]
        self.assertIn("flake-test-packages.py plan", plan)
        self.assertIn("steps.runnable.outputs.test_count", plan)
        self.assertLess(join.index("flake-build-test.py join"),
                        join.index("flake-test-packages.py build"))
        self.assertLess(join.index("flake-test-packages.py build"), join.index("nix copy --to"))
        self.assertIn("needs: [plan, join-builds]", runnable)
        self.assertIn("needs.join-builds.result == 'success'", runnable)
        self.assertIn("needs.plan.outputs.test_count != '0'", runnable)
        self.assertIn("fail-fast: false", runnable)
        self.assertIn("fromJSON(needs.plan.outputs.test_matrix)", runnable)
        self.assertLess(runnable.index("nix copy --no-check-sigs --from"),
                        runnable.index("flake-test-packages.py run"))
        self.assertNotIn("nix build", runnable)
        self.assertIn("needs: [plan, shared, join-builds, test, runnable-test]", verify)
        self.assertIn('test "$RUNNABLE_STATUS" = skipped', verify)
        self.assertIn('test "$RUNNABLE_STATUS" = success', verify)
        self.assertIn("flake-test-packages.py verify", verify)
        for command in ("leaf", "join", "verify"):
            self.assertIn("flake-dag-stages.py " + command, source)
        self.assertNotIn("id-token:", source)
        self.assertNotIn("TS_", source)
        self.assertNotIn("secrets: inherit", source)
        self.assertNotIn("primary-key: remote-", source)

    def test_generic_name_and_quoted_nix_attribute(self):
        self.assertEqual(subject.target("x86_64-linux", "basic-smoke"),
                         '.#packages.x86_64-linux."test.basic-smoke"')
        for name in ("", "../bad", "bad.name", "bad space"):
            with self.assertRaises(ValueError):
                subject.target("x86_64-linux", name)

    def test_empty_inventory_is_valid_and_malformed_is_not(self):
        subject.validate(test_plan())
        subject.validate(test_plan(unit=test_item("unit")))
        for value in [
            {**test_plan(), "commit": "dirty"},
            {**test_plan(), "digest": "forged"},
            test_plan(bad={"drv": "not store", "out": "no"}),
        ]:
            with self.assertRaises(ValueError):
                subject.validate(value)

    def test_discovery_uses_actual_flake_packages_without_domain_names(self):
        payload = {
            "test.basic": test_item("basic"),
            "test.integration": test_item("integration"),
        }
        with mock.patch.object(subject.subprocess, "check_output",
                               return_value=json.dumps(payload)) as query:
            self.assertEqual(subject.discover_inventory("x86_64-linux"), {
                "basic": test_item("basic"), "integration": test_item("integration")})
            args = query.call_args.args[0]
            self.assertEqual(args[-1], ".#packages.x86_64-linux")
            self.assertIn("builtins.filter", args[-2])
            self.assertIn("builtins.attrNames", args[-2])

    def test_verify_full_exact_head_set_and_fail_closed(self):
        plan = test_plan(alpha=test_item("alpha"), beta=test_item("beta"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, b = root/"a"/"evidence.json", root/"b"/"evidence.json"
            a.parent.mkdir()
            b.parent.mkdir()
            def evidence(name):
                return {"schemaVersion": 1, "digest": plan["digest"],
                        "commit": C, "name": name, **plan["tests"][name],
                        "status": "pass"}
            subject.save(a, evidence("alpha"))
            subject.save(b, evidence("beta"))
            subject.save(root/"plan.json", plan)
            subject.verify_command(type("Args", (), {"plan": root/"plan.json",
                                                      "results": root})())
            subject.save(b, evidence("alpha"))
            with self.assertRaisesRegex(ValueError, "unknown or repeated"):
                subject.verify_command(type("Args", (), {"plan": root/"plan.json",
                                                          "results": root})())
            subject.save(b, {**evidence("beta"), "commit": "f"*40})
            with self.assertRaisesRegex(ValueError, "failed, stale"):
                subject.verify_command(type("Args", (), {"plan": root/"plan.json",
                                                          "results": root})())
            b.unlink()
            with self.assertRaisesRegex(ValueError, "expected 2"):
                subject.verify_command(type("Args", (), {"plan": root/"plan.json",
                                                          "results": root})())


if __name__ == "__main__":
    unittest.main()
