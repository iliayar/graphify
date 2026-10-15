{ lib
, runCommand
, python3Packages
, tree-sitter-grammars
  # Names under `tree-sitter-grammars` (without the `tree-sitter-` prefix) that
  # graphify can load. Add a language here (or pass `extraGrammars`) -- no change
  # to pyproject.toml is needed, graphify finds parsers at runtime.
  #
  # Only grammars whose nixpkgs version passes graphify's extractor tests are
  # listed. Kotlin, Julia, Swift, Objective-C, PowerShell and Groovy are left
  # out: nixpkgs either lacks them or ships a different grammar generation than
  # the PyPI packages the extractors target, so those files would extract worse.
, grammarNames ? [
    "bash" "c" "c-sharp" "cangjie" "commonlisp" "cpp" "elixir" "fortran" "go"
    "hcl" "java" "javascript" "json" "lua" "nix" "ocaml" "ocaml-interface" "php"
    "python" "ruby" "rust" "scala" "solidity" "sql" "tsx" "typescript"
    "verilog" "zig"
  ]
  # Extra grammar derivations, e.g. `[ (pkgs.tree-sitter.buildGrammar { ... }) ]`.
  # Each must come from `tree-sitter.buildGrammar` (has `$out/parser` and `.language`).
, extraGrammars ? [ ]
}:

let
  pyproject = builtins.fromTOML (builtins.readFile ../pyproject.toml);

  # The PyPI `tree-sitter-<lang>` pins in pyproject.toml are replaced by the
  # grammars below, discovered through GRAPHIFY_TS_GRAMMAR_PATH.
  pinnedGrammarPackages = map
    (dep: builtins.head (builtins.match "([A-Za-z0-9._-]+).*" dep))
    (lib.filter (dep: lib.hasPrefix "tree-sitter-" dep) pyproject.project.dependencies);

  grammars =
    map (name: tree-sitter-grammars."tree-sitter-${name}") grammarNames ++ extraGrammars;

  # One flat directory in the layout graphify/ts_loader.py searches:
  #   <language>.so  and  queries/<language>/*.scm
  grammarPath = runCommand "graphify-tree-sitter-grammars" { } ''
    mkdir -p $out/queries
    ${lib.concatMapStringsSep "\n" (g: ''
      ln -s ${g}/parser $out/${g.language}.so
      if [ -d ${g}/queries ]; then ln -s ${g}/queries $out/queries/${g.language}; fi
    '') grammars}
  '';

  runtimeDependencies = with python3Packages; [
    networkx
    numpy
    rapidfuzz
    tree-sitter
  ];
in
python3Packages.buildPythonApplication {
  pname = "graphify";
  inherit (pyproject.project) version;
  pyproject = true;

  src = lib.cleanSourceWith {
    src = lib.cleanSource ../.;
    filter = path: _type:
      let rel = lib.removePrefix (toString ../. + "/") (toString path);
      in !(lib.any (p: rel == p || lib.hasPrefix "${p}/" rel) [ "worked" "docs" "nix" "flake.nix" "flake.lock" ]);
  };

  build-system = [ python3Packages.setuptools ];
  dependencies = runtimeDependencies;

  pythonRemoveDeps = pinnedGrammarPackages;
  # pyproject asks for a newer setuptools than some nixpkgs revisions carry; the
  # build only uses plain setuptools features.
  postPatch = ''
    substituteInPlace pyproject.toml --replace-fail '"setuptools>=83.0.0"' '"setuptools"'
  '';

  makeWrapperArgs = [ "--suffix" "GRAPHIFY_TS_GRAMMAR_PATH" ":" "${grammarPath}" ];

  doCheck = false;
  pythonImportsCheck = [ "graphify" "graphify.extractors.tags" ];

  passthru = { inherit grammarPath runtimeDependencies; };

  meta = {
    description = "Turn a folder of code, docs, papers, images, or videos into a queryable knowledge graph";
    homepage = "https://graphify.com";
    license = lib.licenses.asl20;
    mainProgram = "graphify";
  };
}
