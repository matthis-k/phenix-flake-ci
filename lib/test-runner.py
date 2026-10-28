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


class MissingReference(Exception):
    """A previous execution did not produce an expected observation."""
    pass


def is_reference(value):
    return isinstance(value, dict) and set(value) == {"ref"}


def validate_references(value, available, where):
    """Reject forward/unknown references before executing any command."""
    if is_reference(value):
        ref = value["ref"]
        require(isinstance(ref, dict) and set(ref) in (
            {"step", "field"}, {"step", "field", "json"}),
            f"{where}: ref needs step, field and optional json")
        require(isinstance(ref["step"], str) and ref["step"] in available, f"{where}: reference to unknown or future step {ref['step']!r}")
        require(ref["field"] in ("stdout", "stderr", "exitCode"),
                f"{where}: unknown observation field")
        require("json" not in ref or ref["field"] in ("stdout", "stderr"),
                f"{where}: json selector only works on output streams")
        if "json" in ref:
            validate_pointer(ref["json"])
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_references(item, available, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate_references(item, available, f"{where}[{index}]")


def validate_pointer(location):
    require(isinstance(location, str) and (location == "" or location.startswith("/")),
            f"invalid JSON pointer: {location!r}")
    require(re.search(r"~(?![01])", location) is None,
            f"invalid JSON pointer escape: {location!r}")


def materialize_refs(value, observations):
    if is_reference(value):
        ref = value["ref"]
        observed = observations.get(ref["step"], {})
        if ref["field"] not in observed:
            raise MissingReference(f"{ref['step']}.{ref['field']} not available")
        actual = observed[ref["field"]]
        if "json" in ref:
            try:
                actual = pointer(json.loads(actual), ref["json"])
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise MissingReference(f"{ref['step']}.{ref['field']} JSON {ref['json']!r}: {error}") from error
        return actual
    if isinstance(value, list):
        return [materialize_refs(item, observations) for item in value]
    if isinstance(value, dict):
        return {key: materialize_refs(item, observations) for key, item in value.items()}
    return value


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
    validate_pointer(location)
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


def check(expectations, actual, prefix, failures, results, observations=None):
    require(isinstance(expectations, dict), f"{prefix}: expectations must be an attribute set")
    for selector, predicate in expectations.items():
        label = f"{prefix}.{selector}"
        try:
            resolved = materialize_refs(predicate, observations or {})
            ok, description = compare(actual[selector], resolved)
            result = {"assertion": label, "pass": ok}
            if not ok:
                result.update({"expected": resolved, "actual": actual[selector], "reason": description})
                failures.append(result)
            results.append(result)
        except MissingReference as error:
            result = {"assertion": label, "pass": False, "reason": f"unavailable reference: {error}"}
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
    require(isinstance(args, list) and all(isinstance(x, str) or is_reference(x) for x in args),
            f"{label}.command.args must contain strings or references")
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
    # Validate every reference and command shape before any side effect.
    seen = set()
    for index, step in enumerate(steps):
        require(isinstance(step, dict), f"step[{index}] must be an attribute set")
        sid = step.get("id", f"step-{index}")
        require(isinstance(sid, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", sid) is not None,
                f"step[{index}].id is invalid")
        require(sid not in seen, f"duplicate step ID: {sid}")
        argv, _ = validate_command(step.get("command"), sid)
        validate_references(argv, seen, f"{sid}.command")
        validate_references(step.get("expect", {}), seen, f"{sid}.expect")
        seen.add(sid)
    validate_references(final_expect, seen, "expect")
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
            try:
                argv = materialize_refs(argv, observations)
                if not all(isinstance(arg, str) for arg in argv):
                    raise MissingReference("command argument resolved to a non-string; select a string field")
            except MissingReference as error:
                failures.append({"assertion": f"{sid}.execution", "pass": False,
                                 "reason": f"cannot resolve command argument: {error}"})
                observations[sid] = {}
                continue
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
            check({"exitCode": expected.get("exitCode", 0)}, observed, sid, failures, results, observations)
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
                                      f"{sid}.{channel}.json", failures, results, observations)
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
                          f"{sid}.{channel}", failures, results, observations)
            if "files" in expected:
                verify_files(root, expected["files"], f"{sid}.files", failures, results, observations)
        require(set(final_expect) <= {"files"}, "top-level expect supports only files for multi-step tests")
        if "files" in final_expect:
            verify_files(root, final_expect["files"], "files", failures, results, observations)
    return {"name": spec.get("name", "test"), "status": "fail" if failures else "pass",
            "assertions": results, "failures": failures, "steps": list(observations)}


def verify_files(root, files, prefix, failures, results, observations):
    require(isinstance(files, dict), f"{prefix} must be an attribute set")
    for filename, predicates in files.items():
        path = safe_path(root, filename)
        label = f"{prefix}.{filename}"
        require(isinstance(predicates, dict) and bool(predicates),
                f"{label}: file assertions must be a nonempty set")
        require(set(predicates) <= {"exists", "text", "json", "size"},
                f"{label}: unknown file assertions")
        exists = path.exists()
        presence = predicates.get("exists", True)
        require(isinstance(presence, bool), f"{label}.exists must be a boolean")
        require(presence or len(predicates) == 1,
                f"{label}: cannot request content of an expected-absent file")
        check({"exists": presence}, {"exists": exists}, label,
              failures, results, observations)
        if not exists or len(predicates) == 1 and "exists" in predicates:
            continue
        require(path.is_file(), f"{label}: content assertions require a regular file")
        if "size" in predicates:
            check({"size": predicates["size"]}, {"size": path.stat().st_size},
                  label, failures, results, observations)
        if "text" in predicates or "json" in predicates:
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                result = {"assertion": f"{label}.text", "pass": False,
                          "reason": f"cannot read UTF-8 content: {error}"}
                failures.append(result)
                results.append(result)
                continue
            if "text" in predicates:
                text_tests = predicates["text"]
                require(isinstance(text_tests, dict) and bool(text_tests) and
                        set(text_tests) <= {"eq", "ne", "contains", "matches", "empty"},
                        f"{label}.text: invalid string assertions")
                for operation, expected_value in text_tests.items():
                    actual = len(content) == 0 if operation == "empty" else content
                    predicate = expected_value if operation == "empty" else {operation: expected_value}
                    check({operation: predicate}, {operation: actual},
                          f"{label}.text", failures, results, observations)
            if "json" in predicates:
                require(isinstance(predicates["json"], dict), f"{label}.json must be an attribute set")
                try:
                    document = json.loads(content)
                except json.JSONDecodeError as error:
                    result = {"assertion": f"{label}.json", "pass": False,
                              "reason": f"invalid JSON: {error}"}
                    failures.append(result)
                    results.append(result)
                    continue
                for location, predicate in predicates["json"].items():
                    try:
                        actual = pointer(document, location)
                        check({location: predicate}, {location: actual},
                              f"{label}.json", failures, results, observations)
                    except (KeyError, IndexError, TypeError) as error:
                        result = {"assertion": f"{label}.json.{location}", "pass": False,
                                  "reason": f"missing JSON location: {error}"}
                        results.append(result)
                        failures.append(result)


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
