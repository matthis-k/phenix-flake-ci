# Parallel GitHub CI groups

GitHub Actions creates one check run for each job execution. Matrix entries therefore remain visible as separate checks. GitHub cannot run scenarios on separate runners while exposing them as one literal check run.

`parallelGroups` keeps runner-level parallelism and adds one stable summary check for the logical group. Branch protection can require the summary check while each scenario remains available for logs and reruns.

## Product scenarios

Semantic product suites use stage IDs with the `product-` prefix. Group them without listing every scenario:

```nix
ci.github.parallelGroups.product = {
  name = "Product";
  jobPrefix = "product-";
};
```

The generated workflow contains one matrix job definition. Every matching stage is one matrix entry and gets its own runner. The matrix uses `fail-fast: false`, so one failing scenario does not cancel its siblings.

The generator also emits a `Product` summary check after the matrix finishes. Require `Product` in branch protection when product scenarios should count as one logical merge requirement.

Use an explicit stage list for a group that does not follow a prefix convention:

```nix
ci.github.parallelGroups.protocols = {
  name = "Protocol compatibility";
  jobs = [
    "integration-acp"
    "integration-mcp"
  ];
};
```

## Safety rules

A parallel group must contain at least two stages. Each stage must have one command. Grouped stages must use the same runner, timeout, dependencies, and environment. Set `cache = false` on grouped stages because concurrent matrix cells must not write the same Actions cache key.

Grouped stages are currently CI leaves. Another stage cannot depend on one grouped member. This keeps the declared dependency DAG exact instead of replacing a dependency on one scenario with a dependency on the whole matrix.

Each matrix entry still uses its command-scoped flake output. Grouping changes GitHub scheduling and presentation only. It does not widen Nix closures or change local maintenance commands.
