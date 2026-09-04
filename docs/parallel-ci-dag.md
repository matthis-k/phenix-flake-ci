# Parallel CI DAG

Semantic phases classify suites. `needs` defines execution dependencies. Generated GitHub CI runs independent suites as separate jobs so they execute concurrently. Non-build suites depend on all build suites by default, while `needs = [ ]` opts an independent suite out of that barrier. Shared GitHub cache metadata can transfer reusable build state from build jobs to dependent jobs without forcing unrelated jobs to restore it.
