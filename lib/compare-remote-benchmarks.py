#!/usr/bin/env python3
"""Compare completed, exact-head Nix remote-builder benchmarks."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


def load_variant(count, directory):
    folder = Path(directory)
    timing = json.loads((folder / "timing.json").read_text())
    evidence = json.loads((folder / "evidence.json").read_text())
    seconds = timing.get("wallSeconds")
    if (timing.get("mode") != "remote-nix-ssh-ng"
            or timing.get("workerCount") != count
            or type(seconds) not in (int, float) or not 0 < seconds < 86400):
        raise ValueError(f"invalid elapsed build time or pool size for {count} builders")
    checks = evidence.get("checks")
    if (evidence.get("mode") != "native-nix-dag" or not isinstance(checks, dict)
            or not checks or not all(isinstance(c, dict) and c.get("status") == "pass"
                                      for c in checks.values())):
        raise ValueError(f"incomplete Nix check evidence for {count} builders")
    return {"count": count, "wall": seconds, "evidence": evidence}



def job_metrics(items, counts):
    """Use completed GitHub Actions jobs for true wall and runner-minutes."""
    values = {}
    if not isinstance(items, list):
        raise ValueError("invalid GitHub job listing")
    for count in counts:
        prefix = f"{count} remote builders / "
        relevant = [item for item in items
                    if isinstance(item, dict) and item.get("name", "").startswith(prefix)]
        # Each reusable invocation has a plan, coordinator, acceptance and N workers.
        if len(relevant) != count + 3:
            raise ValueError(f"incomplete GitHub jobs for {count} builders")
        durations = []
        for item in relevant:
            if item.get("status") != "completed" or item.get("conclusion") != "success":
                raise ValueError(f"unfinished or failed GitHub job in pool {count}")
            try:
                start = datetime.fromisoformat(item["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(item["completed_at"].replace("Z", "+00:00"))
            except (KeyError, AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid GitHub job timestamps for {count}") from exc
            if end <= start:
                raise ValueError(f"non-positive GitHub job duration for {count}")
            durations.append((start, end))
        wall = (max(end for _, end in durations) - min(start for start, _ in durations)).total_seconds()
        runner = sum((end - start).total_seconds() for start, end in durations)
        values[count] = (wall / 60, runner / 60)
    return values


def report(variants, jobs=None):
    if len(variants) < 2 or len({v["count"] for v in variants}) != len(variants):
        raise ValueError("provide at least two distinct worker counts")
    variants = sorted(variants, key=lambda v: v["count"])
    baseline = variants[0]
    fields = ("commit", "system", "planDigest", "graphDigest", "checks")
    for variant in variants[1:]:
        for field in fields:
            if variant["evidence"].get(field) != baseline["evidence"].get(field):
                raise ValueError(f"benchmark {variant['count']} has mismatched {field}")
    evidence = baseline["evidence"]
    if not all(evidence.get(key) for key in ("commit", "system", "planDigest", "graphDigest")):
        raise ValueError("incomplete exact-head evidence metadata")
    output = [
        "## Distributed Nix builders: exact-head comparison",
        "",
        f"Commit: `{evidence['commit']}`; checks: {len(evidence['checks'])}; "
        f"platform: `{evidence['system']}`.",
        "",
        "| Remote workers | Nix build wall time | Speed vs smallest pool |",
        "| ---: | ---: | ---: |",
    ]
    for v in variants:
        output.append(f"| {v['count']} | {v['wall']:.1f} s | "
                      f"{baseline['wall'] / v['wall']:.2f}x |")
    if jobs is not None:
        metrics = job_metrics(jobs, [v["count"] for v in variants])
        output += [
            "",
            "| Remote workers | Full GitHub stage wall time | GitHub runner-minutes |",
            "| ---: | ---: | ---: |",
        ]
        for v in variants:
            wall, runner = metrics[v["count"]]
            output.append(f"| {v['count']} | {wall:.1f} min | {runner:.1f} min |")
    output += [
        "",
        "These times cover the Nix build command only. They exclude runner provisioning, "
        "tailnet discovery, artifact transfer and GitHub Actions queue time.",
        "A successful benchmark still needs workflow-level wall time and runner-minutes "
        "comparison before selecting the default pool size.",
    ]
    return "\n".join(output) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--variant", action="append", required=True, metavar="COUNT:DIR")
    p.add_argument("--jobs-json", help="Completed GitHub Actions run jobs JSON")
    args = p.parse_args()
    try:
        variants = []
        for item in args.variant:
            count, sep, directory = item.partition(":")
            if not sep or not count.isdigit() or int(count) < 1 or not directory:
                raise ValueError(f"invalid variant: {item!r}")
            variants.append(load_variant(int(count), directory))
        jobs = None
        if args.jobs_json:
            payload = json.loads(Path(args.jobs_json).read_text())
            if payload.get("total_count") != len(payload.get("jobs", [])):
                raise ValueError("incomplete paginated GitHub job inventory")
            jobs = payload["jobs"]
        print(report(variants, jobs=jobs), end="")
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"remote-benchmark: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
