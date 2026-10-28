let
  mkTest = import ./mk-test.nix;
  pkgs = {
    python3 = "/nix/store/fake-python";
    runCommand = name: attrs: script: { inherit name attrs script; };
  };
  simple = mkTest {
    inherit pkgs;
    name = "cli-contract";
    command = {
      program = "/nix/store/demo/bin/cli";
      args = [ "status" "--json" ];
      timeoutSeconds = 10;
    };
    expect = {
      exitCode = 0;
      stderr.empty = true;
      stdout.json."/status" = "ready";
      stdout.json."/count" = { gte = 2; };
    };
  };
  spec = builtins.fromJSON simple.attrs.testSpec;

  sequence = mkTest {
    inherit pkgs;
    name = "lifecycle";
    fixtures."config.json" = "{}";
    steps = [
      {
        id = "create";
        command = { program = "/bin/true"; };
      }
      {
        id = "inspect";
        command = { program = "/bin/true"; };
        expect.exitCode = 0;
      }
    ];
    expect.files."state.json".exists = true;
  };
  sequenceSpec = builtins.fromJSON sequence.attrs.testSpec;

  invalidBoth = builtins.tryEval (builtins.deepSeq (mkTest {
    inherit pkgs;
    name = "bad";
    command.program = "/bin/true";
    steps = [ { command.program = "/bin/true"; } ];
  }) true);
  invalidNeither = builtins.tryEval (builtins.deepSeq (mkTest {
    inherit pkgs;
    name = "bad";
  }) true);
  invalidEnv = builtins.tryEval (builtins.deepSeq (mkTest {
    inherit pkgs;
    name = "bad";
    command.program = "/bin/true";
    env.COUNT = 1;
  }) true);
  testAssert = import ./test-assert.nix;
  helperTest = mkTest {
    inherit pkgs;
    name = "artifacts";
    command.program = "/bin/true";
    expect = {
      stdout.json."/status" = testAssert.eq "ready";
      exitCode = testAssert.eq 0;
      files =
        (testAssert.files.required [ "one.json" "two.json" ])
        // (testAssert.files.json "three.json" { "/count" = testAssert.gte 3; });
    };
  };
  helperSpec = builtins.fromJSON helperTest.attrs.testSpec;
  refTest = testAssert.stdoutJson "create" "/id";
  mkTestCases = import ./mk-test-cases.nix;
  matrix = mkTestCases {
    inherit pkgs;
    namePrefix = "cli-";
    defaults = {
      command.program = "/nix/store/demo/bin/cli";
      env.LANG = "C";
      fixtures."input.json" = "{}";
    };
    cases = {
      ready = {
        command.args = [ "status" ];
        expect.stdout.contains = "ready";
      };
      invalid = {
        command.args = [ "invalid" ];
        expect.exitCode = 7;
        env.MODE = "strict";
      };
    };
  };
  matrixReady = builtins.fromJSON matrix.ready.attrs.testSpec;
  matrixInvalid = builtins.fromJSON matrix.invalid.attrs.testSpec;
  invalidMatrix = builtins.tryEval (builtins.deepSeq (mkTestCases {
    inherit pkgs;
    cases = { };
  }) true);

in
{
  mkTestCreatesCheckDerivation = 
    assert simple.name == "test-cli-contract";
    assert simple.attrs.passAsFile == [ "testSpec" ];
    assert builtins.match ".*test-runner.py.*" (builtins.replaceStrings [ "\n" ] [ "" ] simple.script) != null;
    true;

  mkTestKeepsMultipleAssertionsOnOneCommand =
    assert spec.command.args == [ "status" "--json" ];
    assert spec.expect.stdout.json."/status" == "ready";
    assert spec.expect.stdout.json."/count".gte == 2;
    assert spec.expect.stderr.empty;
    true;

  mkTestSupportsDeclarativeSteps =
    assert builtins.length sequenceSpec.steps == 2;
    assert sequenceSpec.fixtures."config.json" == "{}";
    assert sequenceSpec.expect.files."state.json".exists;
    true;

  mkTestRejectsInvalidShapes =
    assert !invalidBoth.success;
    assert !invalidNeither.success;
    assert !invalidEnv.success;
    true;

  mkTestCasesGeneratesIndependentChecks =
    assert builtins.attrNames matrix == [ "invalid" "ready" ];
    assert matrix.ready.name == "test-cli-ready";
    assert matrix.invalid.name == "test-cli-invalid";
    assert matrixReady.command.program == "/nix/store/demo/bin/cli";
    assert matrixReady.command.args == [ "status" ];
    assert matrixInvalid.command.args == [ "invalid" ];
    assert matrixInvalid.env.LANG == "C";
    assert matrixInvalid.env.MODE == "strict";
    assert matrixReady.fixtures."input.json" == "{}";
    true;

  mkTestCasesRejectsEmptyMatrix =
    assert !invalidMatrix.success;
    true;

  testAssertHelpersProduceOnlyExistingIR =
    assert helperSpec.expect.stdout.json."/status" == { eq = "ready"; };
    assert helperSpec.expect.exitCode == { eq = 0; };
    assert helperSpec.expect.files."one.json".exists;
    assert helperSpec.expect.files."two.json".exists;
    assert helperSpec.expect.files."three.json".json."/count".gte == 3;
    assert refTest == { ref = { step = "create"; field = "stdout"; json = "/id"; }; };
    assert testAssert.files.absent [ "debug.log" ] == { "debug.log".exists = false; };
    true;

}
