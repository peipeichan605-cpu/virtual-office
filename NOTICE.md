The 3D interface derives from Bodila51/grokbot-wallpaper at commit 76232f7af1a94d3cbd3d0b1cf05e460c85637acf.
Original assets and complete upstream source are fetched only during local setup and are not bundled. If the primary GitHub repository is unavailable, the installer can retrieve the same pinned commit from its HerbertGao/grokbot-wallpaper fork. Both sources use identical per-file hashes.
No root license file was found at that pinned revision. This package does not assert a redistribution license for that upstream work; confirm permission before public release of derivative source or assets.
Python services and adapters here are local project additions. No user settings, credentials or conversations are included.


## Platform character identification

The Codex, Cursor and Hermes Three.js Office characters are original scene models, not official platform mascots. Grok Bot uses the original upstream Office avatar modules. Platform artwork is displayed separately as identification for agents using those services. The Codex product icon belongs to OpenAI, Grok artwork to its respective owner, Cursor artwork to Cursor, and Hermes artwork to Nous Research. Original marks retain their proportions and colors. Sources and SHA256 provenance are in `public/office/platform-icons/SOURCES.json` (installed UI) / `ui-overrides/office/platform-icons/SOURCES.json` (portable source). These brand assets are not relicensed by this package.

Sources: https://openai.com/codex/ ; https://grok.com/ ; https://cursor.com/brand ; https://hermes-agent.nousresearch.com/ .

## Optional Grok Bot SDK

The optional experimental integration vendors RongleCat/grok-bot-sdk 0.78.1 at commit 42c75eb13f24630d1b689629502d4320c4219cdb under its MIT license. See vendor/grok-bot-sdk/LICENSE and PROVENANCE.json. Local compatibility changes are documented in OFFICE-COMPATIBILITY.md; this is not an official Grok SDK.

## Local Three.js runtime

The desktop and mobile interfaces use selected Three.js 0.170.0 runtime modules under the MIT license. See ui-overrides/assets/vendor/three/LICENSE and PROVENANCE.json (installed at public/assets/vendor/three/). The existing upstream Office assets remain separately fetched during setup.

## Owner pairing QR encoder

The optional owner pairing dialog uses qrcode-generator 2.0.4 by Kazuhiko Arase under the MIT license. The unchanged local encoder, LICENSE and PROVENANCE.json are in ui-overrides/office/vendor/qrcode-generator/ and are installed under public/office/vendor/qrcode-generator/. They are not added to the mobile asset allowlist.
