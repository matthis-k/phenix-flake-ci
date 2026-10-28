#!/usr/bin/env python3
"""Self-contained behavioral tests for the declarative test runner."""
import importlib.util
from pathlib import Path
import sys
import unittest

loader = importlib.util.spec_from_file_location("test_runner", Path(__file__).with_name("test-runner.py"))
runner = importlib.util.module_from_spec(loader)
loader.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def command(self, source, *args):
        return {"program": sys.executable, "args": ["-c", source, *args]}

    def test_multiple_assertions_single_execution(self):
        spec = {"command": self.command("from pathlib import Path; import json; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print(json.dumps({'status':'ready','count':2,'items':['a','b']}))"),
                "expect": {"exitCode": 0, "stdout": {"json": {"/status": "ready", "/count": {"gte": 2}, "/items/1": "b"}},
                           "stderr": {"empty": True}, "files": {"counter": {"exists": True}}}}
        evidence = runner.test(spec)
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(len(evidence["assertions"]), 6)
        self.assertEqual(evidence["steps"], ["main"])

    def test_expected_nonzero_exit_is_success(self):
        evidence = runner.test({"command": self.command("import sys; print('invalid', file=sys.stderr);sys.exit(9)"),
                                "expect": {"exitCode": 9, "stderr": {"contains": ["invalid"], "empty": False}}})
        self.assertEqual(evidence["status"], "pass")

    def test_wrong_exit_and_output_all_reported(self):
        evidence = runner.test({"command": self.command("print('different')"),
                                "expect": {"exitCode": 7, "stdout": {"contains": ["expected"], "matches": "^never"},
                                           "stderr": {"empty": False}}})
        self.assertEqual(evidence["status"], "fail")
        self.assertEqual(len(evidence["failures"]), 4)

    def test_steps_share_workspace_and_fixture(self):
        evidence = runner.test({"fixtures": {"fixtures/input.txt": "hello"},
                                "steps": [
                                    {"id": "first", "command": self.command("from pathlib import Path; Path('state').write_text(Path('fixtures/input.txt').read_text())")},
                                    {"id": "second", "command": self.command("from pathlib import Path; print(Path('state').read_text())"),
                                     "expect": {"stdout": {"contains": "hello"}}},
                                ], "expect": {"files": {"state": {"exists": True}}}})
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["steps"], ["first", "second"])

    def test_missing_json_path_is_assertion_failure(self):
        evidence = runner.test({"command": self.command("print('{}')"),
                                "expect": {"stdout": {"json": {"/missing": 4}}}})
        self.assertEqual(evidence["status"], "fail")
        self.assertIn("missing JSON location", evidence["failures"][0]["reason"])

    def test_invalid_json_is_assertion_failure(self):
        evidence = runner.test({"command": self.command("print('not JSON')"),
                                "expect": {"stdout": {"json": {"/x": True}}}})
        self.assertEqual(evidence["status"], "fail")

    def test_timeout_always_fails(self):
        evidence = runner.test({"command": {**self.command("import time; time.sleep(2)"), "timeoutSeconds": 0.01},
                                "expect": {"exitCode": {"ne": 0}}})
        self.assertEqual(evidence["status"], "fail")
        self.assertIn("timed out", evidence["failures"][0]["reason"])

    def test_command_args_are_not_shell_expanded(self):
        evidence = runner.test({"command": self.command("import sys; print(sys.argv[1])", "literal; $(echo danger)"),
                                "expect": {"stdout": {"contains": "literal; $(echo danger)"}}})
        self.assertEqual(evidence["status"], "pass")

    def test_invalid_path_rejected(self):
        with self.assertRaises(runner.InvalidSpec):
            runner.test({"command": self.command("pass"), "fixtures": {"../escape": "bad"}})

    def test_predicate_type_strict(self):
        self.assertEqual(runner.compare(1, True)[0], False)
        self.assertEqual(runner.compare("hello", {"contains": "ell"})[0], True)
        self.assertEqual(runner.compare(3, {"lt": 4, "gte": 2})[0], True)
        self.assertEqual(runner.compare({"a": {"b": 3}}, {"contains": {"a": {"b": 3}}})[0], True)

    def test_pointer_escaping(self):
        self.assertEqual(runner.pointer({"a/b": {"~x": 7}}, "/a~1b/~0x"), 7)


if __name__ == "__main__":
    unittest.main()
