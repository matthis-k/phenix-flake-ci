{
  maintenance,
  paths,
}:
let
  inherit (builtins)
    attrNames
    concatLists
    concatStringsSep
    elem
    foldl'
    hasAttr
    head
    map
    replaceStrings
    tail
    ;

  fail = message: throw "phenix-flake-ci: ${message}";
  pathId = path: concatStringsSep "/" path;
  functionId = path: "n_${builtins.hashString "sha256" (pathId path)}";
  shellQuote = value: "'${replaceStrings [ "'" ] [ "'\"'\"'" ] value}'";

  nodeAtPath =
    path:
    let
      descend =
        nodes: remaining:
        if remaining == [ ] then
          fail "scoped command path must not be empty"
        else
          let
            segment = head remaining;
            rest = tail remaining;
          in
          if !hasAttr segment nodes then
            fail "unknown scoped command `${concatStringsSep " " path}`"
          else if rest == [ ] then
            nodes.${segment}
          else
            descend (nodes.${segment}.commands or { }) rest;
    in
    descend maintenance.commands path;

  prefixes =
    path:
    let
      go = prefix: rest:
        if rest == [ ] then
          [ ]
        else
          let
            next = prefix ++ [ (head rest) ];
          in
          [ next ] ++ go next (tail rest);
    in
    go [ ] path;

  unique = foldl' (items: item: if elem item items then items else items ++ [ item ]) [ ];
  includedPaths = unique (concatLists (map prefixes paths));
  included = path: elem path includedPaths;

  childNames =
    path: node:
    builtins.filter (name: included (path ++ [ name ])) (attrNames (node.commands or { }));

  renderRun =
    path: node:
    let
      id = functionId path;
      children = childNames path node;
    in
    if hasAttr "exec" node then
      ''
        run_${id}() {
          ${node.exec}
        }
      ''
    else
      ''
        run_${id}() {
          ${concatStringsSep "\n" (map (name: "run_${functionId (path ++ [ name ])}") children)}
        }
      '';

  renderDispatch =
    path: node:
    let
      id = functionId path;
      children = childNames path node;
      cases = concatStringsSep "\n" (map (name: ''
        ${shellQuote name})
          shift
          dispatch_${functionId (path ++ [ name ])} "$@"
          ;;
      '') children);
    in
    if children == [ ] then
      ''
        dispatch_${id}() {
          run_${id} "$@"
        }
      ''
    else
      ''
        dispatch_${id}() {
          case "''${1:-}" in
            "")
              run_${id}
              ;;
            ${cases}
            *)
              printf 'Unknown scoped command: %s\n' "$1" >&2
              return 2
              ;;
          esac
        }
      '';

  renderNode =
    path:
    let
      node = nodeAtPath path;
    in
    renderRun path node + renderDispatch path node;

  rootNames = builtins.filter (name: included [ name ]) (attrNames maintenance.commands);
  rootCases = concatStringsSep "\n" (map (name: ''
    ${shellQuote name})
      shift
      dispatch_${functionId [ name ]} "$@"
      ;;
  '') rootNames);
in
''
  ${concatStringsSep "\n" (map renderNode includedPaths)}

  dispatch_root() {
    case "''${1:-}" in
      ${rootCases}
      *)
        printf 'Unknown scoped command: %s\n' "''${1:-}" >&2
        return 2
        ;;
    esac
  }

  dispatch_root "$@"
''
