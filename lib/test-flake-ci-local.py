#!/usr/bin/env python3
"""Regression tests for local CI reuse of the GitHub planner and runtime runner."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


LIB = Path(__file__).parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, LIB / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


local = module("flake_ci_local", "flake-ci-local.py")
runtime = module("flake_test_packages", "flake-test-packages.py")


class LocalCITests(unittest.TestCase):
    def args(self, directory, **overrides):
        values = {
            "config": "ci/flake-check-config.nix",
            "output_dir": directory,
            "checks_only": False,
            "timeout_seconds": 300,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def harness(self, case, names):
        calls = []

        def check_output(argv, **kwargs):
            if argv[1:3] == ["rev-parse", "--show-toplevel"]:
                return str(Path.cwd())
            if argv[1:3] == ["status", "--porcelain"]:
                return ""
            if argv[:3] == ["nix", "eval", "--json"]:
                return '{"schemaVersion":1,"system":"x86_64-linux"}'
            raise AssertionError(f"unexpected query: {argv!r}")

        def run(script, *args):
            args = list(map(str, args))
            calls.append((script, args))
            if script == "flake-test-packages.py" and args[0] == "plan":
                path = Path(args[args.index("--output") + 1])
                path.write_text(json.dumps({"tests": {name: {} for name in names}}))

        with mock.patch.object(local.subprocess, "check_output",
                               side_effect=check_output), mock.patch.object(
                                   local, "command", side_effect=run):
            local.run(case)
        return calls

    def test_full_local_build_and_runtime_preserve_order_and_no_nar(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = self.harness(self.args(directory), ["rpc", "rpc-repeat"])
            steps = [(s, a[0]) for s, a in calls]
            self.assertEqual(steps, [
                ("flake-check-plan.py", "plan"),
                ("flake-check-dag.py", "analyze"),
                ("flake-test-packages.py", "plan"),
                ("flake-check-dag.py", "build"),
                ("flake-check-dag.py", "verify"),
                ("flake-test-packages.py", "probe"),
                ("flake-test-packages.py", "batch"),
                ("flake-test-packages.py", "verify"),
            ])
            build = calls[3][1]
            self.assertIn("--test-packages", build)
            self.assertIn("--test-root", build)
            batch = calls[6][1]
            self.assertIn("--local", batch)
            self.assertIn("--available", batch)
            self.assertNotIn("--closure", batch)
            self.assertNotIn("--expected-commit", batch)
            self.assertEqual(json.loads(batch[batch.index("--names-json") + 1]),
                             ["rpc", "rpc-repeat"])
            self.assertTrue(list(Path(directory).glob("run-*/config.json")))

    def test_build_failure_still_runs_runtime_and_never_reports_success(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def check_output(argv, **kwargs):
                if argv[1:3] == ["rev-parse", "--show-toplevel"]:
                    return str(Path.cwd())
                if argv[1:3] == ["status", "--porcelain"]:
                    return ""
                if argv[:3] == ["nix", "eval", "--json"]:
                    return '{"schemaVersion":1,"system":"x86_64-linux"}'
                raise AssertionError(argv)

            def run(script, *args):
                args = list(map(str, args))
                calls.append((script, args[0]))
                if script == "flake-test-packages.py" and args[0] == "plan":
                    Path(args[args.index("--output") + 1]).write_text(
                        json.dumps({"tests": {"rpc": {}}}))
                if script == "flake-check-dag.py" and args[0] == "build":
                    raise subprocess.CalledProcessError(100, ["nix", "build"])

            with mock.patch.object(local.subprocess, "check_output",
                                   side_effect=check_output), mock.patch.object(
                                       local, "command", side_effect=run):
                with self.assertRaisesRegex(ValueError, "incomplete build"):
                    local.run(self.args(directory))
            self.assertIn(("flake-test-packages.py", "probe"), calls)
            self.assertIn(("flake-test-packages.py", "batch"), calls)
            self.assertIn(("flake-test-packages.py", "verify"), calls)
            self.assertNotIn(("flake-check-dag.py", "verify"), calls)

    def test_failed_dag_evidence_still_runs_runtime_and_never_passes(self):
        # A successful Nix invocation does not imply acceptable check
        # evidence. Runtime tests still run for diagnostic completeness.
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def check_output(argv, **kwargs):
                if argv[1:3] == ["rev-parse", "--show-toplevel"]:
                    return str(Path.cwd())
                if argv[1:3] == ["status", "--porcelain"]:
                    return ""
                if argv[:3] == ["nix", "eval", "--json"]:
                    return '{"schemaVersion":1,"system":"x86_64-linux"}'
                raise AssertionError(argv)

            def run(script, *args):
                args = list(map(str, args))
                calls.append((script, args[0]))
                if script == "flake-test-packages.py" and args[0] == "plan":
                    Path(args[args.index("--output") + 1]).write_text(
                        json.dumps({"tests": {"rpc": {}}}))
                if script == "flake-check-dag.py" and args[0] == "verify":
                    raise subprocess.CalledProcessError(2, ["verify"])

            with mock.patch.object(local.subprocess, "check_output",
                                   side_effect=check_output), mock.patch.object(
                                       local, "command", side_effect=run):
                with self.assertRaisesRegex(ValueError, "incomplete build"):
                    local.run(self.args(directory))
            self.assertLess(calls.index(("flake-check-dag.py", "verify")),
                            calls.index(("flake-test-packages.py", "batch")))
            self.assertIn(("flake-test-packages.py", "probe"), calls)
            self.assertIn(("flake-test-packages.py", "verify"), calls)

    def test_no_runtime_cases_skips_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = self.harness(self.args(directory), [])
            self.assertEqual([(s, a[0]) for s, a in calls], [
                ("flake-check-plan.py", "plan"),
                ("flake-check-dag.py", "analyze"),
                ("flake-test-packages.py", "plan"),
                ("flake-check-dag.py", "build"),
                ("flake-check-dag.py", "verify"),
            ])
            self.assertNotIn("--test-root", calls[3][1])

    def test_checks_only_skips_runtime_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = self.harness(self.args(directory, checks_only=True), ["rpc"])
            self.assertEqual([s for s, _ in calls], [
                "flake-check-plan.py", "flake-check-dag.py",
                "flake-check-dag.py", "flake-check-dag.py",
            ])
            self.assertNotIn("--test-packages", calls[2][1])

    def test_dirty_checkout_fails_before_nix_and_no_false_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            def dirty(argv, **kwargs):
                if argv[1:3] == ["rev-parse", "--show-toplevel"]:
                    return str(Path.cwd())
                return " M flake.nix"
            with mock.patch.object(local.subprocess, "check_output",
                                   side_effect=dirty), mock.patch.object(
                                       local, "command") as runner:
                with self.assertRaisesRegex(ValueError, "committed tracked files"):
                    local.run(self.args(directory))
                runner.assert_not_called()

    def test_bad_timeout_rejected_before_nix(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(local, "require_clean_root"), mock.patch.object(
                    local.subprocess, "check_output") as nix:
                with self.assertRaisesRegex(ValueError, "timeout"):
                    local.run(self.args(directory, timeout_seconds=0))
                nix.assert_not_called()


class LocalRuntimeTests(unittest.TestCase):
    def test_local_verifies_real_checkout_and_existing_outputs_no_import(self):
        plan = {"commit": "a" * 40, "tests": {"rpc": {}}}
        args = argparse.Namespace(local=True, closure=None, expected_commit=None)
        with mock.patch.object(runtime, "verify_head") as head, mock.patch.object(
                runtime, "verify_outputs") as outputs, mock.patch.object(
                    runtime, "import_closure") as imported:
            runtime.prepare_execution(plan, ["rpc"], args)
            head.assert_called_once_with(plan)
            outputs.assert_called_once_with(plan, ["rpc"])
            imported.assert_not_called()

    def test_local_cannot_supply_commit_or_archive(self):
        plan = {"commit": "a" * 40}
        for archive, commit in (("closure.nar", None), (None, "a" * 40)):
            args = argparse.Namespace(local=True, closure=archive,
                                      expected_commit=commit)
            with self.assertRaisesRegex(ValueError, "cannot be combined"):
                runtime.prepare_execution(plan, [], args)

    def test_artifact_runner_requires_archive_and_still_imports(self):
        plan = {"commit": "a" * 40}
        with self.assertRaisesRegex(ValueError, "--closure is required"):
            runtime.prepare_execution(
                plan, [], argparse.Namespace(local=False, closure=None,
                                             expected_commit="a" * 40))
        args = argparse.Namespace(local=False, closure="closure.nar",
                                  expected_commit="a" * 40)
        with mock.patch.object(runtime, "verify_head") as head, mock.patch.object(
                runtime, "verify_outputs") as outputs, mock.patch.object(
                    runtime, "import_closure") as imported:
            runtime.prepare_execution(plan, ["rpc"], args)
            head.assert_called_once_with(plan, args.expected_commit)
            imported.assert_called_once_with("closure.nar")
            outputs.assert_called_once_with(plan, ["rpc"])


if __name__ == "__main__":
    unittest.main()
