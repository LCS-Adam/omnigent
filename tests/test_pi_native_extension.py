"""End-to-end tests for the generated pi-native bridge extension."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_delivery_cap_drops_followup_without_failed_session_status(
    tmp_path: Path,
) -> None:
    """The extension must not terminal-fail a session when follow-up delivery caps.

    This runs the real JavaScript extension under Node with a real inbox payload
    and mocked Pi/fetch boundaries. Five consecutive ``sendUserMessage`` throws
    should consume the inbox file and emit an informational conversation item,
    never ``external_session_status`` with ``status: "failed"``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-msg.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({ id: "msg-1", type: "user_message", content: "follow up" }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
    authHeaders: { authorization: "Bearer test" },
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const sendAttempts = [];
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage(content, options) {
    sendAttempts.push({ content, options });
    throw new Error("Pi is not ready");
  },
};

require(extensionPath)(pi);

(async () => {
  assert.equal(typeof handlers.session_start, "function");
  await handlers.session_start({}, {
    sessionManager: { getSessionId: () => "native-session-1" },
    ui: { setTitle() {}, setStatus() {}, notify() {} },
  });
  assert.equal(typeof pollInbox, "function");

  for (let attempt = 0; attempt < 5; attempt += 1) {
    pollInbox();
  }
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(
    sendAttempts,
    Array.from({ length: 5 }, () => ({
      content: "follow up",
      options: { deliverAs: "followUp" },
    })),
  );
  assert.equal(fs.existsSync(payloadPath), false);
  assert.equal(
    postedEvents.some(
      (event) =>
        event.type === "external_session_status" &&
        event.data &&
        event.data.status === "failed",
    ),
    false,
    JSON.stringify(postedEvents),
  );

  const dropNote = postedEvents.find(
    (event) =>
      event.type === "external_conversation_item" &&
      event.data &&
      event.data.item_type === "error" &&
      event.data.item_data &&
      event.data.item_data.code === "pi_followup_delivery_dropped",
  );
  assert.ok(dropNote, JSON.stringify(postedEvents));
  assert.equal(dropNote.data.item_data.source, "execution");
  assert.match(dropNote.data.response_id, /^pi-deliver-dropped-/);
  // The note must be actionable: include the dropped message id and a preview
  // of its content so an operator can identify what was lost.
  assert.match(dropNote.data.item_data.message, /msg-1/);
  assert.match(dropNote.data.item_data.message, /follow up/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def _extension_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )


def _run_node(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")
    return subprocess.run(
        [node, "-e", script, *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


# Shared Node preamble: load the real extension with mocked fetch/setInterval/pi,
# drive its event handlers, and expose the posted event bodies.
_STREAMING_HARNESS = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const configPath = path.join(tmpDir, "config.json");

fs.writeFileSync(
  configPath,
  JSON.stringify({ serverUrl: "http://omnigent.test", sessionId: "session-1" }),
);
process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const posted = [];
global.fetch = async (_url, request) => {
  posted.push(JSON.parse(request.body));
  return { ok: true, async json() { return { result: "" }; } };
};
global.setInterval = () => ({ fakeInterval: true });

const handlers = {};
const pi = {
  registerCommand() {},
  on(name, handler) { handlers[name] = handler; },
  sendUserMessage() {},
};

require(extensionPath)(pi);

const ctx = { isIdle: () => false, ui: { setTitle() {}, setStatus() {}, notify() {} } };

// Helpers to build the Pi AssistantMessageEvent shapes the extension consumes.
function textDelta(contentIndex, delta) {
  return { type: "text_delta", contentIndex, delta, partial: {} };
}
function textEnd(contentIndex, content) {
  return { type: "text_end", contentIndex, content, partial: {} };
}
async function feed(assistantMessageEvent) {
  await handlers.message_update({ assistantMessageEvent }, ctx);
}
async function endMessage(text) {
  await handlers.message_end(
    {
      message: {
        role: "assistant",
        content: [{ type: "text", text }],
      },
    },
    ctx,
  );
}

function deltas() {
  return posted.filter((e) => e.type === "external_output_text_delta");
}
function items() {
  return posted.filter((e) => e.type === "external_conversation_item");
}
function assistantText(item) {
  return item.data.item_data.content.map((b) => b.text).join("");
}
"""


def test_text_deltas_post_incrementally_with_stable_id() -> None:
    """Each token posts as an external_output_text_delta with a stable id.

    Drives the real extension with a sequence of Pi ``text_delta`` events for
    one assistant message, then ``message_end``. Asserts: every chunk shares one
    ``message_id``, chunk ``index`` is monotonic from 0, the joined deltas equal
    the streamed text, a final marker closes the stream, and the authoritative
    assistant item carries the full text exactly once (no duplication).
    """
    script = (
        _STREAMING_HARNESS
        + r"""
(async () => {
  await handlers.agent_start({}, ctx);
  await handlers.turn_start({ turnIndex: 1 }, ctx);

  const chunks = ["Hello", ", ", "world", "!"];
  for (const c of chunks) await feed(textDelta(0, c));
  await feed(textEnd(0, chunks.join("")));
  await endMessage(chunks.join(""));

  const ds = deltas();
  // One text chunk per delta plus a single final marker.
  const textChunks = ds.filter((d) => d.data.delta !== "");
  const finals = ds.filter((d) => d.data.final === true);
  assert.equal(textChunks.length, chunks.length, JSON.stringify(ds));
  assert.equal(finals.length, 1, JSON.stringify(ds));

  // Stable message_id across every chunk and the final marker.
  const ids = new Set(ds.map((d) => d.data.message_id));
  assert.equal(ids.size, 1, "expected one stable message_id: " + JSON.stringify([...ids]));
  const messageId = [...ids][0];
  assert.ok(typeof messageId === "string" && messageId.length > 0);

  // Monotonic, gapless index from 0.
  const indices = ds.map((d) => d.data.index);
  assert.deepEqual(indices, indices.map((_, i) => i), JSON.stringify(indices));

  // Joined streamed deltas equal the streamed text.
  assert.equal(textChunks.map((d) => d.data.delta).join(""), chunks.join(""));

  // The final marker is last and carries no new text.
  assert.equal(ds[ds.length - 1].data.final, true);
  assert.equal(ds[ds.length - 1].data.delta, "");

  // Exactly one authoritative assistant item, carrying the full text once.
  const assistantItems = items().filter(
    (i) => i.data.item_type === "message" && i.data.item_data.role === "assistant",
  );
  assert.equal(assistantItems.length, 1, JSON.stringify(assistantItems));
  assert.equal(assistantText(assistantItems[0]), chunks.join(""));
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exit(1); });
"""
    )
    result = _run_node(script, str(_extension_path()), "/tmp")
    assert result.returncode == 0, result.stdout + result.stderr


def test_multiple_text_blocks_share_one_message_preview(tmp_path: Path) -> None:
    """Multiple text blocks in one message stream under one message_id.

    The web UI finalizes the oldest live preview per authoritative item (FIFO,
    one item per message), so a message with two text blocks (e.g. text → tool
    call → text) must stream as ONE growing preview — not two — or the second
    preview is orphaned. Asserts both blocks' chunks share one ``message_id``
    with a single monotonic index.
    """
    script = (
        _STREAMING_HARNESS
        + r"""
(async () => {
  await handlers.agent_start({}, ctx);
  await handlers.turn_start({ turnIndex: 1 }, ctx);

  // Text block 0, a tool call at index 1, then text block 2.
  await feed(textDelta(0, "First "));
  await feed(textDelta(0, "part."));
  await feed(textEnd(0, "First part."));
  await feed(textDelta(2, " Second "));
  await feed(textDelta(2, "part."));
  await feed(textEnd(2, " Second part."));
  await endMessage("First part. Second part.");

  const ds = deltas();
  const ids = new Set(ds.map((d) => d.data.message_id));
  assert.equal(ids.size, 1, "both blocks must share one id: " + JSON.stringify([...ids]));

  const textChunks = ds.filter((d) => d.data.delta !== "");
  assert.equal(textChunks.length, 4, JSON.stringify(ds));
  // Single monotonic index spanning both blocks.
  const indices = ds.map((d) => d.data.index);
  assert.deepEqual(indices, indices.map((_, i) => i), JSON.stringify(indices));
  assert.equal(
    textChunks.map((d) => d.data.delta).join(""),
    "First part. Second part.",
  );
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exit(1); });
"""
    )
    result = _run_node(script, str(_extension_path()), str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_successive_messages_in_turn_get_distinct_ids(tmp_path: Path) -> None:
    """Two assistant messages in one turn stream under distinct message_ids.

    After a tool round-trip Pi begins a fresh assistant message. Its preview
    must NOT reuse the first message's (finalized) id, or the web UI would fold
    the new text into the already-committed first bubble. Asserts the second
    message's deltas carry a different ``message_id`` whose index restarts at 0.
    """
    script = (
        _STREAMING_HARNESS
        + r"""
(async () => {
  await handlers.agent_start({}, ctx);
  await handlers.turn_start({ turnIndex: 1 }, ctx);

  await feed(textDelta(0, "Looking"));
  await feed(textEnd(0, "Looking"));
  await endMessage("Looking");

  // Second assistant message (after a tool round-trip) in the same turn.
  await feed(textDelta(0, "Done"));
  await feed(textEnd(0, "Done"));
  await endMessage("Done");

  const ds = deltas();
  const ids = [...new Set(ds.map((d) => d.data.message_id))];
  assert.equal(ids.size === undefined ? ids.length : ids.length, 2, JSON.stringify(ids));

  // Group indices per id; each must restart at 0 and be monotonic.
  const byId = new Map();
  for (const d of ds) {
    if (!byId.has(d.data.message_id)) byId.set(d.data.message_id, []);
    byId.get(d.data.message_id).push(d.data.index);
  }
  for (const [, idxs] of byId) {
    assert.deepEqual(idxs, idxs.map((_, i) => i), JSON.stringify(idxs));
  }

  // Two authoritative items, one per message, no cross-contamination.
  const assistantItems = items().filter(
    (i) => i.data.item_type === "message" && i.data.item_data.role === "assistant",
  );
  assert.equal(assistantItems.length, 2);
  assert.equal(assistantText(assistantItems[0]), "Looking");
  assert.equal(assistantText(assistantItems[1]), "Done");
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exit(1); });
"""
    )
    result = _run_node(script, str(_extension_path()), str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_message_without_streamed_text_posts_no_delta(tmp_path: Path) -> None:
    """A message_end with no preceding text_delta emits no delta events.

    A tool-only assistant message (or a non-streaming path) must not post a
    stray final marker — there is no live preview to close, so a spurious delta
    could create an empty preview bubble in the web UI.
    """
    script = (
        _STREAMING_HARNESS
        + r"""
(async () => {
  await handlers.agent_start({}, ctx);
  await handlers.turn_start({ turnIndex: 1 }, ctx);

  // No text_delta at all — just the authoritative item.
  await endMessage("");

  assert.equal(deltas().length, 0, JSON.stringify(deltas()));
})().catch((e) => { console.error(e && e.stack ? e.stack : e); process.exit(1); });
"""
    )
    result = _run_node(script, str(_extension_path()), str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_compact_payload_triggers_ctx_compact_and_brackets_spinner(
    tmp_path: Path,
) -> None:
    """A ``compact`` inbox payload calls ``ctx.compact()`` and brackets the spinner.

    Runs the real JavaScript extension under Node. A queued ``compact`` payload
    must (1) call the resident ``ExtensionContext.compact()`` with the custom
    instructions and ``onComplete``/``onError`` callbacks, (2) post
    ``external_compaction_status`` ``in_progress`` BEFORE compact() so the web UI
    spinner is bracketed, and (3) post ``completed`` once Pi invokes
    ``onComplete``. The payload file must be consumed (unlinked) so compaction
    is not re-triggered every poll tick.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-compact.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({
    id: "compact-1",
    type: "compact",
    custom_instructions: "focus on the refactor",
  }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
    authHeaders: { authorization: "Bearer test" },
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const compactCalls = [];
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage() {},
};

require(extensionPath)(pi);

// The resident context the poller compacts. compact() is fire-and-forget; we
// simulate Pi finishing successfully by invoking the onComplete callback.
const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
  abort() {},
  isIdle: () => false,
  compact(options) {
    compactCalls.push(options);
    if (options && typeof options.onComplete === "function") {
      options.onComplete({ summary: "done" });
    }
  },
};

(async () => {
  // session_start remembers ctx and starts the inbox poller.
  await handlers.session_start({}, ctx);
  assert.equal(typeof pollInbox, "function");

  pollInbox();
  await new Promise((resolve) => setImmediate(resolve));

  // 1) ctx.compact() was called exactly once with the custom instructions and
  // both lifecycle callbacks.
  assert.equal(compactCalls.length, 1, JSON.stringify(compactCalls));
  assert.equal(compactCalls[0].customInstructions, "focus on the refactor");
  assert.equal(typeof compactCalls[0].onComplete, "function");
  assert.equal(typeof compactCalls[0].onError, "function");

  // 2/3) The spinner is bracketed: in_progress posted before compact(), then
  // completed from onComplete. No failed edge on the happy path.
  const compactionStatuses = postedEvents
    .filter((event) => event.type === "external_compaction_status")
    .map((event) => event.data && event.data.status);
  assert.deepEqual(
    compactionStatuses,
    ["in_progress", "completed"],
    JSON.stringify(postedEvents),
  );

  // The payload file is consumed so compaction is not re-triggered each tick.
  assert.equal(fs.existsSync(payloadPath), false);

  // A second poll tick must NOT re-trigger compaction (file gone + deduped).
  pollInbox();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(compactCalls.length, 1, "compaction must not re-trigger");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_compact_payload_without_ctx_compact_posts_no_status_and_consumes_file(
    tmp_path: Path,
) -> None:
    """A ``compact`` payload with no compactable context strands nothing.

    Runs the real JavaScript extension under Node with a resident context that
    exposes no ``compact`` function (e.g. an older Pi without the extension
    compaction API). ``triggerCompaction`` returns early WITHOUT posting an
    ``in_progress`` edge, so the web UI spinner — created only by the
    ``response.compaction.in_progress`` SSE — is never raised and cannot strand.
    This pins the safety property the review relies on: zero
    ``external_compaction_status`` events, no throw, and the payload file is still
    consumed (unlinked) so the poller does not re-read it forever.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-compact.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({ id: "compact-1", type: "compact" }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage() {},
};

require(extensionPath)(pi);

// Resident context WITHOUT a compact() function: triggerCompaction must
// short-circuit and post nothing.
const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
  abort() {},
  isIdle: () => false,
};

(async () => {
  await handlers.session_start({}, ctx);
  assert.equal(typeof pollInbox, "function");

  // Must not throw even though ctx.compact is absent.
  pollInbox();
  await new Promise((resolve) => setImmediate(resolve));

  const compactionStatuses = postedEvents.filter(
    (event) => event.type === "external_compaction_status",
  );
  // No spinner is ever raised: zero compaction-status edges (most importantly
  // no in_progress), so nothing can strand.
  assert.deepEqual(compactionStatuses, [], JSON.stringify(postedEvents));

  // The payload file is still consumed so the poller does not re-read it.
  assert.equal(fs.existsSync(payloadPath), false);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_compact_payload_synchronous_throw_dismisses_spinner(
    tmp_path: Path,
) -> None:
    """A synchronous throw from ``ctx.compact()`` still dismisses the spinner.

    ``triggerCompaction`` posts ``in_progress`` BEFORE calling the fire-and-forget
    ``ctx.compact()``. If that call throws synchronously (before any
    ``onComplete``/``onError`` can fire), the catch must post ``failed`` so the
    raised spinner is dismissed rather than stranded. Edges must be exactly
    ``[in_progress, failed]`` — distinct from the ``onError`` path, which reaches
    ``failed`` via the callback.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-compact.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({ id: "compact-1", type: "compact" }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage() {},
};

require(extensionPath)(pi);

const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
  abort() {},
  isIdle: () => false,
  compact() {
    throw new Error("compact() exploded synchronously");
  },
};

(async () => {
  await handlers.session_start({}, ctx);
  // Must not throw out of the poller even though compact() throws.
  pollInbox();
  await new Promise((resolve) => setImmediate(resolve));

  const compactionStatuses = postedEvents
    .filter((event) => event.type === "external_compaction_status")
    .map((event) => event.data && event.data.status);
  // in_progress was posted before compact(); the synchronous throw is caught
  // and posts failed to dismiss the spinner.
  assert.deepEqual(
    compactionStatuses,
    ["in_progress", "failed"],
    JSON.stringify(postedEvents),
  );

  // The payload file is consumed so compaction is not re-triggered each tick.
  assert.equal(fs.existsSync(payloadPath), false);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_compact_payload_failure_dismisses_spinner(tmp_path: Path) -> None:
    """When ``ctx.compact()`` reports an error, the extension posts ``failed``.

    Pi's ``compact()`` surfaces failures through the ``onError`` callback. The
    extension must publish ``external_compaction_status`` ``failed`` so the web
    UI's "Compacting…" spinner is dismissed rather than stranded.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-compact.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({ id: "compact-1", type: "compact" }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage() {},
};

require(extensionPath)(pi);

const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
  abort() {},
  isIdle: () => false,
  compact(options) {
    if (options && typeof options.onError === "function") {
      options.onError(new Error("compaction blew up"));
    }
  },
};

(async () => {
  await handlers.session_start({}, ctx);
  pollInbox();
  await new Promise((resolve) => setImmediate(resolve));

  const compactionStatuses = postedEvents
    .filter((event) => event.type === "external_compaction_status")
    .map((event) => event.data && event.data.status);
  // in_progress raised the spinner; failed (from onError) dismisses it.
  // completed must never fire on an errored compaction.
  assert.deepEqual(
    compactionStatuses,
    ["in_progress", "failed"],
    JSON.stringify(postedEvents),
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
