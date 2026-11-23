# Send instructions from OFFICE

Open an enrolled agent's overhead chat, enter a message and send it. OFFICE uses the selected native route when available. Other enrolled agents have a [private inbox](INBOX.md) that can hold messages until a receiver is available. The chat distinguishes a saved message, receipt, execution and final delivery.

Existing macOS Codex App chats and [Hermes ACP](HERMES.md) have native background routes. [Codex background](CODEX-BACKGROUND.md) creates a separate OFFICE-managed thread. [Cursor Cloud](CURSOR.md) sends to the selected cloud agent. [Grok Webhook](GROK-BOT.md) wakes a selected bot. Use the platform's own guide for what can be observed when a user types directly in the App. Fixture tests and real-account verification are documented separately.

## Enable an original Codex App chat

The normal invitation verifies the original chat after its first report and binds its native queue. Older enrollments can use **Enable continuous receiving** in their avatar chat. The existing identity, history and outstanding messages are preserved. No repeated wait is needed.

For an advanced manual installation, first enroll the intended original chat using the native `codex` driver. Enable commands for its exact identifier and reload this installation after current work is settled. Substitute the actual session ID in the following example.

```sh
python3 office.py enable-commands --agent "codex:SESSION_ID"
python3 office.py stop
python3 office.py start
```

For custom configs, supply `--config /absolute/path/to/config.json` before every subcommand. Use `disable-commands --agent "codex:SESSION_ID"` and restart to disable new dispatch. Work already submitted remains under the original App's control.

This route requires macOS, the same local Codex home for CLI and observer, and an installed binary with real `queue` support. OFFICE detects the App-bundled CLI. Use `--binary /absolute/path/to/codex` to select a verified version. The connector does not install or upgrade Codex. The original App owns the project, login, model and approvals.

`codex queue` submits in the background without changing desktop focus. An unloaded chat or closed App may leave a message queued until the user opens that chat. After 45 seconds OFFICE can show a conditional hint. Only the native turn record confirms execution. Recheck behavior after a Codex upgrade.

## States and retries

| State | Evidence |
| --- | --- |
| Pending | OFFICE saved the user's request locally. |
| Queued | The native Codex queue acknowledged the selected chat, or Hermes is waiting for native receipt. |
| Running | The bound chat started a matching turn. |
| Completed, failed or cancelled | A corresponding source event ended that turn. |
| Unconfirmed | A send timed out or the process restarted mid-send. OFFICE waits for matching source evidence. |

Each submission has a stable UUID `request_id`. Identical retries return one command. Reusing the ID with different content is rejected. For adapters that use an OFFICE marker, the first native prompt line carries the command ID. The display omits that line. Cursor Cloud correlates by native run ID and resumes reads after restart. A Grok Webhook HTTP 200 leaves the command waiting for the bot's own start and final reports.

## Logout and uncertain work

Known running, queued or approval-pending work blocks logout. If only old unconfirmed work remains, review the explanation and explicitly choose **Sign out anyway and keep records**. A changed identity or work state requires a fresh check. Logout cancels unsent messages and preserves public conversations, deliveries and unknown outcomes. To cancel a provider task, check it on the provider.

## Command API and local storage

`POST /api/office/commands` accepts `agent_id`, UUID `request_id` and `text`. `GET /api/office/commands?agent=…` returns capability and the most recent 20 redacted records. The local owner API checks the owner credential, origin and enrolled identity. Reporting credentials cannot dispatch commands. The private mobile gateway separately applies its view/send grants.

Original instructions go to the bound session. Pending text is temporarily stored in local `commands.sqlite3` with restricted access. Native routes clear it after the dispatch attempt. The generic inbox clears it at confirmed start or cancellation before receipt. Public OFFICE records are redacted. The original platform applies its own retention. Codex briefly passes the text in local CLI arguments. Hermes sends it over stdio. Both share the current OS account's permissions.

## Other command adapters

Routes bind a runner and session. Hermes ACP resumes the same context through `session/load`. Other CLIs can implement the generic inbox. There is no dedicated native OpenCode receiver in this release.

An adapter provides `available()` and `submit(session_id, text)`, plus configuration validation and a matching session observer. A submit receipt confirms queue acceptance. The observer must establish execution and the terminal result for the bound session. Hermes correlates the ACP session and its unique in-flight request with the OFFICE command ID. Check native resume, queue, busy, wake and stop semantics before advertising persistent receipt. An unknown outcome must not trigger an unbounded external retry. [Disable and restore receiving](INBOX.md#disable-and-restore-a-receiver)

## Connection assistant checks

For Grok Webhook, use **Connect and test**. It sends a short check through the selected native route and existing Python queue. No extra receiving service is needed.

- The owner submits `POST /api/office/connections/check` with `agent_id` and a fresh UUID `request_id`. Reusing the UUID checks the same test. Active work keeps its place in the queue.
- `GET /api/office/connections/check?agent=…` reads `check` and the latest `capability`. The `test_id` equals the original request ID. Reading and reopening the page do not send another test.
- `received` requires an authenticated matching start. `replied` requires the matching public reply. `verified` additionally requires a normal terminal event for that same agent, command and turn.
- `route_revision` lets the UI discard stale results. It contains no secret. Changing the route, disabling commands or signing out invalidates old success. Current availability is checked separately.
- The dispatcher can cancel an expired unsent test. A sent or uncertain test is never automatically resent. A matching callback can resolve it later. The local test records live in private `commands.sqlite3`.

`capability.public_sync` reports `mode`, `scope`, `state`, `configured`, `automatic` and `manual_app_automatic` for a specific source. Saved receiver settings cannot establish App-originated public sync. If a Grok bot already reports authenticated work, an OFFICE command remains pending with `waiting_for_reported_work`. Connection checks and route replacement also wait for the real work to finish.
