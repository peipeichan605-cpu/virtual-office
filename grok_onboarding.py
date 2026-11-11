"""Human-readable Grok pairing guidance, independent of a particular installation."""
import hashlib


def grok_connection_hint(name, agent_id):
    suffix = hashlib.sha256(agent_id.encode()).hexdigest()[:8]
    return {'suggested_routine_name': f'OFFICE 收訊 · {name} · {suffix}'}


def grok_connection_handoff(name, agent_id):
    title = grok_connection_hint(name, agent_id)['suggested_routine_name']
    return (
        '\nGrok 入場後的 OFFICE 連接卡：\n'
        f'只有確定尚無本身份入口、需要新建 routine 時，名稱使用「{title}」。'
        '若已有核對相同 office.agent_id 的有效入口，沿用實際名稱；入口已設定時不要另建、改名或覆寫 routine。'
        '不要靠名稱相似猜選既有流程，不要修改其他 OFFICE 或團隊工具流程。\n'
        '建立或核對後，讀回平台回傳的實際 routine 名稱，在原 Bot 聊天回覆以下「OFFICE 連接卡」，'
        '不要只說回到 OFFICE 或帶回第一項資料：\n'
        'OFFICE 工作流程：填寫實際完整名稱。\n'
        '開啟工作流程：如平台有原生「開啟例行任務」按鈕或連結，附上該流程的入口；不要編造連結。\n'
        '收訊地址（Webhook URL）：若平台工具確實提供此 routine 的 HTTPS POST to 地址，直接列出地址本身，方便擁有人複製到 OFFICE。'
        '不能從名稱猜地址；若 URL 含密碼、金鑰、Authorization、認證 query 或 fragment，不在聊天顯示。\n'
        '若平台不讓 Bot 讀取地址，如實寫「我無法讀取收訊地址；請打開上面同名流程，入面先有 Webhook／POST 到 欄位，按『複製 Webhook URL』，再貼到 OFFICE 的『收訊地址』」。'
        '流程列表只顯示名稱，不會直接顯示 Webhook URL；不能要求人類在列表找這個欄位，也不能要求繞過平台限制。\n'
        '連接金鑰：只在同一流程內按「複製 Webhook 金鑰」，貼到 OFFICE 的「連接金鑰」；'
        '不要在聊天列印 key、Authorization 標頭、憑證內容或私人儀表板連結。'
        'URL 與 key 都能合法取得時，仍優先依指引私下交回 OFFICE，不要求人類重複貼資料。'
        '不能取得 key 時，清楚說明一次性連接尚欠金鑰；列出名稱或地址不代表已連接，仍以實際測試收到及回覆為準。\n'
    )
