"""ACP as the default backend, and remembering a backend chosen by hand.

Resolution order under test:

* ``detect_backend()``: saved choice (if usable) -> ACP agent -> API keys
* ACP agent command: ``GRAPHIFY_ACP_COMMAND`` -> saved ``acp_command`` -> built-in
  default (``omp acp``; replaced here by the fake agent through
  ``GRAPHIFY_ACP_DEFAULT_COMMAND``, which conftest empties for every test)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import graphify.__main__ as mainmod
from graphify import acp, backend_config, llm

AGENT = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"


@pytest.fixture
def fake_agent(monkeypatch):
    """The fake ACP agent's command line; the agent replies with a fixed extraction."""
    monkeypatch.setenv("FAKE_ACP_MODE", "extraction")
    llm._warned_unusable_saved.clear()
    acp.shutdown()
    yield f'"{sys.executable}" "{AGENT}"'
    acp.shutdown()


def test_acp_is_the_default_when_the_builtin_agent_is_available(fake_agent, monkeypatch):
    monkeypatch.setenv(acp.ENV_DEFAULT_COMMAND, fake_agent)
    assert acp.configured_command() == (fake_agent, "built-in default")
    assert llm.detect_backend() == "acp"


def test_no_default_agent_falls_back_to_api_keys(monkeypatch):
    monkeypatch.setenv(acp.ENV_DEFAULT_COMMAND, "")
    assert llm.detect_backend() is None
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert llm.detect_backend() == "gemini"


def test_missing_default_agent_binary_falls_back_to_api_keys(monkeypatch):
    monkeypatch.setenv(acp.ENV_DEFAULT_COMMAND, "no-such-acp-agent-binary acp")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert llm.detect_backend() == "gemini"


def test_an_available_agent_is_chosen_over_api_keys(fake_agent, monkeypatch):
    monkeypatch.setenv(acp.ENV_DEFAULT_COMMAND, fake_agent)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert llm.detect_backend() == "acp"


def test_agent_command_precedence_env_then_saved_then_default(fake_agent, monkeypatch):
    monkeypatch.setenv(acp.ENV_DEFAULT_COMMAND, "from-default")
    assert acp.configured_command() == ("from-default", "built-in default")
    backend_config.save(acp_command="from-saved")
    assert acp.configured_command()[0] == "from-saved"
    monkeypatch.setenv(acp.ENV_COMMAND, "from-env")
    assert acp.configured_command() == ("from-env", f"${acp.ENV_COMMAND}")


def test_saved_backend_beats_the_acp_default(fake_agent, monkeypatch):
    monkeypatch.setenv(acp.ENV_DEFAULT_COMMAND, fake_agent)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    backend_config.save(backend="gemini")
    assert llm.detect_backend() == "gemini"


def test_unusable_saved_backend_is_ignored_with_a_warning(fake_agent, monkeypatch, capsys):
    monkeypatch.setenv(acp.ENV_DEFAULT_COMMAND, fake_agent)
    backend_config.save(backend="gemini")  # no GEMINI_API_KEY in this environment
    assert llm.detect_backend() == "acp"
    assert "saved backend 'gemini' is not usable" in capsys.readouterr().err


def test_saved_model_applies_only_to_its_own_backend():
    backend_config.save(backend="gemini", model="gemini-custom")
    assert llm._default_model_for_backend("gemini") == "gemini-custom"
    assert llm._default_model_for_backend("openai") == llm.BACKENDS["openai"]["default_model"]


def test_changing_backend_drops_a_model_chosen_for_the_old_one():
    backend_config.save(backend="gemini", model="gemini-custom")
    backend_config.save(backend="claude")
    assert backend_config.load() == {"backend": "claude"}
    assert backend_config.save(backend="claude") is False  # unchanged -> no rewrite


def test_unreadable_config_reads_as_empty():
    path = backend_config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert backend_config.load() == {}
    path.write_text(json.dumps({"backend": 3, "unknown": "x", "model": "m"}), encoding="utf-8")
    assert backend_config.load() == {"model": "m"}


# ── CLI: a manually supplied choice is saved and reused ───────────────────────

def _docs_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# Alpha\nAlpha references Beta.\n", encoding="utf-8")
    return corpus


def _extract(monkeypatch, capsys, corpus: Path, out: Path, *flags: str) -> str:
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys, "argv",
        ["graphify", "extract", str(corpus), "--out", str(out), "--no-cluster", *flags],
    )
    try:
        mainmod.main()
    except SystemExit as exc:
        assert exc.code in (None, 0)
    captured = capsys.readouterr()
    return captured.out + captured.err


def _labels(out: Path) -> set[str]:
    graph = json.loads((out / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    return {n["label"] for n in graph["nodes"]}


def test_manual_backend_and_agent_are_saved_and_reused_without_flags(
    fake_agent, monkeypatch, capsys, tmp_path,
):
    corpus = _docs_corpus(tmp_path)

    first = _extract(monkeypatch, capsys, corpus, tmp_path / "out1",
                     "--backend", "acp", "--acp-command", fake_agent)
    assert "via acp" in first and "saved backend=acp" in first
    assert _labels(tmp_path / "out1") == {"Alpha", "Beta"}
    assert backend_config.load() == {"backend": "acp", "acp_command": fake_agent}

    # No flags, no env, no built-in default: only the saved choice can find the agent.
    monkeypatch.delenv(acp.ENV_COMMAND, raising=False)
    second = _extract(monkeypatch, capsys, corpus, tmp_path / "out2")
    assert "via acp" in second and "saved backend" not in second
    assert _labels(tmp_path / "out2") == {"Alpha", "Beta"}


def test_acp_command_alone_implies_the_acp_backend(fake_agent, monkeypatch, capsys, tmp_path):
    _extract(monkeypatch, capsys, _docs_corpus(tmp_path), tmp_path / "out",
             "--acp-command", fake_agent)
    assert backend_config.load()["backend"] == "acp"


def test_no_save_leaves_the_saved_choice_untouched(fake_agent, monkeypatch, capsys, tmp_path):
    _extract(monkeypatch, capsys, _docs_corpus(tmp_path), tmp_path / "out",
             "--backend", "acp", "--acp-command", fake_agent, "--no-save")
    assert not backend_config.config_path().exists()


def test_an_agent_that_cannot_start_is_an_error_and_is_not_saved(
    monkeypatch, capsys, tmp_path,
):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(
        mainmod.sys, "argv",
        ["graphify", "extract", str(_docs_corpus(tmp_path)), "--out", str(tmp_path / "out"),
         "--acp-command", "no-such-acp-agent-binary"],
    )
    with pytest.raises(SystemExit) as exc:
        mainmod.main()
    assert exc.value.code == 1
    assert not backend_config.config_path().exists()
