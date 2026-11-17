# Pure Nix API regressions; no compiler or GitHub environment required.
let
  mk = import ./mk-rust-tests.nix;
  lib = rec {
    unique = builtins.foldl' (acc: value:
      if builtins.elem value acc then acc else acc ++ [ value ]) [ ];
    concatMap = f: list: builtins.concatLists (map f list);
    hasPrefix = prefix: str:
      builtins.substring 0 (builtins.stringLength prefix) str == prefix;
    escapeShellArg = value: "'${value}'";
    escapeShellArgs = args: builtins.concatStringsSep " " (map escapeShellArg args);
  };
  pkgs = {
    inherit lib;
    python3 = "/nix/store/mock-python3";
    runtimeShell = "/bin/sh";
    rustPlatform.buildRustPackage = attrs: {
      outPath = "/nix/store/mock-${attrs.pname}";
      inherit attrs;
    };
    writeShellApplication = attrs: attrs // {
      outPath = "/nix/store/mock-${attrs.name}";
    };
  };
  defaults = {
    inherit pkgs;
    src = ../tests/cargo-test-consumer;
    lockFile = ../tests/cargo-test-consumer/Cargo.lock;
  };
  make = args: mk (defaults // args);
  one = make { crate = "alpha"; };
  independent = make { crates = [ "alpha" "beta" ]; };
  shared = make {
    crates = [ "alpha" "beta" ];
    strategy = "shared";
    cargoArgs = [ "--features" "exp" ];
  };
  fail = attrs: !(builtins.tryEval (builtins.deepSeq (make attrs) true)).success;
  flatten = builtins.replaceStrings [ "\n" ] [ " " ];
in {
  singleCrateProducesRunnableNixTestPackage =
    assert builtins.attrNames one.packages == [ "test.rust-alpha" ];
    assert one.packages."test.rust-alpha".attrs.pname == "rust-test-alpha";
    assert builtins.match ".*--package.*alpha.*--all-targets.*"
      (flatten one.packages."test.rust-alpha".attrs.buildPhase) != null;
    assert one.packages."test.rust-alpha".attrs.doCheck == false;
    true;

  isolatedCratesHaveSeparateCompilerFailureDomains =
    assert independent.packages."test.rust-alpha".outPath
      != independent.packages."test.rust-beta".outPath;
    assert builtins.match ".*--package.*beta.*"
      (flatten independent.packages."test.rust-beta".attrs.buildPhase) != null;
    true;

  sharedCratesUseExactlyOneCompiledTestCorpus =
    let
      alpha = shared.packages."test.rust-alpha";
      beta = shared.packages."test.rust-beta";
    in
    assert builtins.attrNames shared.packages == [ "test.rust-alpha" "test.rust-beta" ];
    assert builtins.match ".*/rust-test-shared.*--package 'alpha'.*" alpha.text != null;
    assert builtins.match ".*/rust-test-shared.*--package 'beta'.*" beta.text != null;
    assert builtins.match ".*--package.*alpha.*--package.*beta.*--all-targets.*--features.*exp.*"
      (flatten (make {
        crates = [ "alpha" "beta" ];
        strategy = "shared";
        cargoArgs = [ "--features" "exp" ];
      }).packages."test.rust-alpha".text) == null;
    # Wrappers only execute prebuilt binaries. Cargo never appears here.
    assert builtins.match ".*cargo (test|build).*" (flatten alpha.text) == null;
    true;

  invalidOrAmbiguousCrateSelectionsFailClosed =
    assert fail { crates = [ ]; };
    assert fail { crates = [ "alpha" "alpha" ]; };
    assert fail { crates = [ "not/a/crate" ]; };
    assert fail { crate = "alpha"; crates = [ "beta" ]; };
    assert fail { crate = "alpha"; strategy = "unknown"; };
    assert fail { strategy = "isolated"; };
    assert fail { crate = "alpha"; cargoArgs = [ "--package" "beta" ]; };
    assert fail { crate = "alpha"; cargoArgs = [ "--doc" ]; };
    assert fail { crate = "alpha"; cargoArgs = [ "--no-run" ]; };
    assert fail { crate = "alpha"; cargoArgs = [ "--message-format=json" ]; };
    true;
}
