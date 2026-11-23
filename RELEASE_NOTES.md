# 0.2.2-preview

這版加入 OpenRouter 免費文字 Agent、Windows 安裝與啟動修正，以及手機私人接入助手。從 GitHub 首頁即可找到 Agent 的安裝步驟。 [Agent 安裝指南](AGENTS.md) · [更新已有 OFFICE](docs/UPGRADING.md)

- 在「＋ 邀請 Agent」選 OpenRouter，可從當時有免費端點的文字模型建立對話。每次發送會再核對指定型號及零定價。金鑰由擁有人在自己的 OFFICE 設定。
- 原生 Windows 已驗證原位更新、127 個 UI 檔案、三個平台執行環境及 Chrome 3D 大堂。19 項 Windows 測試全部通過，原有 Agent、對話及手機授權均已核對。
- macOS 瀏覽器以模擬供應商完成兩輪對話並讀回交付紀錄。Windows 及 macOS 已從 OpenRouter 讀取免費模型清單。此次未使用真實 API key 發送推理請求。[驗證紀錄](VERIFICATION.json)

# 0.2.2-rc.1

這是通用版的發佈候選版本。下載來源以封裝的版本及 Git commit 辨認。

- 全新安裝以 UTF-8 讀取介面清單。原作者素材來源暫時無法下載時，會從保留同一個 commit 的副本取回檔案，按原清單逐檔核對。下載中途斷線時，安裝器會重試受影響的檔案。
- 桌面與手機都從本機 OFFICE 載入同版本的 Three.js。完成安裝後，打開 3D 大堂毋須再連外部 JavaScript CDN。
- Windows 的 Agent 邀請、公開回報與 Cursor 子 Agent 觀察器使用原生檔案權限、檔案鎖及 PowerShell 命令。背景工作狀態以唯讀方式查詢程序。Agent 可以透過舊語系的命令列讀取接入結果。
- 升級舊版 Windows 上的 Grok 入場指引時，會沿用原檔的字元編碼追加內容，保留使用者原文。同時編輯指引時仍會提示版本衝突。
- macOS／Linux 的選用私人手機入口在結束程序時，會等候系統確認程序已退出，再回報已停止。系統暫時查不到程序名稱時，也能繼續完成這項確認。
- 手機配對支援同帳戶下獨立裝置、從舊書籤返回大堂與對話紀錄，以及載入中斷後的復原。既有登入、對話及權限的更新流程見 [更新指南](docs/UPGRADING.md)。

# 0.2.1-preview

發布日期：2026-11-18。

標準安裝現在先準備 Codex、Cursor SDK、Grok SDK 三個執行環境，再交付 OFFICE。`setup-platforms` 會安裝本 OFFICE 專用的固定 Cursor SDK，檢查 Codex 原生執行程式及 Grok 的 Node.js；任何一項未備妥便回報失敗及下一步。帳戶登入、Agent 選擇及實際對話驗證保持分開，新安裝仍是空名單。

- 修正 Codex 狀態檢查超過 5 秒便被介面中斷的問題；整個原生探測有共用期限，介面等待時間與其配合。
- Windows 使用相容的原生 `codex.exe`，避免把 `.cmd`／`.bat` 當成原生背景入口；程序以隱藏視窗啟動，清理只針對本次建立的程序。
- Cursor 顯示具體 SDK 缺失、版本或 Python 相容原因。標準安裝處理固定 SDK 及專用環境；已有 Cursor Desktop 不代表已有 SDK。
- 修正 Windows 原生路徑、靜態頁面、設定檔 UTF-8 及私人檔案權限檢查；使用 Windows ACL，保留對其他帳戶授權的拒絕規則，不自行修改已有資料夾權限。
- Windows 啟動回條提供可直接使用的 PowerShell 停止指令及精確 argv；服務紀錄使用對應平台的指定 PID 停止方式。
- 加入跨平台換行規則與 Windows／macOS／Linux 自動驗證。既有 3D 場景、光影、效能設定、手機與子 Agent 功能保留。

Windows 隔離新安裝已實際完成三平台 runtime 準備、126 個 UI 檔案校驗、空白 OFFICE 啟動、HTTP 狀態檢查及停止。Codex 原生入口回報可用及已登入；Cursor SDK 1.3.1 可用，該測試安裝未配置 Cursor API key，因此沒有宣稱完成新帳戶對話收發。測試數量、版本與限制見 `VERIFICATION.json`。GitHub 自動測試不代替每個帳戶的真實收發或 3D 畫面檢查。

更新請依 [更新指南](docs/UPGRADING.md) 保留自己的設定及私人資料，再依 [安裝指引](README.md#安裝) 補齊執行環境。不要用其他電腦的憑證或設定覆蓋本機安裝。

---

# 0.2.0-preview

發布日期：2026-11-18。

本次通用預覽版加入手機私人接入、更多平台對話模式及共用對話介面更新。每個安裝使用自己的設定、帳戶與 Agent 名單；新安裝仍是空辦公室，由使用者選擇接入哪些 Agents。

## 手機首次連接與日常登入

- 「手機接入」新增首次連接助手，逐步說明下載官方 Tailscale、同帳戶登入、VPN 批准、確認 Connected、選擇 Agent 權限，以及用手機相機掃一次性 QR 碼。
- 手機保留 3D 辦公室、樓層切換、公開對話、交付紀錄及獲授權 Agent 的發訊息功能。查看與發訊息權限獨立，可選固定名單，或明確選擇跟隨所有已登記 Agents。
- 可自行啟用「記住手機」，最多 30 日，連續 7 日未使用失效；有效期內自動續期短期登入。舊登入不會自動延長，登出及撤銷亦不會自動恢復。
- 可使用獨立 tsnet 私人入口；手機配對與登入仍使用同一套 OFFICE 權限。需要完成各自的 Tailscale／HTTPS 設定，OFFICE 主機須保持開機、連網及服務運行。
- 日常開啟已配對的普通 OFFICE 書籤即可，不需 ADB、USB 線或另一部 Windows 電腦。見 [手機首次連接與日常使用](docs/MOBILE-QUICKSTART.md)。

## 平台收發與同步

- **Grok Bot SDK · 實驗模式**：登入後選定已有 Bot 及原生聊天，同步 OFFICE 與該 App 聊天的公開輸入、回覆、工作狀態和交付。沿用固定版本的非官方開源 SDK，原有 Webhook 方法繼續保留。見 [Grok SDK](docs/GROK-SDK.md)。
- **Codex · 背景模式**：建立 OFFICE 管理的獨立對話，續接同一原生 thread；支援公開進度、交付及下一輪模型設定。macOS 可使用相容的 Codex Desktop 隨附執行程式，亦可指定相容 CLI。這個模式不接管已有 App 聊天，也不保證出現在 App 側欄。見 [Codex 背景模式](docs/CODEX-BACKGROUND.md)。
- **Cursor Cloud**：沿用選定 Cloud Agent，將 OFFICE 發訊與直接在 Cursor App 發起的新工作同步回同一份公開對話、狀態及交付紀錄。加入持久化去重、恢復與缺漏提示。見 [Cursor 接駁](docs/CURSOR.md)。
- **Cursor SDK · 純對話**：可選的 OFFICE 管理對話，保留同一 session 與上下文；沒有檔案或 Shell 工具權限。與 Cursor Desktop 本機聊天及 Cloud Agent 分開。見 [Cursor SDK](docs/CURSOR-SDK.md)。
- 「＋ 邀請 Agent」與樓層「＋ 新對話」共用平台選擇入口，不預設平台、不自動匯入帳戶中的全部 Agents。Codex App 原聊天、Hermes ACP、Cursor Hooks 及 Grok Webhook 的既有接駁保留。

## 3D 與對話操作

- 保留 G/F 大堂、Workspace 樓層、休息區、Pantry、CEO Room、平台角色配色及座位拖曳；沿用現有繪製最佳化、畫面設定與閒置節能。
- 所有平台共用可拖曳、縮放的對話框，可在「浮動」與「回到頭頂」間切換。移動時保留草稿及閱讀位置；關閉再開啟不再跳回最頂。
- 修正右上角控制項重疊、手機對話關閉點擊與樓層入口；閱讀對話留在原 OFFICE 畫面，移除需要跳頁的「開啟對話／記錄」。
- 保留已支援平台的父子 Agent 顯示；子 Agent 對話維持唯讀。各平台及接駁模式的子 Agent 能力見 [父子 Agent 機制](docs/SUBAGENTS.md)。

可調整大小的對話介面與 Cursor 整合參考 [dominicliu69/virtual-office](https://github.com/dominicliu69/virtual-office/tree/0753acc8591e48cfaf3199a6f1b2cabbdc1bbb07)；上游 3D 資源與其他來源說明保留於 [NOTICE.md](NOTICE.md)。

## 安裝與更新範圍

新安裝按 [README](README.md) 準備 UI，初始化並啟動自己的 OFFICE，再由介面選擇平台。手機功能是可選設定；公開倉庫提供本機執行的程式，GitHub Pages 不會替使用者運行 Python 服務或 Agents。

私人設定、平台憑證、配對連結、對話、資料庫及已登記 Agent 不屬於發佈內容。升級既有安裝前，請由擁有人備份其私人資料目錄並保留原設定，勿用其他安裝的資料覆蓋。

## 驗證與已知限制

`VERIFICATION.json` 保留各次功能測試的時間、範圍及限制；歷史測試數量不代表本次發布重新完成所有測試。程式測試、單一帳戶實測與每個使用者的部署結果必須分開判斷。

- Grok SDK 為非官方實驗接駁；上游 App／協議變更可能影響相容性。長時間休眠、憑證過期及不同帳戶／版本仍需各自驗證。
- Codex 背景對話不等於已有 Desktop 聊天；可用模型依原生執行版本及帳戶權限決定。此模式尚不支援子 Agent 同步。
- Cursor Desktop 本機聊天仍沒有已驗證的原聊天背景派訊息入口；Cloud 及 SDK 的能力不能套用到它。
- Grok Webhook 子 Agent 使用合作回報；SDK 子 Agent 完整公開對話／交付同步及 Hermes 子 Agent 觀察未完成。
- 手機接入須個別確認私人網絡、裝置批准、平台收訊與主機可用性；套件不會自動設定開機啟動，也不宣稱所有作業系統或 VPN 組合已驗證。
- 未確認的派工結果不會自動重送或改成成功。發布版本不代表安全認證或零漏洞。

---

以下保留初版發布紀錄；其中測試數量及當時支援範圍屬歷史資料。

# 0.1.0-preview

## 通用可移動對話框

各平台與子 Agent 的公開對話加入標題列拖曳、四邊及右下角縮放、「浮動／回到頭頂」、鍵盤微調及畫面邊界限制。完整及簡易模式使用同一個讀取器與輸入框，保留草稿、捲動位置、歷史、收發能力及子 Agent 唯讀限制。幾何設定只留在目前瀏覽器，沒有新增派送、輪詢或 Agent 接駁。Agent 搬座位按鈕改名為「⇄ 搬座位」，與移動對話框分開。

參考 dominicliu69/virtual-office `0753acc8591e48cfaf3199a6f1b2cabbdc1bbb07` 的可調整大小對話區，擴充自由移動並保留本版的訊息身份隔離。驗證範圍見 `VERIFICATION.json` 的 `movable_dialogue`。


首個 GitHub 通用預覽包，預設中文介面、空 Agent 名單及本機私人儲存。

- 3D G/F 大堂、Workspace 樓層、休息區、Pantry、CEO Room、角色配色及拖曳座位。
- 角色頭頂公開對話、工作狀態、交付紀錄與平台支援範圍內的父子 Agent 顯示。
- Codex App、Hermes ACP、Grok Bot Webhook、Cursor Cloud 的現有接駁；Cursor 本機聊天的公開同步。
- Grok 的 Webhook 方法保留；本機桌面送訊實驗未納入。

新安裝請依 README 執行 setup_ui.py、office.py init 及 start，再由介面選擇及邀請自己的 Agent。GitHub 儲存通用程式；Python 服務仍在使用者本機運行，這不是 GitHub Pages 雲端 OFFICE。

495 項 Python 與 138 項 JavaScript 測試通過，並驗證從固定上游版本全新安裝、空名單啟動與本機 HTTP 服務。這些驗證不代表所有平台均具備完整雙向背景收訊，詳見 README 支援表與 VERIFICATION.json。

上游 3D 內容在本機安裝時下載，來源與權利說明見 NOTICE.md。私人設定、入口連結、憑證、對話及已登記 Agent 均不包含在此包。

## Cursor Cloud 雙向同步更新

已選定的 Cursor Cloud Agent 支援 OFFICE 原生收發，以及 Cursor Desktop 直接發起工作時的公開對話、狀態與交付自動同步。新增持久化去重、斷線／重啟恢復、完成狀態核對及缺漏提示。保留原有 3D、節能及子 Agent 機制，不自動匯入帳戶名單。舊版無法配對的未確認指令保持原狀，不自動重送；詳見 VERIFICATION.json。

## 手機與桌面操作修正

右上角控制項改用共用排列；手機對話關閉按鈕不再被透明縮放範圍遮蓋。手機可切換已授權的工作樓層，簡易模式保留樓層入口。移除「開啟對話／記錄」跳頁按鈕，保留即時對話及交付資料。

手機權限新增可選的「跟隨已登記 Agents」，查看與發訊息獨立，舊登入維持原有選取範圍。更新範圍保留登入並阻止尚未派出的舊指令；已派出工作不會被誤報取消。測試證據見 `VERIFICATION.json` 的 `mobile_usability`；程式測試不等於真實手機接通。
