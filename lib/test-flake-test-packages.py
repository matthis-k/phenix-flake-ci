#!/usr/bin/env python3
"""Behavioral proof of generic test package discovery and strict evidence."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "flake_test_packages", Path(__file__).with_name("flake-test-packages.py"))
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

D = "/nix/store/" + "a" * 32
C = "a" * 40

def test_item(name):
    return {"drv": D + f"-test-{name}.drv", "out": D + f"-test-{name}",
            "exe": D + f"-test-{name}/bin/test-{name}"}

def test_plan(**entries):
    result = {"schemaVersion": subject.SCHEMA, "commit": C,
              "system": "x86_64-linux", "tests": entries}
    result["digest"] = subject.digest(result)
    return result


class TestPackageTests(unittest.TestCase):
    def test_deterministic_batch_partition_preserves_all_tests_above_256(self):
        plan = test_plan(**{f"case{i:04d}": test_item(f"case{i:04d}")
                            for i in range(1024)})
        self.assertIs(subject.validate(plan), plan)
        batches = subject.make_batches(plan["tests"])
        self.assertEqual(batches, subject.make_batches(plan["tests"]))
        self.assertEqual(len(batches), subject.DEFAULT_MAX_TEST_JOBS)
        self.assertTrue(all(batch["names"] for batch in batches))
        self.assertEqual(set(name for batch in batches for name in batch["names"]),
                         set(plan["tests"]))
        self.assertEqual(sum(len(batch["names"]) for batch in batches), 1024)
        self.assertLessEqual(max(map(lambda x: len(x["names"]), batches)) -
                             min(map(lambda x: len(x["names"]), batches)), 1)
        self.assertEqual(subject.make_batches({}), [])
        self.assertEqual(len(subject.make_batches({
            "one": test_item("one"), "two": test_item("two")})), 1)
        self.assertEqual(len(subject.make_batches({
            "one": test_item("one"), "two": test_item("two")},
            tests_per_job=1)), 2)
        for bad in (0, -1, 257, True):
            with self.subTest(max_jobs=bad), self.assertRaises(ValueError):
                subject.make_batches(plan["tests"], max_jobs=bad)
        for bad in (0, -4, True):
            with self.subTest(tests_per_job=bad), self.assertRaises(ValueError):
                subject.make_batches(plan["tests"], tests_per_job=bad)

    def test_untrusted_staged_dag_executes_all_prebuilt_test_packages(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    ".github/workflows/flake-dag-staged.yml").read_text()
        plan = workflow.split("  plan:\n", 1)[1].split("  shared:\n", 1)[0]
        join = workflow.split("  join:\n", 1)[1].split("  recovery:\n", 1)[0]
        test = workflow.split("  test:\n", 1)[1].split("  acceptance:\n", 1)[0]
        gate = workflow.split("  acceptance:\n", 1)[1]
        self.assertIn("flake-test-packages.py plan", plan)
        self.assertIn("github.event.pull_request.number", workflow)
        self.assertNotIn("github.head_ref || github.ref_name", workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", workflow)
        self.assertIn("test_matrix:", plan)
        self.assertIn("test_count:", plan)
        self.assertIn("--test-packages ci-plan/test-packages.json", join)
        self.assertIn("--test-root", join)
        self.assertNotIn("flake-test-packages.py build", join)
        self.assertLess(join.index("flake-dag-stages.py join"),
                        join.index("flake-dag-stages.py verify"))
        self.assertLess(join.index("flake-dag-stages.py verify"),
                        join.index("flake-test-packages.py export"))
        self.assertIn("name: nix-dag-test-closure", join)
        self.assertIn("compression-level: 1", join)
        self.assertIn("needs: [plan, join, recovery]", test)
        self.assertIn("--closure dag-test-closure/closure.nar", test)
        self.assertIn("ci-plan/runtime-runner.py batch", test)
        self.assertIn("cp .ci-tools/flake-ci/lib/flake-test-packages.py ci-plan/runtime-runner.py", plan)
        self.assertNotIn("actions/checkout@", test)
        self.assertIn("toJSON(matrix.names)", test)
        self.assertIn("--results dag-test-result/", test)
        self.assertIn("matrix.id", test)
        self.assertIn("--expected-commit", test)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.sha || github.sha }}", test)
        self.assertIn("access-tokens =", test)
        self.assertNotIn("github_access_token:", test)
        self.assertNotIn("secrets.GITHUB_TOKEN", test)
        self.assertIn("contents: read", test)
        self.assertNotIn("nix build", test)
        for forbidden in ("TS_OAUTH", "id-token: write", "secrets: inherit"):
            self.assertNotIn(forbidden, test)
        self.assertIn("needs: [plan, shared, leaf, join, recovery, test]", gate)
        self.assertIn("flake-test-packages.py verify", gate)
        self.assertLess(gate.index("flake-test-packages.py verify"),
                        gate.index("Summarize upstream jobs and enforce final acceptance"))
        self.assertIn("id: verify_runtime", gate)
        self.assertIn("if: ${{ !cancelled() }}", gate)

    def test_failed_join_recovers_independent_runtime_without_green_path_rebuild(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    ".github/workflows/flake-dag-staged.yml").read_text()
        recovery = workflow.split("  recovery:\n", 1)[1].split("  test:\n", 1)[0]
        tests = workflow.split("  test:\n", 1)[1].split("  acceptance:\n", 1)[0]
        gate = workflow.split("  acceptance:\n", 1)[1]
        # Recovery does not compile anything if the normal join succeeded.
        self.assertIn("needs.join.result != 'success'", recovery)
        self.assertIn("needs.plan.result == 'success'", recovery)
        # A check-only flake must enter recovery; only the runtime probe waits
        # for a nonempty executable test inventory.
        self.assertNotIn("needs.plan.outputs.test_count", recovery.split("    steps:", 1)[0])
        self.assertIn("if: ${{ !cancelled() && needs.plan.outputs.test_count != '0' }}", recovery)
        self.assertIn("flake-check-dag.py build", recovery)
        self.assertIn("flake-dag-stages.py recover-leaves", recovery)
        self.assertIn("pattern: dag-leaf-*", recovery)
        self.assertIn("continue-on-error: true", recovery)
        self.assertLess(recovery.index("recover-leaves"),
                        recovery.index("flake-check-dag.py build"))
        self.assertIn("--plan ci-plan/plan.json", recovery)
        self.assertIn("--graph ci-plan/graph.json", recovery)
        self.assertIn("--test-packages ci-plan/test-packages.json", recovery)
        self.assertIn("--output dag-recovery/evidence.json", recovery)
        self.assertNotIn("flake-test-packages.py build", recovery)
        self.assertIn("continue-on-error: true", recovery)
        self.assertIn("flake-test-packages.py probe", recovery)
        self.assertIn("flake-test-packages.py export", recovery)
        self.assertIn("flake-test-packages.py all-blocked", recovery)
        self.assertIn("name: dag-runnable-test-recovery-blocked", recovery)
        self.assertIn("path: dag-blocked-result/", recovery)
        self.assertIn("if-no-files-found: error", recovery)
        self.assertIn('echo "ready=false" >> "$GITHUB_OUTPUT"', recovery)
        self.assertIn("pattern: dag-runnable-test-*", gate)
        self.assertIn("--available dag-test-closure/available.json", recovery)
        self.assertIn("runtime_ready: ${{ steps.ready.outputs.ready }}", recovery)
        self.assertIn("save: false", recovery)
        self.assertNotIn("id-token: write", recovery)
        # The test runners import exactly one of the two verified archives.
        self.assertIn("needs: [plan, join, recovery]", tests)
        self.assertIn("needs.recovery.outputs.runtime_ready == 'true'", tests)
        self.assertIn("needs.join.result == 'success'", tests)
        self.assertIn("name: nix-dag-test-closure", tests)
        self.assertIn("name: nix-dag-recovery-test-closure", tests)
        self.assertIn('available=(--available dag-test-closure/available.json)', tests)
        self.assertNotIn("flake-test-packages.py build", tests)
        self.assertNotIn("actions/checkout@", tests)
        self.assertIn("needs: [plan, shared, leaf, join, recovery, test]", gate)
        self.assertIn('test "$JOIN" = success || failed=1', gate)
        self.assertIn('test "$RECOVERY" = skipped || failed=1', gate)
        self.assertIn("flake-test-packages.py verify", gate)

    def test_workflow_requires_same_run_handoff_before_execution(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/flake-nix-builders.yml").read_text()
        coordinator = workflow.split("  coordinator:\n", 1)[1].split("  test:\n", 1)[0]
        runner = workflow.split("  test:\n", 1)[1].split("  acceptance:\n", 1)[0]
        self.assertLess(coordinator.index("--test-packages"), coordinator.index("flake-test-packages.py export"))
        self.assertLess(coordinator.index("flake-test-packages.py export"), coordinator.index("name: remote-test-closure-"))
        self.assertIn("if-no-files-found: error", coordinator)
        self.assertIn("name: remote-test-closure-", coordinator)
        self.assertIn("compression-level: 1", coordinator)
        self.assertIn("needs: [plan, coordinator]", runner)
        self.assertLess(runner.index("name: remote-test-closure-"), runner.index("ci-plan/runtime-runner.py batch"))
        self.assertIn("toJSON(matrix.names)", runner)
        self.assertIn("--results \"ci-result/\"", runner)
        self.assertIn("matrix.id", runner)
        self.assertIn("--expected-commit", runner)
        self.assertIn("cp .ci-tools/flake-ci/lib/flake-test-packages.py ci-plan/runtime-runner.py", workflow)
        self.assertNotIn("actions/checkout@", runner)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.sha || github.sha }}", runner)
        self.assertIn("--closure ci-test-closure/closure.nar", runner)
        self.assertNotIn("cache-nix-action", runner)
        self.assertNotIn("Restore compiled test packages", runner)
        self.assertIn("contents: read", runner)
        self.assertIn("access-tokens =", runner)
        self.assertNotIn("github_access_token:", runner)
        self.assertNotIn("secrets.GITHUB_TOKEN", runner)
        for forbidden in ("TS_OAUTH", "id-token: write", "nix build", "secrets: inherit"):
            self.assertNotIn(forbidden, runner)

    def test_probe_preserves_independently_built_runtime_packages(self):
        plan = test_plan(**{n: test_item(n) for n in ("alpha", "beta", "gamma")})
        available = {"alpha", "gamma"}

        def query(argv, **kwargs):
            self.assertEqual(argv[:3], ["nix", "path-info", "--offline"])
            if len(argv) > 4 or argv[-1] == plan["tests"]["beta"]["out"]:
                raise subprocess.CalledProcessError(1, argv)
            return argv[-1] + "\n"

        with mock.patch.object(subject.subprocess, "check_output",
                               side_effect=query), mock.patch.object(
                Path, "is_file", return_value=True), mock.patch.object(
                subject.os, "access", return_value=True):
            ready = subject.availability_for(plan, sorted(plan["tests"]))
        self.assertEqual(ready, sorted(available))

        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "available.json"
            subject.save(manifest, subject.availability_plan(plan, ready))
            self.assertEqual(subject.read_availability(plan, manifest), available)
            original = subject.load(manifest)
            for mutation in (
                    {**original, "digest": "forged"},
                    {**original, "available": ["alpha", "alpha"]},
                    {**original, "available": ["missing"]},
                    {**original, "commit": "f" * 40}):
                subject.save(manifest, mutation)
                with self.assertRaisesRegex(ValueError, "invalid|stale"):
                    subject.read_availability(plan, manifest)

    def test_all_blocked_records_full_evidence_without_runner_or_nix(self):
        plan = test_plan(**{name: test_item(name) for name in
                            ("alpha", "beta", "gamma")})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject.save(root / "plan.json", plan)
            availability = root / "available.json"
            subject.save(availability, subject.availability_plan(plan, []))
            args = type("Args", (), {"plan": root / "plan.json",
                                      "available": availability,
                                      "results": root / "results"})()
            with mock.patch.object(subject, "git_head", return_value=C), \
                 mock.patch.object(subject.subprocess, "run") as no_nix, \
                 mock.patch.object(subject.subprocess, "check_output") as no_query:
                subject.all_blocked_command(args)
                no_nix.assert_not_called()
                no_query.assert_not_called()
            self.assertEqual(sorted(path.parent.name for path in
                             args.results.rglob("evidence.json")),
                             ["alpha", "beta", "gamma"])
            self.assertTrue(all(subject.load(
                args.results / name / "evidence.json")["status"] == "blocked"
                for name in plan["tests"]))
            with self.assertRaises(ValueError) as error:
                subject.verify_command(type("Args", (), {
                    "plan": args.plan, "results": args.results})())
            report = str(error.exception)
            for name in ("alpha", "beta", "gamma"):
                self.assertIn("blocked test package " + name, report)
            # Never allow the fallback to manufacture blocked records when
            # even one case can still run independently.
            subject.save(availability, subject.availability_plan(plan, ["beta"]))
            with self.assertRaisesRegex(ValueError, "some are available"):
                subject.all_blocked_command(args)
            subject.save(availability, subject.availability_plan(plan, []))
            tampered = subject.load(availability)
            tampered["digest"] = "f" * 64
            subject.save(availability, tampered)
            with self.assertRaisesRegex(ValueError, "stale or invalid"):
                subject.all_blocked_command(args)

    def test_all_blocked_rejects_foreign_commit_and_empty_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = test_plan(alpha=test_item("alpha"))
            subject.save(root / "plan.json", plan)
            subject.save(root / "available.json", subject.availability_plan(plan, []))
            args = type("Args", (), {"plan": root / "plan.json",
                                      "available": root / "available.json",
                                      "results": root / "results"})()
            with mock.patch.object(subject, "git_head", return_value="b" * 40):
                with self.assertRaisesRegex(ValueError, "exact head"):
                    subject.all_blocked_command(args)
            self.assertFalse(args.results.exists())
            empty = test_plan()
            subject.save(args.plan, empty)
            subject.save(args.available, subject.availability_plan(empty, []))
            with mock.patch.object(subject, "git_head", return_value=C):
                with self.assertRaisesRegex(ValueError, "no runnable"):
                    subject.all_blocked_command(args)

    def test_partial_runtime_batch_runs_siblings_and_never_passes_blocked_test(self):
        plan = test_plan(**{n: test_item(n) for n in ("alpha", "beta", "gamma")})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "plan.json"
            availability = root / "available.json"
            subject.save(manifest, plan)
            subject.save(availability, subject.availability_plan(
                plan, ["alpha", "gamma"]))
            args = type("Args", (), {
                "plan": manifest, "local": True, "available": availability,
                "closure": None, "expected_commit": None,
                "names_json": '["alpha","beta","gamma"]',
                "results": root / "results",
            })()
            ran = []

            def execute(argv, **kwargs):
                ran.append(argv[0])
                location = Path(kwargs["env"]["PHENIX_TEST_OUTPUT"])
                location.mkdir(parents=True, exist_ok=True)
                (location / "result.json").write_text('{"status":"pass"}')
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(subject, "verify_head"), mock.patch.object(
                    subject, "verify_outputs") as verified, mock.patch.object(
                    subject, "import_closure") as imported, mock.patch.object(
                    subject, "run_isolated", side_effect=execute):
                with self.assertRaisesRegex(ValueError, "1 of 3 test packages failed"):
                    subject.batch_command(args)
                verified.assert_called_once_with(plan, ["alpha", "gamma"])
                imported.assert_not_called()
            self.assertEqual(ran, [plan["tests"][n]["exe"] for n in ("alpha", "gamma")])
            self.assertEqual([
                subject.load(root / "results" / n / "evidence.json")["status"]
                for n in ("alpha", "beta", "gamma")], ["pass", "blocked", "pass"])
            self.assertIn("required runtime output", subject.load(
                root / "results/beta/evidence.json")["reason"])
            with self.assertRaisesRegex(ValueError, "blocked test package beta"):
                subject.verify_command(type("Args", (), {
                    "plan": manifest, "results": root / "results"})())

    def test_missing_output_after_probe_blocks_only_affected_case(self):
        # A package can disappear after the coordinator captures availability.
        plan = test_plan(**{name: test_item(name)
                            for name in ("alpha", "beta", "gamma")})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "plan.json"
            available = root / "available.json"
            subject.save(manifest, plan)
            subject.save(available, subject.availability_plan(
                plan, ["alpha", "beta", "gamma"]))
            args = type("Args", (), {
                "plan": manifest, "local": True, "available": available,
                "closure": None, "expected_commit": None,
                "names_json": '["alpha","beta","gamma"]',
                "results": root / "results",
            })()
            queried = []
            executed = []

            def query(argv, **kwargs):
                self.assertEqual(argv[:3], ["nix", "path-info", "--offline"])
                roots = argv[3:]
                queried.append(roots)
                if plan["tests"]["beta"]["out"] in roots:
                    raise subprocess.CalledProcessError(1, argv)
                return "\n".join(roots) + "\n"

            def execute(argv, **kwargs):
                executed.append(argv[0])
                runtime = Path(kwargs["env"]["PHENIX_TEST_OUTPUT"])
                runtime.mkdir(parents=True, exist_ok=True)
                (runtime / "result.json").write_text('{"status":"pass"}')
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(subject, "verify_head"), mock.patch.object(
                    subject.subprocess, "check_output", side_effect=query), mock.patch.object(
                    subject, "run_isolated", side_effect=execute):
                with self.assertRaisesRegex(ValueError, "1 of 3 test packages failed"):
                    subject.batch_command(args)
            self.assertEqual(queried, [
                [plan["tests"][name]["out"] for name in ("alpha", "beta", "gamma")],
                *[[plan["tests"][name]["out"]] for name in ("alpha", "beta", "gamma")],
            ])
            self.assertEqual(executed, [
                plan["tests"]["alpha"]["exe"], plan["tests"]["gamma"]["exe"]])
            self.assertEqual([
                subject.load(root / "results" / name / "evidence.json")["status"]
                for name in ("alpha", "beta", "gamma")],
                ["pass", "blocked", "pass"])
            with self.assertRaisesRegex(ValueError, "blocked test package beta"):
                subject.verify_command(type("Args", (), {
                    "plan": manifest, "results": root / "results",
                })())

    def test_runtime_partial_workflow_never_weakens_final_evidence(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    ".github/workflows/flake-nix-builders.yml").read_text()
        coordinator = workflow.split("  coordinator:\n", 1)[1].split(
            "  test:\n", 1)[0]
        tester = workflow.split("  test:\n", 1)[1].split(
            "  acceptance:\n", 1)[0]
        final = workflow.split("  acceptance:\n", 1)[1]
        self.assertIn("runtime_ready: ${{ steps.runtime_ready.outputs.ready }}",
                      coordinator)
        self.assertIn("!cancelled() && steps.build.outcome != 'skipped'", coordinator)
        self.assertIn("!cancelled() && needs.plan.result == 'success'", tester)
        self.assertIn("flake-test-packages.py probe", coordinator)
        self.assertIn("flake-test-packages.py export", coordinator)
        self.assertIn("--available ci-test-closure/available.json", coordinator)
        self.assertIn("needs.coordinator.outputs.runtime_ready == 'true'", tester)
        self.assertIn("--available ci-test-closure/available.json", tester)
        self.assertIn('test "$COORDINATOR" = success', final)
        self.assertIn("flake-test-packages.py verify", final)

    def test_acceptance_verdicts_execute_independently_and_fail_closed(self):
        # Exercise the actual generated gate scripts, not just substrings.
        # A failed coordinator or a failed verifier must never be green.
        import textwrap
        for workflow_name, stages in (
            ("flake-nix-builders.yml",
             ("PLAN", "WORKERS", "COORDINATOR", "TESTS", "TEST_COUNT",
              "DAG_VERDICT", "RUNTIME_VERDICT")),
            ("flake-dag-staged.yml",
             ("PLAN", "SHARED", "LEAF", "JOIN", "RECOVERY", "TESTS", "TEST_COUNT",
              "RUNTIME_VERDICT")),
        ):
            with self.subTest(workflow=workflow_name):
                workflow = (Path(__file__).resolve().parents[1] /
                            ".github/workflows" / workflow_name).read_text()
                acceptance = workflow.split("  acceptance:\n", 1)[1]
                self.assertLess(
                    acceptance.index("flake-test-packages.py verify"),
                    acceptance.index("Summarize upstream jobs and enforce final acceptance"))
                entry = acceptance.split(
                    "      - name: Summarize upstream jobs and enforce final acceptance\n", 1)[1]
                script = textwrap.dedent(entry.split("        run: |\n", 1)[1])
                defaults = {key: "success" for key in stages}
                defaults.update(TEST_COUNT="2")
                if "RECOVERY" in stages:
                    # The normal successful join skips recovery entirely.
                    defaults["RECOVERY"] = "skipped"
                with tempfile.TemporaryDirectory() as folder:
                    summary = Path(folder) / "summary.md"
                    for overrides, expected_code in (
                        ({}, 0),
                        ({"COORDINATOR": "failure"}, 1),
                        ({"JOIN": "failure"}, 1),
                        ({"JOIN": "failure", "RECOVERY": "success"}, 1),
                        ({"RECOVERY": "success"}, 1),
                        ({"RUNTIME_VERDICT": "failure"}, 1),
                        ({"TESTS": "failure"}, 1),
                        ({"TEST_COUNT": "0", "TESTS": "skipped"}, 0),
                        ({"TEST_COUNT": "0", "TESTS": "success"}, 1),
                    ):
                        if any(key not in stages for key in overrides):
                            continue
                        with self.subTest(overrides=overrides):
                            values = {**defaults, **overrides}
                            result = subprocess.run(
                                ["bash", "-e", "-c", script],
                                env={**os.environ, **values,
                                     "GITHUB_STEP_SUMMARY": str(summary)},
                                capture_output=True, text=True)
                            self.assertEqual(result.returncode, expected_code,
                                             result.stderr + result.stdout)
                    self.assertIn("Exact-head CI acceptance", summary.read_text())

    def test_batch_imports_closure_once_and_retains_individual_failure_evidence(self):
        plan = test_plan(**{name: test_item(name) for name in ("alpha", "beta", "gamma")})
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            archive = folder / "closure.nar"
            archive.write_bytes(b"one-archive")
            manifest = folder / "plan.json"
            subject.save(manifest, plan)
            args = type("Args", (), {
                "plan": manifest, "closure": archive,
                "names_json": '["alpha","beta","gamma"]',
                "results": folder / "results", "expected_commit": C,
            })()
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv == ["nix-store", "--import"]:
                    self.assertEqual(kwargs["stdin"].read(), b"one-archive")
                    return subprocess.CompletedProcess(argv, 0)
                self.assertEqual(len(argv), 1)
                name = next(name for name, item in plan["tests"].items()
                            if item["exe"] == argv[0])
                workspace = Path(kwargs["cwd"])
                self.assertEqual(kwargs["env"]["HOME"], str(workspace))
                self.assertEqual(kwargs["env"]["TMPDIR"], str(workspace))
                if name == "alpha":
                    (workspace / "marker").write_text("alpha-only")
                else:
                    self.assertFalse((workspace / "marker").exists())
                if name == "beta":
                    raise subprocess.CalledProcessError(7, argv)
                runtime_dir = Path(kwargs["env"]["PHENIX_TEST_OUTPUT"])
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "result.json").write_text('{"status":"pass"}')
                return subprocess.CompletedProcess(argv, 0)

            paths = "\n".join(sorted(x["out"] for x in plan["tests"].values())) + "\n"
            with mock.patch.object(subject, "git_head") as no_git, mock.patch.object(
                    subject.subprocess, "run", side_effect=fake_run), mock.patch.object(
                    subject, "run_isolated", side_effect=fake_run), mock.patch.object(
                    subject.subprocess, "check_output", return_value=paths) as info:
                with self.assertRaisesRegex(ValueError, "1 of 3 test packages failed"):
                    subject.batch_command(args)
                no_git.assert_not_called()
                info.assert_called_once()
            self.assertEqual(sum(argv == ["nix-store", "--import"] for argv in calls), 1)
            self.assertEqual(len(calls), 4)
            for name, expected in (("alpha", "pass"), ("beta", "failed"),
                                   ("gamma", "pass")):
                evidence = subject.load(args.results / name / "evidence.json")
                self.assertEqual(evidence["name"], name)
                self.assertEqual(evidence["status"], expected)
                self.assertEqual(evidence["digest"], plan["digest"])
            self.assertIn("returned non-zero exit status",
                          subject.load(args.results / "beta" / "evidence.json")["reason"])
            subject.save(args.results / "beta" / "evidence.json",
                         {**subject.load(args.results / "beta" / "evidence.json"),
                          "status": "pass"})
            with self.assertRaisesRegex(ValueError, "expected 3"):
                (args.results / "beta" / "evidence.json").unlink()
                subject.verify_command(type("Args", (), {
                    "plan": manifest, "results": args.results,
                })())

    def test_timeout_preserves_per_case_failure_and_continues_batch(self):
        plan = test_plan(**{name: test_item(name) for name in ("hang", "next")})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "plan.json"
            subject.save(manifest, plan)
            archive = root / "closure.nar"
            archive.write_bytes(b"nar")
            args = type("Args", (), {
                "plan": manifest, "closure": archive,
                "names_json": '["hang","next"]',
                "results": root / "results", "expected_commit": C,
                "timeout_seconds": 2,
            })()
            invoked = []
            def fake_isolated(argv, **kwargs):
                invoked.append(kwargs["timeout"])
                if argv[0] == plan["tests"]["hang"]["exe"]:
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"],
                                                    output="begin", stderr="stuck")
                runtime = Path(kwargs["env"]["PHENIX_TEST_OUTPUT"])
                runtime.mkdir(parents=True, exist_ok=True)
                (runtime / "result.json").write_text('{"status":"pass"}')
                return subprocess.CompletedProcess(argv, 0, "", "")
            info = "\n".join(sorted(x["out"] for x in plan["tests"].values())) + "\n"
            with mock.patch.object(subject.subprocess, "run"), mock.patch.object(
                    subject, "run_isolated", side_effect=fake_isolated), mock.patch.object(
                    subject.subprocess, "check_output", return_value=info):
                with self.assertRaisesRegex(ValueError, "1 of 2 test packages failed"):
                    subject.batch_command(args)
            self.assertEqual(invoked, [2, 2])
            failed = subject.load(args.results / "hang" / "evidence.json")
            self.assertEqual(failed["status"], "failed")
            self.assertIn("timed out", failed["reason"])
            self.assertEqual(failed["stderrTail"], "stuck")
            self.assertEqual(subject.load(args.results / "next" / "evidence.json")["status"], "pass")

    def test_case_timeout_limits_and_process_group_cleanup(self):
        from test_services import run_isolated
        for value in (0, -1, 3601, True, "15"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                subject.validate_timeout(value)
        self.assertEqual(subject.validate_timeout(300), 300)
        with tempfile.TemporaryDirectory() as directory:
            script = ("import subprocess,sys,time; "
                      "subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)']);"
                      "time.sleep(30)")
            with self.assertRaises(subprocess.TimeoutExpired):
                run_isolated([sys.executable, "-c", script],
                             cwd=directory, env=os.environ.copy(), timeout=0.2)

    def test_bad_batch_cannot_import_or_execute_anything(self):
        plan = test_plan(alpha=test_item("alpha"))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "plan.json"
            subject.save(path, plan)
            with mock.patch.object(subject, "git_head", return_value=C), mock.patch.object(
                    subject.subprocess, "run") as no_run:
                for invalid in ('[]', '["alpha","alpha"]', '["missing"]',
                                '["alpha",3]', '"alpha"', 'not json'):
                    args = type("Args", (), {
                        "plan": path, "closure": folder / "nonexistent.nar",
                        "names_json": invalid, "results": folder / "results",
                        "expected_commit": C,
                    })()
                    with self.subTest(names_json=invalid), self.assertRaises(ValueError):
                        subject.batch_command(args)
                no_run.assert_not_called()

    def test_closure_handoff_executes_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = root / "plan.json"
            subject.save(plan, test_plan(unit=test_item("unit")))
            fake = root / "nix"
            fake.write_text('''#!'''+sys.executable+'''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps([pathlib.Path(sys.argv[0]).name, *args]) + "\\n")
if args[0] == os.environ.get("FAIL"):
    sys.exit(9)
out = os.environ["OUT"]
if args[0] == "--export":
    assert out in args and out + "-dependency" in args
    sys.stdout.buffer.write(b"closure")
elif args[0] == "--import":
    assert sys.stdin.buffer.read() == b"closure"
    pathlib.Path(os.environ["IMPORTED"]).touch()
elif args[0] == "path-info":
    assert "--offline" in args
    if "--recursive" in args:
        print(out + "-dependency")
    else:
        assert pathlib.Path(os.environ["IMPORTED"]).exists()
    print(out)
else:
    sys.exit(10)
''')
            fake.chmod(0o755)
            (root / "nix-store").symlink_to(fake)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "CALLS": str(root / "calls"), "OUT": test_item("unit")["out"],
                   "IMPORTED": str(root / "imported")}
            archive = root / "closure.nar"
            export_args = type("Args", (), {"plan": plan, "output": archive})()
            run_args = type("Args", (), {"plan": plan, "closure": archive,
                                        "name": "unit", "output": root / "evidence.json"})()
            native_run = subprocess.run
            executable = test_item("unit")["exe"]

            def run_prebuilt(argv, *args, **kwargs):
                if argv != [executable]:
                    return native_run(argv, *args, **kwargs)
                with open(os.environ["CALLS"], "a") as log:
                    log.write(json.dumps(["direct", "executable", *argv]) + "\n")
                if os.environ.get("FAIL") == "run":
                    raise subprocess.CalledProcessError(9, argv)
                self.assertTrue(Path(os.environ["IMPORTED"]).exists())
                directory = Path(kwargs["env"]["PHENIX_TEST_OUTPUT"])
                self.assertEqual(kwargs["env"]["HOME"], kwargs["cwd"])
                self.assertEqual(kwargs["env"]["TMPDIR"], kwargs["cwd"])
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "result.json").write_text(json.dumps(
                    {"status": "pass", "assertions": [
                        {"assertion": "main.exitCode", "pass": True}]}))
                return subprocess.CompletedProcess(argv, 0)

            with mock.patch.dict(os.environ, env), mock.patch.object(
                    subject, "git_head", return_value=C), mock.patch.object(
                    subject.subprocess, "run", side_effect=run_prebuilt), mock.patch.object(
                    subject, "run_isolated", side_effect=run_prebuilt):
                subject.export_command(export_args)
                self.assertEqual(archive.read_bytes(), b"closure")
                subject.run_command(run_args)
                calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
                self.assertEqual([call[1] for call in calls],
                                 ["path-info", "--export", "--import", "path-info", "executable"])
                self.assertEqual(calls[-1][2], executable)
                self.assertEqual(subject.load(run_args.output)["status"], "pass")
                self.assertEqual(subject.load(run_args.output)["runtime"]["status"], "pass")
                self.assertEqual(
                    subject.load(run_args.output)["runtime"]["assertions"][0]["assertion"],
                    "main.exitCode")
                for failure in ("--export", "--import", "path-info", "run"):
                    with self.subTest(failure=failure), mock.patch.dict(os.environ, {"FAIL": failure}):
                        before = len((root / "calls").read_text().splitlines())
                        with self.assertRaises(subprocess.CalledProcessError):
                            if failure == "--export":
                                subject.export_command(export_args)
                            else:
                                archive.write_bytes(b"closure")
                                subject.run_command(run_args)
                        tail = [json.loads(line)[1] for line in
                                (root / "calls").read_text().splitlines()[before:]]
                        self.assertEqual(tail[-1], "executable" if failure == "run" else failure)
                with mock.patch.object(subject, "git_head", return_value="b" * 40):
                    with self.assertRaisesRegex(ValueError, "exact head"):
                        subject.export_command(export_args)
                    with self.assertRaisesRegex(ValueError, "exact head"):
                        subject.run_command(run_args)

    def test_artifact_runner_verifies_commit_without_checkout(self):
        plan = test_plan(unit=test_item("unit"))
        with mock.patch.object(subject, "git_head") as git:
            subject.verify_head(plan, C)
            git.assert_not_called()
        for bad in ("b" * 40, "incomplete", None):
            if bad is None:
                continue
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "exact head"):
                subject.verify_head(plan, bad)

    def test_generated_runtime_report_is_bounded_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "result.json"
            report.write_bytes(b"x" * (subject.MAX_RUNTIME_REPORT_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "runtime report exceeds"):
                subject.read_runtime_report(report)
            report.unlink()
            secret = root / "outside.json"
            secret.write_text('{"status":"pass"}')
            report.symlink_to(secret)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                subject.read_runtime_report(report)
            report.unlink()
            report.write_text('{"status":"pass"}')
            self.assertEqual(subject.read_runtime_report(report)["status"], "pass")

    def test_large_runtime_report_produces_failed_evidence(self):
        plan = test_plan(alpha=test_item("alpha"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "plan.json"
            subject.save(manifest, plan)
            archive = root / "closure.nar"
            archive.write_bytes(b"nar")
            args = type("Args", (), {
                "plan": manifest, "names_json": '["alpha"]',
                "results": root / "results", "closure": archive,
                "expected_commit": C,
            })()
            def fake_run(argv, **kwargs):
                if argv == ["nix-store", "--import"]:
                    return subprocess.CompletedProcess(argv, 0)
                runtime = Path(kwargs["env"]["PHENIX_TEST_OUTPUT"])
                runtime.mkdir(parents=True, exist_ok=True)
                (runtime / "result.json").write_bytes(
                    b"x" * (subject.MAX_RUNTIME_REPORT_BYTES + 1))
                return subprocess.CompletedProcess(argv, 0)
            with mock.patch.object(subject.subprocess, "run", side_effect=fake_run), mock.patch.object(
                    subject, "run_isolated", side_effect=fake_run), mock.patch.object(
                    subject.subprocess, "check_output", return_value=plan["tests"]["alpha"]["out"]+"\n"):
                with self.assertRaisesRegex(ValueError, "1 of 1 test packages failed"):
                    subject.batch_command(args)
            result = subject.load(args.results / "alpha" / "evidence.json")
            self.assertEqual(result["status"], "failed")
            self.assertIn("runtime report exceeds", result["reason"])
            self.assertIn("runtime report exceeds", result["runtimeError"])

    def test_rejects_executables_outside_manifest_output(self):
        item = test_item("unit")
        for exe in (item["out"] + "/bin/../escape",
                    item["out"] + "/bin/nested/escape",
                    item["out"] + "-another/bin/test-unit", "/tmp/untrusted"):
            with self.subTest(exe=exe), self.assertRaises(ValueError):
                subject.validate(test_plan(unit={**item, "exe": exe}))

    def test_generic_name_and_quoted_nix_attribute(self):
        self.assertEqual(subject.target("x86_64-linux", "basic-smoke"),
                         '.#packages.x86_64-linux."test.basic-smoke"')
        for name in ("", "../bad", "bad.name", "bad space"):
            with self.assertRaises(ValueError):
                subject.target("x86_64-linux", name)

    def test_empty_inventory_is_valid_and_malformed_is_not(self):
        subject.validate(test_plan())
        subject.validate(test_plan(unit=test_item("unit")))
        for value in [
            {**test_plan(), "commit": "dirty"},
            {**test_plan(), "digest": "forged"},
            test_plan(bad={"drv": "not store", "out": "no"}),
        ]:
            with self.assertRaises(ValueError):
                subject.validate(value)

    def test_discovery_uses_actual_flake_packages_without_domain_names(self):
        payload = {
            "test.basic": test_item("basic"),
            "test.integration": test_item("integration"),
        }
        with mock.patch.object(subject.subprocess, "check_output",
                               return_value=json.dumps(payload)) as query:
            self.assertEqual(subject.discover_inventory("x86_64-linux"), {
                "basic": test_item("basic"), "integration": test_item("integration")})
            args = query.call_args.args[0]
            self.assertEqual(args[-1], ".#packages.x86_64-linux")
            self.assertIn("builtins.filter", args[-2])
            self.assertIn("builtins.attrNames", args[-2])
            self.assertIn(".mainProgram", args[-2])
            self.assertIn("/bin/", args[-2])

    def test_verify_reports_all_independent_failures_and_preserves_successes(self):
        plan = test_plan(**{name: test_item(name) for name in (
            "alpha", "beta", "gamma", "delta", "epsilon")})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / "plan.json"
            subject.save(manifest, plan)

            def evidence(name, status="pass", reason=None):
                record = {"schemaVersion": subject.SCHEMA, "digest": plan["digest"],
                          "commit": C, "name": name, **plan["tests"][name],
                          "status": status}
                if reason is not None:
                    record["reason"] = reason
                return record

            subject.save(root / "alpha" / "evidence.json",
                         evidence("alpha", "failed", "compiler error on one test"))
            subject.save(root / "beta" / "evidence.json",
                         evidence("beta", "blocked", "missing output"))
            subject.save(root / "gamma" / "evidence.json", evidence("gamma"))
            subject.save(root / "epsilon" / "evidence.json",
                         {**evidence("epsilon"), "commit": "f" * 40})
            subject.save(root / "unexpected" / "evidence.json",
                         {**evidence("gamma"), "name": "unexpected"})
            args = type("Args", (), {"plan": manifest, "results": root})()
            with self.assertRaises(ValueError) as raised:
                subject.verify_command(args)
            report = str(raised.exception)
            for name in ("alpha", "beta", "delta", "epsilon", "unexpected"):
                self.assertIn(name, report)
            self.assertIn("failed test package alpha", report)
            self.assertIn("blocked test package beta", report)
            self.assertIn("incomplete runnable test package evidence", report)
            self.assertIn("failed, stale or forged", report)
            self.assertNotIn("failed test package gamma", report)

    def test_verify_corrupt_evidence_does_not_hide_other_failures(self):
        plan = test_plan(alpha=test_item("alpha"), beta=test_item("beta"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subject.save(root / "plan.json", plan)
            damaged = root / "alpha" / "evidence.json"
            damaged.parent.mkdir()
            damaged.write_text("{broken json")
            subject.save(root / "beta" / "evidence.json", {
                "schemaVersion": subject.SCHEMA, "digest": plan["digest"],
                "commit": C, "name": "beta", **plan["tests"]["beta"],
                "status": "failed", "reason": "independent assertion"})
            with self.assertRaises(ValueError) as raised:
                subject.verify_command(type("Args", (), {
                    "plan": root / "plan.json", "results": root})())
            self.assertIn("unreadable test-package evidence", str(raised.exception))
            self.assertIn("failed test package beta", str(raised.exception))

    def test_per_case_timing_is_measured_without_becoming_a_gate(self):
        plan = test_plan(alpha=test_item("alpha"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            evidence = root / "alpha" / "evidence.json"
            subject.save(root / "plan.json", plan)
            with mock.patch.object(
                    subject.time, "monotonic", side_effect=[15.0, 18.125]), \
                 mock.patch.object(subject, "run_isolated", return_value=
                                   subprocess.CompletedProcess(["test-alpha"], 0, "", "")):
                subject.execute_case(plan, "alpha", evidence)
            record = subject.load(evidence)
            self.assertEqual(record["status"], "pass")
            self.assertEqual(record["durationSeconds"], 3.125)
            args = type("Args", (), {
                "plan": root / "plan.json", "results": root})()
            with redirect_stdout(io.StringIO()) as output:
                subject.verify_command(args)
            self.assertIn("alpha=3.125s", output.getvalue())
            self.assertIn("1/1 measured", output.getvalue())
            # Optional timing metadata cannot override the exact-head status.
            subject.save(evidence, {**record, "durationSeconds": "untrusted"})
            with redirect_stdout(io.StringIO()) as output:
                subject.verify_command(args)
            self.assertNotIn("case timings", output.getvalue())
            subject.save(evidence, {**record, "durationSeconds": -99.0})
            subject.verify_command(args)
            subject.save(evidence, {**record, "status": "failed"})
            with self.assertRaisesRegex(ValueError, "failed test package alpha"):
                subject.verify_command(args)

    def test_evidence_must_be_under_its_planned_case_directory(self):
        plan = test_plan(alpha=test_item("alpha"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subject.save(root / "plan.json", plan)
            record = {"schemaVersion": subject.SCHEMA, "digest": plan["digest"],
                      "commit": C, "name": "alpha", **plan["tests"]["alpha"],
                      "status": "pass"}
            wrong = root / "other" / "evidence.json"
            subject.save(wrong, record)
            args = type("Args", (), {"plan": root / "plan.json", "results": root})()
            with self.assertRaises(ValueError) as raised:
                subject.verify_command(args)
            self.assertIn("misplaced test-package evidence", str(raised.exception))
            self.assertIn("missing alpha", str(raised.exception))
            wrong.unlink()
            canonical = root / "alpha" / "evidence.json"
            subject.save(canonical, record)
            subject.verify_command(args)
            canonical.unlink()
            canonical.symlink_to(wrong)
            with self.assertRaisesRegex(ValueError, "symlink test-package evidence"):
                subject.verify_command(args)

    def test_evidence_summary_counts_distinct_failures_and_missing(self):
        plan = test_plan(alpha=test_item("alpha"), beta=test_item("beta"),
                         gamma=test_item("gamma"), delta=test_item("delta"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subject.save(root / "plan.json", plan)
            def write(name, status):
                subject.save(root / name / "evidence.json", {
                    "schemaVersion": subject.SCHEMA, "digest": plan["digest"],
                    "commit": C, "name": name, **plan["tests"][name],
                    "status": status, "reason": "test explanation"})
            write("alpha", "pass")
            write("beta", "failed")
            write("gamma", "blocked")
            args = type("Args", (), {"plan": root / "plan.json", "results": root})()
            with redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(ValueError):
                    subject.verify_command(args)
            self.assertIn(
                "Runtime evidence: 1/4 passing; 1 failed; 1 blocked; 1 missing; 0 invalid",
                output.getvalue())

    def test_verify_full_exact_head_set_and_fail_closed(self):
        plan = test_plan(alpha=test_item("alpha"), beta=test_item("beta"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, b = root/"alpha"/"evidence.json", root/"beta"/"evidence.json"
            a.parent.mkdir()
            b.parent.mkdir()
            def evidence(name):
                return {"schemaVersion": subject.SCHEMA, "digest": plan["digest"],
                        "commit": C, "name": name, **plan["tests"][name],
                        "status": "pass"}
            subject.save(a, evidence("alpha"))
            subject.save(b, evidence("beta"))
            subject.save(root/"plan.json", plan)
            subject.verify_command(type("Args", (), {"plan": root/"plan.json",
                                                      "results": root})())
            subject.save(b, evidence("alpha"))
            with self.assertRaisesRegex(ValueError, "unknown or repeated"):
                subject.verify_command(type("Args", (), {"plan": root/"plan.json",
                                                          "results": root})())
            subject.save(b, {**evidence("beta"), "commit": "f"*40})
            with self.assertRaisesRegex(ValueError, "failed, stale"):
                subject.verify_command(type("Args", (), {"plan": root/"plan.json",
                                                          "results": root})())
            b.unlink()
            with self.assertRaisesRegex(ValueError, "expected 2"):
                subject.verify_command(type("Args", (), {"plan": root/"plan.json",
                                                          "results": root})())


if __name__ == "__main__":
    unittest.main()
