# Generic CI inputs. A toolchain strategy is injected by the caller.
# Nix remains the source of checks; patterns below only influence scheduling.
{
  system,
  shardCount ? 4,
  strategy ? { },
  ci ? null,
  checkHints ? { },
  prebuildTargets ? [ ],
  cache ? {
    keyPrefix = "flake-checks";
    keyFiles = [ "flake.lock" ];
  },
}:
let
  fail = message: throw "phenix-flake-ci: ${message}";
  valid =
    if !builtins.isString system || system == "" then
      fail "flake checks require a non-empty system"
    else if !builtins.isInt shardCount || shardCount < 1 || shardCount > 64 then
      fail "flake shardCount must be between 1 and 64"
    else if !builtins.isAttrs strategy || !builtins.isAttrs checkHints then
      fail "flake strategy and checkHints must be attribute sets"
    else if !builtins.isList prebuildTargets || !(builtins.all builtins.isString prebuildTargets) then
      fail "flake prebuildTargets must be a list of Nix installables"
    else if !(builtins.isAttrs cache) then
      fail "flake cache must be an attribute set"
    else if ci != null && !builtins.isAttrs ci then
      fail "flake ci policy must be an attribute set"
    else
      true;
in
assert valid;
{
  schemaVersion = 1;
  inherit system shardCount cache;
  strategy = strategy.id or "generic";
  strategyOptions = builtins.removeAttrs strategy [ "id" "checkHints" "prebuildTargets" ];
  checkHints = (strategy.checkHints or { }) // checkHints;
  prebuildTargets = (strategy.prebuildTargets or [ ]) ++ prebuildTargets;
}
// (if ci == null then { } else { inherit ci; })
