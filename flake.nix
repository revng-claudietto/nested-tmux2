{
  description = "Declarative terminal trees with nested tmux rows and SSH";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          package = pkgs.python3Packages.buildPythonApplication {
            pname = "nested-tmux";
            version = "0.1.0";
            pyproject = true;
            src = pkgs.lib.cleanSourceWith {
              src = ./.;
              filter =
                path: type:
                !(builtins.elem (baseNameOf path) [
                  ".git"
                  ".agents"
                  ".codex"
                  "__pycache__"
                  ".pytest_cache"
                  ".ruff_cache"
                  ".venv"
                  "build"
                  "dist"
                  "result"
                  "demo-result"
                ])
                && !(pkgs.lib.hasSuffix ".egg-info" (baseNameOf path))
                && pkgs.lib.cleanSourceFilter path type;
            };
            build-system = [ pkgs.python3Packages.setuptools ];
            dependencies = [ pkgs.python3Packages.pyyaml ];
            nativeCheckInputs = [
              pkgs.tmux
              pkgs.openssh
            ];
            checkPhase = ''
              runHook preCheck
              export NESTED_TMUX_TEST_NSS_WRAPPER="${pkgs.nss_wrapper}/lib/libnss_wrapper.so"
              export NESTED_TMUX_TEST_BINARY="$out/bin/nested-tmux"
              python3 tests/run-local.py
              runHook postCheck
            '';
            pythonImportsCheck = [ "nested_tmux.cli" ];
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              (pkgs.lib.makeBinPath [
                pkgs.tmux
                pkgs.openssh
              ])
            ];
            meta = {
              description = "Declarative tmux trees with socket-based navigation";
              mainProgram = "nested-tmux";
              license = pkgs.lib.licenses.mit;
              platforms = systems;
            };
          };
        in
        {
          default = package;
          nested-tmux = package;
          demo = import ./demo { inherit pkgs package; };
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/nested-tmux";
        };
      });

      checks = forAllSystems (system: {
        package-and-tests = self.packages.${system}.default;
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: [
                ps.pyyaml
                ps.setuptools
              ]))
              pkgs.tmux
              pkgs.openssh
              pkgs.ruff
              pkgs.nixfmt
            ];
          };
        }
      );

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
