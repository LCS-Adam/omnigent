"""Tests for client-side model routing (``omnigent.repl._client_routing``).

Covers building ``route_options`` from a spec catalog (flatten, dedupe,
``"self"`` → real-harness mapping) and the ``ClientRouter.select`` HTTP
path (snake_case request body, response parsing, and graceful failure).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

import omnigent.model_catalog as model_catalog
from omnigent.api.routing.v1 import routing_pb2 as pb
from omnigent.repl._client_routing import ClientRouter, route_options_from_spec


def _patch_client(transport: httpx.MockTransport):
    """Patch the module's ``httpx.AsyncClient`` to use *transport*."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return patch("omnigent.repl._client_routing.httpx.AsyncClient", factory)


def test_route_options_from_spec_flattens_and_maps_self() -> None:
    """`"self"` maps to the real harness; rows flatten to (model, harness)."""
    catalog = {
        "codex": {"models": [{"id": "gpt-5-5"}, {"id": "gpt-5-4-mini"}]},
        "self": {"models": [{"id": "claude-opus-4-8"}]},
        "cursor": {"models": [{"id": "glm-5-2"}]},
    }
    with (
        patch.object(model_catalog, "catalog_for_spec", return_value=catalog),
        patch.object(model_catalog, "spec_harness", return_value="claude"),
    ):
        options = route_options_from_spec(object())

    pairs = [(o.harness, o.model) for o in options]
    assert ("claude", "claude-opus-4-8") in pairs  # "self" mapped to claude
    assert all(o.harness != "self" for o in options)
    assert ("codex", "gpt-5-5") in pairs
    assert ("cursor", "glm-5-2") in pairs
    assert len(options) == 4


def test_route_options_dedupes_repeated_model() -> None:
    """A model shared by ``"self"`` and its named harness is emitted once."""
    catalog = {
        "claude": {"models": [{"id": "claude-opus-4-8"}]},
        "self": {"models": [{"id": "claude-opus-4-8"}]},
    }
    with (
        patch.object(model_catalog, "catalog_for_spec", return_value=catalog),
        patch.object(model_catalog, "spec_harness", return_value="claude"),
    ):
        options = route_options_from_spec(object())

    assert [(o.harness, o.model) for o in options] == [("claude", "claude-opus-4-8")]


@pytest.mark.asyncio
async def test_select_sends_snake_case_and_parses_selection() -> None:
    """The request body is snake_case proto3-JSON; the model is parsed back."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "route_selection": [
                    {
                        "route_option": {"model": "claude-opus-4-8", "harness": "claude"},
                        "params": {},
                    }
                ],
                "rationale": "task_v0 matched rule 'bugfix_to_opus'.",
            },
        )

    router = ClientRouter(base_url="https://host/ai-gateway/routing/v1", router_name="task_v0")
    with _patch_client(httpx.MockTransport(handler)):
        model = await router.select(
            prompt="fix this code: x = y + 2",
            route_options=[
                pb.RouteOption(model="claude-opus-4-8", harness="claude"),
                pb.RouteOption(model="gpt-5-5", harness="codex"),
            ],
        )

    assert model == "claude-opus-4-8"
    assert captured["url"] == "https://host/ai-gateway/routing/v1/routes:select"
    body = captured["body"]
    assert body["route_selector"]["router_name"] == "task_v0"  # snake_case field
    assert body["task"]["prompt"] == "fix this code: x = y + 2"
    assert body["route_options"][0] == {"model": "claude-opus-4-8", "harness": "claude"}


@pytest.mark.asyncio
async def test_select_empty_options_skips_call() -> None:
    """No candidates → no HTTP call, returns None."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    router = ClientRouter(base_url="http://localhost:6767/v1", router_name="task_v0")
    with _patch_client(httpx.MockTransport(handler)):
        assert await router.select(prompt="hi", route_options=[]) is None
    assert called is False


@pytest.mark.asyncio
async def test_select_swallows_http_error() -> None:
    """A router outage never blocks the turn — returns None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    router = ClientRouter(base_url="http://localhost:6767/v1", router_name="task_v0")
    with _patch_client(httpx.MockTransport(handler)):
        model = await router.select(
            prompt="hi", route_options=[pb.RouteOption(model="a", harness="b")]
        )
    assert model is None


@pytest.mark.asyncio
async def test_select_empty_selection_returns_none() -> None:
    """An empty ``route_selection`` (e.g. routing disabled) yields None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"route_selection": [], "rationale": ""})

    router = ClientRouter(base_url="http://localhost:6767/v1", router_name="task_v0")
    with _patch_client(httpx.MockTransport(handler)):
        model = await router.select(
            prompt="hi", route_options=[pb.RouteOption(model="a", harness="b")]
        )
    assert model is None
