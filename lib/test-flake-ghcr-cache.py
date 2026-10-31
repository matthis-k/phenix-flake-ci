#!/usr/bin/env python3
"""Offline safety tests for the optional GHCR-backed Nix cache policy."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("flake_ghcr_cache", HERE / "flake-ghcr-cache.py")
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class GHCRCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = Path(self.temp.name) / "plan.json"
        self.plan.write_text(json.dumps({
            "commit": "a" * 40, "system": "x86_64-linux", "digest": "b" * 64,
        }))
        self.env = mock.patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "matthis-k/phenix-ai",
            "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "2",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_identity_only_same_repository_image(self):
        self.assertEqual(
            cache.identity("Matthis-K/Phenix-AI", "x86_64-linux"),
            ("ghcr.io/matthis-k/phenix-ai-nix-cache", "shared-x86_64-linux-v1"))
        for invalid in ("", "../outside", "owner/repo/other", "owner/repo:evil"):
            with self.assertRaises(ValueError):
                cache.identity(invalid, "x86_64-linux")

    def test_restore_missing_tag_is_soft_cache_miss(self):
        args = type("A", (), {"plan": str(self.plan)})()
        completed = mock.Mock(returncode=1, stderr="manifest unknown", stdout="")
        with mock.patch.object(cache, "login"), mock.patch.object(
            cache.subprocess, "run", return_value=completed
        ) as call:
            cache.restore(args)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0][:2], ["docker", "pull"])

    def test_restore_auth_denied_is_not_misrepresented_as_cache_miss(self):
        args = type("A", (), {"plan": str(self.plan)})()
        completed = mock.Mock(returncode=1, stderr="unauthorized: access denied", stdout="")
        with mock.patch.object(cache, "login"), mock.patch.object(
            cache.subprocess, "run", return_value=completed
        ):
            with self.assertRaisesRegex(RuntimeError, "pull failed"):
                cache.restore(args)

    def test_promotion_requires_matching_run_head_and_digest(self):
        meta = {
            "format": cache.FORMAT, "repository": "matthis-k/phenix-ai",
            "image": "ghcr.io/matthis-k/phenix-ai-nix-cache",
            "stable": "shared-x86_64-linux-v1",
            "system": "x86_64-linux", "commit": "a" * 40,
            "plan_digest": "b" * 64, "run_id": "12345", "attempt": "2",
            "candidate": "candidate-12345-2", "digest": "sha256:" + "c" * 64,
        }
        path = Path(self.temp.name) / "metadata.json"
        args = type("A", (), {"plan": str(self.plan), "metadata": str(path)})()
        with mock.patch.object(cache, "login") as login, mock.patch.object(cache, "run") as run:
            for invalid in ("commit", "plan_digest", "run_id", "candidate", "digest"):
                wrong = dict(meta)
                wrong[invalid] = "wrong"
                path.write_text(json.dumps(wrong))
                with self.assertRaises(ValueError):
                    cache.promote(args)
            self.assertEqual(login.call_count, 0)
            path.write_text(json.dumps(meta))
            cache.promote(args)
            login.assert_called_once()
            run.assert_called_once_with(
                "docker", "buildx", "imagetools", "create", "--tag",
                "ghcr.io/matthis-k/phenix-ai-nix-cache:shared-x86_64-linux-v1",
                "ghcr.io/matthis-k/phenix-ai-nix-cache@sha256:" + "c" * 64)

    def test_workflow_only_promotes_after_all_runnable_tests(self):
        source = (HERE.parent / ".github/workflows/flake-nix-builders.yml").read_text()
        acceptance = source.split("  acceptance:", 1)[1]
        self.assertLess(acceptance.index("Require every matrix worker and full coordinator success"),
                        acceptance.index("Independently verify all check outputs"))
        self.assertLess(acceptance.index("Require every discovered executable test package to pass"),
                        acceptance.index("Promote GHCR cache only after all check and runnable-test gates"))
        self.assertIn("ghcr_cache: { type: boolean, default: false }", source)
        self.assertIn("if: ${{ inputs.ghcr_cache }}", source)


if __name__ == "__main__":
    unittest.main()
