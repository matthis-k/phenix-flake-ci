#!/usr/bin/env python3
"""Offline, read-only duplicate Nix derivation build-event evidence across shards."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

# Match only direct Nix "building" announcements, never quoted last-log
# summaries, cache substitutions, copied paths, or compiler diagnostics.
BUILD = re.compile(
    r"^(?:\d{4}-\d\d-\d\dT[^ ]+Z )?"
    r"building '(/nix/store/[a-z0-9]{32}-[^'\n]+\.drv)'\.\.\.$"
)


def analyze(logs, *, allow_zero_builds=False):
    """Count direct Nix build events from successful shard logs by derivation path.

    When the caller has already verified successful exact-head shard evidence,
    an all-cache-hit build with no local derivation builds is a valid result.
    """
    if not logs:
        raise ValueError("at least one shard is required")
    owners = defaultdict(set)
    total = 0
    repeated_within_shard = 0
    by_shard = {}
    for shard, log in sorted(logs.items()):
        if not re.fullmatch(r"[0-9]{2}", shard) or not isinstance(log, str):
            raise ValueError("invalid shard ID or log")
        paths = [match.group(1) for line in log.splitlines()
                 if (match := BUILD.fullmatch(line))]
        unique = set(paths)
        for path in unique:
            owners[path].add(shard)
        total += len(paths)
        repeated_within_shard += len(paths) - len(unique)
        by_shard[shard] = {"build_events": len(paths), "unique_paths": len(unique)}
    if not total and not allow_zero_builds:
        raise ValueError("no direct Nix build announcements in shard logs")
    cross_shard = sum(1 for shard_ids in owners.values() if len(shard_ids) > 1)
    # The event count includes any repeated local attempts within one shard.
    return {
        "shards": by_shard,
        "build_events": total,
        "distinct_derivation_paths": len(owners),
        "repeated_build_events": total - len(owners),
        "paths_seen_in_multiple_shards": cross_shard,
        "repeated_events_within_a_shard": repeated_within_shard,
        "repeated_events_across_shards": total - len(owners) - repeated_within_shard,
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
    print(json.dumps(analyze(logs, allow_zero_builds=arguments.allow_zero_builds),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
