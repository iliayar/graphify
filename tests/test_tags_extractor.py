"""Tags-query driven extractor (Cangjie, Nix).

These grammars are not Python dependencies; the tests run when a compiled
grammar is discoverable (``GRAPHIFY_TS_GRAMMAR_PATH``) and skip otherwise.
"""
from __future__ import annotations

import importlib.util

import pytest

import graphify.extractors  # noqa: F401  (installs the dynamic grammar finder)
from graphify.extract import _get_extractor
from graphify.extractors.base import _make_id
from graphify.extractors.tags import extract_tags

needs_cangjie = pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_cangjie") is None,
    reason="tree-sitter-cangjie grammar not available",
)
needs_nix = pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_nix") is None,
    reason="tree-sitter-nix grammar not available",
)


def _edges(result, relation):
    labels = {n["id"]: n["label"] for n in result["nodes"]}
    return {(labels[e["source"]], labels[e["target"]])
            for e in result["edges"] if e["relation"] == relation}


CANGJIE = """\
package demo

import std.collection.*
import other.pkg.Thing

interface Shape {
    func area(): Float64
}

class Base <: Shape {
    func area(): Float64 { 0.0 }
}

class Circle <: Base {
    func area(): Float64 {
        return helper(1.0) * this.scale()
    }
    func scale(): Float64 { 2.0 }
}

extend Circle <: Printable {
    func show(): Unit { println("x") }
}

func helper(x: Float64): Float64 { return x }

main(): Int64 {
    let c = Circle()
    return 0
}
"""


@needs_cangjie
def test_cangjie_symbols_and_members(tmp_path):
    f = tmp_path / "shapes.cj"
    f.write_text(CANGJIE)
    r = extract_tags(f)
    assert "error" not in r
    labels = {n["label"] for n in r["nodes"]}
    assert {"Shape", "Base", "Circle", "helper()", ".scale()", ".show()"} <= labels
    methods = _edges(r, "method")
    assert ("Circle", ".scale()") in methods
    # `extend Circle` members attach to Circle's own node, not the file.
    assert ("Circle", ".show()") in methods


@needs_cangjie
def test_cangjie_supertypes_split_class_and_interface(tmp_path):
    f = tmp_path / "shapes.cj"
    f.write_text(CANGJIE)
    r = extract_tags(f)
    assert ("Circle", "Base") in _edges(r, "inherits")
    # A local interface in the first supertype slot is still `implements`.
    assert ("Base", "Shape") in _edges(r, "implements")
    assert ("Circle", "Printable") in _edges(r, "implements")


@needs_cangjie
def test_cangjie_imports_and_calls(tmp_path):
    f = tmp_path / "shapes.cj"
    f.write_text(CANGJIE)
    r = extract_tags(f)
    imported = {t for _s, t in _edges(r, "imports")}
    assert {"std.collection", "other.pkg.Thing"} <= imported
    calls = _edges(r, "calls")
    assert (".area()", "helper()") in calls
    assert (".area()", ".scale()") in calls  # this.scale() resolves within the class
    assert ("shapes.cj", "Circle") in calls  # `Circle()` in main constructs the class
    # An unresolved plain call is left for the cross-file resolver.
    assert any(c["callee"] == "println" for c in r["raw_calls"])


@needs_nix
def test_nix_functions_calls_and_imports(tmp_path):
    (tmp_path / "other.nix").write_text("{ pkgs }: pkgs.hello\n")
    f = tmp_path / "default.nix"
    f.write_text(
        "{ pkgs, ... }:\n"
        "let\n"
        "  helper = x: x + 1;\n"
        "  other = import ./other.nix { inherit pkgs; };\n"
        "in {\n"
        "  a = helper 1;\n"
        "  b = pkgs.callPackage ./pkg.nix { };\n"
        "}\n"
    )
    r = extract_tags(f)
    assert "error" not in r
    assert "helper()" in {n["label"] for n in r["nodes"]}
    assert ("default.nix", "helper()") in _edges(r, "calls")
    # `import ./other.nix` points at the real file node, not a stub.
    imp = [e for e in r["edges"] if e["relation"] == "imports_from"]
    assert [e["target"] for e in imp] == [_make_id(str(tmp_path / "other.nix"))]
    # `import` is not a call; a member call keeps its member flag.
    assert not any(c["callee"] == "import" for c in r["raw_calls"])
    assert any(c["callee"] == "callPackage" and c["is_member_call"] for c in r["raw_calls"])


def test_suffixes_dispatch_to_the_tags_extractor(tmp_path):
    for name in ("x.cj", "x.nix"):
        assert _get_extractor(tmp_path / name) is extract_tags


def test_missing_grammar_reports_not_installed(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAPHIFY_TS_GRAMMAR_PATH", raising=False)
    from graphify.extractors import tags

    monkeypatch.setitem(tags.TAGS_LANGUAGES, ".zzz", "zzz_no_such_grammar")
    f = tmp_path / "a.zzz"
    f.write_text("x")
    r = extract_tags(f)
    # "not installed" is the marker extract() turns into a missing-dependency warning.
    assert "not installed" in r["error"]
