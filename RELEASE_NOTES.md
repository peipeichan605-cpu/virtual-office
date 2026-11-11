# 0.1.0-preview

首個 GitHub 通用預覽包，預設中文介面、空 Agent 名單及本機私人儲存。

- 3D G/F 大堂、Workspace 樓層、休息區、Pantry、CEO Room、角色配色及拖曳座位。
- 角色頭頂公開對話、工作狀態、交付紀錄與平台支援範圍內的父子 Agent 顯示。
- Codex App、Hermes ACP、Grok Bot Webhook、Cursor Cloud 的現有接駁；Cursor 本機聊天的公開同步。
- Grok 的 Webhook 方法保留；本機桌面送訊實驗未納入。

新安裝請依 README 執行 setup_ui.py、office.py init 及 start，再由介面選擇及邀請自己的 Agent。GitHub 儲存通用程式；Python 服務仍在使用者本機運行，這不是 GitHub Pages 雲端 OFFICE。

495 項 Python 與 138 項 JavaScript 測試通過，並驗證從固定上游版本全新安裝、空名單啟動與本機 HTTP 服務。這些驗證不代表所有平台均具備完整雙向背景收訊，詳見 README 支援表與 VERIFICATION.json。

上游 3D 內容在本機安裝時下載，來源與權利說明見 NOTICE.md。私人設定、入口連結、憑證、對話及已登記 Agent 均不包含在此包。
