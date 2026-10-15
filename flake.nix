{
  description = "graphify - turn a folder of code, docs, papers, images, or videos into a queryable knowledge graph";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Not in nixpkgs. Consumed as a plain source tree so the grammar can be built
    # with `tree-sitter.buildGrammar` below.
    tree-sitter-cangjie-src = {
      url = "git+https://gitcode.com/Cangjie-SIG/tree-sitter-cangjie.git";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, tree-sitter-cangjie-src }:
    let
      lib = nixpkgs.lib;
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: lib.genAttrs systems (system: f (import nixpkgs {
        inherit system;
        overlays = [ self.overlays.default ];
      }));
    in
    {
      # `pkgs.graphify` and `pkgs.tree-sitter-grammars.tree-sitter-cangjie`.
      overlays.default = final: prev: {
        tree-sitter-grammars = prev.tree-sitter-grammars // {
          tree-sitter-cangjie = final.tree-sitter.buildGrammar {
            language = "cangjie";
            version = "0-unstable-${tree-sitter-cangjie-src.shortRev or "dirty"}";
            src = tree-sitter-cangjie-src;
            # The scanner's debug logger passes a non-literal format string, which
            # fails under nixpkgs' format hardening.
            patches = [ ./nix/patches/tree-sitter-cangjie-scanner-stderr-logging.patch ];
          };
        };
        graphify = final.callPackage ./nix/package.nix { };
      };

      packages = forAllSystems (pkgs: {
        inherit (pkgs) graphify;
        # The parser directory graphify searches (GRAPHIFY_TS_GRAMMAR_PATH).
        grammars = pkgs.graphify.grammarPath;
        default = pkgs.graphify;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = lib.getExe pkgs.graphify;
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps:
              pkgs.graphify.runtimeDependencies ++ [ ps.pytest ps.setuptools ]))
          ];
          GRAPHIFY_TS_GRAMMAR_PATH = pkgs.graphify.grammarPath;
        };
      });

      # Runs the packaged CLI's extractor on a Cangjie and a Nix file: proves
      # dynamically discovered grammars load inside the wrapped package.
      checks = forAllSystems (pkgs: {
        dynamic-grammars = pkgs.runCommand "graphify-dynamic-grammars-check"
          { nativeBuildInputs = [ pkgs.graphify ]; } ''
          mkdir src && cd src
          cat > a.cj <<'EOF'
          package demo
          class Circle {
              func area(): Float64 { return helper(1.0) }
          }
          func helper(x: Float64): Float64 { return x }
          EOF
          cat > a.nix <<'EOF'
          { pkgs }: let helper = x: x + 1; in { a = helper 1; }
          EOF
          graphify update . >log 2>&1 || { cat log; exit 1; }
          cat log
          ${pkgs.python3}/bin/python - <<'EOF'
          import json, sys
          g = json.load(open("graphify-out/graph.json"))
          labels = {n["label"] for n in g["nodes"]}
          missing = {"Circle", "helper()"} - labels
          if missing:
              sys.exit(f"missing nodes {missing}; got {sorted(labels)}")
          EOF
          touch $out
        '';
      });
    };
}
