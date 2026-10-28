#!/usr/bin/env python3
"""Deterministic subprocess/assertion runner for mkTest-generated Nix checks.

Test specifications are data, never shell scripts. All checks run in a fresh
workspace. The runner retains every applicable assertion failure and prints
machine-readable evidence even when the Nix derivation fails.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class InvalidSpec(ValueError):
    pass


def require(condition, description):
    if not condition:
        raise InvalidSpec(description)


def safe_path(root, name):
    require(isinstance(name, str) and name and not Path(name).is_absolute(),
            f"invalid workspace path: {name!r}")
    path = (root / name).resolve()
    require(path != root and root in path.parents, f"workspace path escapes sandbox: {name!r}")
    return path


def pointer(value, location):
    require(isinstance(location, str) and (location == "" or location.startswith("/")),
            f"invalid JSON pointer: {location!r}")
    for part in location.split("/")[1:] if location else []:
        require(re.search(r"~(?![01])", part) is None, f"invalid JSON pointer escape: {location!r}")
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[part]
        elif isinstance(value, list):
            require(re.fullmatch(r"0|[1-9][0-9]*", part) is not None,
                    f"JSON pointer requires an array index: {part!r}")
            value = value[int(part)]
        else:
            raise KeyError(f"cannot traverse {part!r} in {type(value).__name__}")
    return value


def equals(actual, expected):
    return type(actual) is type(expected) and actual == expected


def contains(actual, expected):
    if isinstance(actual, str):
        return isinstance(expected, str) and expected in actual
    if isinstance(actual, list):
        return any(equals(item, expected) for item in actual)
    if isinstance(actual, dict):
        return isinstance(expected, dict) and all(
            key in actual and (contains(actual[key], val) if isinstance(val, (dict, list))
                                else equals(actual[key], val))
            for key, val in expected.items()
        )
    return False


def compare(actual, predicate):
    if not isinstance(predicate, dict):
        return equals(actual, predicate), (None if equals(actual, predicate) else f"eq {predicate!r}")
    require(bool(predicate), "empty predicate")
    ops = {"eq", "ne", "contains", "matches", "gt", "gte", "lt", "lte", "exists", "empty"}
    require(set(predicate) <= ops, f"unknown predicates: {sorted(set(predicate) - ops)}")
    for op, expected in predicate.items():
        if op == "eq":
            ok = equals(actual, expected)
        elif op == "ne":
            ok = not equals(actual, expected)
        elif op == "contains":
            ok = (all(contains(actual, item) for item in expected)
                  if isinstance(expected, list) and isinstance(actual, str) else contains(actual, expected))
        elif op == "matches":
            require(isinstance(expected, str), "matches expects regex text")
            ok = isinstance(actual, str) and re.search(expected, actual) is not None
        elif op in ("gt", "gte", "lt", "lte"):
            require(isinstance(expected, (int, float)) and not isinstance(expected, bool),
                    f"{op} expects a number")
            valid = isinstance(actual, (int, float)) and not isinstance(actual, bool)
            ok = valid and {"gt": lambda: actual > expected, "gte": lambda: actual >= expected,
                            "lt": lambda: actual < expected, "lte": lambda: actual <= expected}[op]()
        elif op in ("exists", "empty"):
            require(isinstance(expected, bool), f"{op} expects a boolean")
            require(isinstance(actual, bool), f"{op} requires a boolean observation")
            ok = actual is expected
        else:
            raise AssertionError(op)
        if not ok:
            return False, f"{op} {expected!r}"
    return True, None


def check(expectations, actual, prefix, failures, results):
    require(isinstance(expectations, dict), f"{prefix}: expectations must be an attribute set")
    for selector, predicate in expectations.items():
        label = f"{prefix}.{selector}"
        try:
            ok, description = compare(actual[selector], predicate)
            result = {"assertion": label, "pass": ok}
            if not ok:
                result.update({"expected": predicate, "actual": actual[selector], "reason": description})
                failures.append(result)
            results.append(result)
        except (InvalidSpec, TypeError, ValueError, re.error) as error:
            raise InvalidSpec(f"{label}: {error}") from error
        except (KeyError, IndexError) as error:
            result = {"assertion": label, "pass": False, "reason": f"missing observation: {error}"}
            failures.append(result)
            results.append(result)


def validate_command(command, label):
    require(isinstance(command, dict) and isinstance(command.get("program"), str)
            and bool(command["program"]), f"{label}.command.program must be a nonempty string")
    args = command.get("args", [])
    require(isinstance(args, list) and all(isinstance(x, str) for x in args),
            f"{label}.command.args must be a list of strings")
    timeout = command.get("timeoutSeconds", 30)
    require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0,
            f"{label}.command.timeoutSeconds must be positive")
    return [command["program"], *args], timeout


def test(spec):
    require(isinstance(spec, dict), "test must be an attribute set")
    steps = spec.get("steps")
    if steps is None:
        require("command" in spec, "a test requires command or steps")
        steps = [{"id": "main", "command": spec["command"], "expect": spec.get("expect", {})}]
        final_expect = {}
    else:
        require("command" not in spec, "command and steps are mutually exclusive")
        require(isinstance(steps, list) and bool(steps), "steps must be a nonempty list")
        final_expect = spec.get("expect", {})
    require(isinstance(final_expect, dict), "expect must be an attribute set")
    failures, results, observations = [], [], {}
    with tempfile.TemporaryDirectory(prefix="phenix-test-") as temp:
        root = Path(temp).resolve()
        fixtures = spec.get("fixtures", {})
        require(isinstance(fixtures, dict), "fixtures must be an attribute set")
        for name, content in fixtures.items():
            require(isinstance(content, str), f"fixture {name!r} must contain text")
            path = safe_path(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        env = spec.get("env", {})
        require(isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str)
                                               for k, v in env.items()), "env must contain strings")
        environment = dict(os.environ, **env)
        ids = set()
        for index, step in enumerate(steps):
            label = f"step[{index}]"
            require(isinstance(step, dict), f"{label} must be an attribute set")
            sid = step.get("id", f"step-{index}")
            require(isinstance(sid, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", sid) is not None,
                    f"{label}.id is invalid")
            require(sid not in ids, f"duplicate step ID: {sid}")
            ids.add(sid)
            argv, timeout = validate_command(step.get("command"), sid)
            expected = step.get("expect", {})
            require(isinstance(expected, dict), f"{sid}.expect must be an attribute set")
            require(set(expected) <= {"exitCode", "stdout", "stderr", "files"},
                    f"{sid}.expect has unknown observations")
            try:
                run = subprocess.run(argv, cwd=root, env=environment,
                                     capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
                observed = {"exitCode": run.returncode, "stdout": run.stdout, "stderr": run.stderr}
            except subprocess.TimeoutExpired as error:
                observed = {"timeout": True, "stdout": error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or "",
                            "stderr": error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""}
                failures.append({"assertion": f"{sid}.execution", "pass": False, "reason": f"timed out after {timeout}s"})
            except OSError as error:
                observed = {}
                failures.append({"assertion": f"{sid}.execution", "pass": False, "reason": str(error)})
            observations[sid] = observed
            if "exitCode" not in observed:
                continue
            check({"exitCode": expected.get("exitCode", 0)}, observed, sid, failures, results)
            for channel in ("stdout", "stderr"):
                if channel not in expected:
                    continue
                tests = expected[channel]
                require(isinstance(tests, dict), f"{sid}.{channel} must be an attribute set")
                require(set(tests) <= {"empty", "contains", "matches", "eq", "ne", "json"},
                        f"{sid}.{channel} has unknown assertions")
                if "json" in tests:
                    require(isinstance(tests["json"], dict), f"{sid}.{channel}.json must be an attribute set")
                    try:
                        document = json.loads(observed[channel])
                        for location, predicate in tests["json"].items():
                            try:
                                actual = pointer(document, location)
                                check({location: predicate}, {location: actual},
                                      f"{sid}.{channel}.json", failures, results)
                            except (KeyError, IndexError, TypeError) as error:
                                result = {"assertion": f"{sid}.{channel}.json.{location}", "pass": False,
                                          "reason": f"missing JSON location: {error}"}
                                results.append(result)
                                failures.append(result)
                    except json.JSONDecodeError as error:
                        result = {"assertion": f"{sid}.{channel}.json", "pass": False,
                                  "reason": f"invalid JSON: {error}"}
                        results.append(result)
                        failures.append(result)
                ordinary = {key: val for key, val in tests.items() if key != "json"}
                for operation, expected_value in ordinary.items():
                    observation = len(observed[channel]) == 0 if operation == "empty" else observed[channel]
                    predicate = expected_value if operation == "empty" else {operation: expected_value}
                    check({operation: predicate}, {operation: observation},
                          f"{sid}.{channel}", failures, results)
            if "files" in expected:
                verify_files(root, expected["files"], f"{sid}.files", failures, results)
        require(set(final_expect) <= {"files"}, "top-level expect supports only files for multi-step tests")
        if "files" in final_expect:
            verify_files(root, final_expect["files"], "files", failures, results)
    return {"name": spec.get("name", "test"), "status": "fail" if failures else "pass",
            "assertions": results, "failures": failures, "steps": list(observations)}


def verify_files(root, files, prefix, failures, results):
    require(isinstance(files, dict), f"{prefix} must be an attribute set")
    for filename, predicates in files.items():
        path = safe_path(root, filename)
        require(isinstance(predicates, dict) and "exists" in predicates,
                f"{prefix}.{filename}: a file assertion needs exists")
        require(set(predicates) == {"exists"}, f"{prefix}.{filename}: only exists is supported")
        check({"exists": predicates["exists"]}, {"exists": path.exists()},
              f"{prefix}.{filename}", failures, results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        evidence = test(spec)
    except (InvalidSpec, OSError, ValueError) as error:
        evidence = {"status": "invalid", "reason": str(error)}
    for failed in evidence.get("failures", []):
        print(f"FAIL {failed['assertion']}: {failed['reason']}", file=sys.stderr)
        if "expected" in failed:
            print(f"  expected: {failed['expected']!r}\n  actual:   {failed['actual']!r}", file=sys.stderr)
    if evidence["status"] == "invalid":
        print(f"INVALID: {evidence['reason']}", file=sys.stderr)
    print(json.dumps(evidence, sort_keys=True, ensure_ascii=False))
    if evidence["status"] == "pass":
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    return 2 if evidence["status"] == "invalid" else 1


if __name__ == "__main__":
    sys.exit(main())
