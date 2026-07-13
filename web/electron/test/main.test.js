// Regression guard for how src/main.js WIRES workspace-chrome injection, run
// with `node --test` (no extra deps). The wiring itself lives in
// src/workspace-chrome.js (registerWorkspaceChromeHide registers a
// did-finish-load listener that injects the chrome-hide CSS) and its BEHAVIOR is
// unit-tested in workspace-chrome.test.js. This guards the complementary half
// that no behavior test can see: that main.js still actually INVOKES
// registerWorkspaceChromeHide(win.webContents) as live code — not removed, not
// commented out.
//
// A naive source-string match would pass even if the call were commented out
// (the text still appears in the comment), so we strip comments from the source
// before asserting. URL slashes (`https://`) are preserved by only treating a
// `//` NOT preceded by `:` as a line comment. (This cannot prove the call runs
// at runtime — only an Electron launch could — but it does catch the call being
// removed or commented out, which the behavior test in workspace-chrome.test.js
// cannot, because that test never touches main.js.)

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");

// Strip block comments, then line comments (leaving `://` in URLs intact).
const liveCode = mainSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

describe("workspace chrome injection wiring (src/main.js)", () => {
  it("invokes registerWorkspaceChromeHide(win.webContents) as live code", () => {
    assert.match(
      liveCode,
      /registerWorkspaceChromeHide\(win\.webContents\)/,
      [
        "src/main.js no longer has a live registerWorkspaceChromeHide(win.webContents)",
        "call (it was removed or commented out). That call wires the did-finish-load",
        "listener that injects WORKSPACE_CHROME_HIDE_CSS to hide the Databricks workspace",
        "top-nav/switcher in the desktop window. Without it the switcher reappears and users",
        "can navigate out of Omnigent into other workspace apps. Re-add the call (the wiring",
        "is defined in src/workspace-chrome.js); do not delete this test.",
      ].join(" "),
    );
  });

  it("does not gate the wiring behind a URL/path check", () => {
    assert.doesNotMatch(
      liveCode,
      /registerWorkspaceChromeHide[\s\S]{0,200}(WORKSPACE_UI_PATH|pathname|startsWith)/,
      [
        "A URL/path gate was reintroduced around the chrome-hide wiring. It must stay",
        "UNCONDITIONAL: the original bug gated on pathname.startsWith(WORKSPACE_UI_PATH),",
        "which skipped injection on auth redirects and path variants and left the workspace",
        "switcher visible. The CSS targets .omnigent-app (workspace-embedded build only), so",
        "injecting on every load is a safe no-op elsewhere. See src/workspace-chrome.js.",
      ].join(" "),
    );
  });
});

// Wiring guards for the window-open policy (src/popupPolicy.js decides,
// main.js enforces). The policy's BEHAVIOR is unit-tested in
// popupPolicy.test.js; these guard the enforcement half no behavior test
// can see: that main.js still routes window.open through the policy, and
// that an allowed OAuth popup is created HARDENED (no shell preload,
// sandboxed) and then run through hardenOauthPopup. Losing any of these
// silently reopens the chromeless-credential-window hole the policy exists
// to close.
describe("window-open policy wiring (src/main.js)", () => {
  it("routes setWindowOpenHandler decisions through decideWindowOpen as live code", () => {
    assert.match(
      liveCode,
      /setWindowOpenHandler\(\s*\(\{\s*url,\s*disposition,\s*features\s*\}\)\s*=>\s*\{[\s\S]{0,200}decideWindowOpen\(/,
      [
        "src/main.js no longer passes window.open through decideWindowOpen (src/popupPolicy.js).",
        "Without the policy, either every popup is denied (OAuth sign-in breaks again — the",
        "callback needs window.opener + the opener's localStorage) or, worse, popups are",
        "allowed without the pinned-opener/https/allowlist conditions. Restore the",
        "decideWindowOpen dispatch; do not inline a weaker check.",
      ].join(" "),
    );
  });

  it("attaches the no-op popup preload and sandbox to allowed popups", () => {
    assert.match(
      liveCode,
      /preload:\s*POPUP_PRELOAD[\s\S]{0,120}sandbox:\s*true/,
      [
        "The allowed-popup overrideBrowserWindowOptions no longer force preload: POPUP_PRELOAD",
        "and sandbox: true. Without them the child window can inherit the SHELL preload",
        "(omnigentDesktop/omnigentSetup IPC bridges) and run unsandboxed while showing",
        "third-party sign-in pages. Restore both overrides (see popup_preload.js).",
      ].join(" "),
    );
  });

  it("hardens created popups via did-create-window → hardenOauthPopup as live code", () => {
    assert.match(
      liveCode,
      /did-create-window[\s\S]{0,120}hardenOauthPopup\(/,
      [
        "src/main.js no longer runs allowed popups through hardenOauthPopup on",
        "did-create-window. That call stamps the current host into the popup's title",
        "(the only origin indicator a chromeless window has), and denies",
        "popups-from-popups. Re-add the wiring; do not delete this test.",
      ].join(" "),
    );
  });
});

// Guards for the popup ↔ localhost-trust bridge. E2E-verified failure mode
// when this wiring is lost: Okta FastPass runs INSIDE the OAuth popup,
// queries the Local Network Access permission for its localhost helper,
// receives "denied" (the popup is deliberately not a shell window), and
// fails closed — "The browser is blocking communication with Okta Verify" —
// which blocks sign-in for every Okta-fronted provider.
describe("OAuth popup localhost trust wiring (src/main.js)", () => {
  it("registers popups in oauthPopups inside hardenOauthPopup as live code", () => {
    assert.match(
      liveCode,
      /function hardenOauthPopup\(child\)\s*\{[\s\S]{0,120}oauthPopups\.add\(child\)/,
      [
        "hardenOauthPopup no longer registers the popup in oauthPopups (with a matching",
        "closed → delete). Without the registry entry, isCurrentPopupOrigin never matches,",
        "the popup's IdP pages get a denied LNA answer, and Okta FastPass fails closed",
        "inside every sign-in popup. Restore the oauthPopups.add(child) + cleanup.",
      ].join(" "),
    );
  });

  it("extends isLocalhostTrustedOrigin to live popup pages as live code", () => {
    assert.match(
      liveCode,
      /function isLocalhostTrustedOrigin\(origin\)\s*\{[\s\S]{0,300}isCurrentPopupOrigin\(origin\)/,
      [
        "isLocalhostTrustedOrigin no longer consults isCurrentPopupOrigin. The popup's",
        "current top-level page needs the same auth-surface localhost trust as a shell",
        "window's (Okta FastPass probes its localhost helper from inside the popup);",
        "without this line the popup OAuth flow breaks for every Okta-fronted provider.",
      ].join(" "),
    );
  });
});
