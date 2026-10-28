#!/usr/bin/env python3
"""Pure DAG proof for four build workers preceding any check execution."""
import importlib.util
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

module = Path(__file__).with_name("flake-build-test.py")
spec = importlib.util.spec_from_file_location("four_builders_test", module)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)

D = "/nix/store/" + "a" * 32
def drv(n): return D + f"-{n}.drv"

class FourBuildersTests(unittest.TestCase):
    def sample(self):
        check = {"unit": drv("test-unit"), "shipping": drv("test-ship")}
        config = {"schemaVersion": 1, "system": "x86_64-linux", "shardCount": 2,
                  "prebuildTargets": [], "cachePrefix": "a", "configDigest": "b"}
        plan = {"schemaVersion": 1, "system": "x86_64-linux",
                "commit": "a"*40, "checks": check,
                "shards": [{"id":"shard-01","checks":["unit"]},
                           {"id":"shard-02","checks":["shipping"]}],
                "prebuildTargets": [], "cachePrefix": "a",
                "configDigest": "b"}
        plan["digest"] = subject.planner.digest(plan)
        graph = {"schemaVersion": 1, "planDigest": plan["digest"],
                 "commit": plan["commit"], "system": plan["system"],
                 "graphDigest": "g", "checkDependencies": {"unit": [], "shipping": ["unit"]},
                 "derivationDependencies": {
                     drv("shared"): [],
                     drv("one"): [drv("shared")],
                     drv("two"): [drv("shared")],
                     drv("test-unit"): [drv("one"), drv("two")],
                     drv("test-ship"): [drv("test-unit")],
                 }}
        return plan, graph

    def test_four_build_owners_before_any_check(self):
        p,g = self.sample()
        a = subject.build_plan(p,g)
        self.assertEqual(len(a["workers"]),4)
        self.assertEqual(set(a["roots"]),{drv("one"),drv("two")})
        self.assertEqual(set(sum((w["roots"] for w in a["workers"]),[])),set(a["roots"]))
        self.assertTrue(all(x not in p["checks"].values() for x in a["roots"]))
        self.assertEqual(a,subject.build_plan(p,g))
        self.assertEqual(a["estimatedUniqueDrvs"],3)
        self.assertEqual(a["commonRoots"],[drv("shared")])
        self.assertEqual(a["estimatedSharedDrvs"],1)
        self.assertEqual(a["estimatedOverlapAfterCommon"],0)

    def test_no_compilation_depends_on_checks(self):
        p,g=self.sample()
        # A non-check wrapper can depend on another check; it belongs to
        # the test phase, not the pure build frontier.
        g["derivationDependencies"][drv("wrapper")] = [drv("test-unit"),drv("two")]
        g["derivationDependencies"][drv("test-ship")] = [drv("wrapper")]
        self.assertEqual(set(subject.build_plan(p,g)["roots"]), {drv("one"),drv("two")})

    def test_bad_graph_fails_closed(self):
        p,g=self.sample()
        g["derivationDependencies"][drv("test-unit")] = [drv("missing")]
        with self.assertRaisesRegex(ValueError,"incomplete"):
            subject.build_plan(p,g)
        p,g=self.sample()
        g["derivationDependencies"][drv("test-unit")] = []
        g["derivationDependencies"][drv("test-ship")] = [drv("test-unit")]
        with self.assertRaisesRegex(ValueError,"no independent compilation"):
            subject.build_plan(p,g)

    def test_shared_frontier_evidence_is_exact_head_and_complete(self):
        p,g=self.sample()
        b=subject.build_plan(p,g)
        common={"schemaVersion":1,"role":"common","commit":p["commit"],
                "planDigest":p["digest"],"graphDigest":g["graphDigest"],
                "buildDigest":b["digest"],
                "outputs":{drv("shared"):["/nix/store/"+"b"*32+"-common"]}}
        import json
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path=Path(directory)/"common.json"
            path.write_text(json.dumps(common))
            self.assertEqual(subject.common_outputs(p,g,b,path),common["outputs"][drv("shared")])
            for forged in ({**common,"outputs":{}},
                           {**common,"buildDigest":"stale"},
                           {**common,"outputs":{drv("shared"):["nix-store-not-absolute"]}}):
                path.write_text(json.dumps(forged))
                with self.assertRaisesRegex(ValueError,"missing|stale|invalid"):
                    subject.common_outputs(p,g,b,path)

    def test_isolated_worker_reinstantiates_only_exact_head_derivations(self):
        p, _ = self.sample()
        from unittest.mock import patch
        with patch.object(subject.planner, "inventory", return_value=p["checks"]) as mock:
            subject.materialize_exact_head_derivations(p)
            mock.assert_called_once_with(".", p["system"])
        with patch.object(subject.planner, "inventory",
                          return_value={**p["checks"], "extra": drv("extra")}):
            with self.assertRaisesRegex(ValueError, "unexpected"):
                subject.materialize_exact_head_derivations(p)
        with patch.object(subject.planner, "inventory",
                          return_value={**p["checks"], "unit": drv("changed")}):
            with self.assertRaisesRegex(ValueError, "changed"):
                subject.materialize_exact_head_derivations(p)
        with patch.object(subject.planner, "inventory",
                          return_value={"shipping": p["checks"]["shipping"]}):
            with self.assertRaisesRegex(ValueError, "missing"):
                subject.materialize_exact_head_derivations(p)

    def test_join_execution_rejects_invalid_handoffs(self):
        p, g = self.sample()
        b = subject.build_plan(p, g)
        for case, message in (("missing", "four builder"),
                              ("repeated", "repeated builder"),
                              ("wrong-owner", "derivation evidence"),
                              ("stale", "stale"),
                              ("missing-archive", "missing build archive"),
                              ("empty-archive", "empty archive"),
                              ("corrupt", None),
                              ("valid", None)):
            with self.subTest(case=case), TemporaryDirectory() as directory:
                root = Path(directory)
                args = SimpleNamespace(plan="plan", graph="graph", build_plan="build",
                                       builders=root, common="common", prebuild="prebuild",
                                       archive=root / "joined.nar", output=root / "joined.json")
                for i, worker in enumerate(b["workers"]):
                    if case == "missing" and i == 3:
                        continue
                    folder = root / worker["id"]
                    folder.mkdir()
                    item = {"schemaVersion": 1, "role": worker["id"],
                            "commit": p["commit"], "planDigest": p["digest"],
                            "graphDigest": g["graphDigest"], "buildDigest": b["digest"],
                            "outputs": {d: [D + "-out"] for d in worker["roots"]},
                            "exportedPaths": [D + "-out"]}
                    if case == "repeated" and i == 1:
                        item["role"] = b["workers"][0]["id"]
                    if case == "wrong-owner" and i == 0:
                        item["outputs"] = {drv("not-owned"): [D + "-out"]}
                    if case == "stale" and i == 0:
                        item["buildDigest"] = "stale"
                    (folder / "evidence.json").write_text(json.dumps(item))
                    if case != "missing-archive" or i != 0:
                        (folder / "store.nar").write_bytes(
                            b"" if case == "empty-archive" else b"archive")
                with patch.object(subject, "validated", return_value=(p, g, b)), \
                     patch.object(subject, "exact_head"), \
                     patch.object(subject, "common_outputs", return_value=[]), \
                     patch.object(subject.staged, "shared_outputs", return_value=[]), \
                     patch.object(subject.staged, "export_incremental_archive", return_value=[]) as export, \
                     patch.object(subject.planner, "run_command", return_value=D + "-out\n"), \
                     patch.object(subject.subprocess, "run") as run:
                    if case == "corrupt":
                        run.side_effect = subprocess.CalledProcessError(1, ["nix-store", "--import"])
                        with self.assertRaises(subprocess.CalledProcessError):
                            subject.join_cmd(args)
                    elif case == "valid":
                        subject.join_cmd(args)
                        self.assertEqual(run.call_count, 4)
                        report = json.loads(args.output.read_text())
                        self.assertEqual(set(report["outputs"]), set(b["roots"]))
                        self.assertEqual(report["role"], "build-join")
                        export.assert_called_once()
                        continue
                    else:
                        with self.assertRaisesRegex(ValueError, message):
                            subject.join_cmd(args)
                    self.assertFalse(args.output.exists())
                    export.assert_not_called()

    def test_duplicate_and_wrong_build_evidence_fails(self):
        p,g=self.sample()
        b=subject.build_plan(p,g)
        w=next(x for x in b["workers"] if x["roots"])
        ok={"schemaVersion":1,"role":w["id"],"commit":p["commit"],
            "planDigest":p["digest"],"graphDigest":g["graphDigest"],
            "buildDigest":b["digest"],"outputs":{x:["/nix/store/"+"b"*32+"-out"] for x in w["roots"]},
            "exportedPaths":[]}
        subject.verify_worker(p,g,b,w,ok)
        with self.assertRaisesRegex(ValueError,"missing or duplicate"):
            subject.verify_worker(p,g,b,w,{**ok,"outputs":{}})
        with self.assertRaisesRegex(ValueError,"stale"):
            subject.verify_worker(p,g,b,w,{**ok,"commit":"f"*40})

if __name__ == "__main__":
    unittest.main()
