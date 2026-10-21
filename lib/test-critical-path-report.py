#!/usr/bin/env python3
"""Deterministic offline tests for GitHub Actions critical-path evidence."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest


path = Path(__file__).with_name("critical-path-report.py")
spec = importlib.util.spec_from_file_location("critical_path_report", path)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

HEAD = "a" * 40
BASE = datetime(2026, 10, 21, tzinfo=timezone.utc)


def iso(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def sample():
    run = {
        "id": 17, "head_sha": HEAD, "status": "completed",
        "conclusion": "success", "run_started_at": iso(0),
        "updated_at": iso(300),
    }
    jobs = [
        {"id": 1, "name": "candidate / Shared Nix build and cache writer",
         "status": "completed", "conclusion": "success",
         "started_at": iso(10), "completed_at": iso(70)},
        {"id": 2, "name": "candidate / Check shard-00",
         "status": "completed", "conclusion": "success",
         "started_at": iso(75), "completed_at": iso(145)},
        {"id": 3, "name": "candidate / Check shard-02",
         "status": "completed", "conclusion": "success",
         "started_at": iso(75), "completed_at": iso(275)},
        {"id": 4, "name": "candidate / Flake matrix evidence aggregate",
         "status": "completed", "conclusion": "success",
         "started_at": iso(277), "completed_at": iso(290)},
    ]
    return run, {"total_count": len(jobs), "jobs": jobs}


class CriticalPathReportTests(unittest.TestCase):
    def test_observed_wall_runner_minutes_and_shard_tail(self):
        run, jobs = sample()
        result = subject.report(run, jobs, HEAD)
        self.assertEqual(result["workflow_wall_seconds"], 300)
        self.assertEqual(result["runner_minutes"], 5.72)
        self.assertEqual(result["tail_shard"], "02")
        self.assertEqual(result["shards"], {"00": 70, "02": 200})
        self.assertEqual(result["last_finished_job"],
                         "candidate / Flake matrix evidence aggregate")
        self.assertEqual(result["initial_idle_seconds"], 10)

    def test_head_mismatch_and_incomplete_pages_fail_closed(self):
        run, jobs = sample()
        with self.assertRaisesRegex(ValueError, "different commit"):
            subject.report(run, jobs, "b" * 40)
        jobs["total_count"] += 1
        with self.assertRaisesRegex(ValueError, "incomplete"):
            subject.report(run, jobs, HEAD)

    def test_failed_missing_or_invalid_intervals_reject(self):
        run, jobs = sample()
        jobs["jobs"][1]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "failed or unfinished"):
            subject.report(run, jobs, HEAD)
        jobs["jobs"][1]["conclusion"] = "success"
        jobs["jobs"][1]["completed_at"] = iso(70)
        with self.assertRaisesRegex(ValueError, "negative"):
            subject.report(run, jobs, HEAD)
        jobs["jobs"][1]["completed_at"] = iso(145)
        jobs["jobs"][0]["started_at"] = "2026-10-21T12:34:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            subject.report(run, jobs, HEAD)

    def test_duplicate_jobs_reject(self):
        run, jobs = sample()
        jobs["jobs"][1]["id"] = 1
        with self.assertRaisesRegex(ValueError, "duplicate"):
            subject.report(run, jobs, HEAD)


if __name__ == "__main__":
    unittest.main()
