# Optional Rust strategy. It supplies metadata to the generic Nix planner.
# Cargo impact analysis remains owned by the existing cargo-impact.py planner.
{
  workspace ? "rust",
  cargoLock ? "rust/Cargo.lock",
  prebuildTargets ? [ ],
  checkHints ? { },
  shippingPattern ? "*shipping*",
}:
let
  valid =
    builtins.isString workspace
    && workspace != ""
    && builtins.isString cargoLock
    && cargoLock != ""
    && builtins.isList prebuildTargets
    && builtins.all builtins.isString prebuildTargets
    && builtins.isAttrs checkHints;
in
assert valid;
{
  id = "rust";
  inherit prebuildTargets;
  checkHints = { "${shippingPattern}" = { isolate = true; weight = 100; }; } // checkHints;
  cargo = { inherit workspace cargoLock; };
}
