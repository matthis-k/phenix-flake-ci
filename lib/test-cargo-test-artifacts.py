#!/usr/bin/env python3
"""Cargo test binary packaging has no compiler invocation at run time."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "cargo_test_artifacts", Path(__file__).with_name("cargo-test-artifacts.py"))
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class CargoArtifactsTest(unittest.TestCase):
    def record(self, name, path, test=True):
        return json.dumps({
            "reason": "compiler-artifact",
            "package_id": f"path+file:///source#{name}@0.1.0",
            "target": {"name": name, "kind": ["lib"], "test": True},
            "profile": {"test": test},
            "executable": str(path),
        })

    def test_selection_ignores_non_test_compiler_artifacts(self):
        selected = subject.executables([
            "informational line",
            self.record("a", "/tmp/a"),
            self.record("b", "/tmp/b", test=False),
            self.record("a", "/tmp/a"),
        ])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][0][1], "a")

    def test_packs_existing_test_binaries_without_running_cargo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "binary"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            manifest = root / "input.jsonl"
            manifest.write_text(self.record("unit", executable) + "\n")
            dest = root / "out" / "libexec"
            metadata = root / "metadata.json"
            package_id = "path+file:///source#unit@0.1.0"
            metadata.write_text(json.dumps({
                "packages": [{"id": package_id, "name": "unit"}],
                "workspace_members": [package_id],
            }))
            subject.install(manifest, dest, metadata)
            self.assertEqual(len(list(dest.iterdir())), 1)
            record = json.loads((root/"out"/"cargo-tests.json").read_text())[0]
            self.assertEqual(record["target"], "unit")
            self.assertEqual(record["package"], "unit")
            self.assertTrue((dest / record["binary"]).is_file())
            with self.assertRaisesRegex(ValueError, "empty"):
                subject.install(manifest, dest, metadata)
            manifest.write_text(self.record("unit", root / "missing") + "\n")
            with self.assertRaisesRegex(ValueError, "missing"):
                subject.install(manifest, root / "new" / "libexec", metadata)

    def test_empty_input_and_invalid_identity_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "no compiled"):
            subject.executables(["hello"])
        bad = json.dumps({"reason": "compiler-artifact", "executable": "/x",
                          "profile": {"test": True}, "target": {"name": "x"}})
        with self.assertRaisesRegex(ValueError, "package"):
            subject.executables([bad])


if __name__ == "__main__":
    unittest.main()
