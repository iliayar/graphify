"""Generic extractor driven by tree-sitter tags queries.

A language is supported by (1) a grammar that ``import tree_sitter_<name>``
can resolve -- a PyPI package or a compiled parser found through
``GRAPHIFY_TS_GRAMMAR_PATH`` (graphify/ts_loader.py) -- and (2) a row in
:data:`TAGS_LANGUAGES`. No per-language Python is needed.

The query is the grammar's own ``queries/tags.scm`` (the standard
tree-sitter code-navigation format) unless graphify ships a richer
``graphify/queries/<name>.scm``, which then replaces it. Captures:

``@definition.<kind>``  a symbol. With a sibling ``@name`` capture that is the
                        symbol's name and the definition capture is its whole
                        declaration; without one (``(classDefinition
                        (className) @definition.class)``) the captured node
                        *is* the name and its parent is the declaration.
                        ``function``/``method``/``macro`` are callables;
                        everything else is a named entity. A callable nested
                        in ``class``/``interface``/``struct``/``enum``/
                        ``module``/``trait``/``extension`` is a method.
``@reference.call``     a call site; ``@name`` is the callee. An extra
                        ``@receiver`` capture marks a member call.
``@reference.import``   an import; ``@name`` is the module or path.
``@reference.class`` / ``@reference.implementation``
                        a supertype (``@name``) of the enclosing definition:
                        ``inherits`` / ``implements`` edges.

Everything else in a query (predicates, other captures) is ignored.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

from graphify import ts_loader
from graphify.extractors.base import _file_stem, _make_id, _read_source_bytes, _read_text

# File suffix -> grammar name (the `<name>` of `tree_sitter_<name>`).
TAGS_LANGUAGES: dict[str, str] = {
    ".cj": "cangjie",
    ".nix": "nix",
}

_CALLABLE_KINDS = frozenset({"function", "method", "macro"})
_CONTAINER_KINDS = frozenset({
    "class", "interface", "struct", "enum", "module", "trait", "extension",
})
_REFERENCE_RELATIONS = {
    "reference.class": "inherits",
    "reference.implementation": "implements",
}
_QUERY_DIR = Path(__file__).resolve().parent.parent / "queries"


def _query_source(grammar: str) -> str | None:
    packaged = _QUERY_DIR / f"{grammar}.scm"
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    found = ts_loader.find_query(grammar, "tags.scm")
    return found.read_text(encoding="utf-8") if found else None


def _clean(text: str) -> str:
    return "".join(text.split())


def extract_tags(path: Path) -> dict:
    """Extract symbols, imports, supertypes, and calls using the language's tags query."""
    from graphify.rcfile import effective_suffix

    suffix = effective_suffix(path).lower()
    grammar = TAGS_LANGUAGES.get(suffix)
    if grammar is None:
        return {"nodes": [], "edges": [], "error": f"no tags grammar registered for {suffix!r}"}

    try:
        mod = importlib.import_module(f"tree_sitter_{grammar}")
        from tree_sitter import Language, Parser, Query, QueryCursor
    except ImportError:
        return {"nodes": [], "edges": [],
                "error": f"tree_sitter_{grammar} not installed "
                         f"(provide the grammar via {ts_loader.ENV_VAR})"}

    query_src = _query_source(grammar)
    if query_src is None:
        return {"nodes": [], "edges": [], "error": f"no tags query found for {grammar!r}"}

    try:
        language = Language(mod.language())
        query = Query(language, query_src)
        source = _read_source_bytes(path)
        root = Parser(language).parse(source).root_node
        matches = QueryCursor(query).matches(root)
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({"id": nid, "label": label, "file_type": "code",
                          "source_file": str_path, "source_location": f"L{line}"})

    def add_edge(src: str, tgt: str, relation: str, line: int,
                 context: str | None = None) -> None:
        if (src, tgt, relation) in seen_edges:
            return
        seen_edges.add((src, tgt, relation))
        edge = {"source": src, "target": tgt, "relation": relation,
                "confidence": "EXTRACTED", "source_file": str_path,
                "source_location": f"L{line}", "weight": 1.0}
        if context:
            edge["context"] = context
        edges.append(edge)

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    # ── gather captures ──────────────────────────────────────────────────────
    definitions: list[dict] = []
    references: list[tuple[str, Any, Any]] = []  # (kind, name node, receiver node)
    for _pattern, caps in matches:
        name_nodes = caps.get("name", [])
        for cap, cap_nodes in caps.items():
            if cap.startswith("definition."):
                kind = cap[len("definition."):]
                for node in cap_nodes:
                    if name_nodes:
                        name_node, scope = name_nodes[0], node
                    elif node.parent is not None:
                        name_node, scope = node, node.parent
                    else:
                        continue
                    definitions.append({"kind": kind, "name_node": name_node, "scope": scope})
            elif cap.startswith("reference.") and name_nodes:
                receivers = caps.get("receiver", [])
                references.append((cap, name_nodes[0], receivers[0] if receivers else None))

    definitions.sort(key=lambda d: (d["scope"].start_byte, -d["scope"].end_byte,
                                    d["name_node"].start_byte))
    by_scope: dict[int, dict] = {}
    for d in definitions:
        by_scope.setdefault(d["scope"].id, d)

    def enclosing(node) -> dict | None:
        cur = node.parent
        while cur is not None:
            hit = by_scope.get(cur.id)
            if hit is not None and hit.get("nid"):
                return hit
            cur = cur.parent
        return None

    # ── definitions ──────────────────────────────────────────────────────────
    method_owner: dict[str, str] = {}
    callables: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    for d in definitions:
        name = _clean(_read_text(d["name_node"], source))
        if not name:
            continue
        kind, line = d["kind"], d["name_node"].start_point[0] + 1
        parent = enclosing(d["scope"])
        owner_nid = parent["nid"] if parent else None
        in_container = parent is not None and parent["kind"] in _CONTAINER_KINDS
        is_callable = kind in _CALLABLE_KINDS
        nid = _make_id(owner_nid or stem, name)
        if is_callable:
            label = f".{name}()" if in_container else f"{name}()"
        else:
            label = name
        d["nid"], d["name"] = nid, name
        add_node(nid, label, line)
        if owner_nid and in_container and is_callable:
            add_edge(owner_nid, nid, "method", line)
            method_owner[nid] = owner_nid
        else:
            add_edge(owner_nid or file_nid, nid, "contains", line)
        kinds.setdefault(nid, kind)
        # A type name is callable too (`Circle()` constructs a Circle).
        if is_callable or kind in _CONTAINER_KINDS:
            if nid not in callables.setdefault(name, []):
                callables[name].append(nid)

    # ── references ───────────────────────────────────────────────────────────
    seen_calls: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []
    for cap, name_node, receiver in references:
        name = _clean(_read_text(name_node, source))
        if not name:
            continue
        line = name_node.start_point[0] + 1
        owner = enclosing(name_node)

        if cap == "reference.import":
            target = name.strip("\"'")
            if target.startswith("."):
                resolved = Path(os.path.normpath(path.parent / target))
                if resolved.is_dir():
                    resolved = resolved / f"default{path.suffix}"
                if resolved.is_file():
                    add_edge(file_nid, _make_id(str(resolved)), "imports_from", line,
                             context="import")
                    continue
            imp_nid = _make_id(target)
            add_node(imp_nid, target, line)
            add_edge(file_nid, imp_nid, "imports", line, context="import")

        elif cap in _REFERENCE_RELATIONS:
            if owner is None:
                continue
            base = name.split(".")[-1]
            target_nid = _make_id(stem, base)
            if target_nid not in seen_ids:
                target_nid = _make_id(base)
                if target_nid not in seen_ids:
                    # Declared elsewhere: SOURCELESS stub so the corpus-level
                    # rewire can collapse it onto the real definition.
                    seen_ids.add(target_nid)
                    nodes.append({"id": target_nid, "label": base, "file_type": "code",
                                  "source_file": "", "source_location": "",
                                  "origin_file": str_path})
            relation = _REFERENCE_RELATIONS[cap]
            if (kinds.get(target_nid) == "interface"
                    and kinds.get(owner["nid"]) != "interface"):
                # A type implements (not extends) an interface; Cangjie lists a
                # class's superclass and interfaces together.
                relation = "implements"
            add_edge(owner["nid"], target_nid, relation, line)

        elif cap == "reference.call":
            caller = owner["nid"] if owner else file_nid
            is_member = receiver is not None
            candidates = callables.get(name, [])
            target = None
            if is_member:
                # Only `this.m()` / `self.m()` is decidable inside one file.
                recv = _clean(_read_text(receiver, source))
                own = method_owner.get(caller)
                if own and recv in ("this", "self") and _make_id(own, name) in seen_ids:
                    target = _make_id(own, name)
            elif len(candidates) == 1:
                target = candidates[0]
            if target:
                if target != caller and (caller, target) not in seen_calls:
                    seen_calls.add((caller, target))
                    add_edge(caller, target, "calls", line)
            else:
                raw_calls.append({
                    "caller_nid": caller,
                    "callee": name,
                    "is_member_call": is_member,
                    "source_file": str_path,
                    "source_location": f"L{line}",
                })

    clean_edges = [e for e in edges if e["source"] in seen_ids and
                   (e["target"] in seen_ids or e["relation"] == "imports_from")]
    return {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}
