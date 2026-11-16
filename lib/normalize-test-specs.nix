# Normalize shared test inputs once for mkSuite and mkTestCases.
# This is pure Nix data shaping; it does not build, schedule or run tests.
{ tests, defaults ? { } }:
let
  fail = message: throw "phenix-flake-ci.test-specs: ${message}";
  mergeAttrs = field: entry:
    let
      base = defaults.${field} or { };
      override = entry.${field} or { };
    in
    if !builtins.isAttrs base || !builtins.isAttrs override then
      fail "${field} must be an attribute set"
    else
      base // override;
  mergeSockets = entry:
    let
      base = defaults.sockets or [ ];
      override = entry.sockets or [ ];
    in
    if !builtins.isList base || !builtins.isList override then
      fail "sockets must be a list"
    else
      builtins.foldl' (acc: name:
        if builtins.elem name acc then acc else acc ++ [ name ]
      ) [ ] (base ++ override);
  one = key: entry:
    if !builtins.isAttrs entry || entry ? name || entry ? pkgs then
      fail "test ${key} must be an attribute set without name or pkgs"
    else
      let
        hasSteps = entry ? steps || (defaults ? steps && !(entry ? command));
        base = defaults // entry;
      in
      (builtins.removeAttrs base [
        "name" "pkgs" "command" "steps" "env" "fixtures" "services" "sockets"
      ])
      // {
        env = mergeAttrs "env" entry;
        fixtures = mergeAttrs "fixtures" entry;
        services = mergeAttrs "services" entry;
        sockets = mergeSockets entry;
      }
      // (if hasSteps then
        { steps = entry.steps or defaults.steps; }
      else
        { command = mergeAttrs "command" entry; });
in
if !builtins.isAttrs tests then fail "tests must be an attribute set"
else if !builtins.isAttrs defaults || defaults ? name || defaults ? pkgs then
  fail "defaults must be an attribute set without name or pkgs"
else builtins.mapAttrs one tests
