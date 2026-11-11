# 通用公開事件協議 v1

`POST /api/agents/events`，只接受本機來源。Header：`X-Agent-Id` 和 `Authorization: Bearer ...`。憑證只限設定中的單一 Agent；不具有讀取對話、加入其他 Agent 或派工權限。請用 `adapters/client.py` 或 `adapters/emit.py` 讀取本機憑證檔，不要在 Prompt 中貼出憑證。

```json
{
  "event_id": "unique-event-uuid",
  "source_id": "my-grok",
  "run_id": "unique-run-uuid",
  "type": "message",
  "role": "assistant",
  "phase": "commentary",
  "text": "正在核對測試結果。"
}
```

每次工作先送 `turn_started`，可帶 `text` 作使用者任務。之後可送 `message`、`heartbeat`，最後送 `turn_completed`、`turn_failed` 或 `turn_cancelled`。完成事件可帶 `text` 作最終交付。`message` 只接受 `user/prompt`、`assistant/commentary`、`assistant/final`。不要送入工具輸出、分析內容、思考過程或整份原始日誌。

`run_id` 只用英數字、底線和連字號，長度上限 160；`event_id` 另外容許點號及冒號。文字上限 24,000 字元。伺服器回傳 `accepted` 與 `duplicate`，不回傳文字。可選的 `at` 為 ISO 時間。

同一事件再次傳送時必須保留相同 `event_id` 及內容；結果不明時應先核對收據，不要改 ID 無限重試。已儲存的事件收據不因有限 FIFO 到期失效。重複 ID 配不同內容、舊回合完結事件及重啟已完結回合均會被拒絕。新回合取代仍未完結的舊回合時，舊回合記為待確認，不推斷完成。

`turn_started` 可帶 `workspace_path`（專案根目錄的絕對路徑），其他事件不可帶此欄位。`emit.py` 預設使用目前目錄，或以 `--workspace` 指定。此欄位只更新上報者自己的自動 Workspace，不能覆蓋擁有人手動指定的樓層；重複或已拒絕事件不會搬層。未提供目錄的舊版接駁器仍可上報，樓層顯示未指定。這不是檔案存取授權。

擁有人可用 `GET /api/office/workspaces` 查看本機目錄清單；`POST /api/office/workspaces` 傳 `path` 及可選 `name` 建立 Workspace。`POST /api/office/workspaces/assign` 傳 `agent_id`、`mode`（`auto` 或 `manual`）及手動模式的可選 `workspace_id`（null 為未指定），只可操作已選 Agent，均需要 `X-Office-Key`。邀請可指定 `workspace_id`，未指定時由接入程式回報工作目錄。

受保護的本機讀取 API：`/api/office/work`、`/api/office/history?agent=...`、`/api/office/run?id=...`、`/api/office/messages?agent=...&after=SEQ`。讀取需要擁有人的 `X-Office-Key`。訊息支援 `before` 載入較早頁，以及 `run` 篩選工作；使用實際回傳的 sequence 作游標。
