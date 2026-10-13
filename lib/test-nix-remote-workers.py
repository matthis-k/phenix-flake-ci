#!/usr/bin/env python3
"""Offline contract checks for ephemeral remote-builder naming and scheduling."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

source = Path(__file__).with_name("nix-remote-workers.py")
spec = importlib.util.spec_from_file_location("remote_workers", source)
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)


class RemoteBuildersTest(unittest.TestCase):
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
            sshconf.assert_called_once()
            lines = machines.read_text().splitlines()
            self.assertEqual(lines, [
                "ssh-ng://runner@100.64.0.1 x86_64-linux - 3 1 - -",
                "ssh-ng://runner@100.64.0.2 x86_64-linux - 3 1 - -",
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


if __name__ == "__main__":
    unittest.main()
