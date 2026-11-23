# Parent and child agents

Children of an enrolled parent can appear beside it on the same floor. Each child has its own status, public text and delivery history. Children are temporary, read-only participants. The displayed evidence identifies how the integration learned about them.

| Source | Evidence | What OFFICE can establish |
| --- | --- | --- |
| Codex App local records | `native_metadata` | A verified parent relationship and public events from the child's own chat. |
| Cursor local hooks and structured records | `native_hook` | Native identity, parent and terminal state from hooks, with scoped public text from the verified child turn. |
| Grok Bot cooperative reports | `parent_reported` | The parent reports a genuine native Task ID, public progress and outcome. |
| Hermes | No child observer available | The parent's state cannot establish child activity. |

## See children in the office

1. Enroll the parent. Bind the selected original Codex App chat, activate Cursor's native hook, or follow the Grok cooperative instructions as appropriate.
2. Let the parent create a real child. After OFFICE accepts the child's start, its avatar appears near the parent. Click its name to read the public task, progress and delivery.
3. After completion, failure or cancellation, the avatar stays briefly (about 30 seconds). History remains available. Use the parent's **Child agents** selector in full or simple mode to choose among children.

The following arrangement affects only the display. A child has no independent OFFICE send, seat, workspace or logout control. Follow-up work goes through the parent. It does not occupy a permanent seat or inherit the parent's OFFICE credential. When space is limited, the selected child has priority and saved history remains accessible.

## Activate Cursor once

The invitation runs `adapters/connect.py cursor-children --hooks-file …` with the parent's own credential. It creates a pending observer and merges scoped hook commands while preserving other hooks and approval rules. A binding can require an `activation_marker`. Include the full marker exactly once in the **public final reply of the selected original chat**. The matching native `afterAgentResponse` binds the actual conversation ID and rotates the observer token and generation. A pending observer cannot publish work.

The activation reply is setup only. The next real `beforeSubmitPrompt` establishes the parent turn. This installation's shared `--observer-dir` handles enrolled Cursor parents without scanning other homes or installations. Cursor Desktop 3.29.9 was tested with real Task identities, parent/child isolation, public progress, final delivery bodies and persistent history. [Cursor setup and evidence](CURSOR.md)

## Report Grok children

The parent reports its own turn start, creates a real native `Task` and keeps its child ID. On the OFFICE host it calls `adapters/children.py` for starts, public messages and actual terminal results. The parent must be able to execute on that host. A macOS trial covered two real bots and three Task children, including parallel work and one Webhook-initiated turn. This route depends on the parent's cooperation. [Grok Task reporting](GROK-BOT.md#report-native-task-children)

## Identity, public text and lifecycle

- Identity includes the enrolled parent, source generation and native child ID. Each child also carries its immediate parent, root member, depth and parent run. An active matching parent turn is required, including for nested children.
- Events have unique IDs. Repeating an event with the same ID and payload returns a duplicate receipt. A start receipt confirms receipt only.
- Native metadata establishes a relationship. Current activity and terminal outcomes require events from the matching turn. A parent can finish while a child is still working.
- Completion, failure, cancellation and unknown status are distinct. An interrupted source or a restart without new evidence can leave work unconfirmed. Logout or rebinding invalidates the old observer. A late event cannot re-enroll its parent.
- Only delegated task text, user-visible progress and final output pass through redaction. Codex v2 excludes inherited material and earlier turns. If only encrypted task text exists, OFFICE marks it unavailable and can show later public progress.
- Raw tool data and private reasoning are excluded. Cursor tool hooks use only the identity fields needed for correlation. `afterAgentResponse` delivers a complete public message, not a token stream. If the platform provides no intermediate text, the chat can go straight from start to delivery.

Observation does not create an internet endpoint, send child chats to ntfy or run additional models. Local Cursor child observation and Cloud Agent command delivery are independent features.
