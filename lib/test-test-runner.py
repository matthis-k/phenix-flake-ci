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


    def test_reference_json_value_matches_later_output(self):
        reference = {"ref": {"step": "create", "field": "stdout", "json": "/id"}}
        spec = {"steps": [
            {"id": "create", "command": self.command("import json;print(json.dumps({'id':'session-123'}))")},
            {"id": "inspect", "command": self.command("import json;print(json.dumps({'id':'session-123'}))"),
             "expect": {"stdout": {"json": {"/id": {"eq": reference}}}}},
        ]}
        evidence = runner.test(spec)
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(len(evidence["assertions"]), 3)

    def test_reference_can_supply_command_argument_without_shell(self):
        ref = {"ref": {"step": "create", "field": "stdout", "json": "/id"}}
        evidence = runner.test({"steps": [
            {"id": "create", "command": self.command("print('{\"id\":\"literal; $(echo unsafe)\"}')")},
            {"id": "use", "command": {"program": sys.executable,
                 "args": ["-c", "import sys;print(sys.argv[1])", ref]},
             "expect": {"stdout": {"contains": "literal; $(echo unsafe)"}}},
        ]})
        self.assertEqual(evidence["status"], "pass")

    def test_missing_reference_is_assertion_failure(self):
        ref = {"ref": {"step": "first", "field": "stdout", "json": "/absent"}}
        evidence = runner.test({"steps": [
            {"id": "first", "command": self.command("print('{}')")},
            {"id": "second", "command": self.command("print('okay')"),
             "expect": {"stdout": {"contains": "okay", "eq": ref}}},
        ]})
        self.assertEqual(evidence["status"], "fail")
        self.assertEqual(len(evidence["failures"]), 1)
        self.assertIn("unavailable reference", evidence["failures"][0]["reason"])

    def test_bad_prior_output_blocks_dependent_command(self):
        ref = {"ref": {"step": "first", "field": "stdout", "json": "/id"}}
        evidence = runner.test({"steps": [
            {"id": "first", "command": self.command("print('{}')")},
            {"id": "dependent", "command": {"program": sys.executable, "args": ["-c", "print(1)", ref]}},
            {"id": "independent", "command": self.command("print('still runs')"),
             "expect": {"stdout": {"contains": "still runs"}}},
        ]})
        self.assertEqual(evidence["status"], "fail")
        self.assertIn("cannot resolve command argument", evidence["failures"][0]["reason"])
        self.assertEqual(evidence["steps"], ["first", "dependent", "independent"])

    def test_reject_forward_reference_before_running(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "must-not-run"
            ref = {"ref": {"step": "future", "field": "stdout"}}
            with self.assertRaises(runner.InvalidSpec):
                runner.test({"steps": [
                    {"id": "early", "command": self.command(
                        f"from pathlib import Path;Path({str(marker)!r}).touch()"),
                     "expect": {"stdout": {"eq": ref}}},
                    {"id": "future", "command": self.command("print('hi')")},
                ]})
            self.assertFalse(marker.exists())

    def test_reference_to_nonstring_argument_fails(self):
        ref = {"ref": {"step": "first", "field": "stdout", "json": "/count"}}
        evidence = runner.test({"steps": [
            {"id": "first", "command": self.command("print('{\"count\":2}')")},
            {"id": "second", "command": {"program": sys.executable,
                 "args": ["-c", "print('ok')", ref]}},
        ]})
        self.assertEqual(evidence["status"], "fail")
        self.assertIn("non-string", evidence["failures"][0]["reason"])

    def test_files_can_assert_text_json_and_size(self):
        evidence = runner.test({"command": self.command(
            "from pathlib import Path;Path('state.json').write_text('{\"state\":{\"ready\":true}}')"),
            "expect": {"files": {"state.json": {
                "text": {"contains": ["state", "ready"], "matches": "ready"},
                "json": {"/state/ready": True},
                "size": {"gte": 20},
            }}}})
        self.assertEqual(evidence["status"], "pass")

    def test_file_mismatch_reports_multiple_failures(self):
        evidence = runner.test({"command": self.command("from pathlib import Path;Path('data').write_text('wrong')"),
            "expect": {"files": {"data": {
                "text": {"contains": "expected", "matches": "^yes"},
                "size": {"gte": 100},
            }}}})
        self.assertEqual(evidence["status"], "fail")
        self.assertEqual(len(evidence["failures"]), 3)

    def test_nonexistent_file_reports_presence_mismatch(self):
        evidence = runner.test({"command": self.command("pass"),
                                "expect": {"files": {"missing.txt": {"text": {"contains": "hello"}}}}})
        self.assertEqual(evidence["status"], "fail")
        self.assertIn("exists", evidence["failures"][0]["assertion"])

    def test_reference_can_compare_stderr_and_exit_code(self):
        code = "import sys;print('boom',file=sys.stderr);sys.exit(7)"
        evidence = runner.test({"steps": [
            {"id": "first", "command": self.command(code), "expect": {"exitCode": 7}},
            {"id": "second", "command": self.command(code),
             "expect": {"exitCode": {"eq": {"ref": {"step": "first", "field": "exitCode"}}},
                        "stderr": {"eq": {"ref": {"step": "first", "field": "stderr"}}}}},
        ]})
        self.assertEqual(evidence["status"], "pass")



if __name__ == "__main__":
    unittest.main()
