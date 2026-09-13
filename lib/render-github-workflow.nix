{
  jobs,
  outputName ? "phenix-maintenance",
  workflowName ? "CI",
  mainBranch ? "main",
  gateName ? "Maintenance checks",
  parallelGroups ? { },
  checkoutAction ? "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd",
  installNixAction ? "cachix/install-nix-action@a49548c11d9846ad46ecc0115273879b045f001c",
  cacheAction ? "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830",
  clean ? true,
}:
let
  inherit (builtins)
    all
    any
    attrNames
    concatLists
    concatStringsSep
    elem
    filter
    head
    isAttrs
    isList
    isString
    length
    listToAttrs
    map
    match
    replaceStrings
    stringLength
    substring
    toJSON
    ;

  fail = message: throw "phenix-flake-ci: ${message}";
  scopeOutputName = import ./scope-output-name.nix;

  validOutputName = match "^[A-Za-z0-9][A-Za-z0-9_-]*$" outputName != null;
  validId = value: isString value && match "^[A-Za-z0-9][A-Za-z0-9_-]*$" value != null;
  yaml = toJSON;
  joinLines = concatStringsSep "\n";
  shellQuote = value: "'${replaceStrings [ "'" ] [ "'\"'\"'" ] value}'";
  hasPrefix =
    prefix: value:
    stringLength value >= stringLength prefix
    && substring 0 (stringLength prefix) value == prefix;

  jobIds = map (job: job.id) jobs;
  jobMap = listToAttrs (map (job: {
    name = job.id;
    value = job;
  }) jobs);

  normalizeGroup =
    groupId:
    let
      raw = parallelGroups.${groupId};
      explicitJobs = raw.jobs or null;
      jobPrefix = raw.jobPrefix or null;
      hasExplicitJobs = explicitJobs != null;
      hasJobPrefix = jobPrefix != null;
      memberIds =
        if hasExplicitJobs then
          explicitJobs
        else if hasJobPrefix then
          filter (id: hasPrefix jobPrefix id) jobIds
        else
          [ ];
      members = map (id: jobMap.${id}) memberIds;
      shared = if members == [ ] then null else head members;
      groupJobId = "group-${groupId}";
      checkJobId = "${groupJobId}-checks";
      name = raw.name or groupId;
    in
    if !validId groupId then
      fail "GitHub parallel group `${groupId}` must use a simple identifier"
    else if !isAttrs raw then
      fail "GitHub parallel group `${groupId}` must be an attribute set"
    else if hasExplicitJobs == hasJobPrefix then
      fail "GitHub parallel group `${groupId}` must define exactly one of jobs or jobPrefix"
    else if hasExplicitJobs && (!isList explicitJobs || !(all isString explicitJobs)) then
      fail "GitHub parallel group `${groupId}` jobs must be a list of stage IDs"
    else if hasJobPrefix && (!isString jobPrefix || jobPrefix == "") then
      fail "GitHub parallel group `${groupId}` jobPrefix must be a non-empty string"
    else if !isString name then
      fail "GitHub parallel group `${groupId}` name must be a string"
    else if length memberIds < 2 then
      fail "GitHub parallel group `${groupId}` must match at least two CI stages"
    else if !(all (id: elem id jobIds) memberIds) then
      fail "GitHub parallel group `${groupId}` references an unknown CI stage"
    else if elem groupJobId jobIds || elem checkJobId jobIds then
      fail "GitHub parallel group `${groupId}` conflicts with an existing CI stage ID"
    else if !(all (job: length job.commands == 1) members) then
      fail "GitHub parallel group `${groupId}` only supports stages with one command"
    else if !(all (job: job.runner == shared.runner) members) then
      fail "GitHub parallel group `${groupId}` requires one runner across all stages"
    else if !(all (job: job.timeout == shared.timeout) members) then
      fail "GitHub parallel group `${groupId}` requires one timeout across all stages"
    else if !(all (job: job.needs == shared.needs) members) then
      fail "GitHub parallel group `${groupId}` requires identical dependencies across all stages"
    else if !(all (job: job.env == shared.env) members) then
      fail "GitHub parallel group `${groupId}` requires identical environment metadata across all stages"
    else if !(all (job: (job.cache or null) == (shared.cache or null)) members) then
      fail "GitHub parallel group `${groupId}` requires identical cache metadata across all stages"
    else if (shared.cache or null) != null then
      fail "GitHub parallel group `${groupId}` requires cache = false to avoid concurrent cache writes"
    else
      {
        inherit
          groupId
          groupJobId
          checkJobId
          memberIds
          members
          name
          shared
          ;
      };

  groupIds = attrNames parallelGroups;
  groups = map normalizeGroup groupIds;
  groupedIds = concatLists (map (group: group.memberIds) groups);
  generatedGroupJobIds = concatLists (map (group: [
    group.groupJobId
    group.checkJobId
  ]) groups);

  groupsUnique =
    all (
      id: length (filter (candidate: candidate == id) groupedIds) == 1
    ) groupedIds;

  generatedGroupJobIdsUnique =
    all (
      id: length (filter (candidate: candidate == id) generatedGroupJobIds) == 1
    ) generatedGroupJobIds;

  groupsAreLeaves = all (
    job:
    if elem job.id groupedIds then
      true
    else if any (need: elem need groupedIds) job.needs then
      fail "GitHub parallel groups currently require grouped stages to be CI leaves"
    else
      true
  ) jobs;

  groupsDoNotDependOnMembers = all (
    group:
    if any (need: elem need groupedIds) group.shared.needs then
      fail "GitHub parallel groups cannot depend on another grouped stage"
    else
      true
  ) groups;

  groupsValid =
    if !isAttrs parallelGroups then
      fail "GitHub parallelGroups must be an attribute set"
    else if !groupsUnique then
      fail "a CI stage may belong to only one GitHub parallel group"
    else if !generatedGroupJobIdsUnique then
      fail "GitHub parallel groups generate conflicting workflow job IDs"
    else
      groupsAreLeaves && groupsDoNotDependOnMembers;

  ungroupedJobs = filter (job: !(elem job.id groupedIds)) jobs;

  renderEnvLines =
    env:
    let
      names = attrNames env;
    in
    if names == [ ] then
      [ ]
    else
      [ "        env:" ] ++ map (name: "          ${name}: ${yaml env.${name}}") names;

  renderMatrixEnvLines =
    env:
    [ "        env:" ]
    ++ map (name: "          ${name}: ${yaml env.${name}}") (attrNames env)
    ++ [
      "          PHENIX_CI_INVOCATION: \${{ matrix.invocation }}"
      "          PHENIX_CI_OUTPUT: \${{ matrix.output }}"
    ];

  renderNeedsLines =
    needs: if needs == [ ] then [ ] else [ "    needs:" ] ++ map (need: "      - ${need}") needs;

  renderCacheLines =
    cache:
    if cache == null then
      [ ]
    else
      [
        "      - uses: ${cacheAction} # v4"
        "        with:"
        "          path: |"
      ]
      ++ map (path: "            ${path}") cache.paths
      ++ [ "          key: ${yaml cache.key}" ]
      ++ (
        if (cache.restoreKeys or [ ]) == [ ] then
          [ ]
        else
          [ "          restore-keys: |" ] ++ map (key: "            ${key}") cache.restoreKeys
      )
      ++ [ "" ];

  renderStepLines =
    job: command:
    let
      scopedOutput = scopeOutputName {
        inherit outputName;
        path = command.path;
      };
      invocation = toJSON {
        command = command.id;
        source = {
          type = "github-actions";
          job = job.id;
        };
      };
      runCommand = "printf '%s\\n' ${shellQuote invocation} | nix run --quiet .#${scopedOutput} -- invoke";
    in
    [ "      - name: ${yaml command.name}" ]
    ++ renderEnvLines job.env
    ++ [
      "        run: ${yaml runCommand}"
      ""
    ];

  renderCleanLines = [
    "      - name: Repository remains clean"
    "        if: \${{ !cancelled() }}"
    "        run: |"
    "          set -euo pipefail"
    "          git diff --exit-code"
    "          test -z \"$(git status --porcelain=v1 --untracked-files=all)\""
    ""
  ];

  renderSetupLines = job:
    [
      "    runs-on: ${yaml job.runner}"
      "    timeout-minutes: ${toString job.timeout}"
    ]
    ++ renderNeedsLines job.needs
    ++ [
      "    steps:"
      "      - uses: ${checkoutAction} # v5"
      ""
      "      - uses: ${installNixAction} # v31"
      "        with:"
      "          github_access_token: \${{ secrets.GITHUB_TOKEN }}"
      "          extra_nix_config: |"
      "            experimental-features = nix-command flakes"
      "            accept-flake-config = true"
      "            max-jobs = auto"
      ""
    ]
    ++ renderCacheLines (job.cache or null);

  renderJobLines =
    job:
    [
      "  ${job.id}:"
      "    if: github.event_name != 'pull_request' || github.event.pull_request.draft == false"
      "    name: ${yaml job.name}"
    ]
    ++ renderSetupLines job
    ++ concatLists (map (renderStepLines job) job.commands)
    ++ (if clean then renderCleanLines else [ ])
    ++ [ "" ];

  matrixEntry =
    job:
    let
      command = head job.commands;
      scopedOutput = scopeOutputName {
        inherit outputName;
        path = command.path;
      };
      invocation = toJSON {
        command = command.id;
        source = {
          type = "github-actions";
          job = job.id;
        };
      };
    in
    {
      id = job.id;
      label = command.name;
      output = scopedOutput;
      inherit invocation;
    };

  renderMatrixEntryLines =
    entry:
    [
      "          - id: ${yaml entry.id}"
      "            label: ${yaml entry.label}"
      "            output: ${yaml entry.output}"
      "            invocation: ${yaml entry.invocation}"
    ];

  renderGroupLines =
    group:
    let
      entries = map matrixEntry group.members;
      job = group.shared;
    in
    [
      "  ${group.groupJobId}:"
      "    if: github.event_name != 'pull_request' || github.event.pull_request.draft == false"
      "    name: ${yaml "${group.name} / \${{ matrix.label }}"}"
      "    strategy:"
      "      fail-fast: false"
      "      matrix:"
      "        include:"
    ]
    ++ concatLists (map renderMatrixEntryLines entries)
    ++ renderSetupLines job
    ++ [ "      - name: \${{ matrix.label }}" ]
    ++ renderMatrixEnvLines job.env
    ++ [
      "        run: |"
      "          printf '%s\\n' \"$PHENIX_CI_INVOCATION\" | nix run --quiet \".#$PHENIX_CI_OUTPUT\" -- invoke"
      ""
    ]
    ++ (if clean then renderCleanLines else [ ])
    ++ [ "" ];

  renderGroupCheckLines =
    group:
    [
      "  ${group.checkJobId}:"
      "    if: always() && (github.event_name != 'pull_request' || github.event.pull_request.draft == false)"
      "    name: ${yaml group.name}"
      "    needs:"
      "      - ${group.groupJobId}"
      "    runs-on: ubuntu-latest"
      "    steps:"
      "      - name: Verify all grouped scenarios passed"
      "        run: |"
      "          set -euo pipefail"
      "          [[ '\${{ needs.${group.groupJobId}.result }}' == success ]]"
      ""
    ];

  gateJobIds = map (job: job.id) ungroupedJobs ++ map (group: group.checkJobId) groups;
  gateNeedLines = map (id: "      - ${id}") gateJobIds;
  gateTestLines = map (id: "          [[ '\${{ needs.${id}.result }}' == success ]]") gateJobIds;

  workflowLines = [
    "# Generated by phenix-flake-ci. Edit the Nix maintenance declaration, not this file."
    "name: ${yaml workflowName}"
    ""
    "on:"
    "  pull_request:"
    "  push:"
    "    branches:"
    "      - ${yaml mainBranch}"
    "  workflow_dispatch:"
    ""
    "permissions:"
    "  contents: read"
    ""
    "concurrency:"
    "  group: ci-\${{ github.workflow }}-\${{ github.ref }}"
    "  cancel-in-progress: true"
    ""
    "jobs:"
  ]
  ++ concatLists (map renderJobLines ungroupedJobs)
  ++ concatLists (map renderGroupLines groups)
  ++ concatLists (map renderGroupCheckLines groups)
  ++ [
    "  checks:"
    "    if: always() && (github.event_name != 'pull_request' || github.event.pull_request.draft == false)"
    "    name: ${yaml gateName}"
    "    needs:"
  ]
  ++ gateNeedLines
  ++ [
    "    runs-on: ubuntu-latest"
    "    steps:"
    "      - name: Verify all maintenance jobs passed"
    "        run: |"
    "          set -euo pipefail"
  ]
  ++ gateTestLines;
in
assert groupsValid;
if !validOutputName then
  fail "GitHub outputName must be a simple flake output identifier"
else if jobs == [ ] then
  fail "cannot render a GitHub workflow without CI jobs"
else
  joinLines workflowLines + "\n"
