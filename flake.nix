{
  description = "Declarative repository maintenance, CI projection, and opt-in git hooks for Phenix flakes";

  outputs = _: {
    lib = import ./lib;
  };
}
