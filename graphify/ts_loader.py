"""Dynamic tree-sitter grammar discovery.

Extractors historically did ``import tree_sitter_<lang>`` and every grammar had
to be a declared dependency. This module lets a grammar be supplied as a plain
compiled parser library instead, found at runtime, so adding a language never
needs a new package dependency (and works with grammars that are not on PyPI,
e.g. Cangjie, or that a distribution such as Nix ships as a bare ``parser``).

How it works: :func:`install` appends a meta-path finder that is consulted only
after the normal import system failed. For ``import tree_sitter_<name>`` it
searches the grammar directories for a shared library exporting the
``tree_sitter_<name>`` C symbol and, if one exists, returns a synthetic module
with the same surface as the PyPI grammar packages: ``language()`` and
``language_<variant>()`` returning a ``tree_sitter.Language``-compatible
capsule. An installed PyPI package therefore always wins, and every existing
``import tree_sitter_x as ts; Language(ts.language())`` call site works
unchanged for a dynamically supplied grammar.

Search directories come from the ``GRAPHIFY_TS_GRAMMAR_PATH`` environment
variable (``os.pathsep``-separated). Within a directory ``D`` the grammar
``<name>`` is looked up as, in order::

    D/<name>.{so,dylib,dll}               flat directory of parsers
    D/parser/<name>.{so,dylib,dll}        neovim-style runtime directory
    D/lib/<name>.{so,dylib,dll}
    D/tree-sitter-<name>.* / D/libtree-sitter-<name>.*   (and ``_`` spellings)
    D/parser                              single-grammar directory (nixpkgs
                                          ``tree-sitter.buildGrammar`` output)

``<name>`` is tried with ``_`` and with ``-``. A candidate is only accepted if
it exports the ``tree_sitter_<name>`` symbol, so a bare ``parser`` file is
matched by content, not by guesswork.
"""
from __future__ import annotations

import ctypes
import functools
import importlib.abc
import importlib.machinery
import os
import sys
import types
from pathlib import Path

ENV_VAR = "GRAPHIFY_TS_GRAMMAR_PATH"

_MODULE_PREFIX = "tree_sitter_"
_LIB_SUFFIXES = (".so", ".dylib", ".dll")
_FILE_PREFIXES = ("", "tree-sitter-", "tree_sitter_", "libtree-sitter-", "libtree_sitter_")
_SUBDIRS = ("", "parser", "lib")
_CAPSULE_NAME = b"tree_sitter.Language"


def grammar_search_dirs() -> list[Path]:
    """Directories searched for parser libraries, in priority order."""
    raw = os.environ.get(ENV_VAR, "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def _candidate_files(directory: Path, name: str):
    stems = dict.fromkeys((name, name.replace("_", "-"), name.replace("-", "_")))
    for sub in _SUBDIRS:
        base = directory / sub if sub else directory
        for prefix in _FILE_PREFIXES:
            for stem in stems:
                for suffix in _LIB_SUFFIXES:
                    yield base / f"{prefix}{stem}{suffix}"
    # nixpkgs buildGrammar output: a single extension-less `parser` file.
    yield directory / "parser"


@functools.lru_cache(maxsize=None)
def _open_library(path: str) -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL(path)
    except OSError:
        return None


def _symbol(lib: ctypes.CDLL, name: str):
    func = getattr(lib, f"{_MODULE_PREFIX}{name}", None)
    if func is None:
        return None
    func.restype = ctypes.c_void_p
    func.argtypes = []
    return func


@functools.lru_cache(maxsize=None)
def _find_cached(name: str, search_key: tuple[str, ...]):
    for directory in map(Path, search_key):
        for candidate in _candidate_files(directory, name):
            if not candidate.is_file():
                continue
            lib = _open_library(str(candidate))
            if lib is None:
                continue
            func = _symbol(lib, name)
            if func is not None:
                return candidate, func
    return None


def find_grammar(name: str):
    """Locate grammar ``name`` -> ``(library_path, c_function)`` or ``None``."""
    key = tuple(str(d) for d in grammar_search_dirs())
    return _find_cached(name, key) if key else None


def find_query(name: str, filename: str = "tags.scm") -> Path | None:
    """Locate a query file shipped alongside grammar ``name``.

    Checks ``<dir>/queries/<filename>`` (single-grammar dir such as a nixpkgs
    grammar output, next to its ``parser``) and ``<dir>/queries/<name>/<filename>``
    (neovim-style runtime dir), for the directory that provides the parser
    and for every search directory.
    """
    found = find_grammar(name)
    dirs: list[Path] = []
    if found is not None:
        lib_dir = found[0].parent
        dirs.extend([lib_dir, lib_dir.parent] if lib_dir.name in ("parser", "lib") else [lib_dir])
    dirs.extend(grammar_search_dirs())
    stems = dict.fromkeys((name, name.replace("_", "-"), name.replace("-", "_")))
    for directory in dirs:
        for candidate in (directory / "queries" / filename,
                          *(directory / "queries" / stem / filename for stem in stems)):
            if candidate.is_file():
                return candidate
    return None


def _language_capsule(func):
    ptr = func()
    if not ptr:
        raise RuntimeError("tree-sitter grammar returned a null language pointer")
    new = ctypes.pythonapi.PyCapsule_New
    new.restype = ctypes.py_object
    new.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    return new(ptr, _CAPSULE_NAME, None)


def load_language(name: str):
    """``tree_sitter.Language`` for dynamically discovered grammar ``name``.

    Raises ``ImportError`` when no search directory provides it.
    """
    from tree_sitter import Language

    found = find_grammar(name)
    if found is None:
        raise ImportError(
            f"tree-sitter grammar {name!r} not found; set {ENV_VAR} to a directory "
            f"containing a compiled parser for it"
        )
    return Language(_language_capsule(found[1]))


class _DynamicGrammarModule(types.ModuleType):
    """Stand-in for a ``tree_sitter_<name>`` PyPI package."""

    def __init__(self, fullname: str, name: str):
        super().__init__(fullname)
        self._grammar = name
        self.__file__ = None

    def __getattr__(self, attr: str):
        if attr == "language":
            variant = self._grammar
        elif attr.startswith("language_") and len(attr) > len("language_"):
            variant = attr[len("language_"):]
        else:
            raise AttributeError(attr)
        found = find_grammar(variant)
        if found is None:
            raise AttributeError(attr)
        func = found[1]
        return lambda: _language_capsule(func)


class _DynamicGrammarFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if "." in fullname or not fullname.startswith(_MODULE_PREFIX):
            return None
        name = fullname[len(_MODULE_PREFIX):]
        if not name or find_grammar(name) is None:
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=False)

    def create_module(self, spec):
        return _DynamicGrammarModule(spec.name, spec.name[len(_MODULE_PREFIX):])

    def exec_module(self, module):
        return None


def install() -> None:
    """Register the dynamic finder (idempotent, lowest import priority)."""
    if not any(isinstance(f, _DynamicGrammarFinder) for f in sys.meta_path):
        sys.meta_path.append(_DynamicGrammarFinder())
