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
            "00": "\n".join([build(A), build(B)]),
            "01": "\n".join([build(A), build(C)]),
            "02": "\n".join([build(A), build(B), build(C)]),
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
        result = subject.analyze({"00": build(A) + "\n" + build(A), "01": build(B)})
        self.assertEqual(result["repeated_events_within_a_shard"], 1)
        self.assertEqual(result["repeated_events_across_shards"], 0)

    def test_excludes_substitutions_and_repeated_error_summaries(self):
        log = "\n".join([
            f"2026-10-21T11:32:12.123Z copying path '{A}' from cache...",
            f"2026-10-21T11:32:12.123Z        > building '{B}'...",
            "building 'relative-no-store-path.drv'...",
            build(C),
        ])
        result = subject.analyze({"00": log})
        self.assertEqual(result["build_events"], 1)
        self.assertEqual(result["distinct_derivation_paths"], 1)

    def test_verified_all_cache_hit_shards_have_zero_duplicate_builds(self):
        logs = {
            "00": "copying path '/nix/store/abc' from cache...",
            "01": "",
        }
        result = subject.analyze(logs, allow_zero_builds=True)
        self.assertEqual(result["build_events"], 0)
        self.assertEqual(result["distinct_derivation_paths"], 0)
        self.assertEqual(result["repeated_build_events"], 0)
        self.assertEqual(result["paths_seen_in_multiple_shards"], 0)
        self.assertEqual(result["shards"]["00"]["build_events"], 0)
        self.assertEqual(result["shards"]["01"]["build_events"], 0)

    def test_prebuild_duplicates_are_included_in_pipeline(self):
        logs = {"00": "\n".join([build(A), build(A), build(B)]),
                "01": "\n".join([build(A), build(C)])}
        prebuild = "\n".join([build(A), build(C), build(C)])
        result = subject.analyze(logs, prebuild_log=prebuild)
        self.assertEqual(result["build_events"], 5)
        self.assertEqual(result["prebuild_build_events"], 3)
        self.assertEqual(result["repeated_events_within_prebuild"], 1)
        self.assertEqual(result["paths_rebuilt_after_prebuild"], 2)
        self.assertEqual(result["repeated_events_after_prebuild"], 4)
        self.assertEqual(result["pipeline_build_events"], 8)
        self.assertEqual(result["pipeline_distinct_derivation_paths"], 3)
        self.assertEqual(result["pipeline_repeated_build_events"], 5)
        self.assertEqual(result["top_repeated_derivations"][0],
                         {"drv": A, "events": 4, "prebuild": 1,
                          "shards": {"00": 2, "01": 1}})

    def test_prebuild_only_and_invalid_log(self):
        result = subject.analyze({"00": "", "01": ""}, prebuild_log=build(B),
                                 allow_zero_builds=True)
        self.assertEqual(result["pipeline_build_events"], 1)
        self.assertEqual(result["pipeline_repeated_build_events"], 0)
        with self.assertRaisesRegex(ValueError, "prebuild log must be text"):
            subject.analyze({"00": build(A)}, prebuild_log=b"bytes")

    def test_malformed_or_empty_evidence_is_not_interpreted_as_zero_builds(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            subject.analyze({})
        with self.assertRaisesRegex(ValueError, "invalid shard"):
            subject.analyze({"alpha": build(A)})
        with self.assertRaisesRegex(ValueError, "no direct"):
            subject.analyze({"00": "Nix substituted everything from an unknown cache"})


if __name__ == "__main__":
    unittest.main()
