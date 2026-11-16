# One normalized test inventory projects into existing flake outputs.
# Runtime tests become packages."test.<name>"; build checks remain checks.<name>.
{ pkgs, tests, defaults ? { } }:
let
  mkTest = import ./mk-test.nix;
  fail = message: throw "phenix-flake-ci.mkSuite: ${message}";
  normalized = import ./normalize-test-specs.nix { inherit tests defaults; };
  names = if builtins.isAttrs tests then builtins.attrNames tests else [ ];
  role = name: (normalized.${name}).execution or "runtime";
  project = target: prefix:
    builtins.listToAttrs (map (name: {
      name = prefix + name;
      value = mkTest ({ inherit pkgs name; } // normalized.${name} // { execution = target; });
    }) (builtins.filter (name: role name == target) names));
  rolesValid = builtins.all (name: builtins.elem (role name) [ "build" "runtime" ]) names;
in
if !builtins.isAttrs tests then fail "tests must be an attribute set"
else if !rolesValid then fail "execution must be build or runtime"
else {
  packages = project "runtime" "test.";
  checks = project "build" "";
}
