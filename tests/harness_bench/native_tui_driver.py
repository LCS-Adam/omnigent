"""Native-TUI transport driver (phase-2, walking skeleton).

Drives a native-tui harness — a resident vendor CLI (``claude``, ``codex``,
``pi``, ``cursor-agent``, ...) running in a runner-owned tmux pane — through
the bench's :class:`~tests.harness_bench.transport.Driver` protocol.

The research finding this is built on: a native-tui turn rides the *same*
HTTP surface as the full server — ``POST /v1/sessions/{id}/events`` to send,
``GET /v1/sessions/{id}/stream`` for ``response.output_text.delta`` events,
``/v1/sessions/{id}/policies`` for a tool-call deny, and item polling for the
assistant reply. So ~90% of this driver is shared with
:class:`~tests.harness_bench.full_server_driver.FullServerDriver`. Three
things genuinely diverge, and they are the entire reason this is a separate
driver:

1. **Provisioning** — instead of registering an agent tarball, a native
   harness needs a **host daemon** (``omnigent host``) registered with the
   server, the auto-registered ``<harness>-native-ui`` agent, and a session
   created with ``{agent_id, host_id, workspace}``. The runner then launches
   the vendor CLI in tmux itself.
2. **Interrupt detection** — native turns do not persist an "interrupted"
   user-message marker; cancellation surfaces as a ``session.interrupted``
   event / status, so the interrupt probe keys off that.
3. **Auth + login** — the vendor CLI must be interactively logged in on the
   host. ``OMNIGENT_CREDENTIAL`` vendors (claude, codex) can take a minted
   bearer; ``OWN_AUTH`` vendors (cursor, kiro, ...) must be pre-logged-in.
   Either way the bench cannot provision a fresh login, so this transport is
   only exercisable on a host with the vendor CLI already authenticated.

Per-vendor differences beyond auth (tmux paste vs app-server RPC delivery,
readiness signal, tool-deny surface) are captured in :class:`NativeVendor`
records, so adding a harness is a config entry, not a new driver — until a
vendor diverges in kind (codex-native is RPC-delivered; opencode-native is
``native-server`` not ``native-tui``), which will want its own handling.

Scope: this skeleton wires **claude-native** and the shared turn/observe
path. It is structurally complete and offline-tested, but was NOT
live-verified in the authoring environment (no interactive vendor login
available); the gated live test runs it where a login exists.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import lookup_databricks_host
from tests.harness_bench.driver import TurnResult
from tests.harness_bench.full_server_driver import (
    _find_free_port,
    _mint_bearer,
    spawn_omnigent_server,
)
from tests.harness_bench.profile import BenchProfile

_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 45.0
_POLL_INTERVAL_S = 0.3
_TURN_TIMEOUT_S = 180.0

# Native turns take longer (terminal boot + a real interactive vendor turn),
# so the prompts stay short and the timeouts generous.
_STREAM_PROMPT = "Count from 1 to 20 in words, one per line."
_LONG_PROMPT = "Write a detailed 500-word essay about the history of computing."


@dataclass(frozen=True)
class NativeVendor:
    """Per-vendor facts a native-tui harness needs beyond the shared path.

    :param harness: The native harness id, e.g. ``"claude-native"``.
    :param agent_name: The server's auto-registered UI agent, e.g.
        ``"claude-native-ui"``.
    :param own_auth: ``True`` when the vendor uses its own login (cannot take
        a minted bearer); such a harness is only runnable pre-logged-in.
    """

    harness: str
    agent_name: str
    own_auth: bool = False


# Vendor registry. claude-native is the wired skeleton; the others are
# declared so the shape is visible, but only claude-native is exercised here.
_VENDORS: dict[str, NativeVendor] = {
    "claude-native": NativeVendor("claude-native", "claude-native-ui", own_auth=False),
}


class NativeTuiDriver:
    """Drive a native-tui harness through a live server + host daemon.

    Async context manager: on enter it spawns a server, a host daemon (under
    the real ``$HOME`` so the vendor login is inherited), waits for the host
    to come online, and creates a native session bound to that host. The
    ``run_*`` methods drive turns over the same HTTP surface the full-server
    driver uses; the runner mirrors them into the tmux vendor TUI.
    """

    transport = "native-tui"

    def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
        self._profile = profile
        self._db_profile = databricks_profile
        self._vendor = _VENDORS.get(profile.harness)
        self._proc: subprocess.Popen[bytes] | None = None
        self._daemon: subprocess.Popen[bytes] | None = None
        self._client: httpx.Client | None = None
        self._session_id: str | None = None
        self._base_url = ""
        self._tmp = Path("/tmp") / f"omni-bench-nt-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else None."""
        vendor = _VENDORS.get(profile.harness)
        if vendor is None:
            return f"no native-tui vendor entry for {profile.harness!r}"
        if not databricks_profile:
            return "no --profile / databricks profile provided; native-tui needs a gateway route"
        if lookup_databricks_host(databricks_profile) is None:
            return (
                f"databricks profile {databricks_profile!r} missing/hostless in ~/.databrickscfg"
            )
        # The vendor CLI must exist AND be interactively logged in on this
        # host; the bench cannot provision a login. Presence on PATH is the
        # cheapest precondition we can check — a missing login still fails the
        # live turn, reported as a capability-neutral skip by the probes.
        from tests.e2e._harness_probes import cli_unavailable_reason

        binary = profile.cli_binary
        if binary is not None:
            reason = cli_unavailable_reason(binary)
            if reason is not None:
                return reason
        return None

    # ── async driver protocol ────────────────────────────────

    async def __aenter__(self) -> NativeTuiDriver:
        await asyncio.to_thread(self._provision)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(self._teardown)

    async def run_basic_turn(self, marker: str) -> TurnResult:
        prompt = f"Reply with exactly the literal string {marker} and nothing else."
        return await asyncio.to_thread(self._drive_turn, prompt)

    async def run_streaming_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_turn, _STREAM_PROMPT, count_deltas=True)

    async def run_tool_turn(self, *, deny: bool) -> TurnResult:
        # Native tool calls are the vendor's own tools (Bash/Read/...), not a
        # server-dispatched builtin the bench can force, and a native deny
        # surfaces as a vendor permission decision rather than a
        # function_call_output. Wiring that observation is the next
        # increment; today the skeleton reports it unmeasured so the probe
        # records a capability-neutral skip rather than a false verdict.
        result = TurnResult()
        result.error = "native tool/policy observation not yet wired (skeleton)"
        return result

    async def run_interrupt_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_interrupt_turn)

    # ── provisioning ─────────────────────────────────────────

    def _provision(self) -> None:
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert self._vendor is not None
        host = lookup_databricks_host(self._db_profile)
        assert host is not None
        port = _find_free_port()
        self._base_url = f"http://localhost:{port}"
        binding_token = uuid.uuid4().hex

        base_env = {
            **os.environ,
            "OPENAI_API_KEY": _mint_bearer(self._db_profile),
            "OPENAI_BASE_URL": f"{host}/serving-endpoints",
            "DATABRICKS_CONFIG_PROFILE": self._db_profile,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        }
        self._proc = spawn_omnigent_server(self._tmp, port, base_env, binding_token)
        self._wait_health()
        self._daemon = self._spawn_host_daemon(base_env)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        host_id = self._wait_host_online()
        agent_id = self._agent_id(self._vendor.agent_name)
        workspace = self._tmp / "workspace"
        workspace.mkdir(exist_ok=True)
        created = self._client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        created.raise_for_status()
        self._session_id = str(created.json()["id"])

    def _spawn_host_daemon(self, base_env: dict[str, str]) -> subprocess.Popen[bytes]:
        # Under the real $HOME so the vendor's interactive login is inherited
        # (auth cannot be relocated for native harnesses).
        log = (self._tmp / "host-daemon.log").open("wb")
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", self._base_url],
            env=apply_runner_env(base_env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log,
        )

    def _wait_health(self) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self._base_url}/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                # Connection refused while the server boots; keep polling.
                pass
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"server not healthy within {_HEALTH_TIMEOUT_S}s; logs in {self._tmp}")

    def _wait_host_online(self) -> str:
        assert self._client is not None
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            resp = self._client.get("/v1/hosts")
            if resp.status_code == 200:
                online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
                if online:
                    return str(online[0]["host_id"])
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"no host came online within {_HOST_ONLINE_TIMEOUT_S}s")

    def _agent_id(self, agent_name: str) -> str:
        assert self._client is not None
        resp = self._client.get("/v1/agents")
        resp.raise_for_status()
        for agent in resp.json()["data"]:
            if agent.get("name") == agent_name:
                return str(agent["id"])
        raise RuntimeError(f"{agent_name!r} not auto-registered on the server")

    def _teardown(self) -> None:
        if self._client is not None:
            self._client.close()
        for proc in (self._daemon, self._proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── turns ────────────────────────────────────────────────

    def _post_message(self, prompt: str) -> None:
        assert self._client is not None
        self._client.post(
            f"/v1/sessions/{self._session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            },
            timeout=30.0,
        ).raise_for_status()

    def _drive_turn(self, prompt: str, *, count_deltas: bool = False) -> TurnResult:
        """Send *prompt*; poll for the assistant reply the forwarder mirrors back.

        When *count_deltas*, also subscribe to the session SSE stream to count
        ``response.output_text.delta`` events (the same signal the full-server
        streaming probe reads — native forwards real per-chunk deltas).
        """
        assert self._client is not None
        result = TurnResult()
        if count_deltas:
            self._post_message(prompt)
            result.text_delta_count = self._count_stream_deltas()
        else:
            self._post_message(prompt)
        text = self._poll_assistant_text()
        if text is None:
            result.timed_out = True
            return result
        result.completed = True
        result.text = text
        return result

    def _count_stream_deltas(self) -> int:
        """Count response.output_text.delta events on the session SSE stream."""
        assert self._client is not None
        deltas = 0
        try:
            with self._client.stream(
                "GET", f"/v1/sessions/{self._session_id}/stream", timeout=_TURN_TIMEOUT_S
            ) as resp:
                for line in resp.iter_lines():
                    if not line.startswith("event:"):
                        continue
                    etype = line[len("event:") :].strip()
                    if etype == "response.output_text.delta":
                        deltas += 1
                    elif etype in ("response.completed", "response.failed", "session.idle"):
                        break
        except httpx.HTTPError:
            pass
        return deltas

    def _poll_assistant_text(self, timeout: float = _TURN_TIMEOUT_S) -> str | None:
        """Poll session items until an assistant item appears; return its text."""
        assert self._client is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.get(
                f"/v1/sessions/{self._session_id}/items", params={"order": "asc"}
            )
            if resp.status_code == 200:
                for item in resp.json().get("data", []):
                    if item.get("role") == "assistant":
                        text = _assistant_text(item)
                        if text:
                            return text
            time.sleep(_POLL_INTERVAL_S)
        return None

    def _drive_interrupt_turn(self) -> TurnResult:
        """Start a long turn, interrupt it, and detect the session.interrupted signal.

        Native cancellation does not persist an "interrupted" user message
        (unlike full-server); it surfaces as a ``session.interrupted`` SSE
        event, so this subscribes to the stream, posts the interrupt once the
        turn is running, and sets ``cancelled`` when that event arrives.
        """
        assert self._client is not None
        result = TurnResult()
        self._post_message(_LONG_PROMPT)
        interrupted = False
        try:
            with self._client.stream(
                "GET", f"/v1/sessions/{self._session_id}/stream", timeout=_TURN_TIMEOUT_S
            ) as resp:
                for line in resp.iter_lines():
                    if not line.startswith("event:"):
                        continue
                    etype = line[len("event:") :].strip()
                    if etype == "response.output_text.delta":
                        result.text_delta_count += 1
                        if not interrupted:
                            interrupted = True
                            self._client.post(
                                f"/v1/sessions/{self._session_id}/events",
                                json={"type": "interrupt"},
                                timeout=15.0,
                            )
                    elif etype == "session.interrupted":
                        result.cancelled = True
                        break
                    elif etype in ("response.completed", "response.failed", "session.idle"):
                        break
        except httpx.HTTPError as exc:
            result.error = repr(exc)
        return result


def _assistant_text(item: dict[str, Any]) -> str:
    """Concatenate assistant output_text blocks from a session item."""
    if item.get("role") != "assistant":
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
