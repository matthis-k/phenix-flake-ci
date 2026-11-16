#!/usr/bin/env python3
"""Ensure reusable CI Nix store retention is configurable and consistent."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    "flake-checks.yml",
    "flake-dag-staged.yml",
    "flake-nix-builders.yml",
)
SETTING = "cache_max_store_size_linux"


class CacheRetentionWorkflowTests(unittest.TestCase):
    def test_every_reusable_workflow_declares_and_uses_the_same_policy(self):
        defaults = set()
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                source = (ROOT / ".github" / "workflows" / name).read_text()
                declaration = re.search(
                    r"^\s+" + SETTING
                    + r":(?:\s*\{[^}]*default:\s*[\"']?(\d+G)[\"']?[^}]*\}|"
                    + r"\s*\n(?:\s+[^\n]*\n)*?\s+default:\s*[\"']?(\d+G)[\"']?)",
                    source,
                    re.MULTILINE,
                )
                self.assertIsNotNone(declaration, name)
                defaults.add(next(value for value in declaration.groups() if value is not None))
                self.assertEqual(
                    source.count(
                        "gc-max-store-size-linux: ${{ inputs." + SETTING + " }}"
                    ),
                    1,
                    "only the shared cache writer must own GC retention",
                )
                self.assertNotIn('gc-max-store-size-linux: "4G"', source)
        self.assertEqual(defaults, {"6G"})

    def test_build_only_matrix_cannot_silently_skip_runtime_packages(self):
        source = (ROOT / ".github" / "workflows" / "flake-checks.yml").read_text()
        plan = source.split("  plan:\n", 1)[1].split("  prebuild:\n", 1)[0]
        self.assertIn("flake-test-packages.py plan", plan)
        self.assertIn("--output ci-plan/runtime-inventory.json", plan)
        self.assertIn("'.tests | length > 0'", plan)
        self.assertIn("Runtime test.* packages require", plan)
        self.assertLess(plan.index("flake-test-packages.py plan"),
                        plan.index("actions/upload-artifact@"))
        self.assertIn("exit 1", plan)

    def test_read_only_shards_never_publish_shared_store(self):
        for name in ("flake-checks.yml", "flake-dag-staged.yml"):
            source = (ROOT / ".github" / "workflows" / name).read_text()
            self.assertIn("save: false", source)

    def test_staged_pr_prebuild_restores_trusted_main_read_only(self):
        source = (ROOT / ".github" / "workflows" / "flake-dag-staged.yml").read_text()
        self.assertIn("permissions:\n  contents: read", source)
        self.assertNotIn("id-token: write", source)
        self.assertNotIn("TS_OAUTH_CLIENT_ID", source)
        self.assertNotIn("secrets: inherit", source)
        writer = source.split("  shared:\n", 1)[1].split("  leaf:\n", 1)[0]
        self.assertIn(
            "primary-key: ${{ needs.plan.outputs.cache_prefix }}-${{ github.sha }}",
            writer,
        )
        self.assertIn(
            "restore-prefixes-first-match: |\n"
            "            remote-${{ needs.plan.outputs.cache_prefix }}-\n"
            "            ${{ needs.plan.outputs.cache_prefix }}-",
            writer,
        )
        self.assertNotIn("primary-key: remote-", writer)
        # PR-origin data never overwrites or publishes trusted-main cache keys.
        self.assertNotIn("TS_", writer)
        for job in ("  leaf:\n", "  join:\n"):
            section = source.split(job, 1)[1]
            if job == "  leaf:\n":
                section = section.split("  join:\n", 1)[0]
            self.assertIn("save: false", section)
            self.assertNotIn("save: true", section)
            self.assertNotIn("secrets: inherit", section)
            self.assertNotIn(
                "remote-${{ needs.plan.outputs.cache_prefix }}-", section,
                "leaf/join must restore the exact-head prebuild cache",
            )

    def test_remote_builder_cache_stays_in_trusted_coordinator(self):
        source = (ROOT / ".github" / "workflows" / "flake-nix-builders.yml").read_text()
        self.assertIn("Restore and save trusted remote Nix store", source)
        self.assertIn("remote-${{ needs.plan.outputs.cache_prefix }}-", source)


    def test_restore_only_cache_family_preserves_exact_write_keys(self):
        planner = (ROOT / "lib/flake-check-plan.py").read_text()
        self.assertIn('output.write("cache_family="', planner)
        for name in (*WORKFLOWS, "flake-local-dag.yml"):
            with self.subTest(workflow=name):
                source = (ROOT / ".github" / "workflows" / name).read_text()
                self.assertIn("cache_family", source)
                self.assertIn("restore-prefixes-first-match:", source)
                for line in source.splitlines():
                    if "primary-key:" in line:
                        self.assertNotIn("cache_family", line)
        remote = (ROOT / ".github/workflows/flake-nix-builders.yml").read_text()
        coordinator = remote.split("  coordinator:\n", 1)[1].split("  test:\n", 1)[0]
        self.assertIn("inputs.cache_namespace == ''", coordinator)
        self.assertIn("format('remote-{0}-', needs.plan.outputs.cache_family)", coordinator)
        staged = (ROOT / ".github/workflows/flake-dag-staged.yml").read_text()
        shared = staged.split("  shared:\n", 1)[1].split("  leaf:\n", 1)[0]
        self.assertIn("remote-${{ needs.plan.outputs.cache_family }}-", shared)
        self.assertIn("${{ needs.plan.outputs.cache_family }}-", shared)
        self.assertNotIn("cache_family", staged.split("  leaf:\n", 1)[1])
        self.assertIn("save: false", staged.split("  leaf:\n", 1)[1])


if __name__ == "__main__":
    unittest.main()
