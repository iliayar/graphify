; Nix -- graphify tags query (see graphify/extractors/tags.py for the capture
; vocabulary). The grammar's own tags.scm only sees calls with two or more
; arguments; this one also sees single-argument calls, `lib.fn x` selections
; and `import ./path`.

; ── definitions: `name = args: body;` ──
(binding
  attrpath: (attrpath) @name
  expression: (function_expression) @definition.function)

; ── imports: `import ./path` / `import <nixpkgs>` ──
(apply_expression
  function: (variable_expression name: (identifier) @_fn)
  argument: [(path_expression) (spath_expression)] @name
  (#eq? @_fn "import")) @reference.import

; ── calls ──
(apply_expression
  function: (variable_expression name: (identifier) @name)
  (#not-eq? @name "import")) @reference.call
(apply_expression
  function: (select_expression
    expression: (_) @receiver
    attrpath: (attrpath (identifier) @name .))) @reference.call
