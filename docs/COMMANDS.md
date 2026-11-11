# 從 OFFICE 傳送指示

每位已入場 Agent 都可以從頭頂對話輸入訊息。已有原生路由時直接傳送；其他 Agent 自動使用[共用私有收件箱](INBOX.md)，由原 session 的接收工具取得訊息，未連線時先保存留言。畫面會分清等待收取、收到與完成。

macOS Codex App 及 Hermes ACP 已實測原生背景派工：文字進入已綁定的原聊天，由原聊天的模型與權限執行；公開進度、最終回覆及交付記錄沿用現有同步。Hermes 的設定步驟見 [HERMES.md](HERMES.md)，以下 Codex 設定只適用於 Codex App。

另有通用 [Cursor Desktop Cloud adapter](CURSOR.md) 與 [Grok Bot Webhook adapter](GROK-BOT.md)，在「＋ 邀請 Agent」自行接駁，不需改程式或重啟。兩者已通過隔離協議與 UI 驗證；Grok Bot 另已完成一個真實 Bot 的兩輪 Webhook 回報測試，Cursor Cloud 尚待實際帳戶端到端驗證。Cursor 本機聊天不冒充 Cloud/CLI 接駁；Grok routine 只保證指定 Bot 路由，不宣稱綁定原生聊天 session。

## 本機啟用

一般使用者直接用邀請 Prompt。Agent 在原 Codex App session 完成接入及首次回報後，自動驗證並綁定原聊天；現有邀請身份可按「啟用持續收訊」取得升級指引。身份、歷史及未收取留言不變，不需要重啟。入口持續有效，不依賴一小時的工具等待。

以下為進階手動方式。先按 QUICKSTART 安裝 OFFICE，並將指定 Codex 原聊天以原生 `codex` driver 入駐。對已入駐的聊天執行：

```sh
python3 office.py enable-commands --agent codex:<session-id>
python3 office.py stop
python3 office.py start
```

自訂設定檔時，在子命令前加 `--config /absolute/path/config.json`。可用 `disable-commands --agent codex:<session-id>` 停用，重啟 OFFICE 後生效；已送入原 App 的指令仍由原 App 管理。

Python 偵測 App 隨附、實際支援 `queue` 的 CLI；不會更新或安裝 Codex。可用 `--binary /absolute/path/to/codex` 指定已安裝版本。一般命令列版本可能尚未支援 `queue`，必須通過能力檢查。此版本只支援本機 macOS Codex App，觀察來源與 CLI 必須使用同一個 Codex home。App 需要可正常執行原聊天；模型選擇、審批或登入要求繼續由原 App 處理。

實際送達方式是 `codex queue` 背景排隊。傳送不會開啟深層連結、切換聊天、彈出／隱藏 App 或操作桌面焦點；不另開 app-server、不複製認證、不建立新 session。已載入的原聊天可在背景接收；App 未開啟或聊天未載入時，指令可能保持排隊，需要使用者手動開啟該聊天一次。等候超過 45 秒，OFFICE 會顯示條件式提示，仍以真實回合紀錄判斷是否執行。這條接駁依賴已安裝 App 的 queue 行為，升級後需要重驗。

## 狀態與重送

- 等待送出：OFFICE 已保存使用者提交。
- 已排隊：Codex 表示 CLI 回傳指定聊天的原生排隊回條；Hermes 表示 OFFICE 等待原生接收。兩者均未表示開始執行。
- 執行中：觀察到原聊天在實際回合中收到該指令。
- 已完成／執行失敗／已中止：以同一回合的來源結束事件為準。
- 送達結果未確認：外部呼叫超時或送出途中重啟。Python 不自動重送；稍後取得原聊天紀錄時會核對結果。

Cursor Cloud 以原生 run ID 串流／查詢同一 run；重啟只恢復讀取。Grok Webhook HTTP 200 顯示「等待 Bot 回報」，需收到配對的開始及終態才更新工作狀態。兩者都不因接收成功而假定完成。

每次提交帶固定 `request_id`；同編號同內容的網絡重送只保留一筆，不重複派送。不同內容重用編號會被拒絕。原聊天訊息首行帶 OFFICE 指令編號，用來精確配對原 session 和回合；OFFICE 閱讀介面省略該首行。不要單憑角色變成「工作中」推斷某條指令已執行。

## 私隱與範圍

指令 API 只接受擁有人的本機私人入口憑證、同源請求及已選定身份；Agent 的上報憑證沒有派工權限。API／畫面／歷史只提供已遮蓋文字，原文送往原 session。待送原文短暫保存在本機 0600 的 `commands.sqlite3`，原生路由送出嘗試後清除，共用收件箱則在確認開始或收取前取消時清除；顯示記錄及接收端原聊天仍按各自規則保存。Codex 原文會短暫出現在本機 CLI 程序參數；Hermes 使用本機 stdio pipes 傳送。兩者均不能當作對同一 OS 使用者隔離。所有私人資料庫、入口與來源聊天身份均不包含在可攜包內，也不經 ntfy 發送。

## Hermes、OpenCode 與其他 CLI

指令路由按執行器／session 綁定，不按模型品牌綁定。Hermes ACP 已實作，共用 Python 指令佇列、去重和畫面，並以原生 session/load 保留上下文。其他 CLI 可使用共用收件箱協議；OpenCode 專用原生背景 adapter 尚未實作。

新增 adapter 必須提供 `available()` 和 `submit(session_id, text)`，並註冊對應的設定驗證與 session 觀察器。`submit` 回條只表示排隊層接受；觀察器仍須提供綁定 session 的執行證據及終止事件。Hermes 以 ACP 的 session ID、唯一在途請求及回覆關聯 Office 指令 ID；這個指令 ID 不冒稱原生 turn ID。要先核對 CLI 是否真的支援恢復／排隊到原 session、忙碌時行為、喚醒及停止條件，不能將開新程序的新對話當作原 session。沒有可靠去重的外部入口遇到未知結果時，不自動重試。

`POST /api/office/commands` 接受 `agent_id`、UUID `request_id`、`text`；`GET /api/office/commands?agent=...` 返回能力及最近 20 筆已遮蓋記錄。前端不直接執行 CLI、拼接 shell 或指定任意 session。

停用指令會同時封鎖通用收件箱；恢復方式見 [INBOX](INBOX.md#停用與恢復)。平台自身的批准要求仍由原 App 處理。

## 連接助手的收發測試

一般使用者從 Grok 連接助手按「連接並測試」，不用直接呼叫以下接口。測試仍經現有 Python 指令佇列及選定的原生路由，沒有另一個模型或接收循環。

- 擁有人 `POST /api/office/connections/check`，只傳 `agent_id` 與新 UUID `request_id`，建立一項簡短、無其他工作的收發測試。相同 UUID 只核對同一項，不能重新派送。已有未結束工作時不插隊。
- `GET /api/office/connections/check?agent=...` 只讀取狀態，不發送任何測試。回應包含 `check` 及最新 `capability`。`test_id` 與原 `request_id` 相同。
- `received` 來自已驗證、同一 Agent／指令／回合的原始開始事件；`replied` 來自同一回合的相配公開回覆。另須收到正常完成事件，`verified` 才可為 true。接口接收、人工修補歷史或其他工作完成均不能代替。
- `route_revision` 是不含秘密的版本識別，只供前端拒絕舊入口的遲到結果；不是權限憑證。更換入口及登出會使舊證據失效。前端仍要核對目前可用性，離線時不能顯示為目前已連接。
- 未送出的過期測試會由派送器取消；已送出或結果未知的測試不自動重送。真正的相配回報可以稍後補回結果。重新開啟頁面只讀取既有測試。

首版驗證器支援 Grok Webhook，其他平台仍沿用其既有能力與證據，不會因助手介面存在而被宣稱已支援。測試表及路由版本保存在本機私人 `commands.sqlite3`，不包含在可攜包。

### 收訊與公開同步分開

`capability.public_sync` 只描述目前實作的來源與範圍：`mode`、`scope`、`state`、`configured`、`automatic`、`manual_app_automatic`。它不從 `persistent`、`native_ready` 或 Grok 連接測試推論原 App 對話已同步。Hook 安裝待綁定、已登出或舊世代 observer 均不算原聊天觀察來源。

Grok 已有經認證回報的非 OFFICE 工作進行時，新的 OFFICE 指示保留 pending 並標記 `waiting_for_reported_work`；不重送、不覆蓋當前 run，也不以逾時假裝完成。連接測試及更換入口同樣等待真實工作結束。
