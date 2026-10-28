#!/usr/bin/env python3
"""Behavioral proof of generic test package discovery and strict evidence."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
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
    def test_workflow_requires_same_run_handoff_before_execution(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/flake-nix-builders.yml").read_text()
        coordinator = workflow.split("  coordinator:\n", 1)[1].split("  test:\n", 1)[0]
        runner = workflow.split("  test:\n", 1)[1].split("  acceptance:\n", 1)[0]
        self.assertLess(coordinator.index("--test-packages"), coordinator.index("flake-test-packages.py export"))
        self.assertLess(coordinator.index("flake-test-packages.py export"), coordinator.index("name: remote-test-closure-"))
        self.assertIn("if-no-files-found: error", coordinator)
        self.assertIn("needs: [plan, coordinator]", runner)
        self.assertLess(runner.index("name: remote-test-closure-"), runner.index("flake-test-packages.py run"))
        self.assertIn("--closure ci-test-closure/closure.nar", runner)
        self.assertIn("save: false", runner)
        self.assertIn("contents: read", runner)
        for forbidden in ("TS_OAUTH", "id-token: write", "nix build", "secrets: inherit"):
            self.assertNotIn(forbidden, runner)

    def test_closure_handoff_executes_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = root / "plan.json"
            subject.save(plan, test_plan(unit=test_item("unit")))
            fake = root / "nix"
            fake.write_text('''#!'''+sys.executable+'''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps([pathlib.Path(sys.argv[0]).name, *args]) + "\\n")
if args[0] == os.environ.get("FAIL"):
    sys.exit(9)
out = os.environ["OUT"]
if args[0] == "--export":
    assert out in args and out + "-dependency" in args
    sys.stdout.buffer.write(b"closure")
elif args[0] == "--import":
    assert sys.stdin.buffer.read() == b"closure"
    pathlib.Path(os.environ["IMPORTED"]).touch()
elif args[0] == "path-info":
    assert "--offline" in args
    if "--recursive" in args:
        print(out + "-dependency")
    else:
        assert pathlib.Path(os.environ["IMPORTED"]).exists()
    print(out)
elif args[0] == "run":
    assert "--offline" in args and "--no-substitute" in args
    assert args[args.index("--max-jobs") + 1] == "0"
    assert pathlib.Path(os.environ["IMPORTED"]).exists()
else:
    sys.exit(10)
''')
            fake.chmod(0o755)
            (root / "nix-store").symlink_to(fake)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "CALLS": str(root / "calls"), "OUT": test_item("unit")["out"],
                   "IMPORTED": str(root / "imported")}
            archive = root / "closure.nar"
            export_args = type("Args", (), {"plan": plan, "output": archive})()
            run_args = type("Args", (), {"plan": plan, "closure": archive,
                                        "name": "unit", "output": root / "evidence.json"})()
            with mock.patch.dict(os.environ, env), mock.patch.object(subject, "git_head", return_value=C):
                subject.export_command(export_args)
                self.assertEqual(archive.read_bytes(), b"closure")
                subject.run_command(run_args)
                calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
                self.assertEqual([call[1] for call in calls],
                                 ["path-info", "--export", "--import", "path-info", "run"])
                self.assertEqual(subject.load(run_args.output)["status"], "pass")
                for failure in ("--export", "--import", "path-info", "run"):
                    with self.subTest(failure=failure), mock.patch.dict(os.environ, {"FAIL": failure}):
                        before = len((root / "calls").read_text().splitlines())
                        with self.assertRaises(subprocess.CalledProcessError):
                            if failure == "--export":
                                subject.export_command(export_args)
                            else:
                                archive.write_bytes(b"closure")
                                subject.run_command(run_args)
                        tail = [json.loads(line)[1] for line in
                                (root / "calls").read_text().splitlines()[before:]]
                        self.assertEqual(tail[-1], failure)
                with mock.patch.object(subject, "git_head", return_value="b" * 40):
                    with self.assertRaisesRegex(ValueError, "exact head"):
                        subject.export_command(export_args)
                    with self.assertRaisesRegex(ValueError, "exact head"):
                        subject.run_command(run_args)

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
