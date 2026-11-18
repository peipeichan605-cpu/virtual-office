# OFFICE compatibility extension

The upstream runtime is pinned to `42c75eb13f24630d1b689629502d4320c4219cdb` (SDK 0.78.1).
`PROVENANCE.json.files` records the original upstream hashes. `local_patches` records the modified runtime hash.

A narrow local patch in `sdk.mjs` preserves `live.lastTurnSettlement` from the native Grok Bot 1.0.1 schema, which the upstream normalizer drops. The accepted fields are `turnId`, `clientNonce` (falling back to turn ID, following the native client), `outcome` (only success/cancelled/error), and positive integer `settledAtMs`. This applies to selected status reads and streamed `agent.state`. It exposes neither internal reasoning nor tool payloads.

OFFICE must still match native nonce/turn identity to a known public input and verify delivery text. Idle state is not completion. A protocol-compatible schema is not live bidirectional test evidence.

Desktop-version metadata may choose the corresponding request header, but the pinned implementation and this extension remain experimental. New server/client schemas must be audited and tested before extending this patch.
