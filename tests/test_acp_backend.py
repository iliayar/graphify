"""The `acp` backend: graphify as an Agent Client Protocol client.

Runs against tests/fixtures/fake_acp_agent.py, a real subprocess speaking
JSON-RPC over stdio, so framing, routing and process handling are exercised for
real rather than mocked.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graphify import acp, llm

AGENT = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"


@pytest.fixture
def agent(monkeypatch):
    """Point GRAPHIFY_ACP_COMMAND at the fake agent; select behaviour with ``agent(mode)``."""
    monkeypatch.delenv(acp.ENV_AUTH_METHOD, raising=False)

    def select(mode: str = "echo") -> None:
        acp.shutdown()
        monkeypatch.setenv("FAKE_ACP_MODE", mode)
        monkeypatch.setenv(acp.ENV_COMMAND, f'"{sys.executable}" "{AGENT}"')

    select()
    yield select
    acp.shutdown()


def test_reply_is_the_agent_message_chunks_only(agent):
    text, stop, name = acp.prompt("hello world", timeout=20)
    assert name == "fake-agent" and stop == "end_turn"
    # thought chunks and tool-call updates are not part of the reply
    assert text.endswith("|hello world")
    assert "thinking" not in text


def test_one_agent_process_serves_many_prompts_in_fresh_sessions(agent):
    first, _, _ = acp.prompt("one", timeout=20)
    second, _, _ = acp.prompt("two", timeout=20)
    pid1, sess1, _ = first.split("|", 2)
    pid2, sess2, _ = second.split("|", 2)
    assert pid1 == pid2
    assert sess1 != sess2


def test_permission_requests_are_rejected(agent):
    agent("permission")
    text, _, _ = acp.prompt("go", timeout=20)
    assert '"optionId": "deny"' in text


def test_client_side_tools_are_not_offered(agent):
    agent("fs")
    text, _, _ = acp.prompt("go", timeout=20)
    assert text == "-32601"  # method not found: no fs capability was advertised


def test_timeout_cancels_the_turn_and_the_connection_stays_usable(agent):
    agent("hang")
    with pytest.raises(subprocess.TimeoutExpired):
        acp.prompt("never finishes", timeout=1)
    # The agent answered session/cancel late; that stray response must not break
    # the next turn on the same process.
    agent_conn = acp._conn
    assert agent_conn is not None and agent_conn._dead is None
    with pytest.raises(subprocess.TimeoutExpired):
        acp.prompt("still never finishes", timeout=1)
    assert acp._conn is agent_conn


def test_agent_crash_reports_stderr_and_next_prompt_restarts_it(agent, monkeypatch):
    agent("die")
    with pytest.raises(acp.AcpError, match="boom"):
        acp.prompt("x", timeout=20)
    monkeypatch.setenv("FAKE_ACP_MODE", "echo")
    text, _, _ = acp.prompt("again", timeout=20)
    assert text.endswith("|again")


def test_auth_failure_names_the_available_methods(agent, monkeypatch):
    agent("auth")
    with pytest.raises(acp.AcpError, match="fake-login"):
        acp.prompt("x", timeout=20)
    monkeypatch.setenv(acp.ENV_AUTH_METHOD, "fake-login")
    acp.shutdown()
    text, _, _ = acp.prompt("x", timeout=20)
    assert text.endswith("|x")


def test_missing_or_unset_command_is_a_clear_error(monkeypatch):
    monkeypatch.delenv(acp.ENV_COMMAND, raising=False)
    assert not acp.acp_available()
    with pytest.raises(acp.AcpError, match=acp.ENV_COMMAND):
        acp.prompt("x")
    monkeypatch.setenv(acp.ENV_COMMAND, "definitely-not-an-acp-agent-binary")
    assert not acp.acp_available()
    with pytest.raises(acp.AcpError, match="not found"):
        acp.prompt("x")


def test_backend_extracts_a_graph_without_any_api_key(agent, tmp_path):
    agent("extraction")
    doc = tmp_path / "a.md"
    doc.write_text("# Alpha\nAlpha references Beta.\n", encoding="utf-8")
    result = llm.extract_files_direct([doc], backend="acp", root=tmp_path)
    assert {n["label"] for n in result["nodes"]} == {"Alpha", "Beta"}
    assert [(e["source"], e["target"]) for e in result["edges"]] == [("doc_alpha", "doc_beta")]
    assert result["model"] == "fake-agent"


def test_plain_text_calls_use_the_agent_too(agent):
    assert llm._call_llm("ping", backend="acp").endswith("|ping")
