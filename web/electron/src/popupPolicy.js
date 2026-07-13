// Policy for page-initiated window.open / target=_blank (the
// setWindowOpenHandler path in main.js). The default stays "leave the
// shell": web links open in the user's real browser — whose URL bar,
// password manager, and Safe Browsing beat any chromeless Electron window —
// and non-web schemes (vscode://, ssh://) need a consent dialog because
// shell.openExternal launches OS protocol handlers with page-controlled
// arguments and no prompt of its own.
//
// The ONE exception is an OAuth sign-in popup. The workspace UI's OAuth
// flows (connect an MCP service, Catalog Explorer connections, OneChat)
// deliver the authorization code back to the page via
// `window.opener.postMessage` plus a nonce in the OPENER's localStorage —
// both exist only in a real same-profile child window. Punting the popup to
// the external browser severs opener and profile, strands the code, and the
// flow dies ("Sign-in failed"). So a window.open is allowed as a real child
// window when ALL of:
//
//   1. It is popup-SHAPED: disposition "new-window" with explicit
//      width/height features. Pages only pass size features when they want
//      a popup; plain links and bare window.open arrive as
//      "foreground-tab" and keep going external.
//   2. The opener window is pinned AND currently ON its pinned origin —
//      a page reached mid-SSO-redirect on a foreign origin gets nothing.
//   3. The target is https and its origin is the pinned server itself, a
//      well-known OAuth authorization origin (below), or hand-listed in
//      settings.json `popup_allowed_origins`. This is the anti-phishing
//      gate: page content can NEVER open an arbitrary URL in a chromeless
//      window; at most it can open a known sign-in host (landing anywhere
//      else from there requires an open redirect on that host).
//
// Everything the policy allows is additionally hardened by the caller —
// sandboxed, stripped of the shell preload, host stamped into the window
// title, and denied popups of its own (see hardenOauthPopup in main.js).
//
// Runs in the main process (the gate holds regardless of caller); pure +
// dep-free so `node --test` can exercise it without Electron.

"use strict";

/**
 * Schemes that open externally with no confirmation: they land in the
 * user's browser / mail client, which apply their own safety UX. Anything
 * else launches an OS protocol handler (vscode://, ssh://, …) with
 * page-controlled arguments — and `shell.openExternal`, unlike a browser,
 * shows no prompt of its own — so it goes through a consent dialog first.
 */
const WEB_SCHEMES = new Set(["http:", "https:", "mailto:"]);

/**
 * Origins of the OAuth authorization endpoints behind the workspace's
 * managed connections (the `system.ai.*` MCP services and the Catalog
 * Explorer / ingestion connection types). Mirrors the endpoints the
 * workspace web-shared OAuth utilities actually open; extend via
 * settings.json `popup_allowed_origins` rather than editing this list for
 * a private deployment.
 */
const OAUTH_POPUP_ORIGINS = new Set([
  "https://github.com", // GitHub (system.ai.github)
  "https://accounts.google.com", // Google Drive / GA4 / Google Ads
  "https://slack.com", // Slack (system.ai.slack)
  "https://auth.atlassian.com", // Jira / Confluence
  "https://login.microsoftonline.com", // SharePoint / OneDrive / Power BI / Azure SQL
  "https://login.salesforce.com", // Salesforce
  "https://test.salesforce.com", // Salesforce sandbox orgs
]);

/** Popup-shaped features: an explicit width or height entry. */
const SIZE_FEATURE_RE = /(^|,)\s*(width|height)\s*=/i;

/**
 * Decide what to do with a page-initiated window.open / target=_blank.
 *
 * @param {{url: string, disposition?: string, features?: string}} details
 *   The fields Electron's setWindowOpenHandler provides.
 * @param {{
 *   openerOrigin: string | null,
 *   pinnedOrigin: string | null,
 *   extraPopupOrigins?: unknown,
 * }} context `openerOrigin` is the CURRENT top-level origin of the opening
 *   window, `pinnedOrigin` the origin it is pinned to (null when on the
 *   setup page), `extraPopupOrigins` the raw settings.json
 *   `popup_allowed_origins` value (unvalidated user input — non-arrays and
 *   non-string entries are ignored).
 * @returns {{kind: "popup"}
 *   | {kind: "external"}
 *   | {kind: "protocol-consent", scheme: string}
 *   | {kind: "ignore"}} `popup` → allow a hardened child window;
 *   `external` → shell.openExternal; `protocol-consent` → consent dialog;
 *   `ignore` → unparseable URL, nothing safe to open.
 */
function decideWindowOpen(details, context) {
  let parsed;
  try {
    parsed = new URL(details.url);
  } catch {
    return { kind: "ignore" };
  }
  if (!WEB_SCHEMES.has(parsed.protocol)) {
    return { kind: "protocol-consent", scheme: parsed.protocol };
  }
  const popupShaped =
    details.disposition === "new-window" && SIZE_FEATURE_RE.test(details.features ?? "");
  if (!popupShaped) return { kind: "external" };
  const { openerOrigin, pinnedOrigin } = context;
  if (!pinnedOrigin || openerOrigin !== pinnedOrigin) return { kind: "external" };
  if (parsed.protocol !== "https:") return { kind: "external" };
  if (parsed.origin === pinnedOrigin || OAUTH_POPUP_ORIGINS.has(parsed.origin)) {
    return { kind: "popup" };
  }
  const extra = context.extraPopupOrigins;
  if (Array.isArray(extra) && extra.includes(parsed.origin)) {
    return { kind: "popup" };
  }
  return { kind: "external" };
}

module.exports = {
  decideWindowOpen,
  WEB_SCHEMES,
  // Exported for focused unit tests.
  OAUTH_POPUP_ORIGINS,
};
