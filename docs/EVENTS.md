# Public event protocol v1

Send events to the loopback endpoint `POST /api/agents/events` with `X-Agent-Id` and `Authorization: Bearer …`. The credential authorizes reports for one enrolled agent. Use `adapters/client.py` or `adapters/emit.py` to read it from its private file. Owner actions and conversation reads require a separate credential.

```json
{
  "event_id": "unique-event-uuid",
  "source_id": "my-grok",
  "run_id": "unique-run-uuid",
  "type": "message",
  "role": "assistant",
  "phase": "commentary",
  "text": "I am checking the test results."
}
```

Start work with `turn_started`. Its optional `text` is the user's task. Send `message` or `heartbeat` during the turn, then `turn_completed`, `turn_failed` or `turn_cancelled`. A completion event may carry the final delivery in `text`. Supported message pairs are `user/prompt`, `assistant/commentary` and `assistant/final`. Submit only text intended for the user. Exclude reasoning, raw tool output and complete logs.

`run_id` accepts letters, digits, underscores and hyphens, up to 160 characters. `event_id` also allows periods and colons. Text is limited to 24,000 characters. Optional `at` is an ISO timestamp. The response returns `accepted` and `duplicate`.

For a retry, keep the same event ID and payload. Check the receipt if the outcome is uncertain. Persisted receipts survive FIFO eviction. The server rejects reuse of an ID with different content, stale completions and attempts to restart a closed turn. If a new turn supersedes an open one, the old outcome is marked unconfirmed.

Only `turn_started` can carry `workspace_path`, an absolute project root. `emit.py` uses the current directory by default or accepts `--workspace`. This updates the reporting agent's automatic floor. Manual owner assignments take precedence. Duplicate and rejected events leave the floor alone. Older adapters can omit the path and appear unassigned. Reporting a path grants no file access.

## Owner endpoints

Use `X-Office-Key` for these local owner operations.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/office/workspaces` | List registered paths. |
| `POST /api/office/workspaces` | Create a workspace with `path` and optional `name`. |
| `POST /api/office/workspaces/assign` | Assign an enrolled `agent_id` using `mode` (`auto` or `manual`). A manual `workspace_id` of `null` leaves the agent unassigned. |
| `GET /api/office/work` | Read work state. |
| `GET /api/office/history?agent=…` | Read that agent's history. |
| `GET /api/office/run?id=…` | Read a specific run. |
| `GET /api/office/messages?agent=…&after=SEQ` | Read public messages. Use `before` for older pages and `run` to filter. Use the sequence returned by the server as the cursor. |

An invitation can specify `workspace_id`. Otherwise the adapter may report its own working directory.
