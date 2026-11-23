# Private mobile access

The phone connects through Tailscale HTTPS to a dedicated OFFICE mobile gateway.
The existing desktop OFFICE, native Agent adapters and their credentials stay on
the host. This feature does not publish the office on the public internet.

If the host must keep an existing VPN connected, an optional
[tsnet phone frontend](MOBILE-TSNET.md) provides a separate userspace node without
changing host routes or DNS. It keeps the same phone permissions and pairing;
live VPN coexistence and Android access still require verification.

```text
Approved phone + Tailscale + phone session
  → host HTTPS :443 (Tailscale Serve)
  → 127.0.0.1:<mobile port> (restricted mobile gateway)
  → scoped status, public conversation, and approved Agent commands

Desktop owner → 127.0.0.1:<core port> → pairing and revocation controls
```

The mobile view uses the existing 3D office assets. A phone receives only its
allowed Agents and capabilities. The owner explicitly chooses selected Agents or
an enrolled-roster policy, independently for viewing and sending. Selected grants
stay tied to those memberships; enrolled-roster grants follow new enrollments and
logouts. Sending still requires a connected native command capability and keeps
the Agent's existing approval rules.

For the guided phone workflow, start with [手機首次連接與日常使用](MOBILE-QUICKSTART.md).
This technical guide covers host preparation and the security boundary.

## Security boundaries

- Keep the core server, mobile backend and native transports bound to loopback.
  Never forward the core port, an app-server socket, a credential directory or
  an entire local directory through Serve.
- Use one HTTPS Serve mount to the mobile backend. No Funnel, raw TCP forwarding,
  Unix socket proxy, subnet router, exit node, app connector or Tailscale SSH is
  needed for this feature. Do not reset unrelated existing Tailscale settings;
  review conflicts before enabling mobile access.
- Tailscale grants restrict the specific phone node to the specific host's TCP
  port 443. OFFICE additionally checks the Tailscale login identity and an
  independently issued, revocable phone session with explicit Agent permissions.
- A Tailscale user identity is not a device identity. Serve supplies identity
  headers for user-owned sources, but tagged sources have no user headers.
  Shared nodes can also carry user headers. Do not use an identity header alone
  to authorize a phone, and never authenticate from `X-Forwarded-For`.
- Serve removes incoming spoofed identity headers before adding its own. That
  boundary requires the backend to remain loopback-only. A compromised local
  host or administrator remains inside the trust boundary.
- Phones do not receive the desktop owner key, internal signing key, feed topic,
  provider credentials or Agent credentials. Pairing is single-use and expires;
  the phone session uses a Secure, HttpOnly, SameSite cookie and CSRF checks.

See [Serve identity behavior](https://tailscale.com/docs/features/tailscale-serve)
and [grants](https://tailscale.com/docs/reference/syntax/grants).

## Set up a phone from the owner dashboard

Open **手機接入**, choose an online phone under the same Tailscale account, and confirm. The dashboard can prepare a loopback-only mobile listener while the desktop and its Agents keep running. New installations use a private TLS-terminated Tailscale Serve forward with PROXY protocol v2. The listener uses the native node status, stable device identity, account, and current addresses to admit only selected phones. Pairing grants then restrict what each phone may see and send. The setup flow does not edit the tailnet policy, VPN preferences, or Funnel. Other peers allowed by the network might still establish TCP connections to the private port; the application denies them access to OFFICE.

On a legacy installation the owner can initiate a native confirmation on the host. Approval upgrades the local owner credential, disables the old external feed, and opens the verified owner page on that computer. The initiating legacy page never receives the new credential. Existing manual HTTP Serve and tsnet deployments can keep their existing pairing workflow. The steps below document the **manually administered HTTP Serve** alternative.

See [Tailscale Serve and PROXY protocol](https://tailscale.com/docs/reference/tailscale-cli/serve#use-the-proxy-protocol).

## 1. Prepare an up-to-date private network

Install the official Tailscale client on the host and phone. Use a private tailnet,
strong account authentication and device approval. Review the current
[security bulletins](https://tailscale.com/security-bulletins) and
[stable downloads](https://pkgs.tailscale.com/stable/) before deployment.

The reviewed baseline on 2026-11-16 was **1.104.7**. The helper requires this
baseline or a later stable version, but does not query for newer releases or
certify that a version is free of vulnerabilities. TS-2026-016 and TS-2026-017
affect Serve socket/capability handling and were fixed in 1.104.4. TS-2026-018
concerns the dedicated Kubernetes API proxy and was fixed in 1.104.7; this OFFICE
deployment does not use that proxy.

On macOS, prefer the official standalone installation and keep only one variant.
Approve its network extension/VPN configuration in System Settings, then sign in
yourself. These OS and account approvals cannot be replaced by a code test.
The GUI variant needs a logged-in desktop user after reboot. See
[Mac installation](https://tailscale.com/docs/install/mac) and
[macOS variants](https://tailscale.com/docs/concepts/macos-variants).

Choose a non-sensitive machine hostname before enabling HTTPS: certificate names
are recorded in public certificate-transparency logs. Do not put personal names,
project secrets or a dashboard token in the hostname. See
[HTTPS certificate privacy](https://tailscale.com/docs/how-to/set-up-https-certificates).

## 2. Restrict the phone node before exposing the gateway

Review the tailnet's complete access policy, including legacy ACLs and grants.
Permissions are **additive**: adding a narrow phone grant does not cancel a
pre-existing allow-all rule. An omitted rule is not an explicit deny overriding
another rule. For an existing tailnet, review the policy with its owner instead
of replacing it blindly.

For a **new, dedicated tailnet**, this illustrative policy permits one phone node
to reach only the host's HTTPS port. Replace every example address with the
verified Tailscale address of the intended device. Add and test the corresponding
exact IPv6 selectors if used; do not broaden the source to a user or a CIDR.

```json
{
  "hosts": {
    "office-phone": "100.101.102.103",
    "office-host": "100.101.102.104",
    "unapproved-device": "100.101.102.105"
  },
  "grants": [
    {"src": ["office-phone"], "dst": ["office-host"], "ip": ["tcp:443"]}
  ],
  "tests": [
    {"src": "office-phone", "proto": "tcp", "accept": ["office-host:443"],
     "deny": ["office-host:22", "office-host:4318", "office-host:4319"]},
    {"src": "unapproved-device", "proto": "tcp", "deny": ["office-host:443"]},
    {"src": "office-phone", "proto": "udp", "deny": ["office-host:443"]}
  ]
}
```

Ports 4318/4319 above are examples; test your installation's actual core/mobile
ports. Also review node sharing, tags, group membership, admin access and Funnel
capability grants. Policy tests cover the named cases, not every possible port or
application permission. See [grant evaluation](https://tailscale.com/docs/features/access-control/grants/grants-explained)
and [policy tests](https://tailscale.com/docs/reference/syntax/policy-file#tests).

## 3. Separate OFFICE credentials and configure the mobile gateway

Run these from the OFFICE installation directory. Substitute your existing
configuration path, the host's exact `.ts.net` HTTPS origin and the phone's
Tailscale login identity. Choose an unused mobile port different from the core
port. Examples are deliberately not tied to any user's computer or Agent.

```sh
python3 office.py --config /path/to/office.json security-upgrade
python3 office.py --config /path/to/office.json mobile-configure \
  --origin 'https://office.example-tail.ts.net' \
  --identity 'phone-account@example.com' --port 4319 --enabled
```

`security-upgrade` separates owner/feed/internal credentials and disables the
legacy external feed. The old desktop owner link must be replaced by the new
local owner link; never send an owner link to the phone. Keep configuration files
and backups private. The commands report that a service restart is required.
Use the installation's normal service manager once active jobs can tolerate the
restart. Do not run a second competing OFFICE instance.

Before publishing a mount, verify the core and mobile servers listen only on
`127.0.0.1`, and verify mobile rejection tests locally. A configured origin or a
running gateway is not evidence that the tailnet policy is correct.

## 4. Inspect, then explicitly activate the single private HTTPS mount

The read-only helper does not log in, install anything, change policy, start
Serve, open a browser, disable another VPN, or restart apps:

```sh
python3 tailscale_mobile.py \
  --origin 'https://office.example-tail.ts.net' \
  --identity 'phone-account@example.com' --port 4319
```

It looks for the installed CLI using PATH and named installation locations. Each
of at most six read-only probes has a five-second timeout; the first command
failure stops probing. On macOS it forces `TAILSCALE_BE_CLI=1` to avoid starting
another GUI. No raw CLI errors, login links, peer lists or account details are
printed. Output is limited while reading; an overflow terminates only the helper's
own CLI child. An optional `--binary /absolute/path/to/tailscale` selects an existing
trusted executable.

The helper checks the client/daemon baseline, backend readiness, expected account
visibility, node DNS, minimal network preferences (including the hidden remote
control setting), one HTTPS loopback mount and
Funnel state. It rejects unknown configuration shapes. Missing Serve setup is
expected before activation. Review reported conflicts; never auto-reset them.

Exit code `2` means setup or review is required. Exit code `0` means only that the
expected local transport was observed. `policy_verified` and
`end_to_end_verified` remain false in all cases. It does not inspect the admin
policy, verify actual listening sockets or inspect a phone's live request.

Only after policy and local gateway checks pass, activate the intended mount.
On macOS, the explicit executable form is:

```sh
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale \
  serve --bg --https=443 http://127.0.0.1:4319
```

On other platforms use the installed `tailscale` executable with the same
arguments. HTTPS/MagicDNS account configuration may require human confirmation.
Re-run the helper and inspect the effective policy after activation. Do not use
`funnel`, `--yes`, a Unix socket, or a core/native transport as the target.
See [Serve commands](https://tailscale.com/docs/reference/tailscale-cli/serve)
and [CLI on macOS](https://tailscale.com/docs/reference/tailscale-cli?tab=macos).

If the CLI times out or the app shows “Waiting for Network”, complete normal app
login/extension diagnostics first. Do not repeatedly spawn CLI processes, kill
unrelated jobs or silently alter another VPN. A timeout does not identify the
cause or prove a VPN conflict.

## 5. Pair and use the guided owner screen

Open **手機接入** in the local owner dashboard. The first-connection assistant
covers phone Tailscale installation and VPN permission, same-account sign-in,
Connected status, host readiness, explicit Agent grants, and a five-minute
single-use QR. Use the phone camera and its normal browser, not a Tailscale QR
scanner. A full private pairing link or its code can also be pasted into the
pairing page; full links must match this installation's origin and pairing path.
The QR is generated locally; no third-party QR service receives it.

In the standard setup, OFFICE opens the installed Tailscale app when the owner
needs to sign in. Once the computer and phone are connected, the owner selects
the phone by name and activates its private entry directly in the dashboard.
OFFICE checks the native entry and its own listener before offering a QR. Any
account-level approval opens the official provider page. The standalone host
assistant is available to administrators maintaining an older manual deployment.
See the [quick start](MOBILE-QUICKSTART.md) for the dashboard steps. Scan the QR
on the selected phone to verify pairing, then check a reply from an authorized
Agent to verify delivery.

## 6. Daily login and optional remembered phones

Keep the ordinary `/mobile/` page as a bookmark after pairing, never the one-use
pairing link. Keep Tailscale **Connected** and the OFFICE host awake and running.
ADB, USB, and a Windows computer are not required for ordinary use.

New pairing offers an unchecked **remember this phone** choice. Existing phones
can opt in from **手機登入** without re-pairing while their current login is valid.
A remembered phone has a separate Secure, HttpOnly, SameSite=Strict renewal cookie,
not an owner key. The bounded credential is hashed at rest; short access sessions
rotate on renewal. Its grant ends after 30 days in total or 7 days without use,
whichever is earlier. It is not an indefinitely sliding login. Normal short
sessions remain bounded to 24 hours with a one-hour idle limit. Existing phones
are not silently enrolled into remembering.

When eligible, the browser renews a short session in the background and retries
reads. It never automatically resends a command mutation. Transient network
failure gives reconnect guidance instead of treating every error as revoked.
Revocation, expired remembering, and identity mismatch require a new owner pairing.
Remembering does not grant additional Agents or bypass native approvals. Owners
can revoke a remembered phone even when its short session needs renewal.

From **手機登入**, the user can stop remembering or sign out; the owner can revoke
from **手機接入**. Revocation removes both access and renewal rights and prevents
undispatched commands from executing. It cannot undo work already started or
retrieve content already downloaded. Remove the device's tailnet grant as well
if the phone or its network identity is compromised.

## Update an existing phone and stay in the office

From **手機接入**, use a live or renewable phone registration's **查看範圍** and
**發訊息範圍**, then **儲存權限**. The same browser login remains valid; no new QR
is needed for this permission update. Changing the scope invalidates queued work
that has not been dispatched. Already-received work is not reported as cancelled.
A revoked or fully expired grant still requires new pairing; a valid remembered
phone can renew its short session without a new QR.

Following the roster includes only Agents enrolled in this OFFICE and their visible
child Agents. It is not a platform-wide account import. Logged-out Agents disappear;
queued instructions retain their original enrollment identity so a later rejoin
cannot receive an old instruction.

The phone retains the same 3D view and supports its authorized workspace floors,
including a compact floor selector in simple mode. Open an Agent's head dialogue
and use **×** to close it in place. The old **開啟對話／記錄** navigation buttons are
removed; public dialogue and delivery data remain stored and readable in the office.

## Acceptance evidence and rollback

Automated unit tests are not proof of remote access. The Tailscale helper tests
mock every process and perform no network or account changes. Complete and record
these checks for each installation before describing it as verified:

- With phone Wi-Fi off, approved 4G/5G + Tailscale can view only permitted Agents.
  Another unapproved device, a signed-out browser and a revoked session fail.
- Public conversations and deliveries update on the phone. One uniquely marked
  phone command reaches the intended original Agent, produces a matching reply
  and retained delivery, and still works after idle time.
- Replayed/expired pairing, wrong identity, missing CSRF, unauthorized Agent
  access and cross-origin writes fail. Revoking a phone before queued dispatch
  prevents execution. Verify a running command is not falsely reported cancelled.
- The public internet, LAN direct backend address and disallowed tailnet node
  cannot reach owner APIs, credentials, provider setup or native transports.
  Both IPv4 and IPv6 behavior are reviewed; Funnel remains disabled.
- Existing desktop 3D, rendering settings, parent/child Agents and desktop
  command paths retain their established behavior.

At this implementation stage, local tests and real phone/Tailscale acceptance are
separate. Mac account login, Android pairing, mobile-network reachability and
Agent round-trip evidence remain deployment checks; this document does not claim
they have passed.

For rollback, first revoke phone sessions and remove only this deployment's
Serve mount (after verifying no unrelated service owns it):

```sh
TAILSCALE_BE_CLI=1 /Applications/Tailscale.app/Contents/MacOS/Tailscale \
  serve --bg --https=443 off
```

Run `mobile-configure` with the same origin, identity and port **without**
`--enabled`, then restart OFFICE through its normal manager. Review removal of
the associated phone grant. Do not use a global Serve reset or restore the old
shared owner/feed credential to roll back mobile access.
