"""Usage telemetry event dataclasses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionCreatedEvent:
    """Fired when a runner tunnel connects to a session.

    :param session_id: The conversation/session identifier.
    :param agent_id: The agent bound to this session.
    :param harness: Harness kind, e.g. ``"claude-native"`` or ``"pi"``.
    :param surface: Client surface: ``"web"``, ``"desktop"``, ``"ios"``,
        ``"android"``, ``"cli"``, or ``"unknown"``.
    :param installation_id: Server-side installation ID from the telemetry
        store.
    :param runner_installation_id: Installation ID the runner sent in its
        ``HelloFrame`` (runner-side identity).
    :param anon_user_id: First 16 hex chars of
        ``sha256("<installation_id>:<user_id>")``.
    :param is_fork: ``True`` when the session was forked from another.
    :param is_sub_agent: ``True`` when ``conv.kind == "sub_agent"``.
    """

    session_id: str
    agent_id: str | None
    harness: str | None
    surface: str | None
    installation_id: str | None
    runner_installation_id: str | None
    anon_user_id: str | None
    is_fork: bool
    is_sub_agent: bool


@dataclass
class SessionStoppedEvent:
    """Fired after a session is successfully stopped via the runner.

    :param session_id: The conversation/session identifier.
    :param installation_id: Server-side installation ID.
    :param anon_user_id: Anonymised user identifier (see
        :class:`SessionCreatedEvent`).
    """

    session_id: str
    installation_id: str | None
    anon_user_id: str | None


@dataclass
class SessionDeletedEvent:
    """Fired after a session row is deleted from the store.

    :param session_id: The conversation/session identifier.
    :param installation_id: Server-side installation ID.
    :param anon_user_id: Anonymised user identifier.
    :param duration_seconds: Wall-clock lifetime of the session derived
        from ``conv.created_at`` (Unix epoch int).
    :param input_tokens: Cumulative input tokens from ``session_usage``.
    :param output_tokens: Cumulative output tokens from ``session_usage``.
    :param total_cost_usd: Cumulative cost from ``session_usage``.
    """

    session_id: str
    installation_id: str | None
    anon_user_id: str | None
    duration_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_cost_usd: float | None
