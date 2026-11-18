# 手機首次連接與日常使用

手機版保留 OFFICE 的 3D、樓層、公開對話及交付紀錄。手機透過私人連線連到
你自己的 OFFICE 電腦；電腦需要保持開機、連網和 OFFICE 運行。
不需要 ADB、USB 線或另一部 Windows 電腦。

## 第一次連接

1. **先在手機下載 Tailscale。** Android 使用官方
   [Google Play](https://play.google.com/store/apps/details?id=com.tailscale.ipn)，
   iPhone 按 [官方 iOS 安裝教學](https://tailscale.com/docs/install/ios)
   前往 App Store。不要從陌生網站下載安裝檔。
2. **設定手機 Tailscale。** 開啟 App，按 Get Started／開始，允許它加入 VPN
   設定，用 OFFICE 私人網絡所用的同一帳戶登入。登入由你自己完成；不用把
   密碼、驗證碼或金鑰交給 Agent。若管理員要求裝置批准，先完成批准。
   可允許通知，以便收到需要重新登入的提示。
3. **確認真的已連線。** 在 Tailscale 按 Connect／連線，確認畫面顯示
   Connected／已連線。只登入帳戶或看到裝置列表仍不夠。手機通常只能使用
   一個 VPN；如果另一個必需的 VPN 與 Tailscale 衝突，先處理相容性，
   不要盲目關閉保護。無需改動 OFFICE 電腦正在使用的 VPN。
4. **在電腦開 OFFICE → 手機接入。** 跟「首次連接」助手檢查私人入口。
   如果電腦未設定好，可複製助手提供的設定 Prompt 給這部電腦的 Agent，
   由它按本安裝的指引準備；帳戶登入、系統及 HTTPS 批准仍由你完成。
   詳細主機設定見 [MOBILE-ACCESS.md](MOBILE-ACCESS.md)。
5. **選手機可用的 Agents。** 填手機名稱，選擇允許查看哪些 Agents，
   再獨立選擇可否向它們發訊息。只有主動選擇「所有已登記 Agents」時，
   才會自動跟隨之後入場的 Agents。
6. **掃碼配對。** 建立配對連結後，用手機的相機掃碼，再用 Chrome／Safari
   開啟並按配對；不是在 Tailscale 裡掃。QR 只有五分鐘有效、只能用一次。
   掃不到可把完整私人配對連結貼進同一 OFFICE 的配對頁。
   不要公開 QR、連結或把它貼到聊天。
7. **確認結果。** 手機見到獲授權的 Agents，才算完成配對。若需要驗證收發，
   向已連接的 Agent 發一條簡單測試訊息，看到它的回覆及紀錄後再確認。
   電腦顯示入口已設定，不等於手機已連通或指令已完成。

## 平日開啟

- 保持 Tailscale 已連線，直接開配對後的 OFFICE 書籤。收藏正常 OFFICE 頁面，
  不要收藏一次性配對連結。
- 你可在配對時自行選擇記住手機，或在已登入的手機點「手機登入」再啟用。
  最多保留 30 日；連續 7 日沒有使用就失效。在有效期內，短登入會自動續期，
  毋須每天掃碼。共用或不可信任的手機不應選這項。
- 舊手機登入不會自動改成記住；不選擇記住時，沿用 24 小時及一小時閒置限制。
- 在手機「手機登入」可停止記住或登出。在電腦「手機接入」可撤銷手機；
  撤銷後不會自動續期。已開始的 Agent 工作不會因登出自動停止。

## 如果連不上

| 畫面或情況 | 下一步 |
| --- | --- |
| 網站逾時／一直連線中 | 先看手機 Tailscale 是否 Connected，再確認 OFFICE 電腦開機及私人入口運行 |
| Tailscale 要重新登入 | 親自登入原本的帳戶，再回 OFFICE；不要轉用其他帳戶碰碰運氣 |
| 配對碼過期或已用過 | 在電腦重新建立一次性配對連結 |
| 手機登入已撤銷或記住期限已過 | 請擁有人重新配對；重新整理不能恢復已撤銷的權限 |
| 看不到某隻 Agent／不能發訊息 | 由電腦檢查手機權限，以及 Agent 本身是否已接通收訊 |
| 電腦剛重新啟動 | 確認 OFFICE 和私人入口都已啟動；本工具不假設已設定開機自動啟動 |

官方手機操作參考：[Android](https://tailscale.com/docs/install/android)、
[iPhone](https://tailscale.com/docs/install/ios)、
[與其他 VPN 共用](https://tailscale.com/docs/reference/faq/other-vpns)。
