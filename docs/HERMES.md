# Hermes 原生 session 接入

OFFICE 使用已安裝 Hermes 的 **ACP stdio 接口**。Hermes 執行工作及保存原生 session；Python 負責排隊、接收公開訊息及更新畫面。接駁程式沒有指定模型、供應商、API key 或模型路由，沿用所選 Hermes 環境的設定。

## 接入一個 session

先在本機正常設定 Hermes，確認 `hermes acp --check` 成功。在 OFFICE 套件目錄執行：

```sh
python3 office.py connect-hermes --name "My Hermes" --workspace /absolute/path/to/project
```

指令透過 Hermes 原生 API 建立一個 ACP session，並只將這個身份加入 OFFICE。接入本身不發送工作 Prompt，也不做模型推理。CLI 會顯示真正的 session ID。若 `hermes` 不在 PATH，加上 `--binary /absolute/path/to/hermes`。

若要使用另一個已設定好的 Hermes profile，加 `--hermes-home /absolute/path/to/profile`。OFFICE 不會複製帳戶憑證或更改全域模型設定。

OFFICE 已在運行時，接入後執行 `python3 office.py stop`，待服務停止，再執行 `python3 office.py start`。重新整理畫面，選擇該角色 → 點頭頂對話 → 輸入指示 → 傳送。工作區路徑決定角色所在樓層及 Hermes 實際工作目錄。

## 綁定現有 ACP session

```sh
python3 office.py connect-hermes --name "My existing Hermes" \
  --workspace /absolute/path/to/project --session ORIGINAL_ACP_SESSION_ID
```

所選 profile 必須持有該 session，且其 Hermes 版本必須支援 `session/load`。只綁定目前沒有其他客戶端正在操作的 session。OFFICE 會在同一 profile 對 session 加鎖，防止另一個 OFFICE 同時操作。

這個版本使用 ACP 建立及續接的原生 session；不會把另一個互動 CLI、tmux 或 Crouton helper 的臨時工作冒充成同一對話。本機所測 Hermes v0.14.0 的 ACP 只會載入 `source=acp` 的紀錄。其他版本須核對其原生能力。

## 背景執行與紀錄

- 以原 session ID 發送 Prompt；不開啟 App、終端視窗或切換焦點。
- 同一 session 逐輪執行，下一輪保留 Hermes 上下文。重啟後透過原生 `session/load` 恢復。
- 排隊收據與實際執行分開；收到原生更新或對應回覆才標示開始。完成狀態以原生回合回覆為依據，不代表每個工作目標一定成功。
- 公開文字分段更新同一則氣泡訊息；完成後保留交付。Hermes 內部思考、原始工具參數及工具輸出不會複製到 OFFICE。
- Hermes 要求批准時，氣泡顯示「允許今次／拒絕」。不提供永久批准；45 秒未處理、失去連線或拒絕時不會自動放行。
- 同一請求編號只派送一次。執行途中斷線或 OFFICE 意外退出時保留「結果未確認」，暫停該 session 後續派送；不自動重做可能已執行的操作。此版本須核對原 session 後由維護者處理未確認紀錄。

指示暫存在本機私人 outbox，派送後清除。Hermes 自己的 profile 會保留原始對話；OFFICE 資料庫保存遮蓋後的公開內容。遮蓋不是作業系統隔離；工具仍以 Hermes 原有權限與批准設定執行。

目前已在 macOS 實測；接駁使用 POSIX 程序與檔案鎖。Windows 原生執行尚未支援。OFFICE 不需要新增 Python 套件，但 Hermes 的 ACP 依賴必須已安裝。

## 已有自訂 Hermes 啟動器

若使用者已有啟動指定 profile 或模型代理的程式，可提供本機 JSON argv 檔，並使用 `--launch-file /absolute/path/to/argv.json`。該程式須在 stdout/stdin 提供 Hermes ACP，首個 argv 必須是本機可執行檔的絕對路徑。這是擁有人設定，不接受網頁 Prompt 指定 shell、模型或憑證。

通用套件不包含任何私人模型代理、憑證、測試 profile 或使用者 session。不同人可以使用各自已設定的 Hermes 模型。
