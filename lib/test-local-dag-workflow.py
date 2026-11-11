#!/usr/bin/env python3
"""Fail-closed structural regression for the untrusted single-store Nix DAG."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/flake-local-dag.yml"


class LocalDagWorkflowTest(unittest.TestCase):
    def test_untrusted_capabilities_and_cache(self):
        text = WORKFLOW.read_text()
        self.assertIn("workflow_call:", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertNotIn("id-token: write", text)
        self.assertNotIn("TS_OAUTH", text)
        self.assertNotIn("tailscale", text.lower().split("jobs:", 1)[-1])
        self.assertIn("save: false", text)
        self.assertIn("remote-${{ steps.plan.outputs.cache_prefix }}-", text)
        self.assertIn("persist-credentials: false", text)

    def test_full_exact_head_with_one_build_and_verified_evidence(self):
        text = WORKFLOW.read_text()
        self.assertIn("github.event.pull_request.head.sha || github.sha", text)
        self.assertIn("inputs.library_ref", text)
        for operation in (
            "flake-check-plan.py plan",
            "flake-check-dag.py analyze",
            "flake-check-dag.py build",
            "flake-check-dag.py verify",
        ):
            self.assertEqual(text.count(operation), 1, operation)
        self.assertIn("--evidence ci-evidence/evidence.json", text)
        self.assertIn("ci-plan/plan.json", text)
        self.assertNotIn("matrix:", text)
        self.assertNotIn("flake-check-plan.py build", text)


    def test_plan_publishes_nonempty_cache_namespace_consumed_by_workflow(self):
        # Execute the real planner, not a textual mock of its intended output.
        root = Path(__file__).resolve().parents[1]
        module_path = root / "lib/flake-check-plan.py"
        spec = importlib.util.spec_from_file_location("flake_check_plan_probe", module_path)
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "config.json"
            checks = path / "inventory.json"
            plan = path / "plan.json"
            gh_output = path / "github-output.txt"
            config.write_text(json.dumps({
                "schemaVersion": 1,
                "system": "x86_64-linux",
                "shardCount": 1,
                "cache": {"keyPrefix": "review-cache-probe"},
            }))
            checks.write_text(json.dumps({
                "smoke": "/nix/store/" + "a" * 32 + "-smoke.drv",
            }))
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_output)}):
                planner.plan_command(SimpleNamespace(
                    config=str(config), inventory=str(checks),
                    flake=".", commit="a" * 40, output=str(plan),
                ))
            values = dict(row.split("=", 1) for row in gh_output.read_text().splitlines())
            expected = json.loads(plan.read_text())["cachePrefix"]
            self.assertEqual(values.get("cache_prefix"), expected)
            self.assertRegex(expected, r"^review-cache-probe-x86_64-linux-[0-9a-f]{20}$")
            workflow = WORKFLOW.read_text()
            self.assertIn("id: plan", workflow)
            self.assertIn("flake-check-plan.py plan", workflow)
            self.assertIn("steps.plan.outputs.cache_prefix", workflow)
            self.assertIn("remote-${{ steps.plan.outputs.cache_prefix }}-", workflow)
            # A changed strategy must invalidate the namespace rather than
            # inheriting an unrelated trusted-main cache.
            updated = json.loads(config.read_text())
            updated["strategy"] = "different-strategy"
            config.write_text(json.dumps(updated))
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_output)}):
                planner.plan_command(SimpleNamespace(
                    config=str(config), inventory=str(checks),
                    flake=".", commit="a" * 40, output=str(plan),
                ))
            self.assertNotEqual(json.loads(plan.read_text())["cachePrefix"], expected)


if __name__ == "__main__":
    unittest.main()
