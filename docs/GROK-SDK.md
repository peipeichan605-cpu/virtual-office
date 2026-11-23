# Grok Bot SDK experimental mode

Use an existing Grok Bot and its selected native chat from OFFICE. This optional route uses an unofficial open-source SDK for the Grok Bot desktop protocol. The xAI model API creates a separate model conversation. The [Webhook route](GROK-BOT.md) remains available for owners who use routines.

## Connect a bot and chat

1. Open **Invite Agent** or **New conversation** and select **Grok Bot SDK, experimental**.
2. Choose **Sign in** and complete authentication on the platform's own browser page. Return to OFFICE.
3. Select an existing bot, a chat and an OFFICE display name. A verified **Main chat** can be selected even when the platform represents it without a textual session ID.
4. Send a short instruction from the avatar's chat. Then type a different instruction directly into the same chat in Grok App. In each case, verify the public input, working state, corresponding reply and saved delivery.

Listing bots reads available choices. Only the bot and chat you confirm become an OFFICE member and observation source.

## Runtime and credentials

Use Node.js 22.13 or later. The pinned SDK source ships in the package, so a compatible Node installation can run it without `npm install`. The separate browser sign-in flow uses its own account credential. It does not read the Desktop keychain or login files. The credential is stored locally under the installation's private `grok-sdk/` directory with mode `0600`. It has account-level scope. OFFICE limits message delivery and observation to the selected bot and chat.

Login links are valid for 15 minutes. If the account session expires, sign in again to the same account. A different account cannot silently replace an existing connection. The SDK offers no automatic renewal or remote revocation. Signing an agent out of OFFICE releases its OFFICE binding and keeps its history. The Bot, Grok App login and provider account session have separate lifecycles.

## What OFFICE synchronizes

- Public inputs and recognized bot replies from the selected native chat appear for work started on either side. Only public text passes through OFFICE's redaction rules.
- A send receipt acknowledges submission. OFFICE matches native messages and the native turn result to establish completion. Idle state, a closed stream or a pause in text is insufficient.
- The observer persists message identities and its read position. On reconnect it reconciles saved records. Missing text, a lost cursor or an interrupted stream can leave an incomplete record.
- If a send has an unknown outcome, check the original chat and command ID before attempting more work. OFFICE avoids automatic duplicate dispatch.
- The first validation targeted the main chat. A list of SDK children does not establish child-agent conversation sync. The Webhook route has its own cooperative child reporting.

## Background operation and verification

Keep the OFFICE host, Python service and network available. The worker can continue with the browser closed and does not focus the desktop. The upstream SDK does not wake a sleeping cloud computer. For a particular installation, verify an OFFICE-originated turn, an App-originated turn, another after a measured idle period, and one with the Grok App fully exited. Reopen the App to check the same history. Also test an OFFICE restart or network reconnect for missing, duplicated or mismatched turns.

On November 13, 2026, a Grok Desktop 1.0.1 trial on one account's existing main bot chat completed four rounds. These included both message origins, about 2.5 minutes idle and about 75 seconds with the App fully exited. Each round produced a matching public input and reply, native success result and local delivery. The App was still closed when the final reply arrived. Long idle periods, sleeping cloud computers, expired logins and other accounts or versions need their own tests.

## Source and compatibility

The package vendors MIT-licensed `grok-bot-sdk` 0.78.1 at commit `42c75eb13f24630d1b689629502d4320c4219cdb`. Changes to the upstream desktop protocol may affect compatibility. The vendor directory includes provenance hashes and the local native-turn compatibility notes. [Pinned upstream source](https://github.com/RongleCat/grok-bot-sdk/tree/42c75eb13f24630d1b689629502d4320c4219cdb) · [Compatibility notes](../vendor/grok-bot-sdk/OFFICE-COMPATIBILITY.md)
