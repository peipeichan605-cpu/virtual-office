# Release notes

## 0.2.2-preview.1

This documentation revision ships the English README, agent installation guide, manual quick start, upgrade instructions, phone setup and platform guides in one download. It uses the same runtime and 3D assets as `0.2.2-preview`. The previous tag and its checksum remain available. [Agent installation](AGENTS.md) · [Manual installation](docs/QUICKSTART.md) · [Upgrading](docs/UPGRADING.md)

## 0.2.2-preview

- Added OpenRouter free text agents with separate conversations and delivery history. The owner selects an available zero-price model and supplies the local installation's key. The exact model and price are checked again on every send.
- The desktop and phone gateway bind to their numeric loopback address at startup, avoiding a reverse DNS delay. The macOS installer restores the phone helper's owner-execute bit after ZIP extraction by tools that discard file modes.
- Newly created invitations use a native private directory and check permissions before writing credentials. The hosted Windows runner exercises this path.
- A native Windows in-place trial verified 127 UI files, all three platform runtimes, a rendered Chrome lobby and preservation of enrolled agents, conversations and mobile permissions. All 19 focused native Windows tests passed.
- The OpenRouter free catalog was read live on Windows and macOS. A macOS browser test used a deterministic provider for two turns and saved deliveries. Authenticated OpenRouter model inference was not tested in that release. [Verification record](VERIFICATION.json)

## 0.2.2-rc.1

- Read the UI manifest as UTF-8 on a clean installation. Fall back to a mirror of the same upstream commit if necessary, retry an interrupted download and verify the expected hash for every file.
- Desktop and mobile serve a matching Three.js runtime from local OFFICE after setup.
- Windows enrollment, reporting and Cursor child observation use native permissions, locks and PowerShell commands. Status checks are read-only. Legacy console locales can read enrollment results. When updating existing Grok invitation guides, the writer preserves their original encoding and detects concurrent edits.
- On macOS and Linux, stopping the optional private phone frontend waits for confirmed process exit, including when the OS temporarily cannot supply a process name.
- Phone pairing handles distinct devices on one account, returning from old bookmarks, reading history and recovery after a page-load interruption.

## 0.2.1-preview

Released November 18, 2026.

Full setup now prepares Codex, Cursor SDK and Grok SDK runtimes before handover. `setup-platforms` installs the pinned private Cursor environment, checks the native Codex executable and checks Node.js for Grok. Missing prerequisites have specific next steps. New installations start empty. Account sign-in and live message verification happen after the owner chooses a platform.

- Codex's settings check uses a bounded native probe and an appropriate UI timeout.
- Windows uses a compatible native `codex.exe`, starts hidden background processes and targets only the process it launched during cleanup. The start receipt includes the exact arguments and a PowerShell stop command.
- Cursor reports a missing SDK, incompatible Python, unsupported architecture or wrong SDK version separately.
- Fixed Windows paths, static serving, UTF-8 configuration and native private ACL checks. Existing access granted to another account remains grounds for refusing a private path. Existing directory permissions are not silently rewritten.
- Added portable line-ending rules and automated Windows, macOS and Linux checks.

An isolated Windows installation verified three runtimes, 126 UI files, an empty office, HTTP availability and shutdown. The local Codex account reported signed in. Cursor SDK 1.3.1 was available, but the test installation had no Cursor key and did not exercise account delivery. [Recorded checks](VERIFICATION.json)

## 0.2.0-preview

Released November 18, 2026.

- **Private phone access.** A first-connection assistant covers official Tailscale installation, same-account login, device approval, connection, per-agent scope and camera pairing. Phones can view the 3D office, switch floors and message permitted agents. View and send permissions can follow an explicit list or the enrolled roster independently. Optional remembered sessions renew for up to 30 days with a 7-day idle limit. The optional tsnet frontend uses the same OFFICE permissions.
- **Grok and Codex.** The experimental Grok SDK route connects a selected existing bot and chat. The Webhook route remains available. Codex background creates an independent native thread with public progress, delivery, resume and optional next-turn model selection. Its read-only worker can use a compatible App-bundled or standalone executable. It has no child observer.
- **Cursor.** Cloud Agent observation follows work originating in OFFICE or in the App and adds persistent deduplication, recovery and incomplete-history indicators. The chat-only SDK route maintains a separate context with no file or shell tools. Local Desktop observation has no verified inbound background route.
- **3D and conversations.** The lobby, workspace floors, lounge, pantry, CEO room, seating, platform avatars, graphics settings and idle power savings are retained. Chat windows can float, resize or follow an avatar, preserving drafts and reading position. Desktop controls no longer overlap. Mobile chat closes in place, and floor switching and delivery history are available from the lobby.
- **Child agents.** Supported native and cooperative routes retain read-only child chats. Grok Webhook child reports depend on the parent. Full child-body sync for Grok SDK and a Hermes child observer were unavailable in this version.

The shared conversation UI and Cursor integration credit the [community contribution](https://github.com/dominicliu69/virtual-office/tree/0753acc8591e48cfaf3199a6f1b2cabbdc1bbb07). Each installation supplies its own credentials and roster. Phone network, provider availability, real devices and long idle periods require installation-specific testing. Unconfirmed dispatches keep their original outcome. [Notices](NOTICE.md) · [Historical tests and limitations](VERIFICATION.json)

## 0.1.0-preview

The first public portable preview shipped a local Python service, Traditional Chinese UI, empty roster and private local storage. It included G/F and workspace floors, a furnished lounge and pantry, CEO room, platform avatars, seat dragging, public chat, work state, saved deliveries and supported parent/child visualizations. Codex App, Hermes ACP, Grok Webhook and Cursor Cloud had individual routes. Cursor local chats could publish observations. A local Grok desktop-input experiment was excluded.

The original release recorded 495 passing Python tests and 138 passing JavaScript tests, a clean pinned UI install, empty-roster startup and a local HTTP check. Later updates in this preview line added the following.

- Movable, resizable avatar chats, keyboard adjustment, floating and return-to-avatar controls, per-browser geometry and read-only child history. Only the panel moves when its title bar is dragged. [Community source](https://github.com/dominicliu69/virtual-office/commit/0753acc8591e48cfaf3199a6f1b2cabbdc1bbb07)
- Two-way Cursor Cloud follow-ups and observation of new native App work with stable event identity, persistent recovery and explicit gaps.
- Shared desktop controls, a working mobile chat close button, authorized floor switching and in-place history. Owner-selected phone view/send grants can follow enrolled agents, with scope updates preserving the login and suppressing older unsent work that lost permission.

The machine-readable record keeps each test's original date, scope and evidence. [Verification history](VERIFICATION.json)
