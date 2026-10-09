"""Portable regression tests for the Cargo impact planner (no Cargo download)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import importlib.util

module = importlib.util.spec_from_file_location("impact", Path(__file__).with_name("cargo-impact.py"))
impact = importlib.util.module_from_spec(module)
module.loader.exec_module(impact)


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata = {
            "workspace_members": ["leaf", "sdk", "app", "other"],
            "packages": [
                {"id": "leaf", "name": "leaf", "manifest_path": str(self.root / "rust/crates/leaf/Cargo.toml"), "dependencies": []},
                {"id": "sdk", "name": "sdk", "manifest_path": str(self.root / "rust/crates/sdk/Cargo.toml"), "dependencies": [{"path": str(self.root / "rust/crates/leaf")}]},
                {"id": "app", "name": "app", "manifest_path": str(self.root / "rust/crates/app/Cargo.toml"), "dependencies": [{"path": str(self.root / "rust/crates/sdk")}]},
                {"id": "other", "name": "other", "manifest_path": str(self.root / "rust/crates/other/Cargo.toml"), "dependencies": []},
            ],
        }
        self.config = {"workspace": "rust", "jobs": {
            "test-leaf": {"kind": "cargo", "packages": ["leaf"]},
            "test-app": {"kind": "cargo", "packages": ["app"]},
            "test-other": {"kind": "cargo", "packages": ["other"]},
            "clippy": {"kind": "cargo", "packages": None},
        }}

    def at_repo(self, filenames):
        old = Path.cwd()
        import os
        os.chdir(self.root)
        try:
            return impact.select_jobs(self.config, filenames, self.metadata)
        finally:
            os.chdir(old)

    def lock_is_scoped(self, changed, before, after):
        old = Path.cwd()
        import os
        os.chdir(self.root)
        try:
            return impact.workspace_only_lock_changes(
                self.config, changed, self.metadata, before, after
            )
        finally:
            os.chdir(old)

    def test_lockfile_workspace_dependency_edit_preserves_narrow_impact(self):
        external = {
            "name": "external", "version": "1.2.3",
            "source": "registry+https://example.invalid", "checksum": "unchanged",
        }
        old = {"version": 4, "package": [
            {"name": "leaf", "version": "0.1.0", "dependencies": ["serde"]},
            external,
        ]}
        new = {"version": 4, "package": [
            {"name": "leaf", "version": "0.1.0", "dependencies": ["serde", "getrandom"]},
            external,
        ]}
        changed = ["rust/Cargo.lock", "rust/crates/leaf/Cargo.toml"]
        self.assertTrue(self.lock_is_scoped(changed, old, new))
        jobs, crates = self.at_repo(["rust/crates/leaf/Cargo.toml"])
        self.assertEqual(crates, ["app", "leaf", "sdk"])
        self.assertFalse(jobs["test-other"])
        self.assertFalse(self.lock_is_scoped(["rust/Cargo.lock"], old, new))

    def test_lockfile_added_workspace_package_is_scoped_only_with_its_manifest(self):
        old = {"version": 4, "package": []}
        new = {"version": 4, "package": [
            {"name": "other", "version": "0.1.0", "dependencies": ["leaf"]},
        ]}
        self.assertTrue(self.lock_is_scoped(
            ["rust/Cargo.lock", "rust/crates/other/Cargo.toml"], old, new,
        ))
        self.assertFalse(self.lock_is_scoped(["rust/Cargo.lock"], old, new))

    def test_external_or_global_lockfile_changes_still_fail_open(self):
        old = {"version": 4, "package": [
            {"name": "leaf", "version": "0.1.0", "dependencies": []},
            {"name": "dep", "version": "1.0.0", "source": "registry+https://example.invalid", "checksum": "first"},
        ]}
        manifests = ["rust/Cargo.lock", "rust/crates/leaf/Cargo.toml"]
        for changed in [
            {"version": 4, "package": [
                old["package"][0],
                {"name": "dep", "version": "1.0.0", "source": "registry+https://example.invalid", "checksum": "second"},
            ]},
            {"version": 4, "package": [
                old["package"][0],
                {"name": "dep", "version": "1.0.1", "source": "registry+https://example.invalid", "checksum": "second"},
            ]},
            {"version": 3, "package": old["package"]},
            {"version": 4, "package": [
                {"name": "missing-workspace-package", "version": "0.1.0"},
                *old["package"],
            ]},
        ]:
            with self.subTest(changed=changed):
                self.assertFalse(self.lock_is_scoped(manifests, old, changed))

    def test_workspace_member_additions_require_changed_member_manifests(self):
        old = {
            "workspace": {
                "members": ["crates/leaf", "crates/sdk"],
                "resolver": "3",
            },
            "profile": {"release": {"opt-level": 3}},
        }
        new = {
            "workspace": {
                "members": ["crates/leaf", "crates/sdk", "crates/other"],
                "resolver": "3",
            },
            "profile": {"release": {"opt-level": 3}},
        }
        self.assertTrue(impact.workspace_member_only_change(
            self.config, ["rust/Cargo.toml", "rust/crates/other/Cargo.toml"], old, new,
        ))
        self.assertFalse(impact.workspace_member_only_change(
            self.config, ["rust/Cargo.toml"], old, new,
        ))
        removed = {**new, "workspace": {**new["workspace"], "members": ["crates/sdk"]}}
        self.assertFalse(impact.workspace_member_only_change(
            self.config, ["rust/Cargo.toml", "rust/crates/other/Cargo.toml"], old, removed,
        ))
        changed_profile = {**new, "profile": {"release": {"opt-level": 2}}}
        self.assertFalse(impact.workspace_member_only_change(
            self.config, ["rust/Cargo.toml", "rust/crates/other/Cargo.toml"], old, changed_profile,
        ))

    def test_leaf_edit_runs_reverse_dependents_not_unrelated(self):
        jobs, crates = self.at_repo(["rust/crates/leaf/src/lib.rs"])
        self.assertEqual(crates, ["app", "leaf", "sdk"])
        self.assertEqual(jobs, {"test-leaf": True, "test-app": True, "test-other": False, "clippy": True})

    def test_unrelated_crate_does_not_rebuild_other_crates(self):
        jobs, crates = self.at_repo(["rust/crates/other/src/lib.rs"])
        self.assertEqual(crates, ["other"])
        self.assertEqual(jobs, {"test-leaf": False, "test-app": False, "test-other": True, "clippy": True})

    def test_docs_only_disables_cargo_checks(self):
        jobs, crates = self.at_repo(["spec/rfc.md", "README.md"])
        self.assertEqual(crates, [])
        self.assertTrue(all(not active for active in jobs.values()))

    def test_shared_manifest_or_unknown_path_must_fail_open(self):
        for name in ["rust/Cargo.lock", "rust/Cargo.toml", "modules/development.nix", "rust/.cargo/config.toml", "rust/crates/missing/src/lib.rs", ".github/workflows/ci.yml"]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.at_repo([name])

    def test_multiple_independent_crate_changes_union_without_full_fallback(self):
        jobs, crates = self.at_repo([
            "rust/crates/leaf/src/lib.rs",
            "rust/crates/other/src/lib.rs",
        ])
        self.assertEqual(crates, ["app", "leaf", "other", "sdk"])
        self.assertTrue(all(jobs.values()))

    def test_path_prefix_is_not_an_dependency_edge(self):
        self.metadata["workspace_members"].append("leaf-extra")
        self.metadata["packages"].append({
            "id": "leaf-extra",
            "name": "leaf-extra",
            "manifest_path": str(self.root / "rust/crates/leaf-extra/Cargo.toml"),
            "dependencies": [],
        })
        jobs, crates = self.at_repo(["rust/crates/leaf-extra/src/lib.rs"])
        self.assertEqual(crates, ["leaf-extra"])
        self.assertEqual(jobs, {
            "test-leaf": False,
            "test-app": False,
            "test-other": False,
            "clippy": True,
        })

    def test_combined_docs_and_leaf_change_keeps_narrow_selection(self):
        jobs, crates = self.at_repo([
            "spec/ci-selection.md",
            "README.md",
            "rust/crates/other/src/lib.rs",
        ])
        self.assertEqual(crates, ["other"])
        self.assertEqual(jobs, {
            "test-leaf": False,
            "test-app": False,
            "test-other": True,
            "clippy": True,
        })

    def test_unknown_build_input_blocks_selective_skips_even_with_known_leaf(self):
        with self.assertRaisesRegex(ValueError, "unclassified"):
            self.at_repo([
                "rust/crates/other/src/lib.rs",
                "modules/development.nix",
            ])

    def test_dependency_kinds_are_all_included_for_conservative_selection(self):
        self.metadata["packages"][3]["dependencies"] = [{
            "path": str(self.root / "rust/crates/leaf"),
            "kind": "build",
        }]
        jobs, crates = self.at_repo(["rust/crates/leaf/src/lib.rs"])
        self.assertEqual(crates, ["app", "leaf", "other", "sdk"])
        self.assertTrue(jobs["test-other"])

    def test_output_has_packages_and_unknown_plan_uses_null(self):
        output = self.root / "github-output"
        environment = {
            "PHENIX_IMPACT_CONFIG": json.dumps(self.config),
            "GITHUB_OUTPUT": str(output),
        }
        with mock.patch.dict("os.environ", environment):
            with mock.patch.object(impact, "run", return_value=(
                {"test-leaf": True}, ["rust/crates/leaf/src/lib.rs"], ["leaf", "sdk"]
            )):
                impact.main()
            self.assertIn("packages=" + json.dumps(["leaf", "sdk"], separators=(",", ":")) + "\n", output.read_text())
            output.unlink()
            with mock.patch.object(impact, "run", side_effect=ValueError("unknown build input")):
                impact.main()
        self.assertIn("packages=null\n", output.read_text())
        self.assertIn("clippy", output.read_text())

    def test_invalid_declared_target_rejects_plan(self):
        self.config["jobs"]["test-leaf"]["packages"] = ["misnamed"]
        with self.assertRaises(ValueError):
            self.at_repo(["rust/crates/leaf/src/lib.rs"])

    def test_empty_diff_rejects_plan(self):
        with self.assertRaises(ValueError):
            self.at_repo([])


if __name__ == "__main__":
    unittest.main()
