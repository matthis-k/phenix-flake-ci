#!/usr/bin/env python3
"""Tests for full-parity remote worker benchmark reporting."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


source = Path(__file__).with_name("compare-remote-benchmarks.py")
spec = importlib.util.spec_from_file_location("compare_remote_benchmarks", source)
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)


def evidence(commit="abc", checks=None):
    return {
        "mode": "native-nix-dag", "commit": commit,
        "system": "x86_64-linux", "planDigest": "plan",
        "graphDigest": "dag", "checks": checks or {
            "shipping": {"status": "pass", "drv": "/nix/store/example.drv"}
        }
    }


class CompareRemoteBenchmarksTest(unittest.TestCase):
    def write_variant(self, root, count, seconds, item):
        folder = Path(root, str(count))
        folder.mkdir()
        (folder / "timing.json").write_text(json.dumps({
            "mode": "remote-nix-ssh-ng", "workerCount": count, "wallSeconds": seconds
        }))
        (folder / "evidence.json").write_text(json.dumps(item))
        return compare.load_variant(count, folder)

    def test_same_head_complete_check_inventory_and_relative_speedup(self):
        with tempfile.TemporaryDirectory() as root:
            variants = [self.write_variant(root, count, wall, evidence())
                        for count, wall in [(2, 120.0), (4, 80.0), (8, 70.0)]]
            text = compare.report(variants)
            self.assertIn("| 2 | 120.0 s | 1.00x |", text)
            self.assertIn("| 4 | 80.0 s | 1.50x |", text)
            self.assertIn("| 8 | 70.0 s | 1.71x |", text)
            self.assertIn("exclude runner provisioning", text)

    def test_mismatched_commit_or_check_set_fails(self):
        with tempfile.TemporaryDirectory() as root:
            a = self.write_variant(root, 2, 100, evidence())
            b = self.write_variant(root, 4, 50, evidence(commit="other"))
            with self.assertRaisesRegex(ValueError, "mismatched commit"):
                compare.report([a, b])
            b["evidence"] = evidence(checks={
                "changed": {"status": "pass", "drv": "/nix/store/other.drv"}
            })
            with self.assertRaisesRegex(ValueError, "mismatched checks"):
                compare.report([a, b])

    def test_actual_github_job_time_and_runner_minutes(self):
        from datetime import datetime, timedelta, timezone

        origin = datetime(2026, 10, 13, tzinfo=timezone.utc)
        jobs = []
        for count in (2, 4):
            for index in range(count + 3):
                start = origin + timedelta(minutes=index)
                end = start + timedelta(minutes=2)
                jobs.append({
                    "name": f"{count} remote builders / job-{index}",
                    "status": "completed", "conclusion": "success",
                    "started_at": start.isoformat(),
                    "completed_at": end.isoformat(),
                })
        metrics = compare.job_metrics(jobs, [2, 4])
        self.assertEqual(metrics[2], (6, 10))
        self.assertEqual(metrics[4], (8, 14))
        with tempfile.TemporaryDirectory() as root:
            variants = [self.write_variant(root, count, 100, evidence())
                        for count in (2, 4)]
            output = compare.report(variants, jobs=jobs)
            self.assertIn("| 2 | 6.0 min | 10.0 min |", output)
            self.assertIn("| 4 | 8.0 min | 14.0 min |", output)
        jobs[-1]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "failed GitHub job"):
            compare.job_metrics(jobs, [4])

    def test_missing_failed_and_invalid_timing_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            broken = evidence()
            broken["checks"]["shipping"]["status"] = "fail"
            with self.assertRaisesRegex(ValueError, "incomplete"):
                self.write_variant(root, 2, 100, broken)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "invalid elapsed"):
                self.write_variant(root, 2, 0, evidence())
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "invalid elapsed"):
                compare.load_variant(4, self.variant_dir(root))

    @staticmethod
    def variant_dir(root):
        folder = Path(root)
        (folder / "timing.json").write_text(json.dumps({
            "mode": "remote-nix-ssh-ng", "workerCount": 2, "wallSeconds": 100
        }))
        (folder / "evidence.json").write_text(json.dumps(evidence()))
        return folder


if __name__ == "__main__":
    unittest.main()
