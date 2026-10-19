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

    def test_read_only_shards_never_publish_shared_store(self):
        for name in ("flake-checks.yml", "flake-dag-staged.yml"):
            source = (ROOT / ".github" / "workflows" / name).read_text()
            self.assertIn("save: false", source)

    def test_remote_builder_cache_stays_in_trusted_coordinator(self):
        source = (ROOT / ".github" / "workflows" / "flake-nix-builders.yml").read_text()
        self.assertIn("Restore and save trusted remote Nix store", source)
        self.assertIn("remote-${{ needs.plan.outputs.cache_prefix }}-", source)


if __name__ == "__main__":
    unittest.main()
