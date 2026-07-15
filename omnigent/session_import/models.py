"""Models and provenance metadata shared by session import layers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from omnigent.entities import ConversationItem, MessageData, NewConversationItem
from omnigent.entities.conversation import synthesize_conversation_title

ImportSource = Literal["claude", "codex", "cursor"]

IMPORT_SOURCE_LABEL_KEY = "omnigent.import.source"
IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY = "omnigent.import.external_session_id"
IMPORT_ITEM_COUNT_LABEL_KEY = "omnigent.import.item_count"
IMPORT_DIGEST_LABEL_KEY = "omnigent.import.digest"
IMPORT_PROVENANCE_LABEL_KEYS = frozenset(
    {
        IMPORT_SOURCE_LABEL_KEY,
        IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY,
        IMPORT_ITEM_COUNT_LABEL_KEY,
        IMPORT_DIGEST_LABEL_KEY,
    }
)


class SessionImportNotFoundError(FileNotFoundError):
    """Raised when a requested local harness session cannot be found."""


@dataclass(frozen=True)
class LocalSessionImport:
    """One local transcript normalized for the import API."""

    source: ImportSource
    external_session_id: str
    workspace: str | None
    items: tuple[NewConversationItem, ...]

    @property
    def title(self) -> str | None:
        """Return a sidebar title derived from the first user message."""
        return title_from_items(self.items)


def title_from_items(items: Sequence[NewConversationItem]) -> str | None:
    """Return a sidebar title derived from the first user message."""
    for item in items:
        if (
            isinstance(item.data, MessageData)
            and item.data.role == "user"
            and not item.data.is_meta
        ):
            return synthesize_conversation_title(item.data.content)
    return None


def conversation_items_digest(
    items: Sequence[NewConversationItem | ConversationItem],
    *,
    include_created_by: bool = True,
) -> str:
    """Return a stable digest of item content, excluding database identity."""
    normalized: list[dict[str, object]] = []
    for item in items:
        value: dict[str, object] = {
            "type": item.type,
            "response_id": item.response_id,
            "data": item.data.model_dump(mode="json", exclude_none=True),
        }
        if include_created_by:
            value["created_by"] = item.created_by
        normalized.append(value)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
