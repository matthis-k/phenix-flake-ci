#!/usr/bin/env python3
"""Offline GitHub Actions critical-path evidence; no network or cache mutations."""
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


def report(run, response, expected_head):
    if not isinstance(expected_head, str) or len(expected_head) != 40:
        raise ValueError("expected exact 40-byte hex commit id")
    if any(char not in "0123456789abcdef" for char in expected_head):
        raise ValueError("invalid expected head")
    if run.get("head_sha") != expected_head:
        raise ValueError("run belongs to a different commit")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("run is not a successful completed workflow")
    if not isinstance(response, dict) or not isinstance(response.get("jobs"), list):
        raise ValueError("missing jobs payload")
    if response.get("total_count") != len(response["jobs"]):
        raise ValueError("incomplete paginated jobs payload")
    jobs = response["jobs"]
    if not jobs:
        raise ValueError("empty jobs payload")

    seen = set()
    metrics = []
    for job in jobs:
        if job.get("id") in seen or job.get("id") is None:
            raise ValueError("duplicate or missing job id")
        seen.add(job["id"])
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise ValueError("failed or unfinished job")
        start = timestamp(job.get("started_at"))
        end = timestamp(job.get("completed_at"))
        duration = (end - start).total_seconds()
        if duration < 0:
            raise ValueError("negative job duration")
        metrics.append({"name": job["name"], "seconds": duration,
                        "started_at": start, "completed_at": end})

    started = timestamp(run.get("run_started_at"))
    completed = timestamp(run.get("updated_at"))
    wall = (completed - started).total_seconds()
    if wall <= 0 or min(j["started_at"] for j in metrics) < started:
        raise ValueError("invalid workflow timing")
    if max(j["completed_at"] for j in metrics) > completed:
        raise ValueError("jobs end after workflow update")
    # These are observed spans, not a reconstructed dependency DAG. No
    # claim that the slowest job alone explains queueing, Nix build or I/O.
    slowest = max(metrics, key=lambda item: item["completed_at"])
    shard_jobs = [j for j in metrics if "Check shard-" in j["name"]]
    if not shard_jobs:
        raise ValueError("no Nix check shards in this run")
    tail_shard = max(shard_jobs, key=lambda item: item["completed_at"])
    return {
        "head": expected_head, "run_id": run["id"],
        "workflow_wall_seconds": wall,
        "runner_minutes": round(sum(item["seconds"] for item in metrics) / 60, 2),
        "last_finished_job": slowest["name"],
        "last_finished_seconds": slowest["seconds"],
        "tail_shard": tail_shard["name"].split("Check shard-")[-1],
        "shards": {j["name"].split("Check shard-")[-1]: j["seconds"]
                   for j in sorted(shard_jobs, key=lambda item: item["name"])},
        "initial_idle_seconds": (min(j["started_at"] for j in metrics) - started).total_seconds(),
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--run", required=True, type=Path)
    cli.add_argument("--jobs", required=True, type=Path)
    cli.add_argument("--head", required=True)
    args = cli.parse_args()
    print(json.dumps(report(json.loads(args.run.read_text()),
                            json.loads(args.jobs.read_text()), args.head),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
