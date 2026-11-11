# Virtual Office

一個由 Python 管理的本機 3D Agent 辦公室。角色頭頂預設顯示細小姓名及狀態標籤；點選後才展開公開 Prompt、進度及交付，對話會隨角色移動。預設中文介面、空名單及本機儲存。

密集角色的標籤會自動避讓；同一時間只展開一個頭頂對話或子 Agent 名單。主 Agent 頭頂的「子 Agent N」可選擇全部子 Agent，完整及簡易模式均可使用。全文按需要展開及捲動，其他角色不會因新訊息自動彈出大型文字預覽。

## 安裝

需要 Python 3.9 或以上。Python 服務不用 pip 套件、Node.js、模型 API 或付費帳戶。已在 macOS 驗證；其他作業系統仍需實機驗證。前端需要網絡下載固定版本 3D 資源、Three.js 及字型。

將套件放在自己選擇的資料夾，在該資料夾執行：

```sh
python3 setup_ui.py
python3 office.py init
python3 office.py start
python3 office.py url
```

最後一行只在你的終端顯示私人入口。新安裝沒有 Agent，沒有自動掃描聊天，也不使用 ntfy。用 `python3 office.py stop` 停止此 OFFICE；沒有安裝自動啟動服務。

每個安裝的設定、私人入口、Agent 憑證、資料庫及日誌都在 `.office/`。移動專案後可透過 `--config` 指定設定檔；備份或搬移私人資料時由擁有人保管。不要把 `.office/` 推上 GitHub。


> 此版為通用預覽版。各平台的背景收訊與 App 公開對話同步能力不同，請先閱讀下方支援表；未設定或未驗證的入口不會自動標示已接通。


## 簡易模式

按右下角「簡易模式」，畫面會集中顯示 G/F 全部已入場的 Agent 及頭頂對話，收起統計、樓層、成員名單、活動及其他工具。3D 辦公室效果保留；點角色頭頂名稱可讀取紀錄、輸入指示。

右上角「完整模式」可恢復全部功能，再按「＋ 邀請 Agent」邀請新成員。完整模式每次開啟及從簡易模式切回時預設停在 G/F；辦公室未有 Agent 時亦顯示 G/F 大堂。瀏覽器會記住模式，下次開啟亦保留；切換模式不會改變 Agent 身份、權限或歷史。

## G/F 空間與角色管理

G/F 保持 45.5 × 41 米的大堂面積。中央休閒大堂使用原生 sofa、扶手椅、茶几、大電視及植物，座位按建築角度組成 L 字、方形等會客組，基礎提供 36 個獨立休息位；中央不放辦公桌。Pantry 茶水間以原生咖啡吧和高櫈組成。周邊 5 間辦公室分別有 8、6、4、6、8 個工作位，工作台沿建築軸線拼成一字、背對背或 L 字組合，保留入口及走道；暖金啡木地板配柔和玻璃、暖光及接觸陰影。日夜光影仍會變化，室內暖燈保持可讀。較多 Agent 入場時，額外休息區向外延伸。 G/F 後方另有獨立玻璃 CEO Room，配行政工作台、銀色 Apple 風格電腦、大電視及待客沙發；行政位可拖曳安排，並由同一套 Python 座位規則保存。電腦為 3D 場景物件。

Grok Bot 直接沿用原版 `grok-bot.glb`，按 Agent 身份套用橙、綠、藍、珊瑚紅、紫、金六組身體顏色；原有幾何、白色長眼及黑色短梗保持不變。只複製各角色身體材質，共享模型不會被改色。Codex、Cursor、Hermes 亦各有六組原有配色，按穩定 Agent 身份固定分配；改名、重新載入、切換樓層或工作狀態變化時保持配色。同平台可能重色。明確上報的自訂顏色或個人 GLB 會沿用；平台圖案胸章、狀態燈、眼睛動畫、頭頂對話、拖曳及座位安排照常運作。

直接按住角色拖到休息位或辦公室工作台，或在頭頂對話按「⇄ 移位」選擇座位。拖曳時綠色代表空位，紅色代表已有角色；放開後由 Python 保存，重新載入、重啟 OFFICE 或另一個分頁都使用同一份安排。一張 sofa 的不同座位可分別安排角色，每個位置同時只供一位 Agent 使用。搬位不會推走其他角色，也不會改變它的實際工作目錄或 Workspace 樓層；更新視覺設計保留原座位身份及歷史。

每位角色的頭頂對話都有「登出」，完整和簡易模式均可使用。確認後移除該 Agent 的 OFFICE 身份及接收入口、釋放座位、取消尚未交出的留言，保留已存交付歷史；原 App 聊天仍可使用。已送出而未結束或結果不明的指示，需先完成／核對才可登出。舊邀請及收訊憑證會失效；再次入場時請建立新邀請。

G/F 與原生樓層共用日間、黃昏及夜間光照參數，後方沿用原生窗框投影。保留辦公室／CEO 原有六盞局部暖燈，中央休息區補兩盞廣角暖燈，Pantry 補一盞；日間關閉，黃昏及夜晚按原生燈光強度漸變，換層時停用。暖色木地板、家具及座位保持原有設計；擴建地板使用實體光照接收陰影，避免將原版固定家具的烘焙陰影拉伸到新佈局。陰影解析度依樓層及裝置能力調整，補燈不增加陰影圖。


## 入場後持續收訊

入場及首次回報後，程式自動檢查原生指令入口：沿用已保存的路由，或嘗試平台支援的原 session 自動辨認。這不表示每個平台都已有背景入口；Grok Webhook、Hermes ACP 及 Cursor Cloud 仍須完成各自的一次性接駁。完成後，Agent 可以正常結束回合；下次直接在頭頂對話輸入指示，不用再叫它待命，也沒有一小時到期。現有 Agent 按「啟用持續收訊」可升級，保留身份、歷史及尚未收取的留言。

`join.py` 的 `joined=true` 與兩個 `accepted=true` 證明入場及首次回報。另看 `receiver_status`：`ready` 表示入口已設定且目前可用；`configured_unavailable` 表示路由仍保存、等待平台恢復；`setup_required` 表示尚欠原生接駁；`unsupported` 表示目前平台沒有可用的背景入口；`disabled` 表示擁有人停用派工；`rejoin_required` 表示身份已失效，需要由擁有人產生新邀請。`persistent=true` 只表示持久路由已設定，不能單憑它判斷平台目前可用。任何入場結果都不代替實際指令的接收、執行及交付驗證。

Python 持續保存及派送指令，沒有工作時不啟動模型。Codex App 的邀請會辨認目前原 session 並自動綁定；Grok Bot 依同一份指引建立自己的 Webhook 入口。平台入口尚未完成時顯示「持續接駁待完成」，可以先留言。OFFICE 及原平台需要可用；Codex 未載入的聊天仍可能等到手動開啟後才執行。只有收到相配回報才顯示完成。[使用方式及支援邊界](docs/INBOX.md)。

## 平台連接助手

進入 OFFICE 直接顯示辦公室，不會彈出平台設定。「＋ 邀請 Agent」由空白平台選項開始，由你決定使用哪個平台；舊邀請連結亦只提供「查看之前的邀請」，不會自動開啟。

Grok 入場後，同一個邀請視窗會帶你逐步完成最後連接；一次只看一步，按「下一步／上一步」前進。連接資料保持遮蓋，技術欄位收入預設收起的「進階設定」。已有入口可直接測試，毋須重新貼資料或另一段 Prompt。

按「連接並測試」後，只有同一 Bot 真正回傳相配測試的開始、回覆及正常完成，才顯示「已連接」。只保存入口或收到接口接收回應，仍顯示尚未確認收發。結果不明不自動重送；沒有工作時不啟動模型。詳見 [Grok 助手](docs/GROK-BOT.md)。

## 平台持續收訊支援

安裝包沒有綁定開發者電腦或 Agent 名單，各平台使用自己的原生入口：

| 平台 | 接駁方式 | 實際範圍 |
| --- | --- | --- |
| Codex App（macOS） | 邀請時辨認目前 session；檢查本機原生 queue 能力 | 同一聊天背景排隊；未載入的聊天仍需平台載入 |
| Hermes | `office.py connect-hermes` 建立固定 ACP session | 背景執行、公開回覆及上下文續接 |
| Cursor Desktop 的 Cloud Agent | 首次填自己的 API key，再選 Agent 連結 | 沿用同一 Agent，背景執行、公開串流回覆、原生 run 狀態及歷史 |
| Grok Bot | 邀請 Prompt、自己的 Webhook routine、首次本機回報 | 背景喚醒指定 Bot，公開進度與交付由 Bot 回傳 |
| Cursor Desktop 的本機聊天 | 既有 Hooks／主動上報 | 只同步；尚未有已驗證的原聊天背景指令入口 |

設定保存在每個使用者自己的 `.office/`，不用改 Python 或找開發者接駁；這兩條新流程也不用重啟 OFFICE。Grok Bot 需要能使用 OFFICE 所在電腦的本機執行工具，只有雲端 terminal 的 Bot 暫不能回傳。詳見 [Grok Bot](docs/GROK-BOT.md) 及 [Cursor](docs/CURSOR.md)。

新 adapter 已通過協議 fixture 與介面測試；沒有實際帳戶憑證或選定來源時，不宣稱完成平台端到端驗證。新安裝仍是空名單。OFFICE 本身不呼叫模型；經平台發出的任務沿用該平台的權限與費用。

## 公開同步範圍

收訊入口與 App 對話同步分開顯示；已驗證 OFFICE 指示收發，不等於已訂閱原平台全部對話。

| 來源 | 公開同步範圍 |
| --- | --- |
| Codex App | 已選定及綁定的原聊天，由原生觀察器讀取公開事件 |
| Cursor 本機聊天 | 只有完成身份綁定的原生 Hook，才描述為原聊天同步 |
| Hermes ACP／Cursor Cloud | 目前觀察 OFFICE 派發的工作，不能推論涵蓋其他 App 工作 |
| Grok Bot／一般主動回報 Agent | 合作上報；App 直接工作需 Agent 實際回報，沒有一般帳戶的原生全對話訂閱 |

同一份入場 Prompt 現在必須完成 Bot 自己的長期規則保存、讀回核對及設定回條；即使持續收訊入口已 ready，也不能跳過。每個 Bot 使用自己無 token 的 reporting manifest，連到原公開事件憑證，不能混用 inbox 憑證。普通對話、提問及工作均適用；公開輸入保留完整原文並遮蓋私人資料，不能自行摘要。設定回條標示 agent_reported_saved，與後續真實 App 工作驗證分開。

合作回報只保存公開任務、進度及交付；不讀取內部推理或工具原文。沒有來源事件時不猜測狀態。Grok 的新邀請會在同一份指引加入目前 Bot 的長期公開回報規則，仍不把記憶保存當作自動同步證據。

Cursor 本機聊天同一份入場 Prompt 亦必須接續完成原生 Hook 安裝及原聊天綁定，不能因 `receiver_status=unsupported` 就提早結束。`observation_status=setup_required` 或 `binding_required` 表示尚未完成；只有伺服器核對目前身份、世代及原生回覆標記後，才回傳 `configured`。這個公開同步結果與背景派訊息能力分開；`public_sync_onboarding_complete=true` 不代表原聊天可被 OFFICE 喚醒。一次綁定後，後續工作沿用共用 Python Hook，毋須每次提醒 Agent 上報。Python 同時補讀該原聊天已驗證回合的公開文字，主聊天與子 Agent 均按自己的原生回合隔離，不讀取思考、工具原文或未選聊天。

## 邀請 Agent（一般使用方式）

安裝及啟動一次後，日常接入都在 OFFICE 介面完成：

1. 開啟你的私人 OFFICE，按「＋ 邀請 Agent」。
2. 先選擇 Agent 所用的平台，再填寫名稱並產生接入 Prompt。
3. 按「複製 Prompt」，將這份「入場＋持續收訊 Prompt」貼給你想邀請的 Agent 一次，讓它使用同一部電腦的本機工具執行。

Python 會自動建立專用回報及收訊資料、填好目前安裝位置，並在 Agent 執行後即時登記。同一份 Prompt 會要求 Agent 接續完成首次回報、平台身份綁定、公開同步，再讀取 `conversation.guide_path` 完成平台支援的持續收訊設定，不需再貼第二段 Prompt。平台要求一次性批准時，批准後繼續同一份指引；若所選平台是 Grok 且需要擁有人完成最後連接，跟同一個邀請視窗的 Grok 連接助手逐步操作，毋須再貼另一段 Prompt。平台沒有背景入口時，仍會如實回報未接通，合併 Prompt 不能新增平台能力。`capability.persistent=false` 不代表入場失敗；接通原生入口後才可背景收訊。Cursor 本機聊天不為此試接其他平台或啟動等待工具。Grok 如只能由人類讀取 Webhook 資料，會清楚指出尚欠的步驟。登出後需重新產生邀請，不能重用舊憑證。

畫面會依次顯示「等待 Agent 執行接入 Prompt」、「已登記，等待 Agent 首次回報」及「已收到 Agent 回報」；原生入口完成後再顯示「已入場 · 持續收訊已接通」。登記、回報、接駁及實際任務完成是不同階段。未使用的邀請有效 24 小時，可以取消；如果關閉了視窗，再用相同名稱和平台產生邀請可找回尚未使用的 Prompt。

此流程支援有本機執行能力的 Grok Bot、Cursor、Codex App、Crouton 及其他 Agent，使用通用主動回報協議。它不會自動讀取所有平台的全部聊天，也不會讓休息中的模型長駐。雲端 Agent 的 localhost 並不是你的電腦；沒有本機執行能力時應回報未接入。

邀請、憑證及接入指引都存於私人資料目錄，不會包含在發佈包。搬移安裝位置後請重新產生尚未使用的邀請。

## Hermes 原生雙向接駁

已設定好 Hermes 的使用者可以接入一個固定原生 session：

```sh
python3 office.py connect-hermes --name "My Hermes" --workspace /absolute/path/to/project
```

重新啟動 OFFICE 後，點角色頭頂對話即可傳送指示。Hermes 在背景執行、同步公開回覆並保留上下文；OFFICE 不會開啟 App 或終端視窗。所用模型與供應商沿用你的 Hermes 設定，通用套件沒有指定模型路由。支援原生批准要求的「允許今次／拒絕」。詳見 [Hermes 指引](docs/HERMES.md)。

這項接駁使用 Hermes ACP session，與上面的通用上報邀請分開。已在 macOS 的一個真實 Hermes session 驗證兩輪工具執行，以及重啟後續接；其他平台及模型組合仍需各自驗證。

## 主／子 Agent 同場顯示

已入場的 Codex App 主 Agent 呼叫子 Agent 時，Python 會從同一個本機 Codex 資料來源驗證父子關係，將本次工作中的子 Agent 顯示在主 Agent 附近。子 Agent 不用逐一邀請、不會變成永久成員，亦不會佔用已保存的座位。它們沿用 Codex 角色外觀，各自顯示工作狀態、進度及交付；平台提供明文的委派任務會先遮蓋再顯示。若 Codex 只保存加密任務正文，對話會明確標示原平台未提供可讀任務內容。

子 Agent 完成後短暫顯示結果再退場，工作紀錄仍可從交付列表查看。Python 只接納已選主 Agent、本次綁定及相配工作的子 Agent 紀錄；來源不明或停止更新時不會推斷完成。主 Agent 登出或改綁來源後，舊觀察憑證失效。

| 平台 | 父子資料來源 | 驗證範圍 |
| --- | --- | --- |
| Codex App | Python 讀取原生父子元資料及公開紀錄 | 已完成原 App 實測 |
| Cursor Desktop | 原生 Hooks 及共用 Python 結構化公開訊息讀取器 | 3.29.9 原 App 已驗證子 Agent 身份、狀態、公開進度、交付正文及歷史；毋須逐隻設定 |
| Grok Bot | 父 Bot 使用真實 Task ID，按接入指引回報生命週期與公開文字 | 已在同一部 Mac 的兩個 Bot 驗證三位 Task 子 Agent、平行工作及交付；屬合作回報 |

Cursor 一次安裝及綁定父聊天後，共用 Python 會自動尋找後續子 Agent，只同步該 Task 範圍內的公開訊息，並保存完成後正文；不使用混合思考內容的文字匯出。Grok Bot 現階段依靠父 Agent 合作執行回報指引，畫面明示「Agent 合作回報」，沒有獨立訂閱雲端子 Agent 活動。Hermes 子 Agent 觀察器仍未實作。詳見 [父子 Agent 機制](docs/SUBAGENTS.md)。

## 同一 Workspace 同層

**G/F 全員工作總覽**固定在最底層，集中顯示所有已入 OFFICE 的 Agent。大堂面積至少約一般樓層 3 倍，座位隨成員數增加，每位角色顯示名稱及即時狀態；點角色可看頭頂對話和交付紀錄。尚未指定 Workspace 的 Agent 同樣可以在 G/F 登入、工作及待命。

G/F 是同一批成員的總覽，不會複製 Agent、重新登入或重複計算人數。各 Workspace 樓層繼續保留，與 G/F 共用同一份狀態、Prompt 及歷史。新 Agent 完成原本接入流程後，自動加入總覽，無須額外設定。大堂沒有人時仍可開啟；較多成員時可縮放、拖動及從成員名單選擇角色。

樓層按專案工作目錄分組，同一個目錄的 Codex、Cursor、Grok Bot 和 Crouton 可坐在一起。Workspace 使用規範化絕對路徑識別；同名但不同位置的資料夾會分開，符號連結指向同一目錄則合併。

邀請時預設「跟隨 Agent 工作目錄」，亦可選已知 Workspace 或新增專案資料夾。現有角色點「設定 Workspace」即可搬層，不需重啟或重新接入；手動指定會固定樓層，選回「跟隨 Agent 工作目錄」可恢復自動。角色身份、狀態與歷史不變。每層最多 20 個座位；超出時建立同一 Workspace 的續層。

原生 Codex 讀取已選聊天的 `cwd`，約每 30 秒更新。原生 Crouton 使用已驗證回合的 `job.workspace`；缺少時只比對已知專案內的回合路徑，休息時保留最後已知 Workspace。通用接入及 `emit.py` 在開始事件回報目前工作目錄；在子資料夾執行時可用 `--workspace /absolute/project/root` 明確指定專案根目錄。Cursor Hook 使用單一 `workspace_roots`，多根目錄時由擁有人選擇。未提供資料的 Agent 在 G/F 顯示，Workspace 設定仍列為未指定，不猜測所屬專案。

完整目錄只保存在本機私人資料庫及受保護的 Workspace 設定 API；不會發送到可選的 ntfy 通道。這是畫面分組，不會更改 Agent 的實際工作目錄、檔案權限或任務。

## 進階：原生觀察器與手動接入（可選）

以下設定供需要原生 Codex 觀察、Cursor Hooks 或自訂執行器的人使用；一般使用者可直接使用上面的邀請流程。

Codex App：只加入你指定的聊天 ID。

```sh
python3 office.py enroll --provider codex --source YOUR_CHAT_ID --name "My Codex"
```

預設讀取目前使用者的 Codex 資料庫；不同版本或位置可指定：

```sh
python3 office.py configure --codex-db /path/to/state_5.sqlite
```

Cursor：新接入建議使用邀請內的父子 Agent 設定，詳見 [Cursor 接駁](docs/CURSOR.md)。以下為較早的主 Agent 觀察方式，使用 [官方 Hooks](https://cursor.com/docs/hooks) 的 `conversation_id` 選定一個聊天；它只同步主 Agent，不含自動子 Agent 發現。Hooks 只取 Prompt、可見回覆與停止事件，不讀取 thinking 或工具資料。`afterAgentResponse` 是完整訊息事件，並非逐字輸出。

```sh
python3 office.py enroll --provider cursor --source YOUR_CONVERSATION_ID --name "My Cursor" --credential-out .office/cursor.json
python3 office.py cursor-hooks --credential .office/cursor.json --output /path/to/your/project/.cursor/hooks.json
```

命令會保留該檔原有 Hooks，只追加此觀察器；未入選的 conversation_id 會被忽略。接入失敗只在 Hook stderr 留下錯誤種類，會讓原本工作繼續；不會發新 Prompt 或自動重試。記下生成的 hook 命令，搬移安裝資料夾後重新生成，並移除舊路徑的同一觀察器命令。

Grok Bot 或其他框架：先選定固定來源 ID，再使用自己的上報憑證。

```sh
python3 office.py enroll --provider grok_bot --source my-grok --name "My Grok Bot" --credential-out .office/grok.json
```

參閱 [Grok Bot 接入指引](docs/GROK-BOT.md) 和 [通用事件協議](docs/EVENTS.md)。Grok Bot 目前是明確上報式接入，並非自動擷取原生 App 全部對話。其雲端電腦與本機分開；應使用平台提供的本機執行能力，或在 OFFICE 所在機器運行接駁器。[平台說明](https://cursor.com/docs/grok-bot/work)

Crouton：將目前電腦的 Crouton runtime 路徑設為本機設定，再選路由 ID：

```sh
python3 office.py configure --crouton-root /path/to/crouton-runtime --crouton-workspace-root /path/to/allowed/workspaces
python3 office.py enroll --provider crouton --source crouton-01 --name "Crouton 01"
```

原生 Crouton 觀察器目前取得執行狀態、任務及最後回覆；中途訊息需由執行器接通用事件介面。角色休息不代表模型常駐或能自動接收工作。

改完名單或來源設定後，執行 `stop`，待舊程序停止，再 `start`。沒有指派新的 Cursor 或 Grok Bot 身份就不會自動加入。

## 同步範圍

- Python 約每秒讀取選定來源；畫面及展開的對話約每 2 秒更新。來源本身寫出訊息的時間仍會影響延遲。
- 即時訊息是對使用者可見的文字。來源沒有提供的進度不會補寫；不儲存內部推理或工具輸出。
- 常見憑證、私人入口、電郵及使用者路徑會先遮蓋再保存，但遮蓋並非所有私人資料的完整分類器。
- 本機 SQLite 保存訊息、交付與事件收據；重啟不會重複插入同一訊息，過期回合不能把新工作改成完成。
- 每個外部 Agent 只持有自己的上報憑證，不能用它讀取 OFFICE 對話。私人儀表板只接受本機存取；這版沒有雲端共享服務或公網入口。

## 驗證

```sh
python3 -m unittest discover -p 'test_*.py'
node --test test_*.mjs
```

Node 只用於開發測試，啟動 OFFICE 不需要。

## 原始 3D 前端

[原作者專案](https://github.com/Bodila51/grokbot-wallpaper) 固定於 `76232f7af1a94d3cbd3d0b1cf05e460c85637acf`。此套件使用 manifest、校驗值和局部修改描述，安裝時才從原來源下載 UI；未附送你的私人設定或對話。`public/` 為安裝產物，已排除版本控制。

該固定版本未找到根目錄授權檔，因此本套件不把整套原版 3D 程式與素材重新宣告為 MIT 或其他授權。公開發布含原版程式、衍生修改或素材前，仍需核實原作者授權範圍。這次沒有建立公開 GitHub 儲存庫。

## OFFICE 指令輸入

已接通 macOS Codex App 原聊天：點 Agent 頭頂對話即可傳送文字，由 Python 排隊、去重並配對實際回覆。新邀請會在指定的原聊天完成綁定；不會自動選入其他聊天。詳見 [指令接駁及 Hermes／OpenCode 擴充方式](docs/COMMANDS.md)。Hermes ACP 已實作；OpenCode 原生背景 adapter 尚未實作。
