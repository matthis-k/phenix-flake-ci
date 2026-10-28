# An ergonomic combinator over mkTest. Every case is its own Nix check,
# sharing only the declared package references and defaults.
{
  pkgs,
  cases,
  defaults ? { },
  namePrefix ? "",
}:
let
  mkTest = import ./mk-test.nix;
  fail = message: throw "phenix-flake-ci.mkTestCases: ${message}";
  valid =
    if !builtins.isAttrs cases || cases == { } then
      fail "cases must be a nonempty attribute set"
    else if !builtins.all builtins.isAttrs (builtins.attrValues cases) then
      fail "each case must be an attribute set"
    else if !builtins.isAttrs defaults || !builtins.isString namePrefix then
      fail "defaults must be an attribute set and namePrefix a string"
    else if defaults ? name || defaults ? pkgs then
      fail "defaults must not set name or pkgs"
    else
      true;
  mergeMap =
    field: case:
    let
      base = defaults.${field} or { };
      override = case.${field} or { };
    in
    if !builtins.isAttrs base || !builtins.isAttrs override then
      fail "${field} must be an attribute set"
    else
      base // override;
  one =
    key: case:
    let
      hasSteps = case ? steps || (defaults ? steps && !(case ? command));
      base = defaults // case;
      specification =
        (builtins.removeAttrs base [ "name" "pkgs" "command" "steps" "env" "fixtures" ])
        // {
          inherit pkgs;
          name = "${namePrefix}${key}";
          env = mergeMap "env" case;
          fixtures = mergeMap "fixtures" case;
        }
        // (if hasSteps then { steps = case.steps or defaults.steps; }
            else { command = mergeMap "command" case; });
    in
    if case ? name || case ? pkgs then
      fail "case ${key} must not set name or pkgs"
    else if (case ? steps && case ? command) then
      fail "case ${key} must not declare both steps and command"
    else
      mkTest specification;
in
assert valid;
builtins.mapAttrs one cases
