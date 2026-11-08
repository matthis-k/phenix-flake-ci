#!/usr/bin/env python3
"""Offline, read-only duplicate Nix derivation build-event evidence across shards."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

# Match only direct Nix "building" announcements, never quoted last-log
# summaries, cache substitutions, copied paths, or compiler diagnostics.
BUILD = re.compile(
    r"^(?:\d{4}-\d\d-\d\dT[^ ]+Z )?"
    r"building '(/nix/store/[a-z0-9]{32}-[^'\n]+\.drv)'\.\.\.$"
)


def build_paths(log):
    """Extract direct Nix build events from raw-format logs."""
    return [match.group(1) for line in log.splitlines()
            if (match := BUILD.fullmatch(line))]


def analyze(logs, *, prebuild_log=None, allow_zero_builds=False):
    """Count .drv builds across prebuild and shard jobs, preserving shard metrics."""
    if not logs:
        raise ValueError("at least one shard is required")
    if prebuild_log is not None and not isinstance(prebuild_log, str):
        raise ValueError("prebuild log must be text")
    prebuild_counts = Counter(build_paths(prebuild_log or ""))
    owners = defaultdict(set)
    per_path_shards = defaultdict(Counter)
    shard_counts = Counter()
    total = 0
    repeated_within_shard = 0
    by_shard = {}
    for shard, log in sorted(logs.items()):
        if not re.fullmatch(r"[0-9]{2}", shard) or not isinstance(log, str):
            raise ValueError("invalid shard ID or log")
        paths = build_paths(log)
        counts = Counter(paths)
        shard_counts.update(counts)
        for path, count in counts.items():
            owners[path].add(shard)
            per_path_shards[path][shard] = count
        total += len(paths)
        repeated_within_shard += len(paths) - len(counts)
        by_shard[shard] = {"build_events": len(paths), "unique_paths": len(counts)}
    if not total and not allow_zero_builds:
        raise ValueError("no direct Nix build announcements in shard logs")
    cross_shard = sum(1 for shard_ids in owners.values() if len(shard_ids) > 1)
    pipeline_counts = prebuild_counts + shard_counts
    # One-pass counts make duplicate-path diagnostics linear in log size.
    repeated_paths = [
        {"drv": path, "events": count, "prebuild": prebuild_counts[path],
         "shards": dict(sorted(per_path_shards[path].items()))}
        for path, count in sorted(pipeline_counts.items(),
                                  key=lambda item: (-item[1], item[0]))
        if count > 1
    ][:20]
    return {
        "shards": by_shard,
        "build_events": total,
        "distinct_derivation_paths": len(owners),
        "repeated_build_events": total - len(owners),
        "paths_seen_in_multiple_shards": cross_shard,
        "repeated_events_within_a_shard": repeated_within_shard,
        "repeated_events_across_shards": total - len(owners) - repeated_within_shard,
        "prebuild_build_events": sum(prebuild_counts.values()),
        "prebuild_distinct_derivation_paths": len(prebuild_counts),
        "repeated_events_within_prebuild": sum(prebuild_counts.values()) - len(prebuild_counts),
        "paths_rebuilt_after_prebuild": len(set(prebuild_counts) & set(owners)),
        "repeated_events_after_prebuild": sum(
            count for path, count in shard_counts.items() if path in prebuild_counts
        ),
        "pipeline_build_events": sum(pipeline_counts.values()),
        "pipeline_distinct_derivation_paths": len(pipeline_counts),
        "pipeline_repeated_build_events": sum(pipeline_counts.values()) - len(pipeline_counts),
        "top_repeated_derivations": repeated_paths,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shard", action="append", required=True, metavar="NN=JOB_LOG",
        help="One complete successful Nix shard job log, e.g. 02=shard-02.log",
    )
    parser.add_argument(
        "--allow-zero-builds", action="store_true",
        help="Accept zero Nix build announcements only after successful shard evidence verification",
    )
    parser.add_argument("--prebuild-log", metavar="JOB_LOG",
                        help="Shared prebuild log for cross-stage build accounting")
    arguments = parser.parse_args()
    logs = {}
    for item in arguments.shard:
        if "=" not in item:
            parser.error("expected NN=JOB_LOG")
        shard, path = item.split("=", 1)
        if shard in logs:
            parser.error(f"duplicate shard {shard}")
        if not re.fullmatch(r"[0-9]{2}", shard) or not path:
            parser.error("invalid shard or empty log path")
        logs[shard] = Path(path).read_text(encoding="utf-8")
    prebuild_log = (Path(arguments.prebuild_log).read_text(encoding="utf-8")
                    if arguments.prebuild_log else None)
    print(json.dumps(analyze(logs, prebuild_log=prebuild_log,
                             allow_zero_builds=arguments.allow_zero_builds),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
