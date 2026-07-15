"""Tests for importing normalized local harness sessions."""

from __future__ import annotations

import asyncio

import httpx

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _seed_claude_agent(db_uri: str) -> str:
    """Seed the built-in agent because focused app tests skip lifespan startup."""
    agent_id = builtin_agent_id("claude-native-ui")
    SqlAlchemyAgentStore(db_uri).create(
        agent_id,
        name="claude-native-ui",
        bundle_location="builtin://claude-native-ui",
    )
    return agent_id


async def test_import_session_creates_normal_idempotent_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An import creates one native session and a retry returns the same id."""
    agent_id = _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-session-1",
        "workspace": "/repo",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect TODO.md"}],
                },
            },
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "assistant",
                    "agent": "claude-native-ui",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            },
        ],
    }

    created = await client.post("/v1/imports", json=payload)
    repeated = await client.post("/v1/imports", json=payload)

    assert created.status_code == 201
    assert created.json()["status"] == "imported"
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "already_imported"
    assert repeated.json()["session_id"] == created.json()["session_id"]

    session_id = created.json()["session_id"]
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert conversation is not None
    assert conversation.agent_id == agent_id
    assert conversation.external_session_id == "claude-session-1"
    assert conversation.workspace == "/repo"
    assert conversation.title == "inspect TODO.md"
    assert conversation.labels["omnigent.wrapper"] == "claude-code-native-ui"
    items = await client.get(f"/v1/sessions/{session_id}/items")
    assert items.status_code == 200
    assert [item["type"] for item in items.json()["data"]] == ["message", "message"]


async def test_force_import_replaces_source_prefix_and_preserves_later_turns(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Force updates the existing conv while retaining Omnigent-authored items."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-force-1",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "old prompt"}],
                },
            }
        ],
    }
    created = await client.post("/v1/imports", json=payload)
    session_id = created.json()["session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    later = store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="omnigent:later",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "later prompt"}],
                ),
            )
        ],
    )[0]
    payload["items"] = [
        {
            "type": "message",
            "response_id": "claude:turn-1",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "new prompt"}],
            },
        },
        {
            "type": "message",
            "response_id": "claude:turn-1",
            "data": {
                "role": "assistant",
                "agent": "claude-native-ui",
                "content": [{"type": "output_text", "text": "new answer"}],
            },
        },
    ]
    payload["force"] = True

    replaced = await client.post("/v1/imports", json=payload)

    assert replaced.status_code == 200
    assert replaced.json() == {
        "session_id": session_id,
        "status": "replaced",
        "item_count": 2,
    }
    items = store.list_items(session_id, limit=20).data
    assert [item.data.content[0]["text"] for item in items] == [  # type: ignore[union-attr]
        "new prompt",
        "new answer",
        "later prompt",
    ]
    assert items[-1].id == later.id


async def test_force_import_does_not_duplicate_later_turn_already_in_source(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A later mirrored turn represented in fresh source history appears once."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-overlap-1",
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "original"}],
                },
            }
        ],
    }
    created = await client.post("/v1/imports", json=payload)
    session_id = created.json()["session_id"]
    store = SqlAlchemyConversationStore(db_uri)
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="claude:turn-2",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "later"}],
                ),
                created_by="owner@example.com",
            )
        ],
    )
    payload["force"] = True
    payload["items"] = [
        payload["items"][0],
        {
            "type": "message",
            "response_id": "claude:turn-2",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "later"}],
            },
        },
    ]

    replaced = await client.post("/v1/imports", json=payload)

    assert replaced.status_code == 200
    items = store.list_items(session_id, limit=20).data
    assert [item.data.content[0]["text"] for item in items] == [  # type: ignore[union-attr]
        "original",
        "later",
    ]


async def test_concurrent_identical_imports_return_one_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Concurrent retries serialize on source identity and return one conv."""
    _seed_claude_agent(db_uri)
    payload = {
        "source": "claude",
        "external_session_id": "claude-concurrent-1",
        "items": [],
    }

    first, second = await asyncio.gather(
        client.post("/v1/imports", json=payload),
        client.post("/v1/imports", json=payload),
    )

    assert {first.status_code, second.status_code} == {200, 201}
    assert first.json()["session_id"] == second.json()["session_id"]
