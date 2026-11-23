# Virtual Office

Virtual Office is a local 3D dashboard for your AI agents. Click an avatar to read its public prompts, progress and deliveries, or send a message through a supported connection. Python runs the office on your computer. A fresh installation starts with an empty lobby and a Traditional Chinese interface.

## Install with your agent

Give an agent that can operate your computer **this repository link** and ask it to install Virtual Office. The installation steps travel with the repository. The agent can set up the host, open the 3D lobby and verify startup. If it finds an existing office, it can follow the upgrade procedure and keep your agents, conversations and phone pairings.

**For AI agents.** Start with [AGENTS.md](AGENTS.md) when the owner asks you to install or start OFFICE.

[Download the 0.2.2-preview.1 ZIP and SHA-256](https://github.com/peipeichan605-cpu/virtual-office/releases/tag/v0.2.2-preview.1) · [Manual quick start](docs/QUICKSTART.md) · [Upgrade an existing office](docs/UPGRADING.md)

## Installation

For a full first installation, use a writable folder, Python 3.10 or later, Node.js 22.13 or later and a compatible local Codex executable. Windows requires x64 Python for the pinned Cursor SDK and a native `codex.exe` for Codex. An existing Python 3.9 core can use a compatible separate SDK environment. Initial setup needs internet access for the pinned UI and Cursor SDK. The installed desktop and phone lobby load Three.js locally.

From the folder containing `office.py`, run each step after the previous one succeeds. On Windows substitute a verified `py -3`, `python` or full interpreter path.

```sh
python3 setup_ui.py
python3 office.py --config .office/config.json init
python3 office.py --config .office/config.json setup-platforms
python3 office.py --config .office/config.json setup-platforms --check
python3 office.py --config .office/config.json start
python3 office.py --config .office/config.json status
python3 office.py --config .office/config.json url
```

Look for `ui_installed=true`, `runtimes_ready=true`, `state=running` and `local_ok=true`. A new roster has `selected=0`. Open the private URL on the host and check that the lobby renders. The start receipt includes the run ID, log and exact stop command. Use the same config with `stop`. An initialized default Windows installation can also reopen with `Start Office.cmd` from Explorer. [Windows startup and troubleshooting](docs/QUICKSTART.md#starting-and-stopping-on-windows)

`setup-platforms` prepares the pinned Cursor SDK in a private environment, checks Node.js for Grok and checks Codex's app-server. If a requirement is missing, supply the official executable and rerun the check. Platform login and actual conversation delivery are verified when you choose a connection. Keep `.office/`, its private URL, settings, credentials, databases and logs off public repositories. If you already have an installation, use its original config and the [upgrade guide](docs/UPGRADING.md).

## Add an agent

Click **Invite Agent** (`＋ 邀請 Agent`) at the top right or **New conversation** (`＋ 新對話`) on a floor. Pick a platform and a connection type. Opening the menu does not add a member.

- **OFFICE-managed conversation.** Choose Codex background, Cursor SDK chat or OpenRouter to create a separate conversation. A working desktop account and a prepared SDK environment have different requirements. See the table below.
- **Existing chat or Cloud Agent.** Choose the original App or Cloud route. Grok SDK lets you pick a bot and chat after signing in. Cursor Cloud takes the chosen agent's link. Platforms that need local action provide one invitation to paste into the selected original chat. It covers enrollment, the first reports, public sync and the supported receiver setup.
- **Verify your selection.** Check the actual `run_id`, `joined=true` and both `accepted=true` first reports where applicable. Send a short instruction and check its matching public reply and completed delivery before relying on that route. To test work originating in the App, type a different prompt there and check its public events separately.

Unused invitations are valid for 24 hours and can be cancelled. Reopening the invitation flow with the same platform and name can recover an unused prompt. If the installation moves, generate a fresh invitation. Logout revokes used credentials. The owner's local private state contains the invitations and bindings.

## Platform support

| Connection | What the owner selects | Messages and public records |
| --- | --- | --- |
| Codex App on macOS | An existing App chat with the same Codex home and native queue | OFFICE queues into the original session and observes its public events. An unloaded chat may need to be opened in the App. [Codex App commands](docs/COMMANDS.md) |
| Codex background | A name, workspace and optional model | A separate OFFICE-managed native thread keeps context, public work and deliveries. Read-only workspace and one-time approvals. Uses the host's Codex login. [Codex background](docs/CODEX-BACKGROUND.md) |
| Cursor Cloud Agent | An authorized API key and selected `bc-…` agent link | OFFICE-originated and new App-originated turns share the same agent and public history. The observer resumes by native run ID. [Cursor Cloud](docs/CURSOR.md#connect-a-cloud-agent) |
| Cursor local Desktop chat | A chosen chat, native hooks and an identity-binding response | Public parent and child activity can sync from the App. Background delivery into a local chat is unsupported in this release. [Cursor local](docs/CURSOR.md#bind-a-local-cursor-chat) |
| Cursor SDK chat | A name and a User API key with SDK access | A separate OFFICE-managed session with an empty tool list. The first send creates the native session. The bundled setup installs `cursor-sdk==1.3.1`. [Cursor SDK](docs/CURSOR-SDK.md) |
| Grok Bot SDK, experimental | Browser sign-in, one existing bot and one native chat | Public inputs, replies, status and deliveries from either side of that chat. The unofficial SDK source is bundled. [Grok SDK](docs/GROK-SDK.md) |
| Grok Bot Webhook | One existing bot and its routine | The assistant tests receipt, reply and completion. The routine wakes that bot. App-originated work depends on cooperative reporting. [Grok Bot](docs/GROK-BOT.md) |
| Hermes ACP | One configured native ACP session | Background dispatch, public responses and context for OFFICE-issued work. The selected Hermes environment supplies its model. [Hermes](docs/HERMES.md) |
| OpenRouter free text | Your key and an available free text model | Prompts, replies, state and deliveries are scoped to each OFFICE agent. Uses the Python core. [OpenRouter details](#openrouter-free-text-agents) |
| Other local runners | An individually selected source and private reporting credential | Can report public events and use the generic inbox. Native wake-up requires a platform adapter. [Event protocol](docs/EVENTS.md) · [Inbox](docs/INBOX.md) |

### Background receipt and public observation

A ready native receiver lets an agent finish its turn. The Python service holds new instructions until delivery. There is no recurring one-hour wait or idle model call. `receiver_status` gives the next action.

| Reported status | Meaning |
| --- | --- |
| `ready` | A route is configured and currently available. |
| `configured_unavailable` | The route is saved and is waiting for the platform. |
| `setup_required` | Complete the platform's native connection steps. |
| `unsupported` | This route can store a message but cannot wake the original chat. |
| `disabled` | The owner has disabled command delivery. |
| `rejoin_required` | The old identity is invalid. Generate a new invitation. |

`persistent=true` describes a saved route. Check current availability separately. Original App observation also has its own scope. Cursor local watches the bound native chat. Cursor Cloud watches new turns on the selected Cloud Agent. Grok SDK watches the selected chat. Grok Webhook relies on the bot's authenticated cooperative reports. Hermes ACP observes work dispatched through its selected session. [Command states and recovery](docs/COMMANDS.md) · [Inbox protocol](docs/INBOX.md)

For a Grok Webhook invitation, the same window guides the owner through setup and a short test. Its **Connected** state requires a matching start, public reply and successful end from the bot. For existing enrolled agents, **Enable continuous receiving** offers the applicable upgrade steps while preserving identity and history. Unknown sends are reconciled using their original identity before additional work.

## OpenRouter free text agents

In **Invite Agent → OpenRouter**, enter a display name, select a currently available free text model and enter your own OpenRouter key on first use. The key stays in this installation's private backend settings and can be reused for other agents you choose. Each agent keeps its own conversation and deliveries. Paired phones follow their existing per-agent or roster-wide message grants. [Create an OpenRouter key](https://openrouter.ai/settings/keys)

The catalog comes from OpenRouter. Nemotron 3.5 Lightning and Super VL can be selected when they have a supported free endpoint. OFFICE checks the exact `:free` model identifier and zero pricing at creation and on every send. If the model disappears or free quota is exhausted, it reports the cause. This text-only route sends the current message and recent completed turns from that same agent to the provider. Providers may retain content. Check an organization's plugin settings for mandatory paid extras. [OpenRouter limits](https://openrouter.ai/docs/api/reference/limits) · [Plugin settings](https://openrouter.ai/docs/guides/features/plugins/overview)

## The office floor and chat controls

**G/F** shows all enrolled agents, including agents with no assigned workspace. Every floor reads the same identity, public state and history. Full mode opens on G/F. Simple mode concentrates on avatars and overhead chats. The browser remembers the selected mode.

The 45.5 × 41 m lobby has a central lounge with 36 individual resting places, grouped sofas, chairs, tables, plants and a large TV. The pantry has a coffee bar and stools. The five perimeter offices have 8, 6, 4, 6 and 8 desks. The separate glass CEO room has an executive workstation, screen and guest seating. The layout expands for more members. Warm wooden floors, native day/dusk/night lighting, window shadows and adjustable graphics are shared across the scene. The lounge and pantry use additional warm lighting at dusk and night.

- **Read or send a message.** Click the name above an avatar. Drag the title bar to move its chat, drag an edge or corner to resize it, and choose **Return above avatar** to attach it again. The title and resize handles support arrow keys and larger Shift steps. The draft and scroll position survive moving the window. Only window geometry and floating mode are stored in the browser layout setting.
- **Move a seat.** Drag the avatar to a desk or lounge seat, or use **Move seat** in its chat. Green indicates a vacant seat and red an occupied seat. Python persists each assignment across refreshes and restarts. Moving a seat has no effect on the agent's actual working directory.
- **Review children.** Select **Child agents** above an enrolled parent. Their read-only public records are available in either mode. Dense labels avoid each other and one chat or child list opens at a time.
- **Sign out.** Logout releases the seat and the agent's receiving identity, cancels unsent messages and retains history. Known active work must finish first. For an old unconfirmed outcome the owner can explicitly sign out and keep the record. The native task may still be running. [Logout details](docs/COMMANDS.md#logout-and-uncertain-work)

Grok Bot uses the original `grok-bot.glb` geometry with six identity-based body colors. Codex, Cursor and Hermes also have per-platform palettes. A stable agent identity retains its color across renames and floor changes. Explicit avatar or color selections are respected. The movable conversation panel builds on the [community Cursor integration](https://github.com/dominicliu69/virtual-office/blob/0753acc8591e48cfaf3199a6f1b2cabbdc1bbb07/ui-overrides/office/commands.js).

### Workspaces and child agents

Agents sharing a canonical project directory share a floor. Distinct paths with the same final directory name stay separate. Symbolic links to the same directory resolve together. Floors have up to 20 seats and grow into continuation floors. Use **Set workspace** to pin a visual floor manually, or choose **Follow agent working directory** for automatic placement.

Codex checks a selected chat's `cwd` approximately every 30 seconds. Crouton uses the verified run's `job.workspace` and can retain the last known workspace at rest. A generic agent can send an absolute `workspace_path` on `turn_started` or use `--workspace` when invoking `emit.py`. A Cursor hook can use a single `workspace_roots` entry. With multiple roots the owner chooses the project. Full paths live in private storage.

Codex children use native metadata. Cursor children use native hooks and scoped public messages. Grok Task children use explicit parent reports. A child appears beside its parent after an accepted start, and generally leaves about 30 seconds after its own terminal outcome. A missing final event leaves its outcome unconfirmed. Its history remains available after departure. [Parent and child agents](docs/SUBAGENTS.md)

## Private phone access

Open **Phone access** in desktop OFFICE. On the phone install the official Tailscale app, sign in with the same account and confirm **Connected**. Select the phone on the computer, choose the agents it may view and message, and scan the one-time QR with the phone camera. On later visits open the regular bookmarked OFFICE page with Tailscale connected. A trusted phone may opt into renewal for up to 30 days, expiring after 7 unused days.

The owner can update existing grants without pairing again, including separately following all enrolled agents for viewing or sending. The phone can switch permitted floors and use chat without leaving the lobby. The host and private entry must stay online. A separate userspace tsnet node is available for installations that need to preserve an existing host VPN. [Phone quick start](docs/MOBILE-QUICKSTART.md) · [Mobile permissions and recovery](docs/MOBILE-ACCESS.md) · [Optional tsnet frontend](docs/MOBILE-TSNET.md)

## Advanced enrollment

Most users can enroll through the menu. These examples are for explicit source selection or custom integrations. Keep the original `--config` before each subcommand if you use a custom config. For changes to a manual roster or source, finish outstanding work and reload only this OFFICE instance.

```sh
python3 office.py enroll --provider codex --source YOUR_CHAT_ID --name "My Codex"
python3 office.py configure --codex-db /path/to/state_5.sqlite
python3 office.py connect-hermes --name "My Hermes" --workspace /absolute/path/to/project
```

The older parent-only Cursor observer can follow one known conversation ID. For new enrollments, the [Cursor hook setup](docs/CURSOR.md#bind-a-local-cursor-chat) also handles genuine children. Existing hooks are preserved. Regenerate this installation's observer command if you move its files.

```sh
python3 office.py enroll --provider cursor --source YOUR_CONVERSATION_ID --name "My Cursor" --credential-out .office/cursor.json
python3 office.py cursor-hooks --credential .office/cursor.json --output /path/to/your/project/.cursor/hooks.json
python3 office.py enroll --provider grok_bot --source my-grok --name "My Grok Bot" --credential-out .office/grok.json
python3 office.py configure --crouton-root /path/to/crouton-runtime --crouton-workspace-root /path/to/allowed/workspaces
python3 office.py enroll --provider crouton --source crouton-01 --name "Crouton 01"
```

The Crouton observer reads verified task state and the final reply. Intermediate public messages require event reporting by the runner. A generic or Grok reporter needs execution access on the actual OFFICE host. Every external reporting credential belongs to one member and cannot read owner-only conversations.

## Verification, privacy and credits

The observer typically checks active sources about once a second. Dashboard data and an open chat typically refresh about every two seconds. Source write time affects delay. OFFICE stores public messages, delivery history and event receipts in local SQLite. Input is redacted for common secret patterns, private URLs, email addresses and user paths. Review what your chosen platform or agent makes public. Reasoning and raw tool transcripts are excluded.

To run the developer suites, install the pinned UI from this checkout and run the following commands from the repository root.

```sh
python3 setup_ui.py
python3 -m unittest discover -p 'test_*.py'
node --test test_*.mjs
```

Release records distinguish a prepared runtime, simulated provider tests and live-account delivery. For OpenRouter the free model catalog was checked live. The two-turn browser conversation and saved delivery used a deterministic provider. [Verification record](VERIFICATION.json) · [Release notes](RELEASE_NOTES.md)

The original 3D interface comes from `Bodila51/grokbot-wallpaper` at commit `76232f7af1a94d3cbd3d0b1cf05e460c85637acf`. Local setup downloads the pinned sources and verifies the original file hashes. The repository carries patches and manifests. The generated `public/` directory stays outside version control. No root license file was found at that upstream revision. Consult the original rights holder for redistribution of upstream work. [Upstream project](https://github.com/Bodila51/grokbot-wallpaper) · [Notices and provenance](NOTICE.md)
