# Goose-native gap plan

Status: **design / proposed** · Owner: harness · Companion harness: headless `goose` (ACP)

This document specifies how to close the open capability gaps in the
**goose-native** harness (the tmux-TUI mirror added in #955), measured against
the `harness-integration-guide` skill's native-harness checklist. It is the
output of a source-grounded gap analysis across both the omnigent codebase and
the upstream goose source (`github.com/aaif-goose/goose`, goose 1.38).

The companion **headless `goose`** harness (ACP, on main) is referenced
throughout: it drives `goose acp` and rides the structured ACP stream, so it
already solves several gaps that are architecturally hard for the TUI mirror.
Where a gap is capped on native, the headless harness is the recommended path.

---

## 1. Architecture recap (why some gaps are easy and some are hard)

goose-native is an **observe-and-relay** harness:

- **Launch** — the runner spawns `goose session --name <conv-id>` in a runner-owned
  tmux pane (`runner/app.py` `_auto_create_goose_terminal`; env from
  `goose_native_bridge.build_goose_native_spawn_env`).
- **Web → TUI** — user turns are injected into the pane via bracketed-paste
  (`inner/goose_native_executor.py`).
- **TUI → Web** — `goose_native_forwarder.py` tails goose's SQLite store
  (`~/.local/share/goose/sessions/sessions.db`) and mirrors **completed** messages
  back as `external_conversation_item` events (poll cadence 0.4s).
- **Approvals** — `goose_native_permissions.py` scrapes goose's in-terminal
  `cliclack` approval prompt and mirrors it to a web elicitation card.

The store flushes **one row per completed step** (no token deltas), and goose's
TUI exposes **no structured side-channel** for permissions, reasoning, or
compaction — those are rendered to the terminal, not emitted. This is the root
cause of the hard gaps (streaming, compaction). Conversely, anything goose
**persists** to the store (text, tool calls, **thinking**, cost, usage) is
recoverable by the forwarder, which is why most gaps are in fact fillable.

The headless `goose` harness instead consumes `goose acp`: structured
`session/update` notifications (`agent_message_chunk`, `tool_call`,
`usage_update`, `AgentThoughtChunk`) plus structured `session/request_permission`.
That is why streaming/policy/compaction are clean there.

---

## 2. Gap scoreboard (verified)

| # | Gap | Verified state on native | Plan |
|---|---|---|---|
| 1 | **Omnigent policies** | ✗ — no Omnigent eval; goose's own `GOOSE_MODE` gates | **§3 — fill on native** (the hard one) |
| 2 | **Model override** | ✗ — *bug*: model not threaded to `GOOSE_MODEL` at launch | §4.1 — fill |
| 3 | **Reasoning** (P1) | ✗ — thinking *is* persisted, forwarder drops it | §4.2 — fill |
| 4 | **Cost tracking** (P1) | ✗ — `accumulated_cost`/tokens in store, not forwarded | §4.3 — fill |
| 5 | **Resume / fork** (fork P1) | resume ✓; fork ✗ | §4.4 — fill |
| 6 | **Omnigent MCP** | ✗ by design | §5.1 — **fill (in scope, decided)** |
| 7 | **Session-cmd sync** | ✗ | §5.2 — partial (Tier 2) |
| 8 | **Elicitation (web)** | ✓ but mirrors goose's own decision | folded into §3 |
| 9 | **Images** | input ✓ (`[Attached:]`); output N/A | §5.3 — no-op |
| 10 | **Auth** | ✓ (`goose info -v`) | done |
| 11 | **Interrupt** | ✓ (tmux) | done |
| 12 | **Bidirectional sync** | ✓ | done |
| 13 | **Streaming** | complete-only | **§6 — out of scope** (use headless) |
| 14 | **Compaction** | ✗ — goose emits no signal | **§6 — out of scope** (use headless) |

---

## 3. Omnigent policies on goose-native (the centerpiece)

**Requirement** (from the skill): the harness must enforce Omnigent's three
verdicts — ALLOW / ASK / DENY — at the tool-call checkpoint, surfacing ASK as a
web approval card and blocking DENY before the tool runs.

**Why it looked blocked:** tools execute *inside* goose's own agent loop, and
goose has **no tool-hook system** (unlike Claude Code's `PreToolUse`). So there
is no callback to register an Omnigent policy check on.

**Why it is actually solvable:** goose *does* gate tools in-terminal via
`cliclack`, and we already intercept that prompt. The fix is to stop treating
the prompt as "ask a human" and start treating it as "enforce Omnigent policy,
escalate to a human only on ASK." Three facts make this work:

1. **`GOOSE_MODE=approve` forces a prompt on *every* tool.** Verified against
   `permission_inspector.rs` test `approve_requires_approval`
   (`GooseMode::Approve` + no cached permission → `RequireApproval`). By contrast
   `smart_approve` lets goose auto-allow "safe" tools *without prompting* — those
   calls would never reach the mirror. **Today the runner launches
   `GOOSE_MODE=smart_approve`** (`runner/app.py:1987`), which is the real reason
   policy is incomplete: silently-allowed tools bypass interception entirely.
2. **The structured tool call is in the store.** goose persists each call as a
   `ToolRequest { tool_call: CallToolRequestParams }` =
   `{name, arguments}` (`message.rs:82`), so we get the exact tool name + args
   from `sessions.db` — no scraping args off the screen.
3. **`POST /policies/evaluate` already does ALLOW / ASK / DENY end-to-end.**
   The route (`sessions.py:15178`) evaluates the policy engine and, on ASK,
   calls `_hold_native_ask_gate` (`sessions.py:3907`, invoked at `:15366`) which
   **publishes the approval card, parks for the web verdict, and returns a hard
   ALLOW/DENY**. One call collapses the trichotomy to a binary. The request
   shape is already built by
   `native_policy_hook.hook_payload_to_evaluation_request("PreToolUse", …)`
   → `PHASE_TOOL_CALL { name, arguments }`.

### 3.1 Design

Turn the approval mirror into a **policy enforcement point**:

```
GOOSE_MODE=approve  → goose prompts on EVERY tool (cliclack), blocks until answered
        │
   mirror detects pending prompt (existing pane poll)
        │
   read latest pending toolRequest from sessions.db → {name, arguments}   (structured)
        │
   POST /v1/sessions/{id}/policies/evaluate   (PHASE_TOOL_CALL)
        │
   ┌─────────────┬───────────────────────────┬──────────────────┐
 ALLOW          ASK (engine holds gate,      DENY            (eval error)
   │             renders web card, waits)      │                  │
 drive "Allow"   returns hard ALLOW/DENY →    drive "Deny"     drive "Deny"
 (Enter)         drive accordingly            (block)          (fail-closed)
```

Key properties:

- **Real enforcement.** With `approve` mode, goose physically cannot run the
  tool until the cliclack selector is answered. Driving "Deny" *blocks
  execution* — true DENY, not advisory.
- **Human only on ASK.** No-policy / ALLOW verdicts auto-drive "Allow" with no
  card. The engine's default for an unmatched call is ALLOW, so a session with
  no policies configured runs goose freely (parity with headless).
- **Unifies policy + elicitation.** The ASK card is now rendered by the policy
  engine from the **structured** tool name/args (better preview than today's
  scraped subject), and the existing `native-permission-request` blind-ask path
  is retired in favor of `/policies/evaluate`.
- **Fail-closed.** Eval error / mirror crash → the tool stays blocked at the
  cliclack prompt (a human can still answer in the terminal — existing
  fallback). This matches the `native_policy_hook` fail-closed contract.

### 3.2 What changes

- `runner/app.py:1987` — `GOOSE_MODE`: `smart_approve` → **`approve`** (with a
  comment explaining it is required for complete policy interception). This is
  the single most important change.
- `goose_native_forwarder.py` (or a small shared helper) — add
  `read_pending_tool_request(db_path, session) -> {name, arguments} | None`
  that returns the most recent `toolRequest` not yet paired with a
  `toolResponse` (the call awaiting approval).
- `goose_native_permissions.py` — between "prompt detected" and "drive
  selector," call `/policies/evaluate` with the structured call and branch on
  the verdict. Keep the pane scrape only as the **liveness signal** (is a prompt
  showing?) and the **actuator** (drive Allow/Deny); the *decision* moves to the
  engine. Retire the hardcoded `policy_name="goose_native_permission"` blind ask.
- Tests: unit-test the verdict→keystroke mapping (ALLOW→Enter, DENY→Down×N+Enter,
  ASK→card-then-drive, error→fail-closed) and the pending-toolRequest reader.

### 3.3 Honest residual limits

- **Tool-*result* checkpoint is not enforceable on native.** goose's cliclack
  only prompts *before* execution; there is no post-execution hook, so
  `PHASE_TOOL_RESULT` ASK/DENY cannot *block* a result goose has already
  returned to the model. We can post-hoc evaluate the `toolResponse` row for
  **audit/observability**, but not enforcement. The headless harness enforces
  both checkpoints. This is a goose limitation (no post-tool hook), documented,
  not engineered around. **Decision: in scope, scheduled last** — implement the
  post-hoc `toolResponse` audit evaluation (`PHASE_TOOL_RESULT`,
  non-blocking/observability-only) after all enforcement + gap-fills are working.
- **Latency / chattiness.** `approve` mode prompts on every tool; each adds a
  policy round-trip + a cliclack drive. **Decision (v1):** fast-path "no policies
  configured for this session → auto-drive Allow without the round-trip," so the
  per-tool cost is paid only when policies actually exist. Most matched calls
  still resolve to auto-ALLOW server-side (fast).
- **Scrape brittleness remains** for prompt *detection* and *actuation* (the
  cliclack strings are stable per the source, but it is still screen-driving).
  The decision path is now robust (structured + engine); only the actuator is
  scrape-based.

---

## 4. Tier 1 — P1 + the launch bug (all native, all independently shippable)

### 4.1 Model override (it's a bug)

`build_goose_native_spawn_env` accepts `model=` and sets `GOOSE_MODEL`
(`goose_native_bridge.py:99`), but the runner calls it **without** the model
override (`runner/app.py:8111`, `:12874`), so the user's model pick never
reaches goose — it silently uses its configured default.

- **Fix:** thread `model_override → GOOSE_MODEL` at spawn (both call sites). Add
  goose provider/model entries to `model_catalog.py`.
- **Mid-session switch:** not possible — goose reads `GOOSE_MODEL` only at launch
  (no ACP `set_model`, no `/model` command). The only honest implementation is
  relaunch + history replay, which reuses the §4.4 fork/resume machinery. Scope
  mid-session as a follow-up, not Tier 1.
- Effort: launch fix ~½ day; catalog ~½ day. Risk: low.

### 4.2 Reasoning forwarding (P1) — the corrected finding

goose **persists** reasoning: `MessageContent::Thinking` serializes as
`{"type":"thinking","thinking":"…"}` into `content_json` (`message.rs:279`,
`:41`) and also streams over ACP as `AgentThoughtChunk` (`acp/server.rs:1350`).
The native forwarder's `_content_text` only extracts `{"type":"text"}` and
treats thinking-only turns as "reasoning-only turn with no prose" → **drops
them** (`goose_native_forwarder.py:196,255`).

- **Fix:** split content extraction so `{"type":"thinking"}` parts emit a
  reasoning event (mirror codex-native's `output_reasoning`) instead of being
  discarded. Redacted thinking → a redacted marker.
- Effort: ~1–2 days incl. tests. Risk: low. **Lowest-risk P1 — do first.**

### 4.3 Cost tracking (P1)

goose persists `accumulated_cost` + `accumulated_input/output_tokens` in the
store, and ACP carries `usage_update.accumulated_cost`.

- **Fix:** new `goose_native_usage.py` poller modeled on
  `cursor_native_usage.py` — read the store, POST `external_session_usage`,
  dedup by message id, handle the **fork-resets-accumulators** edge case
  (accumulated_* restart in a forked session).
- Effort: ~1–1.5 days incl. tests. Risk: low.

### 4.4 Resume / fork (fork is P1)

Resume already works (live reattach + cold relaunch `goose session --name <id>`,
which reloads prior messages from the store). **Fork is not wired.** Both a CLI
`--fork` and an ACP `ForkSessionRequest` handler exist upstream
(`acp/server/fork_session.rs`, `dispatch.rs:375`).

- **Fix (easy path):** Omnigent SDK fork already works (`chat.py:1663`); pass the
  forked conv-id to goose-native and relaunch `goose session --name <new-id>` —
  goose loads the copied history from its own store. No fork-preamble needed
  (unlike cursor, whose history is server-side).
- **Optional parity:** cursor-style fork-preamble for explicit text continuity.
- Wire a `fork_session_id` param through `run_goose_native` →
  `_prepare_goose_terminal_via_daemon`.
- Effort: ~2 days easy path. Risk: low–medium.

---

## 5. Tier 2 — native polish

### 5.1 Omnigent MCP — **in scope (decided)**

goose loads MCP servers via `--with-extension <cmd>` (stdio),
`--with-streamable-http-extension <url>` (HTTP) (`cli.rs:163,172`),
`config.yaml`, or ACP `extensions/add`. Today the runner writes none (by design).

- **Approach:** `--with-streamable-http-extension <omnigent-relay-url>` at
  launch — no user-config mutation, per-session, points goose at the same
  serve-mcp relay the other native harnesses use
  (`claude_native_bridge serve-mcp`). The `config.yaml`-write alternative
  mutates user state and needs a consent guard, so prefer the launch flag.
- **Synergy with §3:** goose gates extension tools with `GOOSE_MODE`, so the
  Omnigent MCP tools flow through the **same §3 policy path** as any other tool
  — one enforcement point covers both goose builtins and Omnigent MCP. (We do
  *not* want to double-evaluate: `mcp__omnigent__*` tools are already
  policy-checked on the relay path, and `hook_payload_to_evaluation_request`
  skips them — `native_policy_hook.py:108` — so the §3 gate must apply the same
  skip and let the relay gate own those.)
- Decided to fill on native despite the headless harness also having MCP.
- Effort: ~2–3 days. Risk: medium (goose HTTP-extension maturity).

### 5.2 In-harness session-cmd sync

goose advertises `available_commands` over ACP (`compact`, `clear`, `prompts`,
`skills`, …; `acp/response_builder.rs:364`). `/fork` and `/resume` are *not*
in-session goose commands — they are CLI relaunch (→ §4.4).

- **Fill:** wire web `/clear` → inject into the pane; surface goose's command
  list through the #1168 composer-discovery path.
- Effort: ~2 days. Risk: low–medium.

### 5.3 Images

Input works (materialized to disk + `[Attached: <path>]` marker). goose does not
emit image **output**, so there is nothing to mirror. **No-op** until goose
gains image output.

---

## 6. Out of scope (use the headless `goose` harness)

Per decision: **skip streaming and compaction on native.**

- **Token streaming** — the store flushes only completed steps; there are no
  partial deltas to tail. Token streaming requires the ACP stream → headless
  (already streams, verified). The native live-streaming surface is the
  terminal.
- **Compaction** — goose emits no structured signal (only a user-visible
  "Compaction complete" string; the `StatusMessage::Notice` enum has no emitter).
  Usage-delta heuristics are false-positive-prone. Best long-term fix is an
  upstream goose signal; until then neither path surfaces it reliably.

Recommendation: document that **policy/streaming/compaction-sensitive users
should select the headless `goose` harness**, which solves all three natively.

---

## 7. Sequencing, effort, risk

**PR scope (decided):** policies (§3) **and** Omnigent MCP (§5.1) ship together
with the Tier-1 gap-fills (§4) as **one PR** — the user asked for policies in the
same PR, and MCP shares the §3 enforcement path, so they are one coherent unit.
`/clear` + command-discovery and resume-hardening (§5.2) may follow as a small
Tier-2 PR if this one grows too large to review.

| Group | Items | Effort | Risk |
|---|---|---|---|
| **Policy** (§3) | `GOOSE_MODE=approve`; pending-toolRequest reader; mirror→`/policies/evaluate`; no-policy fast-path; tests | ~3–4 days | medium (scrape actuator) |
| **MCP** (§5.1) | `--with-streamable-http-extension` at launch; `mcp__omnigent__*` skip in the §3 gate | ~2–3 days | medium |
| **Tier-1** (§4) | reasoning → model-launch → cost → fork | ~5–6 days | low–medium |

Suggested build order within the PR: **§3 policy first** (the explicit
requirement; recasts the existing elicitation mirror), then §4.2 reasoning
(lowest-risk P1), then §5.1 MCP (rides the §3 gate), then §4.1/§4.3/§4.4.

## 8. Test plan

- **Policy (§3):** unit — verdict→keystroke mapping incl. fail-closed;
  pending-toolRequest reader; `smart_approve`→`approve` env assertion. E2E
  (opt-in, like `test_goose_native_cli_e2e.py`) — configure an ASK policy on a
  tool, drive a turn, assert the web card appears and the verdict gates goose;
  configure a DENY policy, assert the tool is blocked.
- **Tier 1:** unit per item (thinking extraction; `GOOSE_MODEL` threading;
  usage dedup + fork-reset; fork relaunch arg-building). Mock-LLM happy path.
- All work on branch `goose-native-gaps`; keep `package-lock.json` / `uv.lock`
  clean (no proxy leak).

## 9. Decisions (resolved 2026-06-25)

1. **MCP on native (§5.1):** **yes — in scope.** Fill via
   `--with-streamable-http-extension`; tools ride the §3 policy gate (with the
   `mcp__omnigent__*` skip to avoid double-evaluation).
2. **Policy PR scope (§7):** **policies ship in the same PR** as the MCP +
   Tier-1 gap-fills, not as a separate phased PR.
3. **`approve`-mode chattiness (§3.2):** **include the no-policy fast-path in
   v1** — `approve` mode prompts on every tool, so the per-tool round-trip is
   only paid when policies actually exist.
4. **Tool-result audit (§3.3):** **in scope, scheduled last** — implement the
   non-blocking post-hoc `PHASE_TOOL_RESULT` audit evaluation after everything
   else works.
