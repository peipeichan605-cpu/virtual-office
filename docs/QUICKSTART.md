# 快速開始

Virtual Office 是在你自己的電腦運行的 3D Agent 辦公室。新安裝使用空名單，
由你決定接入哪些平台及聊天；GitHub 不會替你運行 OFFICE 或 Agents。

## 全新安裝

標準安裝準備完整的 Codex、Cursor SDK、Grok SDK 執行環境。需要 Python 3.10
或以上、Node.js 22.13 或以上、相容的本機 Codex，以及網絡連線下載固定 UI
素材及 Cursor SDK。Windows 的 Cursor SDK 需要 x64 Python；Codex 使用原生
`codex.exe`。已有 Python 3.9 的 OFFICE 核心仍可沿用，SDK 另用獨立環境。

將套件放在自己選擇的資料夾，在該資料夾執行：

```sh
python3 setup_ui.py
python3 office.py init
python3 office.py setup-platforms
python3 office.py setup-platforms --check
python3 office.py start
python3 office.py status
python3 office.py url
```

Windows 把 `python3` 換成已確認的 `python`、`py -3` 或完整執行檔路徑。
`setup-platforms` 安裝固定版本 Cursor SDK 到這個 OFFICE 的私人環境，並檢查
Node.js／Codex。缺少環境時會逐項列出原因，補齊官方執行程式後重新檢查；
`runtimes_ready=true` 才算完成環境準備。這一步不讀取帳戶憑證或建立聊天，
帳戶登入及實際收發另行驗證。已有 Cursor App 並不代表已裝好 Cursor SDK。

最後一行在你的終端顯示私人入口，請自己開啟，不要公開分享。設定、登入資料、
Agent 綁定、對話與資料庫存於此安裝的 `.office/`，不要上傳 GitHub。
基本安裝不會加入任何 Agent，也不會安裝開機自動啟動服務。

**已有 OFFICE 請改用 [更新指南](UPGRADING.md)，不要重做 `init` 或覆蓋私人資料。**

## 加入自己的 Agent

按右上角「＋ 邀請 Agent」，或樓層的「＋ 新對話」，先選平台及接駁方式：

| 選擇 | 下一步與實際範圍 |
| --- | --- |
| 已有 Codex App 聊天 | 按邀請 Prompt，在自己選定的原聊天完成綁定；見 [指令接駁](COMMANDS.md) |
| Codex · 背景模式 | 建立 OFFICE 管理的獨立對話；見 [Codex 背景模式](CODEX-BACKGROUND.md) |
| Grok Bot SDK · 實驗模式 | 完成自己的瀏覽器登入，再選現有 Bot 及聊天；見 [Grok SDK](GROK-SDK.md) |
| Grok Bot · Webhook | 依同一個邀請視窗的連接助手完成設定；見 [Grok Webhook](GROK-BOT.md) |
| Cursor Cloud Agent | 使用自己的連接資料，選已有 Cloud Agent；見 [Cursor](CURSOR.md) |
| Cursor SDK · 純對話 | 建立 OFFICE 管理的獨立純對話；見 [Cursor SDK](CURSOR-SDK.md) |
| Hermes ACP | 連接固定原生 session；見 [Hermes](HERMES.md) |

Cursor Desktop 本機聊天支援所選聊天的公開同步，目前沒有已驗證的原聊天背景
派訊息入口。各平台的完整支援範圍見 [README](../README.md#平台持續收訊支援)。
平台需要登入或批准時，由你在原平台完成；毋須把密碼或金鑰貼進聊天。

角色入場表示已登記。第一次發訊後，應再核對工作狀態、相配公開回覆及交付紀錄；
不能只看角色出現就當作收發已完成。日常點角色頭頂名稱即可閱讀對話或輸入指示。

## 手機使用

先閱讀 [手機首次連接與日常使用](MOBILE-QUICKSTART.md)。它會帶你在手機下載
Tailscale、用同一帳戶連線，再從電腦「手機接入」選權限及掃碼配對。
可選擇記住手機，最多 30 日，連續 7 日未使用失效。OFFICE 主機仍須開機及連網。
手機接入不需要 ADB、USB 線或另一部電腦。

## 日常操作

- 「簡易模式」集中顯示角色及頭頂對話；完整模式預設返回 G/F。
- 拖曳對話頂部可移動視窗，拖拉邊框可調整大小；「回到頭頂」恢復跟隨角色。
- 「⇄ 搬座位」調整角色的視覺座位；不會更改 Agent 的實際專案或工作目錄。
- 要停止這個安裝，執行 `python3 office.py stop`，再用 `status` 核對。
  自訂設定路徑的安裝須在各命令前保留同一個 `--config`。

詳細能力、限制及來源說明見 [README](../README.md)、[發布說明](../RELEASE_NOTES.md)
及 [NOTICE](../NOTICE.md)。
