# Grok Bot Webhook and cooperative reporting

Use this route to wake a selected Grok Bot through its own routine. The bot reports public work back to the local OFFICE host. To connect directly to a particular existing Grok App chat, see the separate [Grok Bot SDK guide](GROK-SDK.md).

## Use the connection assistant

1. In desktop OFFICE, open **Invite Agent → Connect an existing App / Cloud Agent → Grok Bot**. Give the generated invitation to the selected bot in its original chat.
2. After joining, the bot returns an OFFICE connection card with the **actual routine name** to open in Grok. Grok's routine list shows names. Open the named entry to see its **Webhook / POST to** details. A new routine is named for this agent and its identifier. An existing verified routine can be reused. The bot can show the receiving address if it can read it without exposing authentication data.
3. In the same OFFICE invitation, follow the **Grok connection assistant**. Paste the receiving address, then the connection key from the same routine, into the local form. If the browser cannot use the clipboard, paste manually. Keep the key in the form and out of chat.
4. Select **Connect and test**. This sends a short test and may allow earlier pending messages to start delivery. Finish current work before testing a busy bot.
5. Follow **Test created**, **Bot received** and **Matching reply received**. The assistant shows **Connected** only after it also verifies normal completion for that same bot and test.

A bot with a saved route can go straight to the test. Advanced fields are collapsed. Unsaved inputs clear when switching agents or closing the assistant. Reopening it reads the existing test. Old successful evidence is invalid for a changed or disabled route or a logged-out agent. If Grok temporarily goes offline, the assistant displays the current availability. Platform approvals and owner-only credential screens still require the owner.

## Direct work in Grok App

Webhook verification covers an instruction sent by OFFICE. To show work initiated directly in Grok App, the selected bot has to report it. The same invitation includes the bot's long-term reporting rule. Ordinary chats, questions and tasks all qualify. Only actual public reports appear in OFFICE. Parallel bot chats have no independently verified native event scope on this route.

When `conversation.reporting.method=cooperative`, read `conversation.reporting.guide_path` and `manifest_path`, save the rule in the selected bot's native long-term configuration, then read back its identity, version and manifest path. Run the generated `reporting_setup.py confirm`. Complete this step even if `receiver_status=ready`. Existing enrolled bots can use the updated guide with their original identity, credentials and routine.

For each direct turn, submit the complete public user input as the start report, wait for `accepted=true`, then report the actual public progress and delivery as they occur. Use the original public text and the normal redaction rules. Store only the bot-specific rule and manifest path in long-term memory. The credential belongs to the local reporter. `agent_confirmed` and `agent_reported_saved` are cooperative setup receipts. To verify later work, type an ordinary prompt directly into the App and check the matched working state, conversation, delivery and history in OFFICE. An active reported App turn holds subsequent OFFICE commands and connection checks until its outcome arrives. [Bot rules and memory](https://docs.x.ai/grok-bot/bots#edit-a-bot) · [Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)

## How background delivery works

After the first accepted registration reports, the agent follows `conversation.guide_path`. It can give the official routine's **POST to** address and key to `connect.py grok --details-stdin` on the OFFICE host using its own credential. If only the human can view those fields, the connection assistant provides the owner handoff. This setup preserves the bot's identity. It does not start another receiving service or require repeated wait calls. Keep the key out of shell arguments.

OFFICE sends `{office:{version:1,command_id,agent_id},prompt:{text}}` to the selected official routine URL with a bearer credential. HTTP 200 acknowledges the request. The routine uses its enrollment's `emit.py` and event credential. Its run ID is `office.command_id`. It submits the original `prompt.text`, including the OFFICE marker, with `turn_started` and stable event ID `command_id + ':start'`. Only a newly accepted start may execute the instruction. Handle duplicates or a failed start by reconciling the original command. Report public progress with `message` and the outcome with `turn_completed` or `turn_failed`. Reply in the bot chat and report the delivery to OFFICE.

The bot needs execution access to the **computer running OFFICE** for its callbacks. A cloud computer's localhost refers to that cloud computer. The route reports `same_session:false` because the routine targets the bot. Late matching callbacks can resolve an outcome after OFFICE restarts. In a recorded macOS trial a real bot completed two Webhook rounds, including one after restarting OFFICE, with public replies and saved history. Each account and installation must verify its own route. [Grok Bot Webhooks](https://docs.x.ai/grok-bot/skills-routines-and-automations#webhooks) · [Local execution](https://docs.x.ai/grok-bot/security#local-execution)

## Report native Task children

A parent bot can report a real `Task` child using the ID returned by Grok and the result from `CheckSubagent`. OFFICE labels this evidence `parent_reported`. Only the local reporter reads the parent's credential. A child does not inherit the parent's OFFICE token.

1. Start the parent's actual turn with `emit.py` and retain the accepted `parent_run_id`.
2. When a native Task creates a child, keep its returned ID, allocate a fresh `child_run_id` and call `children.py start`.
3. Send each real public update with `message`. Once a native result exists use `complete` with the public delivery, or `fail` / `cancel` as appropriate.
4. Keep the parent run, child run and native child ID stable. Give each event a unique ID. To check an uncertain report, reuse its original ID and identical payload.

Use the executable and paths from the invitation, as an argument array. Pass the actual public text through stdin, for example with `subprocess.run(..., input=public_text, text=True)`.

```json
[
  "python3", "/absolute/office/adapters/children.py",
  "--credential", "/absolute/parent-credential.json",
  "start", "--child-id", "NATIVE_TASK_ID",
  "--parent-run", "PARENT_RUN_UUID", "--run", "CHILD_RUN_UUID",
  "--event-id", "CHILD_START_ID", "--name", "Data review", "--text-stdin"
]
```

Change the action and event ID for subsequent reports. `message` defaults to public assistant progress. Use `--role user` for delegation text or `--phase final` for a final public message. The separate terminal event closes the child run. A nested child also supplies `--parent-id` with its immediate parent's native ID. That parent must already have an active start.

Report only the delegated task, progress and delivery visible to the user. Omit raw transcripts, reasoning, tool inputs/results and other chats. `accepted=true` acknowledges a cooperative report. A macOS Grok Bot 0.78.5 trial followed three real Task children across two bots, including parallel children from an OFFICE webhook. It verified public progress, all three deliveries, independent parent completion and history after the roughly 30-second departure. [Parent and child agents](SUBAGENTS.md)

## Report public events from a custom runner

The standard invitation prepares the selected agent's identity and private paths automatically. An owner configuring a custom runner can instead use `office.py enroll --provider grok_bot …` with a fixed source ID. Use the agent's own event credential path for its reporter.

```sh
python3 adapters/emit.py --credential .office/grok.json turn_started --run RUN_UUID --event-id START_EVENT_UUID --text-stdin
python3 adapters/emit.py --credential .office/grok.json message --run RUN_UUID --event-id UPDATE_EVENT_UUID --phase commentary --text-stdin
python3 adapters/emit.py --credential .office/grok.json turn_completed --run RUN_UUID --event-id END_EVENT_UUID --text-stdin
```

Use a new run UUID for each task. Supply public text on stdin and send the appropriate terminal event for failure or cancellation. If the reporter cannot reach the local OFFICE, tell the user which synchronization step failed and continue only their authorized work. Sending public events alone does not enable background receipt. [Event protocol](EVENTS.md) · [Persistent receiving](INBOX.md)
