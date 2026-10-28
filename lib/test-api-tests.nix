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
}
