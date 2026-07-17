"""Routing namespace — select a route for a task via a remote routing endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ._errors import OmnigentError, raise_for_status, response_body

# Route name appended to the base endpoint. The base is everything up to and
# including ``/v1`` (OpenAI-style), so we only append the resource verb.
_SELECT_ROUTE = "routes:select"


@dataclass(frozen=True)
class RouteOption:
    """A candidate destination the router may choose from.

    :param model: Model identifier, e.g. ``"claude-opus-4-8"``.
    :param harness: Harness driving the model, e.g. ``"claude"``.
    """

    model: str
    harness: str


@dataclass(frozen=True)
class RouteSelection:
    """The routing decision returned by the endpoint.

    :param route_option: The chosen ``(model, harness)`` pair, or ``None``
        when the endpoint returned no selection.
    :param rationale: Human-readable explanation from the router, or ``""``
        when absent.
    :param raw: The full parsed response body for callers that need fields
        beyond ``route_option`` and ``rationale``.
    """

    route_option: RouteOption | None
    rationale: str
    raw: dict[str, Any] = field(default_factory=dict)


class RoutingNamespace:
    """Client for the ``routes:select`` routing endpoint.

    Sends a task and a list of candidate ``(model, harness)`` pairs to a
    remote routing endpoint and returns the selected route.

    Usage::

        from omnigent_client import OmnigentClient

        async with OmnigentClient(
            base_url="http://localhost:8080",
            routing_endpoint="https://host/ai-gateway/routing/v1",
        ) as client:
            selection = await client.routing.select(
                prompt="Fix the NullPointerException in login",
                route_options=[
                    RouteOption(model="claude-opus-4-8", harness="claude"),
                    RouteOption(model="gpt-5-5", harness="codex"),
                ],
            )
            print(selection.route_option)   # RouteOption(model=..., harness=...)
            print(selection.rationale)

    :param base_url: Routing endpoint base URL up to ``/v1``, e.g.
        ``"https://host/ai-gateway/routing/v1"``.  ``routes:select`` is
        appended automatically.
    :param token: Bearer token for the ``Authorization`` header.  ``None``
        for an unauthenticated endpoint.
    :param http: Optional shared ``httpx.AsyncClient``.  When ``None`` a
        short-lived client is created per request.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/{_SELECT_ROUTE}"
        self._token = token
        self._http = http

    async def select(
        self,
        prompt: str,
        route_options: list[RouteOption],
        *,
        router: str = "task_v0",
        timeout: float = 10.0,
    ) -> RouteSelection:
        """Select a route for a task.

        Sends ``route_options`` and the ``prompt`` to the routing endpoint
        and returns the chosen ``(model, harness)`` pair plus a rationale.

        :param prompt: The task prompt, e.g. ``"Fix the NPE in login"``.
            Truncated to 4 000 characters before sending.
        :param route_options: Candidate ``(model, harness)`` pairs for the
            router to choose from.
        :param router: Routing strategy name, e.g. ``"task_v0"``.
        :param timeout: Per-request timeout in seconds.  Ignored when a
            shared ``http`` client was passed to the constructor.
        :returns: The selected route.
        :raises OmnigentError: On a non-2xx response or an unexpected body.
        """
        body: dict[str, Any] = {
            "route_options": [
                {"model": opt.model, "harness": opt.harness} for opt in route_options
            ],
            "task": {"prompt": prompt[:4000]},
            "route_selector": {"router": router},
        }
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        if self._http is not None:
            resp = await self._http.post(self._url, json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(self._url, json=body, headers=headers)

        parsed = response_body(resp)
        raise_for_status(resp.status_code, parsed)

        if not isinstance(parsed, dict):
            raise OmnigentError(
                f"routes:select returned unexpected body: {str(parsed)[:200]}",
                resp.status_code,
            )

        return _parse_selection(parsed)


def _parse_selection(body: dict[str, Any]) -> RouteSelection:
    """Parse a ``SelectRouteResponse`` body into a :class:`RouteSelection`."""
    rationale = body.get("rationale") or ""
    selections = body.get("routeSelection") or body.get("route_selection") or []
    if not selections or not isinstance(selections, list):
        return RouteSelection(route_option=None, rationale=str(rationale), raw=body)

    first = selections[0]
    option = first.get("routeOption") or first.get("route_option") or {}
    model = option.get("model") or None
    harness = option.get("harness") or None

    route_option = RouteOption(model=model, harness=harness) if model else None
    return RouteSelection(route_option=route_option, rationale=str(rationale), raw=body)
