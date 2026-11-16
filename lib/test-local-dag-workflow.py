#!/usr/bin/env python3
"""Fail-closed structural regression for the untrusted single-store Nix DAG."""
import unittest
from pathlib import Path

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

    def test_build_only_local_dag_fails_before_compiling_runtime_inventory(self):
        text = WORKFLOW.read_text()
        plan = text.split("      - name: Evaluate full exact-head check inventory and dependency DAG", 1)[1]
        plan = plan.split("      - name: Restore compatible verified-main", 1)[0]
        self.assertIn("flake-test-packages.py plan", plan)
        self.assertIn("--output ci-plan/runtime-inventory.json", plan)
        self.assertIn("'.tests | length > 0'", plan)
        self.assertIn("Runtime test.* packages require", plan)
        self.assertIn("exit 1", plan)

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


if __name__ == "__main__":
    unittest.main()
