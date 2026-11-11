# 通用主／子 Agent 機制

這項功能把已入場主 Agent 的子 Agent 放到同一個 3D OFFICE，保存各自的狀態、公開文字及交付。子 Agent 不用逐一邀請，也不會變成永久成員。接駁來源不同，畫面會保留其證據類型，不把合作回報當成原生觀察。

| 平台／來源 | 畫面證據 | 接駁範圍 |
| --- | --- | --- |
| Codex App 本機原生紀錄 | 原生紀錄同步（`native_metadata`） | Python 驗證父子元資料及子聊天自己的工作事件 |
| Cursor 本機原生 Hooks 及結構化公開紀錄 | 原生事件同步（`native_hook`） | Hook 驗證父子身份及狀態；共用 Python 讀取器同步該子回合自己的公開正文 |
| Grok Bot 合作回報 | Agent 合作回報（`parent_reported`） | 父 Bot 使用真正 Task 子 Agent ID，經本機上報程式提交狀態及公開結果 |
| Hermes 子 Agent | 尚未接通 | 不從主 Agent 的狀態推測子 Agent 活動 |

Codex 原有原生觀察流程保留。Grok Bot 合作回報已在同一部 Mac 的兩個實際 Bot、三位 Task 子 Agent 驗證，涵蓋平行工作、公開進度、交付及退場後歷史；其中一輪經 OFFICE Webhook 派工。這仍依靠父 Agent 配合回報，沒有獨立雲端事件觀察。Cursor Desktop 3.29.9 已驗證真正 Task 子 Agent 的原生身份、狀態、父子隔離、公開進度、交付正文及歷史；正文由共用 Python 讀取器從該子回合的結構化公開訊息補取，毋須逐隻接駁。其他機器與帳戶亦需自行驗證。

## 入場與顯示

1. 先按原有流程，讓你選定的主 Agent 加入 OFFICE。
2. Codex 完成原 session 綁定；Cursor 按接入指引安裝及啟用原生 Hook；Grok Bot 按合作回報指引保留父任務與真正子 Agent 的身份。
3. 來源提交已接受的子 Agent 開始事件後，OFFICE 在主 Agent 同層、附近顯示它；點頭頂對話可讀來源提供的公開任務、進度與交付。
4. 完成、失敗或中止後，角色短暫保留約 30 秒再退場，交付紀錄仍在本機歷史。主 Agent 頭頂的「子 Agent N」可選擇其他子 Agent，完整及簡易模式均可用。

跟隨是視覺安排，不會改變子 Agent 真實工作目錄。子 Agent 不接受 OFFICE 派工、拖曳座位、Workspace 修改或獨立登出，亦不繼承主 Agent 的 OFFICE 憑證；追加工作仍由原主 Agent 處理。角色擠迫時會優先安排已選子 Agent；沒有 3D 位置時仍可開啟其本機歷史。

## Cursor 首次啟用

接入指引會執行 `adapters/connect.py cursor-children --hooks-file ...`，使用該主 Agent 自己的接入憑證。程式先建立待綁定的 Observer，再將觀察命令合併到 Cursor Hook 設定；只替換本安裝、同一憑證路徑的已識別舊觀察命令，保留其他 Hook 及批准規則。

若回傳 `activation_marker`，在**目前原聊天的公開最終回覆**原樣包含該完整標記一次。只有相配的原生 `afterAgentResponse` Hook 可以用它綁定實際 `conversation_id`；在 Prompt 貼上標記或出現標記片段不算完成。綁定時會輪換觀察憑證及世代，未綁定憑證不能提交工作事件。

啟用回覆本身不會被合成正在執行的父／子任務。之後下一項實際 Prompt 的 `beforeSubmitPrompt` 才建立父回合，接着處理其子 Agent 活動。Hook 安裝成功、原聊天綁定、收到原生事件及任務完成是不同階段。

每個 OFFICE 安裝只需一個明確的私人 Observer 目錄，Hook 命令按 `--observer-dir` 讀取當中已啟用的 Cursor 綁定；不為每個主 Agent 重複安裝同一組命令，也不掃描其他 OFFICE 或使用者目錄。詳細步驟及支援邊界見 [Cursor 接駁](CURSOR.md)。

## Grok Bot 合作回報

現版沒有已驗證的一般帳戶 Grok Bot 雲端子 Agent 事件訂閱。Grok Bot 的接入指引會加入通用合作回報方法：先回報目前父任務開始，再使用真正 `Task` 建立子 Agent，保留原生 ID，於開始、公開進度及真正交付時呼叫 `adapters/children.py`。

此路徑由 Agent 配合執行，證據保持 `parent_reported`。沒有原生 Task、本機執行能力或相配回報時，不能宣稱子 Agent 同步已接通；原生 App 看到結果，亦不等於 OFFICE 已收到事件。詳細參數見 [Grok Bot 接駁](GROK-BOT.md#原生-task-子-agent合作回報)。

## 資料與生命週期

- 只接受已入場主 Agent 所屬的觀察來源或合作回報。穩定身份包含主 Agent、來源世代及原生子 Agent ID；顯示名稱不作身份識別。
- 每個子 Agent 保留直接父身份、最上層成員、層級與父工作編號。開始事件必須對應已有的父任務；巢狀子 Agent 的直接父亦須已建立且正在工作。
- 每則回報使用唯一事件 ID；相同 ID 及相同內容可取得重複收據，不能用同一 ID 改寫內容。開始收據只證明已接收，不代表完成。
- 原生父子資料只證明關係。歷史中未關閉的聊天不代表仍在工作；主 Agent 完成回合亦不代表子 Agent 完成。
- 完成、失敗、中止與狀態未知分開處理。來源長時間沒有更新，或 OFFICE 重啟後尚未取得新的合作／Hook 回報，會保留待確認狀態，不推測完成。
- 主 Agent 登出或改綁來源後，舊子 Agent 的觀察及紀錄授權會失效。已保存資料留在本機，不會以遲到事件恢復成員。
- 只保存委派任務及對使用者公開的進度／最終文字，先經既有遮蓋流程。內部推理、加密內容、工具參數及工具原文不作對話保存；Cursor 工具 Hook 只使用父子關聯所需的身份欄位。
- Codex v2 會排除繼承內容及舊回合。若只提供加密任務正文，顯示「原平台未提供可讀任務內容；請查看子 Agent 的公開進度與交付。」；不解密或猜測原 Prompt。
- 平台沒有提供公開中途文字時，不補造聊天。Cursor `afterAgentResponse` 是完整訊息事件，不代表逐 token 串流。

這些接駁不建立公網入口、不向 ntfy 發佈子 Agent 對話、不啟動額外模型。Cursor 本機子 Agent 觀察與 OFFICE 背景派工是兩項能力；安裝 Hook 不會令本機聊天取得 Cloud Agent 的指令入口。
