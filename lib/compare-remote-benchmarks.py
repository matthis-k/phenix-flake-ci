#!/usr/bin/env python3
"""Compare completed, exact-head Nix remote-builder benchmarks."""
import argparse
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
            or not checks or not all(c.get("status") == "pass" for c in checks.values())):
        raise ValueError(f"incomplete Nix check evidence for {count} builders")
    return {"count": count, "wall": seconds, "evidence": evidence}


def report(variants):
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
        f"Commit: \`{evidence['commit']}\`; checks: {len(evidence['checks'])}; "
        f"platform: \`{evidence['system']}\`.",
        "",
        "| Remote workers | Nix build wall time | Speed vs smallest pool |",
        "| ---: | ---: | ---: |",
    ]
    for v in variants:
        output.append(f"| {v['count']} | {v['wall']:.1f} s | "
                      f"{baseline['wall'] / v['wall']:.2f}x |")
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
    args = p.parse_args()
    try:
        variants = []
        for item in args.variant:
            count, sep, directory = item.partition(":")
            if not sep or not count.isdigit() or int(count) < 1 or not directory:
                raise ValueError(f"invalid variant: {item!r}")
            variants.append(load_variant(int(count), directory))
        print(report(variants), end="")
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"remote-benchmark: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
