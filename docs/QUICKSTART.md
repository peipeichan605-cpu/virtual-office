# Quick start

Virtual Office runs on your computer and opens a private 3D lobby in your browser. A fresh installation starts with an empty roster. You choose which platforms, agents and conversations to connect. GitHub hosts the software.

**Using an installation agent?** Give it the repository link and ask it to install Virtual Office. It can follow [the agent guide](../AGENTS.md) directly. For a computer that already runs OFFICE, use [the upgrade guide](UPGRADING.md).

## Install on a new computer

1. Download the [release ZIP and its SHA-256 checksum](https://github.com/peipeichan605-cpu/virtual-office/releases/tag/v0.2.2-preview.1), verify the checksum, and extract into a writable folder. Alternatively, clone the matching Git tag as shown in [AGENTS.md](../AGENTS.md).
2. Prepare Python 3.10 or later, Node.js 22.13 or later and a compatible local Codex executable. On Windows, the pinned Cursor SDK needs x64 Python, and Codex needs a native `codex.exe`. An existing Python 3.9 OFFICE core can still run alongside a separate compatible SDK environment. First installation needs internet access for the pinned UI assets and Cursor SDK.
3. Open a terminal in the folder containing `office.py` and run each command after the previous one succeeds. On Windows, replace `python3` with a verified `py -3`, `python` or the full interpreter path.

```sh
python3 setup_ui.py
python3 office.py --config .office/config.json init
python3 office.py --config .office/config.json setup-platforms
python3 office.py --config .office/config.json setup-platforms --check
python3 office.py --config .office/config.json start
python3 office.py --config .office/config.json status
python3 office.py --config .office/config.json url
```

`setup-platforms` installs the pinned Cursor SDK into this installation’s private environment and checks Node.js and Codex. Resolve any reported prerequisite, then run the check again. Look for `runtimes_ready=true`. Account sign-in and message delivery are separate checks.

Open the private URL from the last command on the host. Confirm that the 3D lobby loads, `status` reports `state=running` and `local_ok=true`, and the new roster has `selected=0`. Keep the URL and the installation’s `.office/` folder private. That folder contains configuration, credentials, bindings, conversations and databases.

## Starting and stopping on Windows

For an initialized default installation, double-click `Start Office.cmd` in Explorer. You can also run `start` in a normal PowerShell window and close it after startup succeeds. Some IDEs and installation tools terminate child processes on exit. If OFFICE reports that restriction, launch PowerShell from the Start menu, go to the same folder, and repeat `start` with the same config. Use `serve` to run in the foreground and keep that window open.

After an unexpected exit, check `status` first. The start receipt includes the service `run_id`, service `pid`, `launcher_pid` and local log path. A finished run's `result.json` may record a reason such as `owner_api` or `observer_failed`. A stale `running` record alone cannot tell you why a process exited. For any custom config, keep its original path or `OFFICE_CONFIG` throughout.

## Add your first agent

Open **Invite Agent** (`＋ 邀請 Agent`) at the top right or **New conversation** (`＋ 新對話`) on a floor. Choose the route that matches the conversation you want.

| Choice | What to connect |
| --- | --- |
| Existing Codex App chat | Use the invitation in the selected original chat. [Command delivery](COMMANDS.md) |
| Codex background | Create a separate OFFICE-managed thread. [Codex background](CODEX-BACKGROUND.md) |
| Grok Bot SDK, experimental | Sign in through the browser, then select an existing bot and chat. [Grok SDK](GROK-SDK.md) |
| Grok Bot Webhook | Use the connection assistant in the same invitation. [Grok Bot](GROK-BOT.md) |
| Cursor Cloud | Connect your account and select an existing Cloud Agent. [Cursor Cloud and Desktop](CURSOR.md) |
| Cursor SDK, chat only | Register a separate OFFICE-managed SDK conversation. [Cursor SDK](CURSOR-SDK.md) |
| Hermes ACP | Bind one native ACP session. [Hermes](HERMES.md) |
| OpenRouter free chat | Enter your own key in the local desktop form and select an available free text model. [OpenRouter](../README.md#openrouter-free-text-agents) |

Local Cursor Desktop chats can publish selected conversation events. Background delivery into those local chats is unverified. Platform sign-in and approvals take place in the original platform. Click an agent's name above its avatar to send a short instruction. Verify the working state, matching public reply and saved delivery before treating the route as tested. [Platform support](../README.md#platform-support)

## Phone access

Install the official Tailscale app on your phone, sign in with the same account, and connect. Select **Phone access** in desktop OFFICE to choose permissions and pair the phone. Remembering a trusted phone is optional and lasts up to 30 days, expiring after 7 unused days. Keep the host online. Pairing works without a USB cable or ADB. [First connection and daily use](MOBILE-QUICKSTART.md)

## Everyday controls

- **Simple mode** shows avatars and overhead chats. Full mode opens on G/F.
- Drag the chat title bar to move it or drag an edge to resize it. **Return above avatar** restores tracking.
- **Move seat** changes the visual seat. Workspace and project access are configured separately.
- Stop this installation with `python3 office.py --config .office/config.json stop` and check `status` with the same config.

[Feature guide](../README.md) · [Release notes](../RELEASE_NOTES.md) · [Credits and notices](../NOTICE.md)
