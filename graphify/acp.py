"""Minimal Agent Client Protocol (ACP) client, used as the ``acp`` LLM backend.

ACP (https://agentclientprotocol.com) is JSON-RPC 2.0 over a subprocess's
stdio. graphify plays the *client*: it launches an ACP agent -- Claude Code
(``claude-agent-acp``), Gemini CLI (``gemini --acp``), Codex (``codex-acp``),
opencode, Goose, ... -- and sends it one prompt per extraction chunk. The agent
brings its own authentication (a subscription login, OAuth, ...), so graphify
needs no API key.

Which agent runs, first match wins::

    GRAPHIFY_ACP_COMMAND            the agent command line, e.g. ``claude-agent-acp``
    ~/.graphify/config.json         ``acp_command``, saved by ``extract --acp-command``
    built-in default                ``omp acp`` (lean flags), if ``omp`` is on $PATH
                                    (GRAPHIFY_ACP_DEFAULT_COMMAND replaces it; set
                                    empty to have no default)

    GRAPHIFY_ACP_AUTH_METHOD        optional ``authMethods`` id to ``authenticate``
                                    with before the first session

Only the baseline subset is implemented: ``initialize``, ``authenticate``,
``session/new``, ``session/prompt``, ``session/cancel`` and the
``session/update`` agent-message chunks. graphify advertises no ``fs`` or
``terminal`` capability, so the agent has no client-side tools; a permission
request is rejected, and the agent is expected to answer from the prompt alone
(source text is inlined). Each prompt gets a fresh session in an empty scratch
directory, so no project context or earlier chunk leaks into it.

stdlib only.
"""
from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any

ENV_COMMAND = "GRAPHIFY_ACP_COMMAND"
ENV_DEFAULT_COMMAND = "GRAPHIFY_ACP_DEFAULT_COMMAND"
ENV_AUTH_METHOD = "GRAPHIFY_ACP_AUTH_METHOD"
# omp as the stock agent (`omp acp`). Extraction is a
# pure text-in/text-out task, so switch off everything that only costs tokens or
# time (tools, extensions, skills, rules, LSP) or leaves traces (session files,
# generated titles).
DEFAULT_COMMAND = "omp acp --no-tools --no-session --no-extensions --no-skills --no-rules --no-title --no-lsp"

PROTOCOL_VERSION = 1
_STDERR_TAIL_BYTES = 4000


class AcpError(RuntimeError):
    """The agent could not be started, spoke out of protocol, or reported an error."""


def configured_command() -> tuple[str, str]:
    """The agent command line to run and where it came from (``""`` if none)."""
    explicit = os.environ.get(ENV_COMMAND, "").strip()
    if explicit:
        return explicit, f"${ENV_COMMAND}"
    from graphify import backend_config

    saved = backend_config.load().get("acp_command", "").strip()
    if saved:
        return saved, str(backend_config.config_path())
    default = os.environ.get(ENV_DEFAULT_COMMAND, DEFAULT_COMMAND).strip()
    return (default, "built-in default") if default else ("", "")


def acp_command() -> list[str]:
    """The agent argv to launch, executable resolved against ``$PATH``."""
    raw, source = configured_command()
    if not raw:
        raise AcpError(
            f"No ACP agent configured. Pass --acp-command or set {ENV_COMMAND} to the "
            "agent's command, e.g. 'omp acp', 'claude-agent-acp' or 'gemini --acp'."
        )
    # Windows paths contain backslashes, so split non-POSIX there; that mode
    # keeps the surrounding quotes on a token, which are stripped here.
    argv = [t[1:-1] if os.name == "nt" and len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in shlex.split(raw, posix=os.name != "nt")]
    resolved = shutil.which(argv[0])
    if resolved is None:
        raise AcpError(f"ACP agent command {argv[0]!r} (from {source}) not found on $PATH")
    return [resolved, *argv[1:]]


def acp_available() -> bool:
    """True when an ACP agent is configured (or the omp default) and launchable."""
    try:
        acp_command()
    except AcpError:
        return False
    return True


class _Connection:
    """One agent subprocess. Thread-safe: sessions are routed by ``sessionId``."""

    def __init__(self, argv: list[str]):
        self.argv = argv
        self.agent_name = "acp"
        self._auth_methods: list[str] = []
        self.scratch = tempfile.mkdtemp(prefix="graphify-acp-")
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._chunks: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._dead: str | None = None
        self._stderr_tail = b""
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.scratch,
            )
        except OSError as exc:
            shutil.rmtree(self.scratch, ignore_errors=True)
            raise AcpError(f"could not start ACP agent {argv[0]!r}: {exc}") from exc
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True, name="acp-stderr")
        self._stderr_thread.start()
        threading.Thread(target=self._read_stdout, daemon=True, name="acp-stdout").start()

    # ── transport ────────────────────────────────────────────────────────────

    def _send(self, message: dict) -> None:
        data = (json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AcpError(f"ACP agent connection closed: {self._why_dead() or exc}") from exc

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):  # type: ignore[union-attr]
            self._stderr_tail = (self._stderr_tail + chunk)[-_STDERR_TAIL_BYTES:]

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue  # stdout is JSON-RPC only; tolerate an agent that logs here
            if isinstance(message, dict):
                self._dispatch(message)
        # Let the stderr reader drain first so the crash reason carries the agent's last words.
        self._stderr_thread.join(timeout=2)
        try:
            reason = f"exited with code {self.proc.wait(timeout=2)}"
        except subprocess.TimeoutExpired:
            reason = "closed its output"
        self._mark_dead(reason)

    def _mark_dead(self, reason: str) -> None:
        with self._lock:
            self._dead = reason
            waiting = list(self._pending.values())
        for box in waiting:
            box.put({"error": {"code": -1, "message": f"ACP agent {reason}"}})

    def _why_dead(self) -> str:
        if self._dead is None:
            return ""
        tail = self._stderr_tail.decode("utf-8", errors="replace").strip()
        return f"{self._dead}" + (f": {tail[-500:]}" if tail else "")

    def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:  # a response to one of our requests
            with self._lock:
                box = self._pending.get(message.get("id"))  # type: ignore[arg-type]
            if box is not None:
                box.put(message)
            return
        params = message.get("params") or {}
        if "id" not in message:  # notification
            if method == "session/update":
                self._on_update(params)
            return
        self._answer_agent_request(message["id"], method, params)

    def _on_update(self, params: dict) -> None:
        update = params.get("update") or {}
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content") or {}
        text = content.get("text")
        if content.get("type") == "text" and isinstance(text, str):
            with self._lock:
                buffer = self._chunks.get(params.get("sessionId"))  # type: ignore[arg-type]
                if buffer is not None:
                    buffer.append(text)

    def _answer_agent_request(self, request_id: Any, method: str, params: dict) -> None:
        """Requests the agent makes of us. We expose no tools, so refuse them."""
        if method == "session/request_permission":
            reject = next(
                (o.get("optionId") for o in params.get("options") or []
                 if str(o.get("kind", "")).startswith("reject")),
                None,
            )
            outcome = ({"outcome": "selected", "optionId": reject} if reject
                       else {"outcome": "cancelled"})
            self._send({"id": request_id, "result": {"outcome": outcome}})
        else:
            self._send({"id": request_id,
                        "error": {"code": -32601, "message": f"method not supported: {method}"}})

    # ── requests ─────────────────────────────────────────────────────────────

    def request(self, method: str, params: dict, timeout: float | None) -> dict:
        request_id = next(self._ids)
        box: queue.Queue = queue.Queue()
        with self._lock:
            if self._dead is not None:
                raise AcpError(f"ACP agent {self._why_dead()}")
            self._pending[request_id] = box
        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                message = box.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"ACP {method} timed out after {timeout}s") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if "error" in message:
            err = message["error"] or {}
            detail = err.get("message", "unknown error")
            if self._dead is not None:
                detail += f" ({self._why_dead()})"
            raise AcpError(f"ACP {method} failed: {detail}")
        return message.get("result") or {}

    def initialize(self, timeout: float) -> None:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {},
            "clientInfo": {"name": "graphify", "title": "graphify", "version": _graphify_version()},
        }, timeout)
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise AcpError(
                f"ACP agent speaks protocol version {result.get('protocolVersion')!r}; "
                f"graphify supports {PROTOCOL_VERSION}"
            )
        info = result.get("agentInfo") or {}
        self.agent_name = str(info.get("name") or info.get("title") or "acp")
        self._auth_methods = [m.get("id") for m in result.get("authMethods") or [] if m.get("id")]
        method = os.environ.get(ENV_AUTH_METHOD, "").strip()
        if method:
            self.request("authenticate", {"methodId": method}, timeout)

    def prompt(self, text: str, timeout: float) -> tuple[str, str]:
        """Run one prompt turn in a fresh session -> ``(agent text, stopReason)``."""
        try:
            return self._prompt(text, timeout)
        except AcpError as exc:
            if "authenticat" not in str(exc).lower():
                raise
            methods = (
                f" The agent offers authentication methods {self._auth_methods}; "
                f"set {ENV_AUTH_METHOD} to use one."
                if self._auth_methods and not os.environ.get(ENV_AUTH_METHOD) else ""
            )
            raise AcpError(
                f"{exc}. Sign in with the agent itself first (run its CLI once and log in).{methods}"
            ) from exc

    def _prompt(self, text: str, timeout: float) -> tuple[str, str]:
        session = self.request("session/new", {"cwd": self.scratch, "mcpServers": []}, timeout)
        session_id = session.get("sessionId")
        if not session_id:
            raise AcpError("ACP session/new returned no sessionId")
        with self._lock:
            self._chunks[session_id] = []
        try:
            try:
                result = self.request("session/prompt", {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                }, timeout)
            except TimeoutError:
                try:
                    self._send({"method": "session/cancel", "params": {"sessionId": session_id}})
                except AcpError:
                    pass
                raise subprocess.TimeoutExpired(self.argv, timeout) from None
            with self._lock:
                reply = "".join(self._chunks.get(session_id, []))
        finally:
            with self._lock:
                self._chunks.pop(session_id, None)
        return reply, str(result.get("stopReason") or "")

    def close(self) -> None:
        proc = self.proc
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(self.scratch, ignore_errors=True)


def _graphify_version() -> str:
    try:
        from importlib.metadata import version

        return version("graphifyy")
    except Exception:
        return "0"


_conn: _Connection | None = None
_conn_lock = threading.Lock()


def _connection(timeout: float) -> _Connection:
    """The shared agent process, started (and initialized) on first use."""
    global _conn
    with _conn_lock:
        if _conn is not None and _conn._dead is None and _conn.proc.poll() is None:
            return _conn
        if _conn is not None:
            _conn.close()
            _conn = None
        conn = _Connection(acp_command())
        try:
            conn.initialize(timeout)
        except BaseException:
            conn.close()
            raise
        _conn = conn
        return conn


def shutdown() -> None:
    """Stop the shared agent process (registered with ``atexit``)."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            _conn.close()
            _conn = None


atexit.register(shutdown)


def prompt(text: str, timeout: float = 600.0) -> tuple[str, str, str]:
    """Send ``text`` to the configured ACP agent.

    Returns ``(reply, stop_reason, agent_name)``. A turn that ran out of time is
    cancelled and raises ``subprocess.TimeoutExpired`` (the same exception the
    ``claude-cli`` backend raises, so adaptive retry treats both alike).
    """
    conn = _connection(timeout)
    reply, stop_reason = conn.prompt(text, timeout)
    if stop_reason == "refusal":
        raise AcpError("ACP agent refused the request")
    if stop_reason == "cancelled":
        raise AcpError("ACP agent cancelled the request")
    return reply, stop_reason, conn.agent_name


if __name__ == "__main__":  # `python -m graphify.acp "hello"`: a quick connectivity check
    text, stop, name = prompt(" ".join(sys.argv[1:]) or "Reply with the single word: ok")
    print(f"[{name}, stopReason={stop}]\n{text}")
