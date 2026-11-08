#!/usr/bin/env python3
"""Shared Cargo corpus selection must be safe, complete and compiler-free."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "runner", Path(__file__).with_name("cargo-test-runner.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CargoCheckRunnerTests(unittest.TestCase):
    def fixture(self, root):
        binaries = root / "bin"
        binaries.mkdir()
        records = []
        for index, (package, target, kind) in enumerate([
            ("root", "rootlib", "lib"),
            ("root", "special_integration", "test"),
            ("child", "childlib", "lib"),
        ]):
            path = binaries / f"{index}"
            path.write_text("#!/bin/sh\nprintf '%s\\n' called >> \"$TEST_LOG\"\nexit 0\n")
            path.chmod(0o755)
            records.append({"package": package, "target": target,
                            "targetKinds": [kind], "binary": path.name,
                            "workdir": str(root)})
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(records))
        return manifest, binaries, records

    def test_selects_requested_packages_and_target_types(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, binaries, records = self.fixture(root)
            self.assertEqual([x[1] for x in runner.choose(
                records, binaries, {"root"}, True, set())], ["rootlib"])
            self.assertEqual([x[1] for x in runner.choose(
                records, binaries, {"root"}, False, {"special_integration"})],
                ["special_integration"])
            self.assertEqual(len(runner.choose(records, binaries, set(), False, set())), 3)
            with self.assertRaisesRegex(ValueError, "unknown Cargo packages"):
                runner.choose(records, binaries, {"typo"}, False, set())
            with self.assertRaisesRegex(ValueError, "unknown integration test targets"):
                runner.choose(records, binaries, {"root"}, False, {"not-a-test"})
            no_child_lib = [
                {**record, "targetKinds": ["test"]} if record["package"] == "child"
                else record for record in records
            ]
            with self.assertRaisesRegex(ValueError, "requested lib"):
                runner.choose(no_child_lib, binaries, {"child"}, True, set())

    def test_refuses_binary_escape_and_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, binaries, records = self.fixture(root)
            records[0]["binary"] = "../escape"
            with self.assertRaisesRegex(ValueError, "unsafe"):
                runner.choose(records, binaries, set(), False, set())
            records[0]["binary"] = records[1]["binary"]
            with self.assertRaisesRegex(ValueError, "repeated"):
                runner.choose(records, binaries, set(), False, set())
            records[0]["binary"] = "0"
            records[0]["targetKinds"] = []
            with self.assertRaisesRegex(ValueError, "incomplete"):
                runner.choose(records, binaries, set(), False, set())

    def test_executes_selected_binaries_without_invoking_cargo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest, binaries, _ = self.fixture(root)
            output = root / "calls.log"
            with mock.patch.dict("os.environ", {"TEST_LOG": str(output)}):
                runner.execute(manifest, binaries, root, ["root"], True, [], [])
            self.assertEqual(output.read_text().splitlines(), ["called"])

    def test_rejects_manifest_paths_outside_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest, binaries, records = self.fixture(root)
            records[0]["workdir"] = "/tmp"
            manifest.write_text(json.dumps(records))
            with self.assertRaisesRegex(ValueError, "escapes"):
                runner.execute(manifest, binaries, root, ["root"], True, [], [])

    def test_fails_instead_of_falling_back_to_cargo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest, binaries, _ = self.fixture(root)
            with self.assertRaisesRegex(ValueError, "unknown"):
                runner.execute(manifest, binaries, root, ["wrong"], False, [], [])


if __name__ == "__main__":
    unittest.main()
