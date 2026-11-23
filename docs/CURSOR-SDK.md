# Cursor SDK 純對話

在 OFFICE 按「＋ 新對話」，選擇「Cursor SDK · 純對話」，可以建立一個由 OFFICE 管理的 Cursor SDK session。之後在角色頭頂輸入訊息，公開回覆及交付會回到同一個角色，後續訊息沿用同一條對話。

這個模式與 Cursor Desktop 原有聊天分開。Desktop 的原生 Hook、既有 Cloud 路由及主／子 Agent 同步仍保留；SDK session 不會被描述成已接管 IDE 側欄聊天。

## 一次設定

標準完整安裝的 `setup-platforms` 已包含這一步。已有 Cursor Desktop 不等於已安裝 Cursor SDK；未做完整安裝或要單獨修復此環境時，可使用以下命令。

OFFICE 核心仍支援 Python 3.9。Cursor SDK 需要 Python 3.10 或以上及官方 `cursor-sdk==1.3.1`；Windows 需要 x64 Python（此固定版本未提供 Windows ARM64／32 位 wheel）。使用以下通用命令建立本安裝專用的私人環境：

```sh
python3 office.py setup-cursor-sdk
```

Windows 請將 `python3` 換成已確認的 `python`、`py -3` 或 Python 完整路徑。安裝器會核對目前 Python、PATH 及 Windows Python launcher 已登記的執行環境；偵測過程不會暗中下載 Python。

如果有可用的 `uv`，亦可明確允許下載獨立 Python：

```sh
python3 office.py setup-cursor-sdk --download-python
```

使用自訂 OFFICE 設定時，在子命令前加 `--config "完整設定路徑"`。程式會檢查 Python 的實際匯入能力，保留系統 Python，並將獨立環境及安裝紀錄放在這個 OFFICE 的私人資料目錄。新安裝在首次啟動前準備；已有服務則先妥善處理工作，再用同一設定的 `office.py stop`、`office.py start` 重載。後續啟動會辨認已驗證的私人 SDK 環境。

如果畫面仍未備妥，會分開顯示「尚未安裝 SDK」「服務 Python 太舊」「SDK 版本不符」或「Windows Python 架構不支援」，並提供可交給安裝 Agent 的設定 Prompt。SDK 裝到另一個 Python 並不等於 OFFICE 已使用它；重載後再檢查本安裝的狀態。`setup-platforms --check` 只檢查環境，不讀取帳戶憑證。

## 建立對話

1. 在 OFFICE 按「＋ 新對話」，選擇「Cursor SDK · 純對話」。
2. 填寫對話名稱。預設由 OFFICE 自動建立這條純對話專用的資料夾。若要把角色放到指定專案的樓層，可改選已登記的專案資料夾，或自行輸入路徑。
3. 第一次填寫自己的 Cursor User API key。資料只保存於本安裝的私人設定，其他 SDK 對話可沿用；不會放入瀏覽器儲存空間。
4. 建立後，點角色頭頂輸入訊息。

Cursor Desktop 已登入，不代表此 SDK 版本已有 API key。未設定或平台拒絕認證時，畫面會如實顯示原因；不用把 key 貼到聊天、程式碼或 GitHub。

建立成功只證明 OFFICE 已登記這條對話。首次傳送時才建立原生 SDK session，並在同一個 SDK 連線送出指示；收到原生回條後才確認原生身份。收到相配公開回覆並保存完成紀錄後，才算驗證任務收發。建立或傳送結果未確認時，保留原請求身份作核對，不自動重送或取消其他工作。

## 權限及同步範圍

目前新增模式為「純對話」：每輪沿用已保存的空工具清單，不授予檔案、Shell 或 MCP 操作。Cursor Desktop 的批准設定與 SDK 的工具設定分開處理，不會宣稱兩者自動等同。

OFFICE 保存這條 SDK 對話的公開輸入、assistant 文字、狀態及最終交付，沿用敏感內容遮蓋。思考內容、工具原文及其他未選聊天不會匯入。沒有工作時不啟動模型。重新啟動後可續接同一個 session；尚未確認完成的舊傳送保持結果未確認，不能靠重啟當作成功或自動重做。

既有平台的子 Agent 機制保持獨立。SDK 工具活動本身不等於已驗證的子 Agent 身份、公開對話或完整交付，不能用工具名稱補造角色。

## 來源

整合參考 [dominicliu69/virtual-office 的 Cursor 接駁貢獻](https://github.com/dominicliu69/virtual-office/commit/0753acc8591e48cfaf3199a6f1b2cabbdc1bbb07)，按現有 OFFICE 的權限、派送及畫面機制重新整合。SDK 使用 [Cursor 官方 Python SDK](https://cursor.com/docs/sdk/python) 的公開介面；沒有覆蓋現有大堂、光影、效能設定或原生父子 Agent 接駁。
