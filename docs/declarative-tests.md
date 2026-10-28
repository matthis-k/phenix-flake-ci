
# Declarative executable tests

Status: initial implementation. The mkTest function creates normal Nix check derivations. The existing flake planner discovers them automatically from checks.<system>.

## Single command, multiple assertions

```nix
checks.${system}.cli = ciLib.mkTest {
  inherit pkgs;
  name = "cli-contract";
  command = {
    program = lib.getExe packages.cli;
    args = [ "status" "--json" ];
    timeoutSeconds = 10; # default: 30
  };
  expect = {
    exitCode = 0; # implicit default
    stderr.empty = true;
    stdout.json = {
      "/status" = "ready";
      "/count" = { gte = 2; };
    };
  };
};
```

The program runs once, without a shell. Exit code, stdout and stderr are captured. All applicable assertions are evaluated even if one fails. On success the derivation writes a result.json; on failure human diagnostics and JSON evidence are printed to the Nix build log. Failure outputs cannot be assumed to exist as derivations.

## Expected nonzero exit

```nix
checks.${system}.invalidConfig = ciLib.mkTest {
  inherit pkgs;
  name = "invalid-config";
  command = {
    program = lib.getExe packages.cli;
    args = [ "load" "--config" "missing.json" ];
  };
  expect = {
    exitCode = { ne = 0; };
    stderr.contains = [ "configuration" "not found" ];
    stdout.empty = true;
  };
};
```

A nonzero exit is just an observation; it can satisfy a test. If not specified, exitCode defaults to 0. Timeouts and process launch failures always fail, even for negative tests.

## Multiple steps and fixtures

```nix
checks.${system}.lifecycle = ciLib.mkTest {
  inherit pkgs;
  name = "lifecycle";
  fixtures."settings.json" = ''{"enabled":true}'';
  env.LANG = "C";
  steps = [
    {
      id = "initialize";
      command = {
        program = lib.getExe packages.cli;
        args = [ "init" "--config" "settings.json" ];
      };
    }
    {
      id = "inspect";
      command = {
        program = lib.getExe packages.cli;
        args = [ "inspect" "--json" ];
      };
      expect.stdout.json."/initialized" = true;
    }
  ];
  expect.files."state.json".exists = true;
};
```

Steps execute in declaration order in the same isolated temporary workspace, each once, with independent observations. All steps are attempted even after assertion failures. A test declares command **or** steps, never both. Top-level expect.files is checked after the sequence; all other step expectations belong to their respective step.

Fixtures are literal text files relative to the test workspace. All environment values are strings. Do not include credentials in test declarations: they would be embedded in the Nix store.

## Predicates

| Observation | Supported operations |
| --- | --- |
| exitCode | Bare exact value, eq, ne, gt, gte, lt, lte |
| stdout, stderr | empty, eq, ne, contains, matches, json |
| stdout.json."/pointer" | Bare exact value, eq, ne, contains, matches, gt, gte, lt, lte |
| files."relative/path" | exists, text.{eq,ne,contains,matches,empty}, json."/pointer", size with numeric predicates |

String contains accepts one substring or a list of substrings (all must match). matches uses regex search. JSON selectors use RFC 6901 JSON Pointer. Object contains checks recursive subset inclusion. Numeric predicates require numbers, not booleans.

File size is measured in bytes; file text is read as strict UTF-8. File JSON assertions use the same JSON Pointer semantics as stdout. Content assertions imply exists = true unless explicitly declared. Missing files fail presence assertions rather than aborting the check. Metadata predicates beyond byte size are not yet supported.


## References between steps

A reference is tagged data with the shape { ref = { step = "id"; field = "stdout"; json = "/path"; }; }. The json selector is optional and valid only for stdout/stderr. Other supported fields are stderr and exitCode. References point **only to earlier steps**, by ID. A forward, unknown or self reference is rejected before any command executes.

References can be used inside assertion predicates and command arguments. Argument references must resolve to strings; no implicit stringification or shell interpolation occurs.

```nix
checks.${system}.session = ciLib.mkTest {
  inherit pkgs;
  name = "session";
  steps = [
    {
      id = "create";
      command = {
        program = lib.getExe packages.cli;
        args = [ "create" "--json" ];
      };
    }
    {
      id = "inspect";
      command = {
        program = lib.getExe packages.cli;
        args = [
          "inspect"
          { ref = { step = "create"; field = "stdout"; json = "/id"; }; }
        ];
      };
      expect.stdout.json."/id" = {
        eq = { ref = { step = "create"; field = "stdout"; json = "/id"; }; };
      };
    }
  ];
};
```

If an earlier step fails to produce a referenced observation, the consuming assertion fails with a clear reason. If a command argument cannot be resolved, that command is **not executed**; later unrelated steps continue. Every reference is resolved from the recorded observation, never by re-executing its producer. A singleton { ref = ...; } attribute set is reserved for references.

## Generating independent cases

Use mkTestCases when the same executable or fixtures support several independently schedulable tests. Defaults for command, env and fixtures are shallowly merged with each case. All cases become ordinary Nix check derivations, not steps in one shared test.

```nix
checks.${system} = ciLib.mkTestCases {
  inherit pkgs;
  namePrefix = "cli-";
  defaults.command.program = lib.getExe packages.cli;
  cases = {
    normal = {
      command.args = [ "status" ];
      expect.stdout.contains = "ready";
    };
    invalid = {
      command.args = [ "load" "missing.json" ];
      expect = {
        exitCode = { ne = 0; };
        stderr.contains = "not found";
      };
    };
  };
};
```

This yields independent checks named normal and invalid. Common arguments are not implicitly concatenated; a case can override the complete args list. Shared derivation references are reused by Nix rather than rebuilt for each case.


## Validation

```console
python3 lib/test-test-runner.py -v
nix eval --json .#lib.tests
nix flake check -L
```

The repo CI workflow runs the Python conformance tests and Nix evaluation. The existing CI backends discover and build the test derivations; the library does not introduce a second test registry or job scheduler.
