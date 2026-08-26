{
  maintenance,
  pkgs ? null,
}:
let
  inherit (builtins)
    all
    attrNames
    concatLists
    concatStringsSep
    elem
    foldl'
    hasAttr
    head
    isAttrs
    isFunction
    isList
    isString
    map
    tail
    ;

  fail = message: throw "phenix-flake-ci: ${message}";
  displayPath = path: concatStringsSep " " ([ maintenance.name ] ++ path);
  pathId = path: concatStringsSep "/" path;

  childPaths =
    path: node:
    let
      children = node.commands or { };
    in
    if !isAttrs children then
      fail "`${displayPath path}`: commands must be an attribute set"
    else
      map (name: path ++ [ name ]) (attrNames children);

  commandPaths =
    let
      flatten =
        path:
        let
          node = nodeAtPath path;
        in
        [ path ] ++ concatLists (map flatten (childPaths path node));
    in
    concatLists (map (name: flatten [ name ]) (attrNames maintenance.commands));

  nodeAtPath =
    path:
    let
      descend =
        nodes: remaining:
        if remaining == [ ] then
          fail "command path must not be empty"
        else
          let
            segment = head remaining;
            rest = tail remaining;
          in
          if !hasAttr segment nodes then
            fail "unknown command dependency `${displayPath path}`"
          else if rest == [ ] then
            nodes.${segment}
          else
            let
              node = nodes.${segment};
              children = node.commands or { };
            in
            if !isAttrs children then
              fail "unknown command dependency `${displayPath path}`"
            else
              descend children rest;
    in
    descend maintenance.commands path;

  dependenciesFor =
    path: node:
    let
      dependencies = node.dependencies or [ ];
      validPath = dependency: isList dependency && dependency != [ ] && all isString dependency;
    in
    if !isList dependencies || !(all validPath dependencies) then
      fail "`${displayPath path}`: dependencies must be a list of command path lists"
    else
      dependencies;

  reachablePaths =
    path: stack:
    let
      id = pathId path;
      node = nodeAtPath path;
      nested = childPaths path node ++ dependenciesFor path node;
    in
    if elem id stack then
      fail "command dependency cycle reaches `${displayPath path}`"
    else
      [ path ] ++ concatLists (map (nestedPath: reachablePaths nestedPath (stack ++ [ id ])) nested);

  unique = foldl' (items: item: if elem item items then items else items ++ [ item ]) [ ];

  pathsForPath = path: unique (reachablePaths path [ ]);

  runtimeInputsAt =
    path:
    let
      node = nodeAtPath path;
      raw = node.runtimeInputs or [ ];
      resolved =
        if isFunction raw then
          if pkgs == null then
            fail "`${displayPath path}`: runtimeInputs function requires pkgs"
          else
            raw pkgs
        else
          raw;
    in
    if !isList resolved then
      fail "`${displayPath path}`: runtimeInputs must resolve to a list"
    else
      resolved;

  validatedPaths = concatLists (map (path: reachablePaths path [ ]) commandPaths);
  valid = builtins.deepSeq validatedPaths true;

  runtimeInputsForPath =
    path:
    unique (concatLists (map runtimeInputsAt (pathsForPath path)));

  allRuntimeInputs = unique (concatLists (map runtimeInputsAt commandPaths));
in
assert valid;
{
  inherit
    allRuntimeInputs
    commandPaths
    nodeAtPath
    pathId
    pathsForPath
    runtimeInputsForPath
    valid
    ;
}
