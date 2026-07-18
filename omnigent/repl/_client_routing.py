"""Client-side model routing via a ``routes:select`` endpoint.

At the start of a fresh session the REPL can ask a routing service which
model to run the turn on, then attach that choice as the event's
``model_override``. The service speaks the ``omnigent.api.routing.v1``
proto (``SelectRouteRequest`` -> ``SelectRouteResponse``) over HTTP as
proto3-JSON with the original snake_case field names.

The endpoint is pluggable via a base URL: a remote Databricks AI-Gateway
router, or the omni server itself once it exposes the same API. The
candidate models come from the agent spec's own model catalog
(:func:`omnigent.model_catalog.catalog_for_spec`), the same source the
server-side router uses.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from google.protobuf import json_format

from omnigent.api.routing.v1 import routing_pb2 as pb

_log = logging.getLogger(__name__)

# Routing runs once at session start; a generous-but-bounded timeout keeps
# a slow router from stalling the first turn indefinitely.
_ROUTE_TIMEOUT_S = 20.0


def route_options_from_spec(spec: Any) -> list[pb.RouteOption]:  # type: ignore[explicit-any]  # structural spec
    """Build the ``route_options`` candidate list from an agent spec.

    Enumerates the spec's own harness plus each sub-agent via
    :func:`omnigent.model_catalog.catalog_for_spec`, flattening to one
    ``RouteOption`` per (harness, model). The catalog always includes a
    ``"self"`` row for the brain harness; it is mapped to its real harness
    name (via :func:`spec_harness`) and de-duplicated against a same-named
    sub-agent row.

    :param spec: The agent spec (or structural equivalent).
    :returns: Candidate route options; possibly empty when no models
        resolve (caller should skip routing in that case).
    """
    from omnigent.model_catalog import catalog_for_spec, spec_harness

    catalog = catalog_for_spec(spec)
    self_harness = spec_harness(spec)
    options: list[pb.RouteOption] = []
    seen: set[tuple[str | None, str]] = set()
    for worker, row in catalog.items():
        harness = self_harness if worker == "self" else worker
        for model in row.get("models", []):
            model_id = model.get("id") if isinstance(model, dict) else None
            if not isinstance(model_id, str):
                continue
            key = (harness, model_id)
            if key in seen:
                continue
            seen.add(key)
            options.append(pb.RouteOption(model=model_id, harness=harness or ""))
    return options


class ClientRouter:
    """Selects a model for a turn by calling a ``routes:select`` service.

    Owns nothing session-specific: one instance is reused across a REPL
    session and issues a short-lived HTTP request per :meth:`select`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        router_name: str,
        auth: httpx.Auth | None = None,
    ) -> None:
        """
        :param base_url: Routing service base, e.g.
            ``"https://host/ai-gateway/routing/v1"`` or
            ``"http://localhost:6767/v1"``. ``/routes:select`` is appended.
        :param router_name: Router strategy name, e.g. ``"task_v0"``.
        :param auth: Optional httpx auth for the endpoint's host (a
            Databricks bearer for a remote gateway; ``None`` for a local
            unauthenticated server).
        """
        self._url = base_url.rstrip("/") + "/routes:select"
        self._router_name = router_name
        self._auth = auth

    async def select(
        self,
        *,
        prompt: str,
        route_options: list[pb.RouteOption],
    ) -> str | None:
        """Ask the router which model to use for *prompt*.

        :param prompt: The user's message text.
        :param route_options: Candidate (model, harness) pairs.
        :returns: The chosen model id, or ``None`` when routing is
            unavailable, returns no selection, or fails. Failures are
            logged at debug and swallowed so the turn proceeds.
        """
        if not route_options:
            return None
        request = pb.SelectRouteRequest(
            route_options=route_options,
            task=pb.Task(prompt=prompt),
            route_selector=pb.RouteSelector(router_name=self._router_name),
        )
        # snake_case wire format (the gateway uses the proto field names).
        body = json_format.MessageToDict(request, preserving_proto_field_name=True)
        try:
            async with httpx.AsyncClient(timeout=_ROUTE_TIMEOUT_S) as http:
                resp = await http.post(
                    self._url,
                    headers={"Content-Type": "application/json"},
                    json=body,
                    auth=self._auth,
                )
                resp.raise_for_status()
                out = json_format.ParseDict(resp.json(), pb.SelectRouteResponse())
        except (httpx.HTTPError, ValueError, json_format.ParseError):
            _log.debug("client routing failed; proceeding without override", exc_info=True)
            return None
        if not out.route_selection:
            return None
        model = out.route_selection[0].route_option.model
        if model:
            _log.debug("client routing selected model=%s rationale=%s", model, out.rationale)
        return model or None
