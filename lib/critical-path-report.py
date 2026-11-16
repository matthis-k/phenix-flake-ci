#!/usr/bin/env python3
"""Offline, workload-agnostic GitHub Actions timing evidence.

This is an observed execution timeline, *not* a reconstructed dependency
critical path. It works for matrix, coordinated remote, and local Nix DAG
workflows without assuming any job names, language, or worker count.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path


def timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("missing timestamp")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid timestamp") from error
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timestamp must have timezone")
    return instant


def report(run, response, expected_head, *, diagnostic=False):
    if (not isinstance(expected_head, str) or len(expected_head) != 40
            or any(char not in "0123456789abcdef" for char in expected_head)):
        raise ValueError("expected exact 40-byte hex commit id")
    if not isinstance(run, dict) or run.get("head_sha") != expected_head:
        raise ValueError("run belongs to a different commit")
    if run.get("status") != "completed":
        raise ValueError("run is not completed")
    failed_outcomes = {"failure", "timed_out", "cancelled"}
    allowed = {"success"} | (failed_outcomes if diagnostic else set())
    if run.get("conclusion") not in allowed:
        raise ValueError("run is not a successful completed workflow")
    if not isinstance(response, dict) or not isinstance(response.get("jobs"), list):
        raise ValueError("missing jobs payload")
    if response.get("total_count") != len(response["jobs"]):
        raise ValueError("incomplete paginated jobs payload")
    if not response["jobs"]:
        raise ValueError("empty jobs payload")

    seen = set()
    metrics = []
    skipped = []
    failed = []
    for job in response["jobs"]:
        if not isinstance(job, dict) or job.get("id") in seen or job.get("id") is None:
            raise ValueError("duplicate or missing job id")
        seen.add(job["id"])
        name = job.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("missing job name")
        if job.get("status") != "completed":
            raise ValueError("failed or unfinished job")
        outcome = job.get("conclusion")
        if outcome == "skipped":
            # GitHub can omit timestamps for a skipped job. Skips are visible
            # but never counted as executed work or accepted by this tool
            # as proof of CI/check coverage.
            skipped.append(name)
            continue
        if outcome != "success":
            if not diagnostic or outcome not in failed_outcomes:
                raise ValueError("failed or unfinished job")
            failed.append(name)
        start = timestamp(job.get("started_at"))
        end = timestamp(job.get("completed_at"))
        duration = (end - start).total_seconds()
        if duration < 0:
            raise ValueError("negative job duration")
        metrics.append({
            "name": name, "id": job["id"], "conclusion": outcome,
            "seconds": duration, "started_at": start, "completed_at": end,
        })

    if not metrics:
        raise ValueError("no executed jobs with measurable intervals")
    started = timestamp(run.get("run_started_at"))
    completed = timestamp(run.get("updated_at"))
    wall = (completed - started).total_seconds()
    if wall <= 0 or min(j["started_at"] for j in metrics) < started:
        raise ValueError("invalid workflow timing")
    if max(j["completed_at"] for j in metrics) > completed:
        raise ValueError("jobs end after workflow update")

    last = max(metrics, key=lambda item: item["completed_at"])
    first = min(metrics, key=lambda item: item["started_at"])
    by_name = sorted(metrics, key=lambda item: (item["name"], item["id"]))
    output = {
        "head": expected_head,
        "run_id": run.get("id"),
        "run_conclusion": run["conclusion"],
        "workflow_wall_seconds": wall,
        "runner_minutes": round(sum(item["seconds"] for item in metrics) / 60, 2),
        "job_span_seconds": (last["completed_at"] - first["started_at"]).total_seconds(),
        "initial_idle_seconds": (first["started_at"] - started).total_seconds(),
        "finalization_lag_seconds": (completed - last["completed_at"]).total_seconds(),
        "last_finished_job": last["name"],
        "last_finished_seconds": last["seconds"],
        "skipped_jobs": sorted(skipped),
        "failed_jobs": sorted(failed),
        "jobs": [{
            "name": item["name"], "id": item["id"], "conclusion": item["conclusion"],
            "seconds": item["seconds"],
            "start_offset_seconds": (item["started_at"] - started).total_seconds(),
            "end_offset_seconds": (item["completed_at"] - started).total_seconds(),
        } for item in by_name],
    }
    # Preserve the legacy shard-specific fields only when that workflow
    # actually has named shards. No fabricated shards for other consumers.
    shard_jobs = [item for item in metrics if "Check shard-" in item["name"]]
    if shard_jobs:
        shards = {}
        for item in shard_jobs:
            shard = item["name"].split("Check shard-")[-1]
            if shard in shards:
                raise ValueError("duplicate named check shard")
            shards[shard] = item["seconds"]
        tail = max(shard_jobs, key=lambda item: item["completed_at"])
        output["tail_shard"] = tail["name"].split("Check shard-")[-1]
        output["shards"] = dict(sorted(shards.items()))
    return output


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--run", required=True, type=Path)
    cli.add_argument("--jobs", required=True, type=Path)
    cli.add_argument("--head", required=True)
    cli.add_argument(
        "--diagnostic", action="store_true",
        help="Measure failed/cancelled runs too; never treat them as passing evidence",
    )
    args = cli.parse_args()
    print(json.dumps(report(json.loads(args.run.read_text()),
                            json.loads(args.jobs.read_text()), args.head,
                            diagnostic=args.diagnostic),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
