"""Tests for importing local coding-harness sessions."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from omnigent.session_import.local import (
    load_claude_session,
    load_codex_session,
    load_cursor_session,
)
from omnigent.session_import.models import SessionImportNotFoundError


def test_load_claude_session_normalizes_parent_transcript(tmp_path: Path) -> None:
    """Claude parent messages and tools become ordinary Omnigent items."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "user-1",
            "cwd": "/repo",
            "message": {"role": "user", "content": "inspect TODO.md"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_read_1",
                        "name": "Read",
                        "input": {"file_path": "TODO.md"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "result-1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_read_1",
                        "content": "contents",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "assistant-2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    # A same-id sub-agent transcript must never be selected as the parent.
    subagent = tmp_path / "projects" / "-repo" / "subagents" / f"{session_id}.jsonl"
    subagent.parent.mkdir()
    subagent.write_text("{}\n", encoding="utf-8")

    imported = load_claude_session(session_id, claude_home=tmp_path)

    assert imported.source == "claude"
    assert imported.external_session_id == session_id
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.type for item in imported.items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert imported.items[1].data.model_dump()["call_id"] == "toolu_read_1"
    assert imported.items[3].data.model_dump()["agent"] == "claude-native-ui"


def test_load_codex_session_normalizes_response_items(tmp_path: Path) -> None:
    """Codex response items retain turn grouping and omit scaffolding."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "07"
        / "15"
        / f"rollout-2026-07-15T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records = [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/repo"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn_1", "cwd": "/repo"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "internal"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>",
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "inspect TODO.md"},
                    {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command":"cat TODO.md"}',
                "call_id": "call_1",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_text", "text": "first line\n"},
                    {"type": "input_text", "text": "second line"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch",
                "call_id": "call_2",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_2",
                "output": [{"type": "output_text", "text": "Done!"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        },
    ]
    rollout.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert imported.source == "codex"
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.type for item in imported.items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert {item.response_id for item in imported.items} == {"codex:turn_1"}
    assert imported.items[0].data.model_dump()["is_meta"] is True
    assert imported.items[1].data.model_dump()["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,abc",
    }
    assert imported.items[2].data.model_dump() == {
        "agent": "codex-native-ui",
        "name": "shell",
        "arguments": '{"command":"cat TODO.md"}',
        "call_id": "call_1",
    }
    assert imported.items[3].data.model_dump() == {
        "call_id": "call_1",
        "output": "first line\nsecond line",
    }
    assert imported.items[5].data.model_dump() == {
        "call_id": "call_2",
        "output": "Done!",
    }


def test_load_cursor_session_normalizes_visible_blobs(tmp_path: Path) -> None:
    """Cursor chat blobs become messages while internal rollups are skipped."""
    session_id = "cursor-chat-123"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    workspace_hash = hashlib.md5(os.path.realpath(workspace).encode()).hexdigest()
    store_path = tmp_path / "chats" / workspace_hash / session_id / "store.db"
    store_path.parent.mkdir(parents=True)
    with sqlite3.connect(store_path) as connection:
        connection.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
        connection.executemany(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            [
                (
                    "user-1",
                    json.dumps(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "<user_query>inspect TODO.md</user_query>",
                                }
                            ],
                        }
                    ),
                ),
                (
                    "rollup",
                    json.dumps(
                        {
                            "role": "user",
                            "content": "You have 100 weighted tokens left",
                        }
                    ),
                ),
                (
                    "assistant-1",
                    json.dumps(
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        }
                    ),
                ),
            ],
        )

    imported = load_cursor_session(session_id, cursor_home=tmp_path, workspace=workspace)

    assert imported.source == "cursor"
    assert imported.workspace == os.path.realpath(workspace)
    assert imported.title == "inspect TODO.md"
    assert [item.type for item in imported.items] == ["message", "message"]
    assert imported.items[1].data.model_dump()["agent"] == "cursor-native-ui"


def test_load_cursor_session_requires_original_workspace(tmp_path: Path) -> None:
    """Cursor import fails instead of creating a session that cannot resume."""
    session_id = "cursor-chat-123"
    original_workspace = tmp_path / "original"
    original_workspace.mkdir()
    workspace_hash = hashlib.md5(os.path.realpath(original_workspace).encode()).hexdigest()
    store_path = tmp_path / "chats" / workspace_hash / session_id / "store.db"
    store_path.parent.mkdir(parents=True)
    store_path.touch()
    wrong_workspace = tmp_path / "other"
    wrong_workspace.mkdir()

    with pytest.raises(SessionImportNotFoundError, match="original workspace"):
        load_cursor_session(
            session_id,
            cursor_home=tmp_path,
            workspace=wrong_workspace,
        )
