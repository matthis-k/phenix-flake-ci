
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/e7a3ca8092b61ff85b6a45bf863ea2b2d6a661b3";
    phenix-flake-ci.url = "path:../..";
  };

  outputs =
    { nixpkgs, phenix-flake-ci, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      ciLib = phenix-flake-ci.lib;
      a = ciLib.testAssert;
      cli = pkgs.writeShellScriptBin "fixture-cli" ''
        case "$1" in
          status)
            printf '{"status":"ready","count":2}\n'
            ;;
          invalid)
            printf 'invalid configuration\n' >&2
            exit 7
            ;;
          init)
            printf 'ready' > state.json
            ;;
          create)
            printf '{"id":"session-created"}\n'
            ;;
          echo-id)
            printf '{"id":"%s"}\n' "$2"
            ;;
          write)
            printf '{"state":"ready","count":2}\n' > state.json
            ;;
          inspect)
            if test -f state.json; then
              printf '{"initialized":true}\n'
            else
              printf '{"initialized":false}\n'
            fi
            ;;
          *)
            exit 12
            ;;
        esac
      '';
      program = "${cli}/bin/fixture-cli";
    in
    {
      checks.${system} = {
        cli = ciLib.mkTest {
          inherit pkgs;
          name = "cli-contract";
          command = {
            inherit program;
            args = [ "status" ];
          };
          expect = {
            exitCode = 0;
            stderr.empty = true;
            stdout.json = {
              "/status" = a.eq "ready";
              "/count" = a.gte 2;
            };
          };
        };
        expected-failure = ciLib.mkTest {
          inherit pkgs;
          name = "expected-failure";
          command = {
            inherit program;
            args = [ "invalid" ];
          };
          expect = {
            exitCode = 7;
            stderr.contains = [ "invalid" "configuration" ];
            stdout.empty = true;
          };
        };
        references = ciLib.mkTest {
          inherit pkgs;
          name = "cross-step-references";
          steps = [
            {
              id = "create";
              command = { inherit program; args = [ "create" ]; };
            }
            {
              id = "echo";
              command = {
                inherit program;
                args = [
                  "echo-id"
                  { ref = { step = "create"; field = "stdout"; json = "/id"; }; }
                ];
              };
              expect.stdout.json."/id" = {
                eq = { ref = { step = "create"; field = "stdout"; json = "/id"; }; };
              };
            }
          ];
        };
        multi-artifact = ciLib.mkTest {
          inherit pkgs;
          name = "multi-artifact";
          command = { inherit program; args = [ "write" ]; };
          expect.files =
            (a.files.required [ "state.json" ])
            // (a.files.absent [ "unexpected.log" ]);
        };
        file-content = ciLib.mkTest {
          inherit pkgs;
          name = "file-content";
          command = { inherit program; args = [ "write" ]; };
          expect.files."state.json" = {
            exists = true;
            text.contains = "ready";
            json."/count" = { gte = 2; };
            size = { gt = 10; };
          };
        };
        lifecycle = ciLib.mkTest {
          inherit pkgs;
          name = "lifecycle";
          steps = [
            {
              id = "init";
              command = { inherit program; args = [ "init" ]; };
            }
            {
              id = "inspect";
              command = { inherit program; args = [ "inspect" ]; };
              expect.stdout.json."/initialized" = true;
            }
          ];
          expect.files."state.json".exists = true;
        };
      } // (ciLib.mkTestCases {
        inherit pkgs;
        namePrefix = "case-";
        defaults.command = { inherit program; };
        cases = {
          matrix-status = {
            command.args = [ "status" ];
            expect.stdout.json."/status" = "ready";
          };
          matrix-invalid = {
            command.args = [ "invalid" ];
            expect = {
              exitCode = 7;
              stderr.contains = "invalid configuration";
            };
          };
        };
      });
      # Deliberately failing derivation used solely to prove that failed
      # assertions cause nix build to fail. Not part of the required checks.
      packages.${system}.assertion-fails = ciLib.mkTest {
        inherit pkgs;
        name = "assertion-fails";
        command = { inherit program; args = [ "status" ]; };
        expect = {
          exitCode = 9;
          stdout.contains = "never-there";
        };
      };
    };
}
