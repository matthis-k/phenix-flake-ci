args@{
  commands,
  ...
}:
let
  inherit (builtins)
    attrNames
    concatLists
    concatStringsSep
    filter
    foldl'
    isBool
    map
    ;

  base = import ./render-maintenance.nix args;

  normalizeCi = node:
    let
      raw = node.ci or { };
    in
    if isBool raw then { enable = raw; } else raw;

  flattenNode =
    path: node:
    let
      children = node.commands or { };
      names = attrNames children;
    in
    [ { inherit path node; } ]
    ++ concatLists (map (name: flattenNode (path ++ [ name ]) children.${name}) names);

  flattened = concatLists (
    map (name: flattenNode [ name ] commands.${name}) (attrNames commands)
  );

  ciEntries = filter (entry: ((normalizeCi entry.node).enable or false)) flattened;
  pathId = path: concatStringsSep "/" path;

  cacheByStage = foldl'
    (
      acc: entry:
      let
        ci = normalizeCi entry.node;
        stage = ci.stage or (pathId entry.path);
        cache = ci.cache or null;
        existing = acc.${stage} or null;
      in
      if existing != null && existing != cache then
        throw "phenix-flake-ci: CI stage `${stage}` has conflicting cache metadata"
      else
        acc // { ${stage} = cache; }
    )
    { }
    ciEntries;

  jobs = map (job: job // { cache = cacheByStage.${job.id} or null; }) base.jobs;
  publicJobs = map (job: {
    inherit (job)
      id
      name
      runner
      timeout
      needs
      env
      commands
      cache
      ;
  }) jobs;
  matrix = { include = publicJobs; };
in
base
// {
  inherit
    jobs
    publicJobs
    matrix
    ;
}
