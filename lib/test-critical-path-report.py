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
        self.assertEqual(result["job_idle_gap_seconds"], 7)
        self.assertEqual(result["peak_running_jobs"], 2)

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


    def test_coordinated_workflow_without_legacy_shards(self):
        run, response = sample()
        response["jobs"][1]["name"] = "remote / Nix coordinator"
        response["jobs"][2]["name"] = "remote / Nix builder 1"
        response["jobs"].append({
            "id": 5, "name": "remote / conditional cache publishing",
            "status": "completed", "conclusion": "skipped",
            "started_at": None, "completed_at": None,
        })
        response["total_count"] += 1
        metrics = subject.report(run, response, HEAD)
        self.assertNotIn("tail_shard", metrics)
        self.assertNotIn("shards", metrics)
        self.assertEqual(metrics["skipped_jobs"], ["remote / conditional cache publishing"])
        self.assertEqual(metrics["failed_jobs"], [])
        self.assertEqual(metrics["job_span_seconds"], 280)
        self.assertEqual(metrics["finalization_lag_seconds"], 10)
        self.assertEqual(metrics["runner_minutes"], 5.72)
        self.assertEqual(metrics["jobs"][0]["name"],
                         "candidate / Flake matrix evidence aggregate")
        self.assertEqual(len(metrics["jobs"]), 4)

    def test_failure_diagnostics_do_not_reclassify_failure_as_success(self):
        run, response = sample()
        run["conclusion"] = "failure"
        response["jobs"][2]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "not a successful"):
            subject.report(run, response, HEAD)
        metrics = subject.report(run, response, HEAD, diagnostic=True)
        self.assertEqual(metrics["run_conclusion"], "failure")
        self.assertEqual(metrics["failed_jobs"], ["candidate / Check shard-02"])
        self.assertEqual(metrics["shards"], {"00": 70, "02": 200})
        self.assertEqual(metrics["runner_minutes"], 5.72)
        # Even diagnostic mode rejects incompletely reported jobs.
        response["jobs"][2]["status"] = "in_progress"
        with self.assertRaisesRegex(ValueError, "failed or unfinished"):
            subject.report(run, response, HEAD, diagnostic=True)

    def test_only_skipped_jobs_have_no_measurable_duration(self):
        run, response = sample()
        response["jobs"] = [{
            "id": 99, "name": "not scheduled", "status": "completed",
            "conclusion": "skipped", "started_at": None, "completed_at": None,
        }]
        response["total_count"] = 1
        with self.assertRaisesRegex(ValueError, "no executed"):
            subject.report(run, response, HEAD)

    def test_duplicate_named_shards_fail_instead_of_overwriting(self):
        run, response = sample()
        response["jobs"][2]["name"] = response["jobs"][1]["name"]
        with self.assertRaisesRegex(ValueError, "duplicate named"):
            subject.report(run, response, HEAD)

    def test_invalid_job_name_and_workflow_outcome_rejected(self):
        run, response = sample()
        response["jobs"][0]["name"] = ""
        with self.assertRaisesRegex(ValueError, "missing job name"):
            subject.report(run, response, HEAD)
        response["jobs"][0]["name"] = "writer"
        run["conclusion"] = "cancelled"
        with self.assertRaisesRegex(ValueError, "not a successful"):
            subject.report(run, response, HEAD)
        self.assertEqual(subject.report(run, response, HEAD, diagnostic=True)
                         ["run_conclusion"], "cancelled")


    def test_observed_coordinated_consumer_timing_not_a_matrix(self):
        # Intervals from successful Phenix AI run 43857321453, 2026-11-16.
        # This is a regression example, not a hard-coded workload requirement.
        run = {
            "id": 43857321453, "head_sha": HEAD, "status": "completed",
            "conclusion": "success", "run_started_at": iso(0),
            "updated_at": iso(151),
        }
        names_intervals = [
            ("Plan", 13, 39),
            ("Coordinator", 41, 124),
            ("Builder 1", 41, 111),
            ("Builder 2", 42, 113),
            ("Builder 3", 41, 113),
            ("Builder 4", 42, 113),
            ("Runtime tests", 126, 140),
            ("Shipping gate", 142, 151),
        ]
        response = {
            "total_count": len(names_intervals),
            "jobs": [{
                "id": i, "name": name, "status": "completed",
                "conclusion": "success", "started_at": iso(start),
                "completed_at": iso(end),
            } for i, (name, start, end) in enumerate(names_intervals, 1)],
        }
        metrics = subject.report(run, response, HEAD)
        self.assertEqual(metrics["workflow_wall_seconds"], 151)
        self.assertEqual(metrics["runner_minutes"], 6.93)
        self.assertEqual(metrics["initial_idle_seconds"], 13)
        self.assertEqual(metrics["job_span_seconds"], 138)
        self.assertEqual(metrics["job_idle_gap_seconds"], 6)
        self.assertEqual(metrics["peak_running_jobs"], 5)
        self.assertEqual(metrics["last_finished_job"], "Shipping gate")
        self.assertNotIn("tail_shard", metrics)
        self.assertEqual(len(metrics["jobs"]), 8)


if __name__ == "__main__":
    unittest.main()
