# Cursor Desktop 接駁

本機聊天可使用[共用收件箱](INBOX.md)保存留言，但本版尚未接通其原生背景入口，不會顯示「持續收訊已接通」。短期測試仍可明確啟動原 session 的等待工具；這不是免待命方案。下文的 Cloud Agent 路由可在回合結束後繼續接收 OFFICE 指示，兩者不能互相替代。

## 登出後重新入場

經邀請登入的 Agent 登出 OFFICE 後，原邀請及收件箱憑證會失效。`credential.logged-out-…json` 是停用紀錄，不是可恢復的登入檔；不要改名或重用，也不要用其他平台入口代替目前聊天。

由擁有人在 OFFICE 產生新 Cursor 邀請，把新接入 Prompt 貼到選定的原聊天即可。新邀請仍未兌換時，不會把 Agent 自動放入 OFFICE；舊邀請保持撤銷。先核對 `joined=true`、兩個 `accepted=true` 及實際 `run_id`，再依新邀請完成父聊天的觀察綁定。子 Agent 的公開同步沿用父聊天設定。

**入場／公開同步與背景收訊分開判定。** `capability.persistent=false` 只表示此入口未能背景喚醒原聊天，不會撤銷已成功的入場或公開同步。本機一般聊天的指引不要求 Codex 自動辨認、輪詢待命或試接其他平台；Cursor Cloud 的原生持續收訊流程仍按其既有能力處理。

接駁程式遇到缺少的憑證或伺服器拒絕舊身份時，會回傳 `requires_rejoin=true` 及重新邀請的下一步。辨認登出狀態只檢查同目錄的停用檔名，不讀取或復活該檔內容。

## 本機聊天的父子 Agent 原生 Hooks

本機 Cursor 聊天可以使用原生 Hook 將已入場主 Agent 及其子 Agent 同步到 OFFICE。這是觀察功能，證據標示 **原生事件同步（`native_hook`）**；不會替本機聊天新增背景指令入口，也不會另開 CLI／Cloud Agent。

### 一次接入及原聊天綁定

1. 在 OFFICE 邀請 Cursor Agent，把產生的指引貼到你選定的本機原聊天，先完成一般接入及首次回報。
2. 按指引執行 `adapters/connect.py --credential ... cursor-children --hooks-file ...`。新指引會填好這個安裝的程式路徑及目前使用者的 Cursor Hook 設定位置。
3. 程式會合併這個 OFFICE 的觀察 Hook，保留其他 Hook／批准規則，並回傳待綁定結果及 `activation_marker`。不要自行猜測 `conversation_id`、另開聊天或使用其他 Agent 的憑證。
4. 在**目前原聊天的公開最終回覆**原樣包含該完整標記一次。只有相配的 `afterAgentResponse` 原生 Hook 才可以將實際 `conversation_id` 綁定到此 Observer；Prompt 中的標記或標記片段不會啟用。
5. 綁定成功時，伺服器會輪換同一個本機觀察檔的 token／世代。啟用回覆及其 Stop 不會被補造成正在工作；下一項真正 Prompt 的開始事件才建立原生父回合，之後正常使用 Task 子 Agent。

`hook_installed=true` 證明設定已合併，不等於原生 Hook 已發生。待綁定的 Observer 不能提交工作事件；需分別核對聊天綁定、第一項真實父任務、子 Agent 開始及完成。已綁定的 Observer 可重用，不會因再次執行設定命令而自動降級成待綁定。

### 入場結果分開核對

同一份通用入場 Prompt 會讀取 `conversation.observation` 並接續原生 Hook 設定，毋須用戶再貼第二段 Prompt。即使本機背景收訊是 `unsupported`，亦不能省略公開同步設定。

- `setup_required`：原聊天尚未建立觀察綁定，按該安裝產生的指引執行。
- `binding_required`：Hook 已有待綁定身份；在目前原聊天公開最終回覆包含一次回傳的完整 `activation_marker`，讓原生 Hook 完成核對。
- `configured`：伺服器已核對目前身份、觀察世代及原聊天綁定；下一項真正工作才驗證同步是否完整。
- `stale`／`revoked`：沿用同一指引的明確修復或重新邀請步驟，不猜測或重用舊憑證。

`public_sync_onboarding_complete` 與 `receiver_status` 分開回報。前者確認設定階段，不能代替真實 App 工作驗證；後者未支援時，OFFICE 只能保存留言，不能宣稱原聊天已收到或執行。

### 每個安裝共用一個觀察入口

每個 OFFICE 安裝的 Hook 命令使用 `--observer-dir`，指向該安裝自己的私人 `child-observers/` 目錄。當中保存各主 Agent 獨立的觀察憑證、已啟用的 Cursor 綁定索引及本機事件佇列；命令列不包含 token。再接另一個 Cursor 主 Agent 時重用同一組 Hook 命令，不會每個 Agent 重複安裝一套，也不遍歷其他使用者的目錄。

安裝器會保留其他設定並為改動前內容建立本機備份；若偵測設定在合併期間已被其他程式更改，會停止而不覆蓋新內容。升級時只自動移除本安裝中、使用同一個父 Agent 憑證路徑的精確 `cursor_hook.py --credential ...` 舊命令，避免同一回覆重複上報；不讀取其他憑證，也不改自訂 wrapper 或安全 Hook。

同一個原生聊天若另有自訂觀察 wrapper，或仍由另一套 OFFICE 安裝觀察，擁有人需核對並停用自己已不再使用的重複觀察設定。搬移 OFFICE 位置後，應按新指引重新設定自己的 Hook，清理已失效的舊 OFFICE 命令；不要刪除其他用途的 Hook。同一安裝亦不允許同一個原生聊天同時綁到兩個已選主 Agent。

目前註冊的原生事件包括：

| 事件 | OFFICE 用途 |
| --- | --- |
| `beforeSubmitPrompt` | 已綁定主聊天的父回合開始及公開 Prompt |
| `subagentStart` | 子 Agent 開始、原生身份、直接父關係及平台提供的委派文字 |
| `afterAgentResponse` | 平台有提供時接收原生公開 assistant 訊息；不是逐 token 串流 |
| `subagentStop` | 已對應子工作完成、失敗或中止，及平台提供的公開摘要 |
| `stop` | 父回合完成、失敗或中止 |
| `preToolUse`／`postToolUse`／`postToolUseFailure` | 只作原生父子身份關聯；不保存工具參數或工具原文 |

不能安全對應的 conversation／tool-call／generation 不會靠名稱或文字猜測身份。公開文字缺失時不補造進度；父回合結束亦不會令子 Agent 被當成完成。

若一位全新子 Agent 沒有中途工具事件，其 Stop 只提供新的 `child_conversation_id`、沒有可驗證的子 Agent／Task ID 或既有 alias，程式會保留未確認狀態，不能把摘要猜配給平行工作中的某一位子 Agent。各 Desktop 版本實際提供哪些欄位，須由原 App 測試確認。

觀察失敗不會阻止 Cursor 原本工作，亦不會放寬原有批准規則。未確認送達的回報保留在有上限的本機佇列，後續 Hook 可用相同事件 ID 補送；不會重做原任務或呼叫模型。沒有後續 Hook 時，不保證即時補送，需核對 OFFICE 連線及來源。

### 原 App 實測範圍及限制

Cursor Desktop 3.29.9 的 This Mac 原聊天已完成綁定及兩位真正並行 Task 子 Agent 的驗證：自動出場、父子身份、工作中／完成狀態、父回合獨立完成、約 30 秒退場及退場後歷史均已核對。這些觀察來自原生 Hooks，沒有手動補造子事件。

此版本實測的 Task ID 包含換行。接駁器會把需要編碼的原生 ID，以 `cursor:v1:` 加可逆 base64url 保存；保留完整位元組、區分保留前綴，不能以刪除換行、名稱或最近一位子 Agent 猜配。既有普通 ID 及觀察憑證格式保持原規則。

3.29.9 的子聊天沒有發出 child `afterAgentResponse`，Stop 亦未提供摘要。新增的共用 Python 讀取器會從 Cursor 原生結構化資料補取公開文字；已在原 App 驗證工作中的公開進度及完成後完整交付正文。正文來自該子聊天自己的訊息，不取父聊天轉述，也不使用混合思考內容的文字／JSONL 匯出。[官方 Hooks](https://cursor.com/docs/hooks)、[通用父子機制](SUBAGENTS.md)。

### 共用公開對話同步

一次完成原聊天的 Hook 綁定後，主聊天及新的 Task 子 Agent 共用公開文字讀取器，毋須逐隻邀請、填路徑或每次執行回報命令。Python 沿用 OFFICE 服務，約每秒檢查已驗證回合；不新增模型、常駐 CLI 或公網入口。

主聊天只在相配的原生開始事件被接受後建立讀取範圍，使用已綁定 conversation 及唯一 HUMAN `requestId` 對應同一個原生回合，遇下一則 HUMAN 即停止；不以文字、時間或最近聊天猜配。每次讀取後再核對目前身份、觀察世代及回合。原生 Hook 仍決定開始及終態；已保存的公開最終交付與結構化正文相配時才補入，保留原完成時間及狀態。Hook 與讀取器重複見到同一段公開訊息時沿用穩定的顯示身份，保留接收證據。

讀取器只查指定父聊天及其已驗證子聊天的原生資料鍵，核對父子關係、父回合及精確 Task ID，再以該次 Task 的原生 HUMAN 訊息劃定正文範圍。重用子聊天時，不會將上一項任務的內容混入新回合。只有普通公開 assistant 的 `text` 會通過；thinking、工具原文、服務通知及未知內容不載入 OFFICE。正文按既有規則遮蓋敏感內容後保存。

公開訊息以原生 bubble 身份更新及去重；完成的文字段落會在原角色頭頂對話出現，並非逐 token 串流。原生 Stop 確認後才保存最終正文；原生資料較遲落盤時，會在 120 秒內補讀，同時保留原完成及退場時間。角色退場、重新載入頁面或 OFFICE 重啟後，已保存正文及歷史仍保留。

預設自動使用目前使用者的 Cursor 設定檔位置，macOS、Windows 及 Linux 各自按原生 App 資料目錄處理。使用自訂 Cursor profile 的安裝，只需設定一次整個安裝的來源位置，再重啟 OFFICE：

```sh
python3 office.py --config /absolute/path/to/config.json configure --cursor-public-db /absolute/path/to/Cursor/User/globalStorage/state.vscdb
```

接駁需要 Cursor 的明文結構化公開訊息資料及支援 JSON 的 SQLite；不讀取或解密帳戶憑證／加密內容。資料庫換檔會跳過該輪，再重新驗證身份。來源格式不支援、關係不明或正文尚未落盤時，不補造文字或完成狀態。此版每輪候選查詢上限 128 個子回合；原生完成後若結尾仍是工具／思考等非公開框架，交付正文保持未確認，公開對話仍按已驗證內容保存。其他 Cursor 版本及自訂環境需另行實測。

## Cloud Agent 背景派工路由

這個 adapter 使用 Cursor 官方 Cloud Agents API v1。使用者從 Desktop 的 Agents 視窗選一個 Cloud Agent，OFFICE 向同一個 `bc-…` Agent 發送 follow-up，保留它的對話與工作環境。沒有另開 CLI session，沒有操作或彈出 Desktop 視窗。

## 一次安裝，自己選擇 Agent

1. 在 OFFICE 按「＋ 邀請 Agent」，選「Cursor Desktop · Cloud Agent」。
2. 填顯示名稱、該 Agent 的 `https://cursor.com/agents/bc-…` 連結；首次填自己的 Cursor API key。Key 請直接填在本機 OFFICE，毋須傳給開發者或貼進聊天。
3. 按「接入這個 Cloud Agent」。OFFICE 只讀取指定 Agent 的資料，驗證 ID 及可用狀態，不掃描帳戶名單、不新增 Agent、不呼叫模型。
4. 點角色頭頂對話，輸入第一項指示。公開文字會同步到 OFFICE，最終回覆與紀錄保存在本機。

同一個 OFFICE 安裝會重用已保存的帳戶 key；再選其他 Agent 時可留空 key。每位成員仍由使用者明確選定，毋須編輯 Python 或 JSON，亦不用重啟服務。原 Agent 的平台登入、權限及計費仍適用。

## 支援邊界

本機聊天入場成功或 Hook 綁定成功，不等於可持續收取 OFFICE 指示。沒有原生背景入口時，OFFICE 顯示「查看收訊限制」及「只儲存留言」；重貼邀請 Prompt 或重按收訊圖示不能令原聊天自動喚醒。部分 Cursor 版本的程式內含 Desktop Bridge／`desktop send`，但仍受帳戶開放、使用者設定及執行期入口可用性限制；只有一般 CLI 或相關程式碼存在，不能當作已接通。未開放入口時，OFFICE 不會強改功能開關、繞過批准、轉用其他 session，亦不以等待工具冒充持續收訊。新的本機聊天會如實回報 `receiver_status=unsupported`，可繼續入場及公開同步。

| Desktop 執行環境 | OFFICE 支援 |
| --- | --- |
| Cloud Agent，具有 `bc-…` ID，帳戶允許官方 API | 指示、背景執行、串流公開回覆、最終交付及歷史 |
| Local／本機聊天，只有本機 conversation ID | 可設定上面的原生父子觀察 Hook；仍未有已驗證的原聊天背景指令入口 |
| 本機子 Agent | 已驗證原生身份、狀態、公開進度、交付正文及歷史；追加工作由父 Agent 處理 |
| Cloud 子 Agent，只允許父 Agent 操作 | 以官方 API 拒絕為準；應由使用者選父 Agent，不能以本機 Hook 驗證替代 Cloud 路由驗證 |

不會把本機聊天偷偷轉為 CLI 或新 Cloud Agent。API key 無效或權限不足會停在接入步驟。Desktop 登入成功不等於已建立 API key。

## 回覆與恢復

指令以 `POST /v1/agents/{id}/runs` 發送，不改模型、模式或 MCP 設定。原生 run ID 落盤後才開始讀取該 run 的 SSE。只保存公開 assistant 文字與狀態；thinking、原始工具參數、工具結果及重複的 interaction stream 不寫入 OFFICE。

重啟時只 GET 已記錄的原生 run，絕不重送 POST。SSE 不完整時讀取同一 run 的最終結果；仍未確定就保留未知狀態並擋住後續指令。Office 自己送出的同 Agent 工作會依序排隊；若平台正執行另一來源的工作而回傳 busy，此指令顯示未送達，不自動打斷或 steer。

每個已選定 Cloud Agent 的 key 分別保存在 `.office/platform-secrets/cursor-*.json`，權限 0600；舊安裝的 `cursor.json` 僅作相容讀取，不會覆寫其他 Agent 的 key。公開 API 回應、UI、可攜包及測試報告不返回 key。OFFICE 只建立對官方 API 的出站 HTTPS 連線，沒有開放公網接收埠。

驗證：本版已通過離線協議及 UI 測試，包括兩輪同 Agent、串流、去重、錯誤來源隔離、斷線與重啟恢復。曾在一個 Desktop App 內的 Cloud Agent 驗證 OFFICE 指示及回覆；App 直接發起工作後的完整自動同步、長時間閒置後收訊仍未完成驗證，因此不宣稱整體雙向功能已完整通過。

接口依據：[官方 Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints)。
