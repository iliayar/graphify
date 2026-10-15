"""A tiny ACP agent for tests/test_acp_backend.py (stdio JSON-RPC, protocol v1).

Behaviour is selected with FAKE_ACP_MODE:

echo        reply "<pid>|<sessionId>|<prompt>" as two chunks, after a thought chunk
extraction  reply a fixed graphify extraction JSON object
permission  ask for permission first; reply with the outcome the client chose
fs          call fs/read_text_file first; reply with the error the client returned
hang        never finish the turn on its own; answer session/cancel with "cancelled"
die         exit as soon as a prompt arrives
auth        session/new fails with an auth error until `authenticate` was called
"""
from __future__ import annotations

import json
import os
import sys

MODE = os.environ.get("FAKE_ACP_MODE", "echo")
EXTRACTION = {
    "nodes": [
        {"id": "doc_alpha", "label": "Alpha", "file_type": "document", "source_file": "a.md"},
        {"id": "doc_beta", "label": "Beta", "file_type": "document", "source_file": "a.md"},
    ],
    "edges": [
        {"source": "doc_alpha", "target": "doc_beta", "relation": "references",
         "confidence": "EXTRACTED", "confidence_score": 1.0},
    ],
    "hyperedges": [],
}
authenticated = False
sessions = 0
next_id = 1000


def send(message: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def update(session_id: str, kind: str, text: str) -> None:
    send({"method": "session/update", "params": {
        "sessionId": session_id,
        "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}},
    }})


def ask_client(method: str, params: dict) -> dict:
    """Send a request to the client and block for its response."""
    global next_id
    next_id += 1
    send({"id": next_id, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        message = json.loads(line)
        if message.get("id") == next_id and "method" not in message:
            return message


def finish(request_id, session_id: str, text: str) -> None:
    update(session_id, "agent_thought_chunk", "thinking...")
    half = len(text) // 2
    update(session_id, "agent_message_chunk", text[:half])
    send({"method": "session/update", "params": {
        "sessionId": session_id,
        "update": {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "x", "status": "pending"},
    }})
    update(session_id, "agent_message_chunk", text[half:])
    send({"id": request_id, "result": {"stopReason": "end_turn"}})


hung: dict[str, object] = {}

for raw in sys.stdin:
    message = json.loads(raw)
    method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method == "initialize":
        send({"id": request_id, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {},
            "agentInfo": {"name": "fake-agent", "version": "0"},
            "authMethods": [{"id": "fake-login", "name": "Fake login"}] if MODE == "auth" else [],
        }})
    elif method == "authenticate":
        authenticated = params.get("methodId") == "fake-login"
        send({"id": request_id, "result": {}})
    elif method == "session/new":
        if MODE == "auth" and not authenticated:
            send({"id": request_id, "error": {"code": -32000, "message": "Authentication required"}})
            continue
        sessions += 1
        send({"id": request_id, "result": {"sessionId": f"sess_{sessions}"}})
    elif method == "session/prompt":
        session_id = params["sessionId"]
        prompt = "".join(block.get("text", "") for block in params["prompt"])
        if MODE == "die":
            sys.stderr.write("fake agent crashed: boom\n")
            sys.exit(3)
        elif MODE == "hang":
            hung[session_id] = request_id
        elif MODE == "extraction":
            finish(request_id, session_id, json.dumps(EXTRACTION))
        elif MODE == "permission":
            reply = ask_client("session/request_permission", {
                "sessionId": session_id,
                "toolCall": {"toolCallId": "t1"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            })
            finish(request_id, session_id, json.dumps(reply["result"]["outcome"]))
        elif MODE == "fs":
            reply = ask_client("fs/read_text_file", {"sessionId": session_id, "path": "/etc/hosts"})
            finish(request_id, session_id, str(reply.get("error", {}).get("code")))
        else:
            finish(request_id, session_id, f"{os.getpid()}|{session_id}|{prompt}")
    elif method == "session/cancel":
        session_id = params["sessionId"]
        if session_id in hung:
            send({"id": hung.pop(session_id), "result": {"stopReason": "cancelled"}})
