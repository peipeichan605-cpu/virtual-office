# Grok Bot 接入

一般接入會提供[持續收訊指引](INBOX.md)。Bot 完成首次回報後建立自己的原生 Webhook 入口，正常結束回合；之後 OFFICE 的指示可喚醒指定 Bot，毋須重新待命或受一小時限制。入口尚未完成時只保存留言。

## 使用連接助手

日常接駁不用理解 Webhook、URL 或金鑰。這套助手適用於每個安裝、每位擁有人自己選定的 Grok Bot；不會替你掃描或加入其他 Bot。

1. 在 OFFICE 按「＋ 邀請 Agent」，主動選擇 Grok Bot，再把邀請 Prompt 貼給你選定的 Bot 一次。首頁及其他平台不會顯示 Grok 接駁。
2. Bot 完成入場後會回覆「OFFICE 連接卡」，列出實際流程名稱及可用的原生開啟入口。需要新建時，名稱使用「OFFICE 收訊 · Agent 名稱 · 識別碼」；已有有效入口則沿用原流程，不因改名重建。連接卡若讀得到不含秘密的實際收訊地址（Webhook URL），會直接列出；讀不到時清楚說明。Grok 的列表只顯示流程名稱，打開該項後才看到 Webhook／POST 到 欄位。
3. 在同一個 OFFICE 邀請視窗跟「Grok 連接助手」操作：貼上收訊地址，再從同一流程複製連接金鑰貼回 OFFICE。金鑰不貼聊天；不能讀取剪貼簿時，可手動貼到同一欄。
4. 按「連接並測試」。按鈕會明確說明將發送一則簡短測試；之前待處理的留言亦可能開始交付。有工作正在處理時，完成後再按「測試連接」。
5. 助手依次顯示「測試已建立」、「Bot 已收到」、「收到相配回覆」。同一個 Bot 的正常完成事件也已核對後，才會顯示「已連接」。設定已儲存、接口接受請求或只收到開始事件都不算成功。

已有入口的 Bot 直接進入測試步驟，不需要再複製資料。「進階設定」預設收起，只供手動核對。連接金鑰只經本機 OFFICE 保存，不放在網址、聊天或瀏覽器儲存空間。Bot 可按擁有人要求在原聊天列出實際收訊地址，但不能包含 key、Authorization 或認證參數。更換 Agent 或關閉助手會清除未保存的輸入。

關閉或重新開啟畫面不會自動再發測試；結果不明時只核對同一個測試編號。平台暫時不可用時，保留已驗證的歷史並顯示等待平台，不冒充目前已連接。更換入口、停用派工或登出後，舊測試不能用來顯示新連接成功。

助手沒有繞過 Grok 的批准或本人操作限制；如平台只讓擁有人取得連接資料，仍由擁有人跟畫面完成。以下章節是進階協議資料，一般使用者只需使用助手。

## App 直接工作與 OFFICE 派工是兩個方向

「已連接」驗證的是 OFFICE 發出指示後，Bot 收到並回覆。它不代表 OFFICE 已訂閱 Grok App 的所有對話。

一般帳戶目前使用 **Agent 合作上報**：新邀請的同一份指引會要求已入選 Bot 記住自己的公開回報規則，在 App 直接收到工作時也回報任務、公開進度和交付。這仍由 Bot 執行；記憶或技能保存成功不是 Python 原生觀察器，亦不能當作每次工作都已同步的證據。已有 Bot 沿用自己的身份及憑證，讀取更新後的收訊指引；不重新加入、不改其他 Bot。

只有 Agent 真正送出的公開事件會出現在 OFFICE。未收到事件時，OFFICE 不猜測工作或交付。Bot 的並行聊天缺少獨立原生事件範圍，不能宣稱完整覆蓋；有已回報的 App 工作進行時，新 OFFICE 指示及測試會等待它真正結束，不覆蓋原工作。

官方支援以聊天記住 Bot 規則及保存可重用技能，但未把它們定義為每回合必定執行的本機 Hook。[Bot 規則與記憶](https://docs.x.ai/grok-bot/bots#edit-a-bot)、[技能與 routine](https://docs.x.ai/grok-bot/skills-routines-and-automations)。企業版另有須管理員開啟的公開對話事件匯出；本機通用版未安裝或啟用該服務。[官方 OpenTelemetry 匯出](https://cursor.com/docs/enterprise/opentelemetry-export#conversation-content)

### 入場時完成持續合作回報

通用邀請包含同一個 3b 步驟：讀取 `conversation.reporting.guide_path`／`manifest_path`，為目前 Bot 保存原生長期規則並讀回身份、版本與路徑，再執行指引提供的 `reporting_setup.py confirm`。`receiver_status=ready` 只代表收訊入口可用，不能略過公開同步設定。舊版已入場 Bot 可由相同邀請取得新版指引，保留原身份、憑證及 routine。

規則涵蓋普通對話、提問及工作。每次先送出完整公開用戶原文並收到開始回報 `accepted=true`，再處理工作；進度及交付使用真正已公開的完整文字，不事後批次補報、不摘要。秘密與私人資料仍按原遮蓋規則處理。長期記憶只保存這個 Bot 的規則與 manifest 路徑，沒有 token、Webhook key 或其他 Bot 的設定。

`agent_confirmed`／`agent_reported_saved` 是合作設定回條，並非平台記憶的獨立匯出或原生監聽證據。缺失、舊版本、撤銷或不支援的設定不會回覆完整入場設定已完成；收訊能力仍獨立保留。設定後應直接在 App 發出不提 OFFICE 的普通工作，逐輪核對工作中狀態、公開對話、交付及歷史。

## 背景接收 OFFICE 指示

本版加入官方 Webhook routine adapter。整套流程適用於每個人的 OFFICE 安裝，沒有固定開發者帳戶、Bot 名稱、路徑或名單：

1. 在 OFFICE 按「＋ 邀請 Agent」，選 Grok Bot，填名稱並產生 Prompt。
2. 把 Prompt 給你選定的 Bot。它按本機指引完成首次回報，需要新入口時再依本邀請的完整名稱建立自己的 Webhook routine；已有已核對入口則沿用，不建立定時輪詢。
3. Bot 依 `conversation.guide_path`，用自己的憑證及 `connect.py grok --details-stdin` 將 **POST to** 和 **key** 直接交回本機 OFFICE。若平台只向人類顯示這兩項資料，這一步仍須擁有人完成一次：在你主動開啟的 Grok 邀請視窗內，跟連接助手逐步帶回兩項資料，再按「連接並測試」。技術欄位預設收起；關閉後可從指定 Grok Agent 的連接按鈕繼續，舊邀請連結不會在首頁自動彈窗。單憑首次回報不代表 routine 已建立；未完成前可繼續等待 Bot 的實際結果。Key 不貼聊天、不放 shell 參數；既有 Agent 的頭頂入口只供補接。
4. 在 OFFICE 傳送一項簡短指示。Webhook 接收、Bot 開始、公開進度及完成回報分別顯示；交付保存在本機歷史。

合併 Prompt 能省去第二次貼指引，不能代替平台只向擁有人提供的秘密交接。首次回報未成功時不能啟用背景指示。保存入口不會執行任務，也不會重啟 OFFICE。完成後仍要以第一項實際工作驗證整條回路。

### 原理與邊界

OFFICE 將 JSON `{office:{version:1,command_id,agent_id},prompt:{text}}` 以 `Authorization: Bearer <key>` POST 到使用者指定的官方 routine URL。HTTP 200 只代表已接收；這不是 Grok.com 的簽名 automation API，也不是原生 chat API。

routine 固定使用接入時的 `emit.py` 與自己的憑證檔：以 `office.command_id` 作 run ID，將 `prompt.text` 原文送 `turn_started`（包括 OFFICE 標記），用 `command_id + ':start'` 作固定開始事件 ID。只有非重複且已接受的開始回報才執行任務；重複喚醒或回報失敗時停止該次處理，交由人類核對，不重做未知結果。公開進度用 `message`，交付用 `turn_completed`，失敗用 `turn_failed`；各事件保持唯一 ID。最後同時回覆 Bot 原聊天及 OFFICE。

Bot 必須能使用 OFFICE 所在機器的本機執行工具。雲端 Bot 的 localhost 不等於使用者電腦；只有雲端執行能力時，本版無法回傳到本機 OFFICE。這種情況不會顯示成已完成雙向接駁，也不自動開設公網隧道。routine 沿用指定 Bot，但本版不宣稱能鎖定某個原生聊天 turn/session；能力回傳 `same_session:false`。

接駁只儲存 Bot 主動上報的公開文字，沒有擷取原生全部對話。無任務時不向模型發出請求。未知送達結果不自動重送；真實、匹配同指令的回報可以在 OFFICE 重啟後補回結果。

本版已用隔離 fixture 驗證 Webhook 收據、回報配對、重複／錯誤回報、重啟、歷史及憑證隔離。亦已在 macOS 的一個真實 Bot 完成兩輪 Webhook 喚醒、公開交付及 OFFICE 歷史保存，第二輪在 OFFICE 重啟後完成，兩輪之間沒有等待工具。首次人工複製入口資料後，日常收訊不需再次設定；其他機器與帳戶仍需自行驗證。

接口依據：[Grok Bot 官方 routines / Webhooks](https://docs.x.ai/grok-bot/skills-routines-and-automations#webhooks)。

## 原生 Task 子 Agent（合作回報）

Grok Bot 的真正 Task 子 Agent 可以使用同一套 OFFICE 父子資料、3D 顯示及歷史。這條路徑是 **Agent 合作回報（`parent_reported`）**：父 Bot 使用原生 `Task`／`CheckSubagent` 後，將真正的原生 child ID、已確認狀態及公開結果交給本機 Python。它不是 Python 獨立訂閱 Grok Bot 雲端事件，也不會把另一個長駐 Bot 當成子 Agent。

一般使用者沿用新邀請產生的接入指引，當中會提供 `children.py` 和父 Agent 的憑證路徑。已有 Bot 若使用舊指引，可由擁有人把本節規則補到該原 Bot 聊天，沿用同一身份及憑證；這不會自動修改已在運行的 Bot。父憑證只交給本機上報程式讀取，不在聊天貼出，也不傳給子 Agent。

### 回報順序

1. 父 Bot 先用原有 `emit.py` 回報本次 `turn_started`，保留已接受的父 `run_id`。
2. 真正 Task 建立子 Agent 後，保留平台回傳的原生 child ID，為這次子工作指定一個新的 `child_run_id`，用 `children.py start` 回報。
3. 對使用者可見的進度用 `message`。有原生完成結果才用 `complete` 並附公開交付；失敗／中止分別用 `fail`／`cancel`。父任務完成不能代替子任務完成。
4. 同一個子工作保持原生 ID、`parent_run_id` 及 `child_run_id` 一致；每則事件有固定唯一 `event_id`。若重送同一事件，全部欄位及文字保持不變，不重新執行任務。

使用接入指引中的實際 Python、程式及憑證路徑，以 argv 陣列呼叫。以下只是欄位結構示例；文字透過 `subprocess.run(..., input=public_text, text=True)` 傳入 stdin，不拼接 shell 命令：

```json
[
  "python3", "/absolute/office/adapters/children.py",
  "--credential", "/absolute/parent-credential.json",
  "start", "--child-id", "NATIVE_TASK_ID",
  "--parent-run", "PARENT_RUN_UUID", "--run", "CHILD_RUN_UUID",
  "--event-id", "CHILD_START_ID", "--name", "資料核對", "--text-stdin"
]
```

將 action 換成 `message`、`complete`、`fail` 或 `cancel`，並使用該事件的 ID。`message` 預設是 assistant 公開進度；委派任務文字可用 `--role user`，公開最終訊息可用 `--phase final`，但最終訊息本身不會代替 `complete` 終態。巢狀子 Agent 加 `--parent-id`，填直接父子 Agent 的原生 ID；直接父須已回報開始且仍在工作。

### 證據及限制

- `accepted=true` 只代表 OFFICE 接收該回報，並附上 `evidence=parent_reported`；它不是平台提供的獨立原生收據。
- 不把整份 `ReadTranscript`、工具參數／結果、內部推理或其他聊天內容送入 OFFICE。只選出對使用者公開的任務、進度及交付，再經遮蓋流程保存。沒有可讀公開內容時應如實說明，不能猜測。
- 必須能使用 OFFICE 所在電腦的本機執行工具。雲端的 localhost 不能直接回傳到另一部 Mac 的 OFFICE；這個功能不建立公網隧道。
- 本機 CLI／協議測試不等於原生 App 端到端驗證。已在 macOS Grok Bot 0.78.5 的兩個實際 Bot 驗證三位 Task 子 Agent：一輪由原 App 下指示，一輪經已綁定的 OFFICE Webhook 派工並同時開兩位子 Agent。實際接收、公開進度及三份交付已讀回；子 Agent 完成時父任務仍工作，約三十秒退場後歷史保留，父指令亦由自己的完成事件正常結束。這只驗證合作回報路徑，不宣稱能獨立、自動觀察 Grok Bot 的所有子 Agent；其他機器、版本及帳戶需自行驗證。
- 角色退場及唯讀規則與其他來源一致；詳見 [通用主／子 Agent 機制](SUBAGENTS.md)。

官方文件把 Grok Bot 的本機執行與雲端工作分開；Enterprise 的 Action Recording／OpenTelemetry 亦是另一項接駁，沒有在此安裝流程啟用。[本機執行](https://docs.x.ai/grok-bot/security#local-execution)、[官方事件匯出](https://cursor.com/docs/enterprise/opentelemetry-export)。

## 只同步狀態與公開訊息

這是通用上報方式，適用於能在 OFFICE 所在電腦執行 Python 命令的 Grok Bot。它不要求使用者提供 Grok 登入憑證，也不會遍歷帳戶的 Bot。

一般使用者：在 OFFICE 按「＋ 邀請 Agent」，選 Grok Bot、填名字，再把自動產生的 Prompt 貼給它。身份、程式路徑及專用接入資料都會自動準備；它實際回報後，邀請視窗會顯示「已收到 Agent 回報」。不需要先由開發者登記或重啟服務。

以下是供自訂執行器使用的進階手動方式：由擁有人用 `office.py enroll --provider grok_bot ...` 選定一位 Bot。將程式位置及該 Bot 的憑證檔位置填入下列指引；憑證內容不要放進聊天。

> 你已被選入我的 Virtual Office。請使用本機執行能力，在 OFFICE 所在電腦呼叫 `adapters/emit.py`，只上報你自己的公開任務、對使用者可見的進度及最終回覆。使用我指定的本機憑證檔路徑，不要讀出或貼出檔案內容。
>
> 每次工作產生一個新的 UUID 作 run ID。開始時送 `turn_started`；公開進度送 `message --phase commentary`；完成時送 `turn_completed` 並附最終交付；失敗或中止時送相應終態。每次訊息使用獨立事件 ID。透過 stdin 傳入文字，不拼接成 shell 指令。不要上報內部推理、工具參數、工具原文或憑證。
>
> 命令無法執行或上報失敗時，告知使用者 OFFICE 同步未完成，繼續原本授權工作，不要自動重試、安裝額外程式、改網絡設定或把雲端的 localhost 當成本機。

在 OFFICE 所在電腦執行的形式：

```sh
python3 adapters/emit.py --credential .office/grok.json turn_started --run RUN_UUID --event-id START_EVENT_UUID --text-stdin
python3 adapters/emit.py --credential .office/grok.json message --run RUN_UUID --event-id UPDATE_EVENT_UUID --phase commentary --text-stdin
python3 adapters/emit.py --credential .office/grok.json turn_completed --run RUN_UUID --event-id END_EVENT_UUID --text-stdin
```

文字由執行器安全地傳入 stdin。這些命令不會幫 Agent 生成回覆；只傳它已對使用者發出的內容。Grok Bot 的雲端執行環境不能直接到達另一台電腦的 loopback；尚未提供公網接入方案。平台的本機執行權限仍由擁有人管理。

只同步流程不會自行取得背景接收指示能力；需要完成上面的 routine 接駁。
