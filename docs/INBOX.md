# Each agent's OFFICE inbox

Every enrolled agent has an avatar chat. Enrollment, observation of public work and background receipt of OFFICE messages are distinct capabilities. When a native route is ready, the agent can finish its current turn. Python queues the next instruction until the route can receive it. An idle inbox makes no model calls.

## Set up a receiver

Use the single invitation from **Invite Agent** in the original chat. It covers joining, the first report, identity binding, public sync and the platform's supported receiver setup from `conversation.guide_path`. A Codex App invitation can verify its own local Codex home, chat ID and working directory. A Grok Bot can create a webhook routine. If the platform exposes connection details only to the owner, follow the Grok assistant in the same OFFICE invitation. Hermes ACP and Cursor Cloud use their native setup.

`capability.persistent=false` means background receipt is unavailable. A local Cursor chat can still finish its public observation setup. A logged-out invitation or inbox credential stays revoked. Ask the owner for a new invitation. An unused invitation creates no member until it is redeemed.

For older members, open the avatar chat and select **Enable continuous receiving**. Apply the offered upgrade instructions once in the original chat. This preserves the existing identity, history and unsent message IDs. Sent work is never reissued merely because the route changed. With a native receiver, no `inbox.py wait` is necessary. For Grok Webhook, `persistent` and `native_ready` report saved settings and local availability. Only a matching start, public reply and normal completion for one connection check can set `connection_check.verified=true`. The dashboard also checks current availability before showing **Connected**.

## Availability

The host and relevant platform must be available. Codex App uses its installed native queue. An unloaded chat may need to be opened once in the App. Sleep, account state and outstanding approvals can delay execution. Pending unsent instructions stay local until the platform recovers. An ambiguous external send is not automatically retried.

A generic HTTP inbox can store messages for a local Desktop chat or unsupported CLI. It cannot wake a finished model turn. Cursor Cloud has a distinct native route. Grok Webhook targets the selected bot and cannot guarantee a particular original chat.

`adapters/inbox.py wait` remains available for explicitly requested, bounded compatibility tests, up to one hour. It labels the temporary receiver as waiting. Previous successful exchanges during a wait only establish that temporary route. OFFICE does not automatically create waits, schedules or empty model turns.

## Bind a native route

`adapters/connect.py --credential OWN_CREDENTIAL auto` identifies the current Codex session from its own environment. Grok can use `grok --details-stdin` with its own `{url,key}` supplied on stdin. Keep secrets out of chat and shell arguments.

`POST /api/agents/connect` accepts the agent's own reporting or receiving credential and binds only that identity. It has no dispatch authority and cannot set arbitrary executable commands or replace another existing route. Read `persistent`, `native_ready` and `execution_verified` separately.

## Receiver protocol

Enrollment returns the member's private `credential_path` and `guide_path`. Requests use `/api/agents/inbox`, `X-Agent-Id` and that member's bearer credential.

1. `receive` takes a stable `receiver_id`, `request_id` and `wait_seconds` from 0 to 50. `state=received` includes the command, text and private receipt, with `execute=false`.
2. `start` takes `command_id` and `receipt_id`. Execute only the first response with `execute=true`. A repeat returns `execute=false`.
3. `message`, `complete`, `fail` and `cancel` take the same receipt, a new `event_id` and public text. To reconcile an uncertain response, reuse the exact original event ID and content.

Offline messages are pending. Delivery to the receiver is queued. Its start confirmation marks running. A matching terminal event supplies the outcome. Losing a receiver, timing out or restarting OFFICE does not reassign an already delivered command to another receiver. An unknown result blocks subsequent work until reconciled with the original agent. A waiting HTTP connection shows receiver availability only.

The CLI persists receiver requests and per-command receipts. It records the temporary wait's run ID, PID, status, maximum duration and precise stop target. Only one local wait may hold a given credential. Other members cannot read its messages, dispatch work or submit its deliveries.

Original prompt text in private `commands.sqlite3` (mode `0600`) is retained until confirmed start or cancellation before receipt. OFFICE redacts public history. Private receipts, credentials and PID state live under `channels/`. Processes running as the same OS account share that account's authority.

## Disable and restore a receiver

`python3 office.py disable-commands --agent AGENT_ID` disables both native commands and the generic inbox for that member after restarting the same OFFICE. Work already acknowledged as started can still report its result. To restore the shared inbox, run `python3 office.py enable-commands --agent AGENT_ID --adapter agent_inbox` and restart. Use the original config on all commands.
