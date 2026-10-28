#!/usr/bin/env python3
"""Pure DAG proof for four build workers preceding any check execution."""
import importlib.util
from pathlib import Path
import unittest

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
