# Pure projection of one test inventory into existing flake outputs.
# Runtime tests become packages."test.<name>", which the existing CI handoff
# discovers. Deterministic build-time checks remain checks.<name>.
{ pkgs, tests }:
let
  mkTest = import ./mk-test.nix;
  fail = message: throw "phenix-flake-ci.mkSuite: ${message}";
  attrs = builtins.isAttrs tests;
  names = if attrs then builtins.attrNames tests else [ ];
  spec = name:
    let value = tests.${name}; in
    if !builtins.isAttrs value || value ? name || value ? pkgs then
      fail "test ${name} must be an attribute set without name or pkgs"
    else
      value;
  role = name: (spec name).execution or "runtime";
  project = target: prefix:
    builtins.listToAttrs (map (name: {
      name = prefix + name;
      value = mkTest ({ inherit pkgs name; } // spec name // { execution = target; });
    }) (builtins.filter (name: role name == target) names));
  rolesValid = builtins.all (name: builtins.elem (role name) [ "build" "runtime" ]) names;
in
if !attrs then fail "tests must be an attribute set"
else if !rolesValid then fail "execution must be build or runtime"
else {
  packages = project "runtime" "test.";
  checks = project "build" "";
}
