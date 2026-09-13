# Repository-local Git hooks

Phenix git hooks are repository files, not installed Git metadata.

## Contract

A maintenance declaration may enable a tracked hook path:

```nix
gitHooks = {
  enable = true;
  path = ".githooks";
  preCommit = [ "fix" ];
};
```

`path` defaults to `.githooks` and must be a safe repository-relative path.

The generated `<outputName>-git-hooks` package mirrors the tracked tree. For the default path it contains:

```text
.githooks/
└── pre-commit
```

The adapter contains no maintenance implementation. It invokes the command-scoped flake app for the configured command and feeds the same JSON invocation used by other machine integrations.

## Clone-and-go

A developer who wants hooks active immediately can opt in during clone using standard Git:

```sh
git clone -c core.hooksPath=.githooks <url>
```

This is the only persistent Git configuration required. Git writes it while creating the clone; the repository and its flake provide the hook implementation.

There is no `pre-commit install`, Home Manager module, global Git template, or Phenix-specific Git wrapper.

## Ordinary clone

A normal clone remains valid:

```sh
git clone <url>
cd <repo>
nix develop
```

The generated dev-shell hook activates the configured hook path through Git's `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`, and `GIT_CONFIG_VALUE_*` environment interface. It injects a `gitdir:`-conditional include for the current repository only, pointing at an immutable Nix-store config that contains `core.hooksPath`.

It does not write `.git/config`, create files beneath `.git/hooks`, or affect another repository entered from the same shell. If the repository already has an explicit local `core.hooksPath`, the dev shell leaves it unchanged.

The environment-only activation ends with the dev shell. Use the clone-time `-c core.hooksPath=...` form when the hook should also run from IDEs or ordinary shells outside `nix develop`.

## Tracked-file synchronization

Consumers should commit the generated hook adapter and verify it against the generated package in CI. For an output named `phenix-maintenance`:

```sh
hooks="$(nix build --no-link --print-out-paths .#phenix-maintenance-git-hooks)"
diff -u "$hooks/.githooks/pre-commit" .githooks/pre-commit
```

A deterministic fix command may copy the generated adapter into the repository when it changes.

## Security boundary

A plain `git clone <url>` does not activate repository-supplied hooks. Git intentionally requires an external opt-in before cloned executable content becomes a hook.

`git clone -c core.hooksPath=.githooks ...` is that explicit opt-in. The repository does not gain general write access to `.git`, and the generated hook never mutates Git metadata.

## Hosting compatibility

The mechanism is standard Git. GitHub, GitLab, Forgejo, Gitea, Bitbucket, `gh`, and IDE Git integrations do not need Phenix-specific support. Hosted CI remains authoritative and independently invokes commands from the same maintenance graph.
