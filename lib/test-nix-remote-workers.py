#!/usr/bin/env python3
"""Offline contract checks for ephemeral remote-builder naming and scheduling."""
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

source = Path(__file__).with_name("nix-remote-workers.py")
spec = importlib.util.spec_from_file_location("remote_workers", source)
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)


class RemoteBuildersTest(unittest.TestCase):
    def test_build_children_share_remote_config_and_preserve_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            machines = root / "machines"
            machines.write_text("verified-worker\n")
            # Execute real children, recording their environment and argv rather
            # than only asserting the parent's subprocess.run keyword arguments.
            recorder = root / "dag.py"
            recorder.write_text(
                "import json, os, sys\n"
                "with open(os.environ['CHILD_RECORD'], 'a') as f:\n"
                "    f.write(json.dumps([sys.argv, os.environ['NIX_CONFIG']]) + '\\n')\n")
            packages = root / "flake-test-packages.py"
            packages.write_text(recorder.read_text())
            record = root / "children.jsonl"
            args = [sys.executable, str(source), "build", "--machines", str(machines),
                    "--dag", str(recorder), "--plan", "checks.json", "--graph", "graph.json",
                    "--evidence", "evidence.json", "--timing", str(root / "timing.json"),
                    "--test-packages", "tests.json", "--test-root", "roots"]
            # Copy the actual coordinator beside the recording package helper.
            coordinator = root / source.name
            coordinator.write_text(source.read_text())
            args[1] = str(coordinator)
            env = {**os.environ, "NIX_CONFIG": "experimental-features = nix-command flakes",
                   "CHILD_RECORD": str(record)}
            subprocess.run(args, env=env, check=True)
            children = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertEqual([child[0] for child in children], [
                [str(recorder), "build", "--plan", "checks.json", "--graph", "graph.json",
                 "--output", "evidence.json"],
                [str(packages), "build", "--plan", "tests.json", "--root", "roots"],
            ])
            expected = (env["NIX_CONFIG"] + f"\nbuilders = @{machines}\nmax-jobs = 0\n"
                        "builders-use-substitutes = true\n")
            self.assertEqual([child[1] for child in children], [expected, expected])
            self.assertEqual(json.loads((root / "timing.json").read_text())["workerCount"], 1)
            packages.write_text("raise SystemExit(7)\n")
            result = subprocess.run(args, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exit status 7", result.stderr)
            self.assertEqual(json.loads((root / "timing.json").read_text())["mode"],
                             "remote-nix-ssh-ng")

    def test_exact_pool_names_are_disjoint(self):
        names = {remote.worker_name(38350788475, 1, count, i)
                 for count in (2, 4, 8) for i in range(1, count + 1)}
        self.assertEqual(len(names), 14)
        self.assertEqual(remote.worker_name(123, 2, 2, 1), "fc-123-2-n2-w1")
        with self.assertRaises(ValueError):
            remote.worker_name(123, 1, 4, 5)
        with self.assertRaises(ValueError):
            remote.positive(0)
        with self.assertRaises(ValueError):
            remote.positive(17)

    def test_peers_only_accept_ipv4_tailnet_nodes(self):
        status = {"Peer": {"a": {"HostName": "fc-123-1-n2-w1",
                                 "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.77.3.8"]},
                           "b": {"HostName": "evil", "TailscaleIPs": ["8.8.8.8"]},
                           "c": {"HostName": "other", "TailscaleIPs": []}}}
        self.assertEqual(remote.peers(status), {"fc-123-1-n2-w1": "100.77.3.8"})

    def test_matrix_outputs_json_for_job_matrix(self):
        with redirect_stdout(io.StringIO()) as buf:
            remote.matrix(type("A", (), {"count": 4})())
        self.assertEqual(json.loads(buf.getvalue().split("=", 1)[1]), [1, 2, 3, 4])

    def test_discovery_checks_actual_nix_store_protocol_and_emits_machine_list(self):
        with tempfile.TemporaryDirectory() as directory:
            machines = Path(directory, "machines")
            hosts = Path(directory, "hosts.json")
            args = type("Args", (), {"count": 2, "system": "x86_64-linux", "slots": 3,
                                     "run_id": 22, "attempt": 1, "timeout": 4,
                                     "machines": str(machines), "hosts": str(hosts)})()
            status = {"Peer": {str(i): {"HostName": remote.worker_name(22, 1, 2, i),
                                        "TailscaleIPs": [f"100.64.0.{i}"]}
                               for i in (1, 2)}}
            with mock.patch.object(remote, "invoke", return_value=type(
                    "Result", (), {"stdout": json.dumps(status)})()) as runner, \
                 mock.patch.object(remote, "ssh_config") as sshconf:
                remote.discover(args)
            self.assertEqual(runner.call_count, 3)
            for call in runner.call_args_list[1:]:
                self.assertEqual(call.args[:3], ("sudo", remote.LOCAL_NIX, "store"))
                self.assertIn("remote-program=", call.args[5])
            sshconf.assert_called_once()
            lines = machines.read_text().splitlines()
            self.assertEqual(lines, [
                "ssh-ng://runner@100.64.0.1?remote-program=/nix/var/nix/profiles/default/bin/nix-daemon x86_64-linux - 3 1 - -",
                "ssh-ng://runner@100.64.0.2?remote-program=/nix/var/nix/profiles/default/bin/nix-daemon x86_64-linux - 3 1 - -",
            ])
            self.assertEqual(len(json.loads(hosts.read_text())), 2)

    def test_missing_remote_builder_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            args = type("Args", (), {"count": 2, "system": "x86_64-linux", "slots": 2,
                                     "run_id": 22, "attempt": 1, "timeout": 1,
                                     "machines": str(Path(directory, "machines")),
                                     "hosts": str(Path(directory, "hosts"))})()
            with mock.patch.object(remote, "invoke", return_value=type(
                    "Result", (), {"stdout": '{"Peer":{}}'})()), \
                 mock.patch.object(remote.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    remote.discover(args)
            self.assertFalse(Path(args.machines).exists())


    def test_discovery_exposes_ssh_ng_error_without_repeated_spam(self):
        with tempfile.TemporaryDirectory() as directory:
            args = type("Args", (), {"count": 1, "system": "x86_64-linux",
                                     "slots": 2, "run_id": 22, "attempt": 1,
                                     "timeout": 1,
                                     "machines": str(Path(directory, "machines")),
                                     "hosts": str(Path(directory, "hosts"))})()
            status = {"Peer": {"1": {"HostName": remote.worker_name(22, 1, 1, 1),
                                     "TailscaleIPs": ["100.64.0.1"]}}}

            def fake_invoke(*command, **kwargs):
                if command[:2] == ("tailscale", "status"):
                    return type("Result", (), {"stdout": json.dumps(status)})()
                raise subprocess.CalledProcessError(1, command, stderr="remote Nix store denied")

            with mock.patch.object(remote, "invoke", side_effect=fake_invoke), \
                 mock.patch.object(remote, "ssh_config"), \
                 mock.patch.object(remote.time, "monotonic", side_effect=[0, 0, 2]), \
                 mock.patch.object(remote.time, "sleep"), \
                 redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(RuntimeError):
                    remote.discover(args)
            self.assertEqual(output.getvalue().count("remote Nix store denied"), 1)
            self.assertFalse(Path(args.machines).exists())

    def test_reusable_workflow_uses_distinct_artifacts_for_each_pool_size(self):
        # Reusable calls run in the *same* GitHub workflow run. artifact v4
        # refuses duplicate names, including across sequential 2/4/8 calls.
        workflow = (Path(__file__).parent.parent / ".github/workflows/flake-nix-builders.yml").read_text()
        self.assertEqual(
            workflow.count("name: remote-nix-plan-${{ inputs.worker_count }}"), 4
        )
        self.assertEqual(
            workflow.count("name: remote-nix-evidence-${{ inputs.worker_count }}"), 2
        )
        self.assertNotIn("name: remote-nix-plan\n", workflow)
        self.assertNotIn("name: remote-nix-evidence\n", workflow)


    def test_same_run_cold_warm_namespace_isolated_without_altering_default_cache(self):
        # Reusable cold and warm jobs share github.run_id and github.sha.
        # A unique run namespace must isolate the cold trial from existing
        # caches, while phase suffixes prevent artifact-v4 collisions.
        workflow = (Path(__file__).parent.parent /
                    ".github/workflows/flake-nix-builders.yml").read_text()
        self.assertIn('cache_namespace: { type: string, default: "" }', workflow)
        self.assertIn('artifact_namespace: { type: string, default: "" }', workflow)
        self.assertIn("inputs.cache_namespace", workflow)
        self.assertIn("inputs.artifact_namespace", workflow)
        self.assertEqual(workflow.count("name: remote-nix-plan-${{ inputs.worker_count }}${{"), 4)
        self.assertEqual(workflow.count("name: remote-nix-evidence-${{ inputs.worker_count }}${{"), 2)
        coordinator = workflow.split("  coordinator:\n", 1)[1].split("  test:\n", 1)[0]
        self.assertIn("restore-prefixes-first-match:", coordinator)
        # The namespace appears in both the primary and restore prefix.
        self.assertGreaterEqual(coordinator.count("inputs.cache_namespace"), 2)
        self.assertIn("github.sha", coordinator)
        self.assertIn("gc-max-store-size-linux: ${{ inputs.cache_max_store_size_linux }}", coordinator)

    def test_runnable_jobs_only_start_after_native_coordinator(self):
        workflow = (Path(__file__).parent.parent /
                    ".github/workflows/flake-nix-builders.yml").read_text()
        test_stage = workflow.split("  test:\n", 1)[1].split("  acceptance:\n", 1)[0]
        acceptance = workflow.split("  acceptance:\n", 1)[1]
        coordinator = workflow.split("  coordinator:\n", 1)[1].split("  test:\n", 1)[0]
        self.assertIn("--test-packages ci-plan/test-packages.json --test-root ci-result/test-package-roots",
                      coordinator)
        self.assertNotIn("flake-test-packages.py build", coordinator)
        self.assertIn("needs: [plan, coordinator]", test_stage)
        self.assertIn("needs.coordinator.result == 'success'", test_stage)
        self.assertIn("fromJSON(needs.plan.outputs.test_matrix)", test_stage)
        self.assertIn("save: false", test_stage)
        self.assertNotIn("TS_AUTHKEY:", test_stage)
        self.assertNotIn("TS_OAUTH_CLIENT_ID:", test_stage)
        self.assertIn("flake-test-packages.py run", test_stage)
        self.assertIn("needs: [plan, worker, coordinator, test]", acceptance)
        self.assertIn("flake-test-packages.py verify", acceptance)
        self.assertIn("test \"$TESTS\" = success", acceptance)
        self.assertIn("test \"$TESTS\" = skipped", acceptance)

    def test_remote_coordinator_has_trusted_cross_run_nix_cache(self):
        """A second full run should actually reuse Nix output paths.

        A fresh GitHub runner has a cold Nix store by default. Re-running
        without restoring a cache is not a warm benchmark.
        """
        workflow = (Path(__file__).parent.parent /
                    ".github/workflows/flake-nix-builders.yml").read_text()
        plan = workflow.split("  plan:\n", 1)[1].split("  worker:\n", 1)[0]
        worker = workflow.split("  worker:\n", 1)[1].split("  coordinator:\n", 1)[0]
        coordinator = workflow.split("  coordinator:\n", 1)[1].split("  test:\n", 1)[0]
        self.assertIn("cache_prefix: ${{ steps.plan.outputs.cache_prefix }}", plan)
        self.assertIn("cache_prefix=", plan)
        self.assertIn("cachePrefix", plan)
        self.assertIn("nix-community/cache-nix-action@7df957e333c1e5da7721f60227dbba6d06080569", coordinator)
        self.assertIn("primary-key: remote-${{ needs.plan.outputs.cache_prefix }}-${{ inputs.cache_namespace && format('{0}-', inputs.cache_namespace) || '' }}${{ github.sha }}", coordinator)
        self.assertIn("restore-prefixes-first-match:", coordinator)
        self.assertIn("remote-${{ needs.plan.outputs.cache_prefix }}-", coordinator)
        self.assertIn("gc-max-store-size-linux: ${{ inputs.cache_max_store_size_linux }}", coordinator)
        self.assertLess(coordinator.index("Restore and save trusted remote Nix store"),
                        coordinator.index("Native Nix scheduler executes all exact-head checks"))
        self.assertNotIn("cache-nix-action", worker)
        self.assertIn("name: Require all builders, runnable tests and exact-head shipping evidence", workflow)


if __name__ == "__main__":
    unittest.main()
