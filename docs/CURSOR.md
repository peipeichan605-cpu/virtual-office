# Connect Cursor Desktop or a Cloud Agent

For two-way conversation with the same agent in Cursor Desktop and OFFICE, choose a **Cloud Agent**. For an existing **This Mac / local** chat, install the native observation hooks to display activity and public messages. To create a separate text-only session owned by OFFICE, follow the [Cursor SDK guide](CURSOR-SDK.md).

## Connect a Cloud Agent

1. In the desktop OFFICE owner view, choose **Invite Agent → Connect an existing App / Cloud Agent → Cursor Desktop Cloud Agent**.
2. Enter a display name and the selected agent's `https://cursor.com/agents/bc-…` link. On first use, enter your own Cursor API key directly in the local form. The account must allow Cloud Agent API access.
3. Choose **Connect this Cloud Agent**. OFFICE reads the specified ID and its availability. It does not need to list your account's other agents, create a new one or run a model to register it.
4. Open the avatar's chat. Wait for the initial observation to finish. Send one message from OFFICE, then a different one from Cursor Desktop to the **same Cloud Agent**. Check the public input, reply, final delivery and native run for each. Repeat after idle to verify background delivery.

The installation can reuse its saved account key for later selections. Selecting another member is explicit and requires no Python edit or service restart. The original account's permissions and billing apply. A rejected key or insufficient API permission stops enrollment. If the API only permits a parent for a particular cloud child, select the parent the account can operate.

### Messages, state and recovery

OFFICE sends a follow-up with `POST /v1/agents/{id}/runs`. It preserves the cloud agent's workspace, model, mode and MCP settings. The text seen in Cursor is the text the user typed. Once the native run ID is saved, OFFICE reads that run's event stream and final result. It keeps public inputs, assistant text and state. Reasoning, raw tool data and duplicate interaction events are excluded.

The first successful observation establishes a baseline. Already completed pre-enrollment chats are not imported as new work. An active run and later work originating from OFFICE or the App are tracked by native run ID and deduplicated. Monitoring continues with the OFFICE browser closed while the service and Cursor API remain available. Signing out stops new observation and retains local history.

On restart, the observer resumes with read requests for known runs. It does not replay a send. If an event stream ends early, OFFICE requests the final result for the same run. It records missing public text or an unavailable original prompt explicitly. A final answer may survive even when earlier messages cannot be recovered. An unclear outcome blocks later dispatch until reconciled. A busy provider can reject a new request without interrupting or steering its active run. The key stays under the installation's private `.office/platform-secrets/` with restricted permissions. The connection uses outbound HTTPS.

## Bind a local Cursor chat

1. Create a Cursor invitation in OFFICE and give its generated instructions to the **selected original local chat** once. Check `joined=true`, both `accepted=true` reports and the actual `run_id`.
2. Follow `conversation.observation.guide_path` and its generated `adapters/connect.py --credential … cursor-children --hooks-file …` command. Use the paths supplied for this installation. Approve an installation step in Cursor if requested.
3. The installer merges OFFICE observer commands into existing hooks and supplies an `activation_marker` if a binding is pending. Include the entire marker exactly once in the **public final reply of the original chat**. Its native `afterAgentResponse` hook binds the actual `conversation_id` and rotates the observer token and generation. Text in an incoming prompt or a marker fragment cannot activate a binding.
4. Check `observation_status=configured`. Send a fresh ordinary task in the App to verify the next parent turn. Use an actual Task child to verify the child's identity, state, public progress and final delivery.

`hook_installed=true` confirms the config merge. `binding_required` needs the marker. `setup_required` means setup is still outstanding. For `stale` or `revoked` follow the invitation's recovery or rejoin step. Unbound observers cannot submit work events. The activation reply and its stop event do not create a working turn. A subsequent real `beforeSubmitPrompt` starts observation.

`public_sync_onboarding_complete` describes the configured observer. Local chats have `receiver_status=unsupported` for persistent inbound messaging in this release. They can display selected native work and save OFFICE messages in the [private inbox](INBOX.md). Desktop Bridge or `desktop send` functionality in an installed binary alone does not establish an authorized working native receiver. A temporary wait, CLI conversation or a new Cloud Agent has different guarantees.

### One hook setup per installation

The shared hook uses `--observer-dir` to read this installation's private `child-observers/` directory. It contains the enrolled parents' credentials, active binding index and bounded event queue. Hook command lines contain no tokens. Each additional enrolled parent reuses that installation's hook commands.

The installer backs up the existing hook config, preserves other hooks and approvals, and detects concurrent edits. It recognizes and replaces only its old `cursor_hook.py --credential …` entries for the same parent credential path. If the owner installed a separate wrapper or observer in another OFFICE, review any duplicate manually. After moving OFFICE, rerun its generated instructions for the new location. An installation cannot bind one native chat to two enrolled parents.

| Native hook | What OFFICE records |
| --- | --- |
| `beforeSubmitPrompt` | Parent turn start and public prompt from the bound chat. |
| `subagentStart` | Native child identity, direct parent and available delegation text. |
| `afterAgentResponse` | A complete public assistant message when supplied. |
| `subagentStop` | The identified child's terminal outcome and available public summary. |
| `stop` | The parent turn's terminal outcome. |
| `preToolUse`, `postToolUse`, `postToolUseFailure` | Identity fields needed to correlate parent and child. Tool arguments and output are excluded. |

An unfamiliar child conversation ID in a stop event must be backed by a verified Task ID or known alias before it can be associated with a child. Parallel tasks are never matched by name or recency. Parent completion does not imply child completion. Observation failures leave Cursor's own work and approvals in place. A later hook can retry an undelivered observation with its original event ID. If no later hook arrives, inspect the source and connection.

### Public text and history

The shared Python reader checks verified parent and child turns about once per second. A parent scope begins only after an accepted hook start, matches the bound conversation and a unique native HUMAN `requestId`, and ends at the next HUMAN message. For a child, it verifies the parent, parent turn, exact Task ID and native HUMAN boundary. It rechecks identity, generation and turn after reading. Reused child chats do not mix tasks. Native bubble IDs deduplicate text from the hook and reader.

Only ordinary public assistant `text` is imported. Thinking, service notices, raw tool content and unknown frames are excluded. A native stop confirms the outcome before a final body is stored. If the native database writes late, the reader can fill in a verified final body for up to 120 seconds while preserving the original completion and departure times. Missing public content or an ambiguous tail stays unconfirmed. Each pass considers at most 128 candidate child turns. Stored history survives avatar departure and restart.

Default profile locations follow Cursor on macOS, Windows and Linux. For a custom profile, configure the public source once and restart this installation.

```sh
python3 office.py --config /absolute/path/to/config.json configure --cursor-public-db /absolute/path/to/Cursor/User/globalStorage/state.vscdb
```

The reader needs recognized plaintext structured messages and SQLite JSON support. It skips a replaced database until identities are checked again. It does not decrypt account data or read mixed reasoning/transcript exports.

### What was tested

Cursor Desktop 3.29.9 on **This Mac** supplied real identities for two parallel Task children, distinct parent and child completion, working progress, delivery bodies and history after the roughly 30-second visual departure. That version sent neither a child `afterAgentResponse` nor a stop summary. The reader recovered the public body from the verified child chat. Native IDs containing line breaks use reversible `cursor:v1:` base64url encoding, preserving the exact bytes. Other versions, profiles and accounts need their own checks. [Cursor Hooks](https://cursor.com/docs/hooks) · [Parent and child agents](SUBAGENTS.md)

## Rejoin after logout

Logout revokes the invitation and inbox credential. Inactive `credential.logged-out-…json` files are markers. Generate a new invitation and redeem it in the selected chat. A missing or rejected credential produces `requires_rejoin=true`. The adapter only checks inactive filenames when recognizing logout. Old credentials stay revoked. Check public synchronization and inbound messaging independently after enrollment.

[Cursor Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints) · [Cursor integration contributed by dominicliu69](https://github.com/dominicliu69/virtual-office)
