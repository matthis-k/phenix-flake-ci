# Build-only case constructor. Suite and case declarations share
# identical defaults, socket, fixture and service normalization.
{ pkgs, cases, defaults ? { }, namePrefix ? "" }:
let
  mkTest = import ./mk-test.nix;
  fail = message: throw "phenix-flake-ci.mkTestCases: ${message}";
  tests = import ./normalize-test-specs.nix { inherit defaults; tests = cases; };
in
if !builtins.isAttrs cases || cases == { } then
  fail "cases must be a nonempty attribute set"
else if !builtins.isString namePrefix then
  fail "namePrefix must be a string"
else builtins.mapAttrs (key: spec:
  if (spec.execution or "build") != "build" then
    fail "case ${key} requests runtime execution; use mkSuite for runtime packages"
  else
    mkTest ({ inherit pkgs; name = "${namePrefix}${key}"; } // spec)
) tests
