# Cursor SDK chat

Select **New conversation → Cursor SDK, chat only** to register a separate SDK conversation managed by OFFICE. Subsequent messages, replies and deliveries belong to the same avatar and native SDK session. To follow an existing Cursor Desktop chat or Cloud Agent, use the corresponding [Cursor Desktop and Cloud guide](CURSOR.md).

## Prepare the SDK once

The standard `setup-platforms` step prepares the official `cursor-sdk==1.3.1`. An installed Cursor Desktop app has its own runtime. The SDK needs Python 3.10 or later and, on Windows, an x64 interpreter. This pinned release has no Windows ARM64 or 32-bit wheel. A Python 3.9 OFFICE core can use a separate SDK environment.

```sh
python3 office.py setup-cursor-sdk
```

On Windows use a verified `python`, `py -3` or full interpreter path. The installer checks available interpreters, including those registered with the Windows Python launcher. To explicitly allow `uv` to download a compatible independent Python, use the following command.

```sh
python3 office.py setup-cursor-sdk --download-python
```

Put `--config /absolute/path/to/config.json` before the subcommand when using a custom config. The installer tests actual imports and puts the private environment and installation record under this OFFICE. Prepare a fresh installation before startup. For a running installation, finish outstanding work and reload only that OFFICE using the same config and its stop/start commands. Check `setup-platforms --check`. The form distinguishes a missing SDK, old service Python, wrong SDK version and unsupported Windows architecture.

## Create a conversation

1. Open **New conversation → Cursor SDK, chat only** and enter a display name.
2. Accept the dedicated conversation folder created by OFFICE. You may select a registered project or enter a path if you want the avatar on that project's floor.
3. On first use, enter your Cursor User API key in the local OFFICE form. The backend keeps the key in private installation settings. Other SDK conversations in this OFFICE can reuse it. Your account needs access to the SDK.
4. Register the conversation, open the avatar's chat and send a message.

The first send creates the native SDK session and submits the prompt on the same SDK connection. A native receipt establishes its identity. Look for the corresponding public reply and saved final record. If registration or send returns an uncertain result, check the original request before making another.

## Permissions and records

This route uses an empty tool list on every turn. It grants no file, shell or MCP tools. SDK tool settings are separate from Desktop approvals. OFFICE redacts and stores public prompts, assistant text, state and deliveries. The SDK session can resume after restart. An unconfirmed earlier send keeps its status. Idle chats make no model requests.

SDK tool activity supplies no verified child-agent identity or delivery. Native Desktop hooks and Cloud routes keep their own support boundaries. [Cursor Python SDK](https://cursor.com/docs/sdk/python) · [Original Cursor integration contribution](https://github.com/dominicliu69/virtual-office/commit/0753acc8591e48cfaf3199a6f1b2cabbdc1bbb07)
