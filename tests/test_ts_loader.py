"""Dynamic tree-sitter grammar discovery (graphify/ts_loader.py).

Discovery is exercised with a tiny compiled stub that exports the standard
``tree_sitter_<name>`` C symbol (the stub's language pointer is never handed to
tree-sitter). Loading a real grammar end-to-end is covered by
tests/test_tags_extractor.py whenever Cangjie/Nix grammars are available.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphify import ts_loader

_CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
pytestmark = pytest.mark.skipif(_CC is None, reason="no C compiler to build a stub parser")

_STUB_C = "const void *tree_sitter_stublang(void) { static int x; return &x; }\n"


@pytest.fixture(scope="module")
def stub_library(tmp_path_factory) -> Path:
    assert _CC
    work = tmp_path_factory.mktemp("stub")
    src = work / "stub.c"
    src.write_text(_STUB_C)
    out = work / ("stub.dylib" if sys.platform == "darwin" else "stub.so")
    subprocess.run([_CC, "-shared", "-fPIC", "-o", str(out), str(src)], check=True)
    return out


@pytest.fixture
def grammar_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(ts_loader.ENV_VAR, str(tmp_path))
    ts_loader._find_cached.cache_clear()
    yield tmp_path
    ts_loader._find_cached.cache_clear()


def _place(stub_library: Path, grammar_dir: Path, relpath: str) -> Path:
    dest = grammar_dir / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(stub_library, dest)
    return dest


@pytest.mark.parametrize("relpath", [
    "stublang.so",             # flat directory
    "stublang.dylib",
    "parser/stublang.so",      # neovim runtime layout
    "tree-sitter-stublang.so",
    "parser",                  # nixpkgs buildGrammar output: matched by exported symbol
])
def test_grammar_found_in_every_supported_layout(stub_library, grammar_dir, relpath):
    placed = _place(stub_library, grammar_dir, relpath)
    found = ts_loader.find_grammar("stublang")
    assert found is not None and found[0] == placed


def test_library_without_the_requested_symbol_is_not_a_match(stub_library, grammar_dir):
    _place(stub_library, grammar_dir, "parser")
    assert ts_loader.find_grammar("not_a_language") is None
    with pytest.raises(ImportError):
        ts_loader.load_language("not_a_language")


def test_first_search_directory_wins(stub_library, tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    placed = _place(stub_library, first, "stublang.so")
    _place(stub_library, second, "stublang.so")
    monkeypatch.setenv(ts_loader.ENV_VAR, f"{first}{os.pathsep}{second}")
    ts_loader._find_cached.cache_clear()
    found = ts_loader.find_grammar("stublang")
    assert found is not None and found[0] == placed
    ts_loader._find_cached.cache_clear()


def test_no_search_path_finds_nothing(monkeypatch):
    monkeypatch.delenv(ts_loader.ENV_VAR, raising=False)
    ts_loader._find_cached.cache_clear()
    assert ts_loader.find_grammar("stublang") is None


def test_finder_serves_module_with_language_variants(stub_library, grammar_dir):
    _place(stub_library, grammar_dir, "stublang.so")
    finder = ts_loader._DynamicGrammarFinder()
    spec = finder.find_spec("tree_sitter_stublang")
    assert spec is not None
    module = finder.create_module(spec)
    assert type(module.language()).__name__ == "PyCapsule"
    assert type(module.language_stublang()).__name__ == "PyCapsule"
    assert not hasattr(module, "language_missing")
    assert not hasattr(module, "unrelated")


def test_finder_ignores_non_grammar_modules(stub_library, grammar_dir):
    _place(stub_library, grammar_dir, "stublang.so")
    finder = ts_loader._DynamicGrammarFinder()
    assert finder.find_spec("tree_sitter") is None
    assert finder.find_spec("tree_sitter_stublang.sub") is None
    assert finder.find_spec("tree_sitter_other") is None


def test_find_query_next_to_single_grammar_parser(stub_library, grammar_dir):
    _place(stub_library, grammar_dir, "parser")
    (grammar_dir / "queries").mkdir()
    (grammar_dir / "queries" / "tags.scm").write_text("; tags\n")
    assert ts_loader.find_query("stublang") == grammar_dir / "queries" / "tags.scm"


def test_find_query_in_neovim_runtime_layout(stub_library, grammar_dir):
    _place(stub_library, grammar_dir, "parser/stublang.so")
    (grammar_dir / "queries" / "stublang").mkdir(parents=True)
    (grammar_dir / "queries" / "stublang" / "tags.scm").write_text("; tags\n")
    assert ts_loader.find_query("stublang") == grammar_dir / "queries" / "stublang" / "tags.scm"
