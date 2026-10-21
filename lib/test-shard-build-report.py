#!/usr/bin/env python3
"""Pure regression tests for exact Nix build-announcement de-duplication."""
import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).with_name("shard-build-report.py")
spec = importlib.util.spec_from_file_location("shard_build_report", path)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

A = "/nix/store/" + "a" * 32 + "-phenix-provider-contracts.drv"
B = "/nix/store/" + "b" * 32 + "-phenix-basic-agent.drv"
C = "/nix/store/" + "c" * 32 + "-phenix-basic-composition.json.drv"


def build(path, ts="2026-10-21T11:32:12.1234567Z"):
    return f"{ts} building '{path}'..."


class ShardBuildReportTests(unittest.TestCase):
    def test_exact_path_identity_not_display_name(self):
        logs = {
            "00": "\\n".join([build(A), build(B)]),
            "01": "\\n".join([build(A), build(C)]),
            "02": "\\n".join([build(A), build(B), build(C)]),
        }
        result = subject.analyze(logs)
        self.assertEqual(result["build_events"], 7)
        self.assertEqual(result["distinct_derivation_paths"], 3)
        self.assertEqual(result["repeated_build_events"], 4)
        self.assertEqual(result["paths_seen_in_multiple_shards"], 3)
        self.assertEqual(result["repeated_events_within_a_shard"], 0)
        self.assertEqual(result["repeated_events_across_shards"], 4)
        self.assertEqual(result["shards"]["01"]["build_events"], 2)

    def test_same_name_different_drv_remains_distinct(self):
        other = "/nix/store/" + "d" * 32 + "-phenix-provider-contracts.drv"
        result = subject.analyze({"00": build(A), "01": build(other)})
        self.assertEqual(result["distinct_derivation_paths"], 2)
        self.assertEqual(result["repeated_build_events"], 0)

    def test_repeat_attempt_not_confused_with_cross_shard_duplication(self):
        result = subject.analyze({"00": build(A) + "\\n" + build(A), "01": build(B)})
        self.assertEqual(result["repeated_events_within_a_shard"], 1)
        self.assertEqual(result["repeated_events_across_shards"], 0)

    def test_excludes_substitutions_and_repeated_error_summaries(self):
        log = "\\n".join([
            f"2026-10-21T11:32:12.123Z copying path '{A}' from cache...",
            f"2026-10-21T11:32:12.123Z        > building '{B}'...",
            "building 'relative-no-store-path.drv'...",
            build(C),
        ])
        result = subject.analyze({"00": log})
        self.assertEqual(result["build_events"], 1)
        self.assertEqual(result["distinct_derivation_paths"], 1)

    def test_malformed_or_empty_evidence_is_not_interpreted_as_zero_builds(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            subject.analyze({})
        with self.assertRaisesRegex(ValueError, "invalid shard"):
            subject.analyze({"alpha": build(A)})
        with self.assertRaisesRegex(ValueError, "no direct"):
            subject.analyze({"00": "Nix substituted everything from an unknown cache"})


if __name__ == "__main__":
    unittest.main()
