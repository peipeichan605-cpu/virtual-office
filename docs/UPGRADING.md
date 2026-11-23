# 更新已有 OFFICE

本指南適用於保留同一個安裝位置、同一份私人設定及 Agent 紀錄的更新。
先閱讀 [發布說明](../RELEASE_NOTES.md)。GitHub 的通用程式不包含你的登入資料、
配對、對話及資料庫；更新時應保留這些私人資料。

## 1. 完成目前工作並停止這個安裝

先讓正在執行的 Agent 工作完成，核對尚未確認的指令。在原 OFFICE 資料夾執行：

```sh
python3 office.py status
python3 office.py stop
python3 office.py status
```

預設設定檔是安裝位置的 `.office/config.json`。若原本使用 `OFFICE_CONFIG` 或自訂
設定檔，每次命令都要保留原設定與完整路徑，例如：

```sh
python3 office.py --config /absolute/path/to/office.json status
```

`stop` 只停止這個設定所管理的 OFFICE；原平台正在執行的工作未必會停止。
有獨立 tsnet 手機入口時，依 [私人入口的精確停止方法](MOBILE-TSNET.md#stop-revoke-or-troubleshoot-without-disturbing-the-host-vpn)
記錄並停止該入口的 run ID，再核對停止結果；不要關閉其他 VPN、原平台或無關服務。

## 2. 備份私人資料及舊版程式

由擁有人將整個 `.office/` 複製到公開倉庫以外、只有自己可讀的備份位置。
若使用自訂設定檔或私人資料目錄，連同它們一併備份。備份包含登入及身份資料，
不可提交到 Git、公開雲端連結或聊天。記下目前程式版本，並保留舊版程式副本或
Git commit，以便在需要時比較。

**不要刪除 `.office/`，不要用別人的設定覆蓋，亦不要重新執行 `office.py init`。**
備份應在服務及需要備份的私人入口停止後進行，避免複製仍在寫入的資料庫或狀態。

## 3. 更新程式

### 原本使用 Git clone

先檢查工作目錄：

```sh
git status --short
```

若有自己的修改，先保存並處理差異，不要用強制覆蓋或 reset 丟棄。確認工作目錄
乾淨、位於本倉庫的預設分支，且 remote 指向你要更新的 Virtual Office 倉庫後：

```sh
git pull --ff-only
```

如原安裝依 AGENTS.md 使用發佈標籤，請在備份且工作目錄乾淨後，用 `git fetch origin --tags` 及 `git switch --detach <新版標籤>` 更新至擁有人選擇的版本。上面的 `git pull` 只適用於追蹤遠端的分支。

如果 Git 回報分支分歧，停在這一步，先核對雙方更改；不要強推或覆寫其他人的提交。

### 原本使用 ZIP

先解壓新版到另一個資料夾核對內容，再更新原安裝位置內的程式檔案。保留原私人
設定與資料目錄，不把新版當成另一個空辦公室重新初始化。保留舊版副本；不確定
哪些檔案屬於個人修改時，先比較再搬移。

## 4. 重新準備 UI

`public/` 是安裝產物。`setup_ui.py` 遇到不同版本的既有 UI 會拒絕覆蓋，
所以先把原安裝的 `public/` **搬到倉庫外的備份資料夾**，保留自訂素材及舊版，
確認原位置已沒有 `public/`，再執行：

```sh
python3 setup_ui.py
```

這一步會下載並校驗固定上游素材及套用新版 UI。若失敗，保留輸出並處理原因，
不要在 UI 未備妥時啟動；也不要繞過校驗。安裝成功後可保留舊 UI 備份，毋須立即刪除。

## 5. 準備完整執行環境，再啟動並核對

沿用原設定逐行執行，每一步成功才繼續；使用自訂設定時，每行子命令前都加同一份 `--config` 完整路徑。Windows 請把 `python3` 換成已確認可用的 Python 命令或完整路徑：

```sh
python3 office.py setup-platforms
python3 office.py setup-platforms --check
python3 office.py start
python3 office.py status
python3 office.py url
```

`setup-platforms` 會在這個 OFFICE 的專用環境準備固定 Cursor SDK，並檢查 Codex 與 Grok 執行環境。缺少 Node.js 或 Codex 時，按 [安裝指引](../README.md#安裝) 補齊官方程式；必須確認 `runtimes_ready=true` 才交付完整安裝。這一步不會替你登入帳戶、重建 Agent 或搬移憑證。已有 Cursor App 不代表已有 SDK。

在瀏覽器重新整理，確認原 Agent 名單、樓層及對話仍在，再以一個已連接 Agent
確認正常收發。私人網址留在自己的裝置；如果服務回報遷移或相容性問題，先按
具體訊息處理，不要重做初始化。

## 可選：更新已有手機私人入口

只在原本已啟用手機功能的安裝處理這一步。獨立 tsnet 入口的二進位程式需按
[更新已有私人入口](MOBILE-TSNET.md#upgrade-an-existing-private-frontend) 重建及啟動；
沿用原設定、節點身份、主機名稱和已批准手機，勿另建第二個入口。
詳細生命週期命令以該指引及本次實際 run ID 為準。

Tailscale Serve 安裝則沿用自己的既有設定及 [手機接入指引](MOBILE-ACCESS.md)，
不用為程式更新重設帳戶或擴大手機權限。新版載入後，手機頁面重新整理一次，
核對登入與原有紀錄；配對已過期或撤銷時才重新配對。

## 回退注意

不要把舊私人備份直接覆蓋到已產生新對話或新權限的資料目錄。程式與資料格式
可能一起演進；需要回退時先停止該安裝，保留最新資料，再核對舊版是否支援目前
設定。備份用於復原，不代表可以無條件把較新紀錄及撤銷狀態回到舊時點。
