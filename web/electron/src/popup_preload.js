// Deliberately empty preload for OAuth popup child windows (see the
// setWindowOpenHandler / hardenOauthPopup pair in main.js). A child window
// created by an allowed window.open would otherwise inherit the SHELL's
// preload.js, exposing the omnigentDesktop/omnigentSetup IPC bridges to
// whatever third-party sign-in page the popup shows. Pointing the child at
// this no-op file guarantees the popup gets NO bridge, independent of
// Electron's webPreferences-inheritance defaults.

"use strict";
