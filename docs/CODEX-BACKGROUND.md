# Codex background conversations

Create a separate Codex thread managed by OFFICE. Messages typed above its avatar go to a local Codex app-server process. Public progress and final deliveries return to the same avatar, and subsequent prompts resume the same thread. Keep the host, OFFICE service and Codex account available. The desktop window can stay in the background.

## Register a thread

1. Sign in to Codex on the host as the user running OFFICE. On macOS a compatible executable bundled with Codex Desktop can supply the runtime.
2. Select **New conversation → Codex background**. Enter a name and choose the working folder. If it does not exist, explicitly select the option to create it.
3. Leave the model set to **Use Codex settings**, or select a model from the runtime's list. The choice applies only to this OFFICE conversation.
4. Choose **Register background conversation**. Open the avatar's chat and send the first instruction.

Registration saves the member and connection settings. The first instruction creates the native thread. Its receipt confirms the thread identity. A matching public response and final record verify delivery. If registration is interrupted, use **Check registration result** to look up the same request.

## How OFFICE finds Codex

| Order | Executable |
| --- | --- |
| 1 | An explicit absolute `command_adapters.codex_background.binary`. An invalid explicit path produces an error. |
| 2 | On macOS, look under `/Applications` and `~/Applications` for `ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex` or `Codex.app/Contents/Resources/codex`. |
| 3 | A compatible standalone CLI on PATH or in a standard installation location. On Windows use the native `codex.exe`. |

OFFICE probes the actual `app-server` capability. A Windows `.cmd` or `.bat` wrapper cannot carry the native argument and stdio contract. If only an npm shim is present, install or locate the official native executable. `setup-platforms --check` probes the runtime before launch. The background-settings view allows 60 seconds for a check. The backend shares a 45-second limit across the native probe, plus bounded process cleanup time. A timeout reports a check failure. It does not resend a prompt.

## Account, model and permissions

The worker uses the installed Codex runtime and its host user's existing login. The model catalog comes from that runtime. A listed model's actual availability depends on the runtime version, login method and account. If it reports `model_not_found`, check which binary and version OFFICE selected. OFFICE records the failure and leaves model selection to the owner.

In the avatar's **Model settings**, select a model and choose **Save model and keep conversation**. The next new instruction uses it. The member, native thread, workspace and history stay together. Active work and unconfirmed outcomes must be resolved before changing the model. If saving is interrupted, use **Check settings result** to recover the original operation. A new instruction verifies actual execution.

This mode grants a read-only workspace and uses `on-request` approval. Supported requests are paired with the current thread and turn. The owner can allow an action once or deny it. Expired and unidentified requests receive no automatic approval. Limited local tools can run. Third-party MCP servers, plugins, apps, hooks, web search and multi-agent tools are disabled for this worker. It uses stdio with the local host.

## Public conversation and recovery

OFFICE redacts and stores the selected thread's public user inputs, assistant text, state and final deliveries. Closed browser pages do not stop the Python service. Completed conversations can resume after an OFFICE restart. A send with an uncertain outcome remains unconfirmed until the same native turn has been reconciled. Resolve uncertainty before submitting more work.

Test with one short prompt, verify the working state, matching reply and saved delivery, then send a contextual follow-up. A previous macOS trial completed four instructions on one thread, including one after about 76 idle seconds, one after restarting OFFICE, and a read-only workspace file operation. That trial covered the chosen model. Long idle periods, every listed model and a fully exited Codex Desktop app require separate verification.

## Existing Codex App chats

Choose **Connect an existing App / Cloud Agent** to keep a particular App chat, its native queue, observer and supported child agents. An unloaded original chat can wait for the App to load it. Background mode instead creates an OFFICE-managed thread. It has no guarantee of appearing in the Desktop sidebar and has no child-agent observer. The two routes can coexist. [Existing App commands](COMMANDS.md)

## Logout and maintenance

Sign out from the avatar chat to release the OFFICE binding and keep history. Known running, queued or approval-pending work blocks logout. If only an old unconfirmed result remains, review the explanation and explicitly choose **Sign out anyway and keep records**. The provider may still be working. Unconfirmed work stays recorded as such.

The private installation directory stores logical sessions, native thread/turn IDs and execution state. Before an upgrade or rollback, back up these together with the program version. Older programs may need newer configuration entries set aside. Restoring an old private backup can discard newer conversations. [Upgrade guide](UPGRADING.md) · [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server)
