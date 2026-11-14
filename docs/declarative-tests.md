
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


## Reusable assertions and diagnostics

The pure Nix `ciLib.testAssert` helpers construct the existing predicate IR without adding a new test runner. For example:

```nix
let
  a = ciLib.testAssert;
in
ciLib.mkTest {
  inherit pkgs;
  name = "report-artifacts";
  command = {
    program = lib.getExe package;
    args = [ "--report" ];
  };
  expect = {
    exitCode = a.eq 0;
    stdout.json."/status" = a.eq "ready";
    files =
      (a.files.required [ "report.json" "trace.json" ])
      // (a.files.absent [ "error.log" ]);
  };
}
```

Helpers include `eq`, `ne`, `contains`, `matches`, `gt`, `gte`, `lt`, `lte`, `ref`, `jsonRef`, `stdout`, `stderr`, `exitCode`, `stdoutJson`, and `files.{exists,text,json,size,required,absent}`.

For exact text and structured JSON equality failures, the runner prints a bounded unified diff of expected versus actual values in CI logs. Full expected and actual values remain in the structured failure evidence. Each missing artifact is independently reported; an assertion failure cannot masquerade as a successful derivation.

## Validation

```console
python3 lib/test-test-runner.py -v
nix eval --json .#lib.tests
nix flake check -L
```

The repo CI workflow runs the Python conformance tests and Nix evaluation. The existing CI backends discover and build the test derivations; the library does not introduce a second test registry or job scheduler.

## Runtime execution without recompilation

`mkTest` now has an explicit execution role:

- `execution = "build"` (default for direct `mkTest`): creates an ordinary Nix check derivation and runs the assertions during its build. Suitable for deterministic/cacheable checks.
- `execution = "runtime"`: produces an executable Nix package containing the test specification and runner. The **package build does not execute the test**. Use it under `packages.${system}."test.<id>"` so the distributed CI workflow compiles its closure at the build barrier and executes it in a tester job.

Prefer `mkSuite` for a single project test inventory. It defaults each declared test to runtime execution, while `execution = "build"` projects into `checks`:

```nix
let
  suite = ciLib.mkSuite {
    inherit pkgs;
    tests = {
      smoke = {
        command.program = lib.getExe packages.cli;
        command.args = [ "status" ];
        expect.stdout.contains = "ready";
      };
      format = {
        execution = "build";
        command.program = lib.getExe packages.formatCheck;
      };
    };
  };
in {
  packages.${system} = suite.packages; # "test.smoke"
  checks.${system} = suite.checks;     # format
}
```

The existing `test.*` package discovery, build barrier, closure transfer and offline Nix execution handle runtime packages. The current workflow may choose to execute one package per job; test batching and capability routing are future extensions, not implicit in `mkSuite`.

### Managed local processes and Unix sockets

Simple process-level integration tests reuse the **same** command, expectation and step-reference engine as ordinary `mkTest` tests.

```nix
let
  socket = ciLib.testRef.socket "rpc";
  suite = ciLib.mkSuite {
    inherit pkgs;
    tests.handshake = {
      sockets = [ "rpc" ];
      services.server = {
        command = {
          program = lib.getExe server;
          args = [ "--socket" socket ];
        };
        ready = { unixSocket = "rpc"; timeoutSeconds = 5; };
      };
      command = {
        program = lib.getExe client;
        args = [ "--socket" socket "ping" ];
      };
      expect.stdout.contains = "pong";
    };
  };
in suite
```

The fixture reference is **tagged data**, not shell interpolation. The runner allocates a fresh temporary socket path, substitutes it into commands, launches the server without a shell, waits for the socket to be bound, runs the client, captures bounded server stdout/stderr tails, and terminates the server process group even when assertions fail. A readiness timeout or premature server exit fails the test and blocks client execution.

Current readiness means *a Unix socket has been created*, not a successful application-level handshake. To verify a working protocol, the test command must connect and assert a response. Unix sockets are the first supported managed resource; TCP port leases, VM/container environments and fixture-sharing scopes are not yet implemented. Managed service environments accept only string values and no implicit secrets.

To verify preparation and execution separately:

```console
nix build --no-link '.#packages.x86_64-linux."test.handshake"'
nix run --offline --no-substitute --max-jobs 0 '.#packages.x86_64-linux."test.handshake"'
```

The second command intentionally fails if required outputs are missing rather than silently building them. Set `PHENIX_TEST_OUTPUT` to a writable directory to retain a passing result document. Failures emit machine-readable JSON to stdout and detailed diagnostics to stderr; CI's existing evidence gate still owns run-level acceptance.

