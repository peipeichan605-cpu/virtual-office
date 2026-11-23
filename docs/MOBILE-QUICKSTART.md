# Connect a phone and use it day to day

The phone opens the same 3D office, floors, public chats and deliveries through a private connection. The host computer must stay on, online and running OFFICE. You can pair from your phone camera and browser without ADB or a USB cable.

## First connection

1. On the computer, open **Phone access** (`手機接入`) in the owner dashboard. On your phone, install the official Tailscale app for [Android](https://play.google.com/store/apps/details?id=com.tailscale.ipn) or [iPhone](https://apps.apple.com/app/tailscale/id1470499037). Sign in with the same account used on the computer, allow the VPN connection and tap **Connect**. If Tailscale already shows **Connected**, go straight to device selection.
2. In OFFICE, select your phone and choose **Connect this phone**. If the computer needs to sign in to Tailscale, open its official app from the assistant. Older installations may ask you to confirm a one-time upgrade of the owner entry on that computer. The new owner page takes you back to phone setup. Bookmark that page.
3. Choose which agents the phone may **view** and which it may **message**. Scan the QR code with the phone's regular camera, open it in the mobile browser and tap **Pair**. The code expires after five minutes. Check that the selected agents appear. Send one message and check the reply to verify delivery.

If a phone is missing, open Tailscale on the phone, check the account and **Connected** state, then refresh the list in OFFICE. If an administrator must approve the device or enable private HTTPS, follow the official link shown in the assistant. Issuing an HTTPS certificate puts the displayed computer domain in public certificate logs.

If another service already owns the private endpoint, contact the tailnet administrator to review it. Existing separate-node or self-managed installations can continue using their pairing flow. OFFICE checks the selected device identity on every connection. The tailnet's network rules must also allow the phone to reach the computer.

## Daily use

- Keep Tailscale connected. Bookmark and reopen the **regular OFFICE page** after pairing. The QR link is single use.
- On a trusted personal phone, opt into **Remember this phone** when pairing or later in **Phone login** (`手機登入`). A remembered login renews short sessions for up to 30 days and expires after 7 unused days.
- Without this option, the normal login lasts up to 24 hours and expires after one idle hour. Existing phones keep their original choice.
- On the phone, **Phone login** lets you stop remembering it or sign out. On the computer, **Phone access** lets the owner revoke it. Revocation stops renewal. Work a provider already started can continue.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| This tab lacks owner access | Open the original private owner tab or owner bookmark on the computer, then select **Phone access**. |
| Office assets could not load | Keep Tailscale connected and choose **Reopen OFFICE**. If a retry time appears, wait for it and use the same tab. |
| The browser could not start 3D | Close other 3D tabs on the phone, return and choose **Reopen OFFICE**. |
| A bookmark opens the pairing page | A valid or renewable login returns to the lobby when there is no new code. Confirm on screen before applying a new QR pairing. |
| The site times out | Check **Connected** on the phone, then check the host and its private frontend. |
| Tailscale needs another sign-in | Sign in to the original account and reopen OFFICE. |
| Pairing code expired or already used | Generate a new one-time code on the computer. |
| Login revoked or remember period ended | Ask the owner to pair again. |
| Missing agent or disabled message field | Check the phone's view/send grants and that agent's receiver. |
| Host just restarted | Check that both OFFICE and its private frontend are running. |

[Tailscale on Android](https://tailscale.com/docs/install/android) · [Tailscale on iOS](https://tailscale.com/docs/install/ios) · [Using other VPNs](https://tailscale.com/docs/reference/faq/other-vpns)
