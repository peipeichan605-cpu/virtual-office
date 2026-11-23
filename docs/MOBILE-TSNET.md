# Optional private phone frontend with tsnet

For the source layout, build flags and status file format, see the [private frontend implementation guide](../phone-tsnet/README.md).

This frontend gives OFFICE its own Tailscale node without requiring the host's
Tailscale desktop VPN to connect. It uses the existing restricted mobile gateway,
phone pairing and Agent permissions described in [Mobile access](MOBILE-ACCESS.md).
It is an optional transport; desktop OFFICE and its Agent adapters remain local.

```text
Approved Android + Tailscale + OFFICE phone session
  → dedicated OFFICE tsnet node, private HTTPS :443
  → exact phone node and account check
  → 127.0.0.1:<mobile port>, restricted mobile gateway
  → permitted Agents, public conversations, deliveries and commands

Local owner dashboard → pairing, per-Agent permissions and revocation
```

The frontend is a separate userspace node with its own private state. It creates
no TUN interface and does not change the host's routes, DNS, firewall or existing
VPN configuration. Tailscale documents this embedded-node design in
[tsnet](https://tailscale.com/docs/features/tsnet).

**Keep an essential VPN, including NordVPN, connected throughout setup and tests.**
The frontend's outbound traffic still follows the host's existing network and
proxy settings. Its control, relay and UDP connectivity can still be blocked by
that network. Avoiding a second VPN interface is not proof of coexistence or
remote reachability. Do not disable a kill switch, alter routes, or fall back to a
public tunnel to get a passing result. See
[Tailscale with other VPNs](https://tailscale.com/docs/reference/faq/other-vpns).

## Boundaries

- Only the dedicated node's HTTPS port 443 serves the mobile application. The
  backend is fixed to `127.0.0.1` and must differ from the core OFFICE port.
  Requests cannot select a destination host or port. Explicit mobile routes and
  the mobile gateway's static asset allowlist exclude owner and provider APIs.
- No Funnel, generic HTTP/SOCKS proxy, SSH, exit node, subnet routing, web client
  or remote native Agent transport is enabled. Persisted Serve/Funnel settings
  are excluded from this node's state before startup, including login mode.
- Port mapping is disabled inside the helper process with
  `TS_DISABLE_PORTMAPPER=true`; it does not request UPnP/NAT-PMP/PCP router rules.
  This is not a claim that all Tailscale traffic uses HTTPS or only outbound TCP.
- Each request is checked through this node's `WhoIs` API. Both the exact login
  identity and an explicitly allowed phone `StableID` must match. Tagged sources
  are rejected. A hostname, display name, claimed header or matching account
  alone cannot authorize a phone. Incoming forwarding and identity headers are
  removed before the verified identity reaches the mobile backend.
- OFFICE independently requires its revocable phone cookie, CSRF protection and
  per-Agent view/send permissions. Phones never receive owner, feed, internal
  signing or provider credentials. Existing Agent approval rules still apply.
- Node state, private status, login handoff, pairing links and run files stay on
  the installation host. Do not publish, copy between hosts or commit them.
  Client log upload is disabled for this helper; Tailscale account configuration
  and audit records still exist. This setting can limit vendor diagnostics.

## Requirements and platform scope

- An existing OFFICE installation with the mobile security migration and
  restricted loopback gateway available.
- Python 3 and a trusted **Go 1.27.4 or newer** executable. The source module pins
  `tailscale.com` to **v1.104.7** with its dependency checksums. These are reviewed
  implementation versions, not a permanent claim of the latest secure release.
- A private Tailscale account, an enrolled Android phone and authority to review
  the tailnet policy. The host's Tailscale GUI is not used by this frontend.
- MagicDNS and HTTPS certificate support, enabled only after hostname and policy
  review. The helper uses the official Tailscale control plane.

The lifecycle helper supports macOS and Linux process management. Windows process
identity and lifecycle support are not implemented or verified. Linux support in
the code does not imply that a Linux phone deployment has passed live acceptance.

## 1. Build explicitly

Run commands from the OFFICE installation directory. Replace all example paths
and values with this installation's values; none identify a particular user,
computer or Agent.

```sh
python3 phone_tsnet.py --config /path/to/office.json build \
  --go /path/to/trusted/go
```

The helper never installs Go or downloads a replacement Go toolchain. The first
build may download pinned Go modules from the configured public Go module
service. It uses isolated caches and a read-only module manifest. The resulting
binary is recorded with a digest in OFFICE's private data directory.
The build includes the required `ts_omit_peerapiserver` tag. A manually built
binary without that omission refuses to start; do not remove the startup check.

Every build or frontend run has a unique run ID and bounded status/log/result
files. Keep the returned run ID for stopping that exact frontend. There is no
automatic service installation, login item or reboot persistence in this helper.

## 2. Enroll the independent node without serving OFFICE

Choose a neutral name without a personal name, email, project secret or dashboard
token. Reuse the same name and private state for subsequent starts.

```sh
python3 phone_tsnet.py --config /path/to/office.json login \
  --hostname virtual-office
python3 phone_tsnet.py --config /path/to/office.json status
```

**Login mode opens no application listener.** It does contact Tailscale to enroll
the node. The ordinary status output contains a phase and `private_status_path`,
not login URLs, account details or peer lists. An authorized local setup tool can
read that private file to open `auth_url` in the owner's browser. Do not print the
file into a shared terminal transcript or ask the owner to paste its contents.

The human signs in using their own account and completes any device approval.
The helper cannot complete that account approval for them. Existing private node
state is reused, so normal restarts do not demand a fresh login; expiration or
account policy may still require reauthentication later.

After authentication, the private status includes the node's exact DNS name and
eligible same-account peer summaries. Select the intended Android device and
record its exact `node_id` (Tailscale StableID), checking it against the account's
device inventory. Never approve every peer automatically. A removed and
re-enrolled phone may receive a new StableID and needs explicit review.

Stop the login-only process before serving:

```sh
python3 phone_tsnet.py --config /path/to/office.json stop \
  --run-id '<run_id returned by login>'
```

## 3. Review network policy and HTTPS before opening the listener

Allow only the intended Android node to reach this dedicated OFFICE node on TCP
443. Review the full tailnet policy: grants and legacy ACLs are additive, so a
narrow rule does not cancel an existing allow-all rule. Include negative tests
for another device, UDP and the actual core/mobile ports. The example in
[Mobile access, policy review](MOBILE-ACCESS.md#2-restrict-the-phone-node-before-exposing-the-gateway)
applies with the **dedicated tsnet node** as destination. Do not substitute the
host's separate Tailscale GUI node or broaden the source to the whole account.

Enable MagicDNS and HTTPS support in the tailnet only after reviewing the account
change. Certificate issuance publishes the node's full DNS hostname in public
certificate-transparency logs; it does not make OFFICE publicly reachable. See
[Tailscale HTTPS privacy](https://tailscale.com/docs/how-to/set-up-https-certificates).

Configure the loopback mobile backend with that exact HTTPS origin and login
identity. If the security migration is already complete, retain its result.
Use the normal OFFICE service manager for any required restart; both commands
require configured services stopped. Keep existing jobs and NordVPN intact.

```sh
python3 office.py --config /path/to/office.json security-upgrade
python3 office.py --config /path/to/office.json mobile-configure \
  --origin 'https://virtual-office.example-tail.ts.net' \
  --identity 'phone-account@example.com' --port 4319 --enabled
```

Verify the core and mobile backend listen only on loopback and pass their local
authorization checks. The origin must be this node's real DNS name, without a
path, query, fragment or nonstandard port. Do not start a separate Tailscale Serve
mount: this frontend owns its own private HTTPS listener.

## 4. Start private HTTPS explicitly, then pair the phone

```sh
python3 phone_tsnet.py --config /path/to/office.json serve \
  --hostname virtual-office \
  --origin 'https://virtual-office.example-tail.ts.net' \
  --identity 'phone-account@example.com' \
  --node '<verified Android StableID>'
python3 phone_tsnet.py --config /path/to/office.json status
```

`--node` can be repeated only for individually approved devices. The node's own
account and DNS name must also match the supplied identity and origin.

| Status | Meaning |
| --- | --- |
| `login_required` | Human authentication is needed; use only the private handoff. |
| `device_approval_required` | A tailnet administrator must approve this node. |
| `authenticated` | The node is enrolled; login mode still serves nothing. |
| `ready` | A current certificate for the configured hostname is prepared and the private listener is open; phone acceptance is not proven. |
| `error` | Inspect the sanitized error code; do not weaken policy to bypass it. |
| `status_stale` | The worker receipt is not current; no listener or login-handoff claim is made. |
| `process_identity_unknown` | Process identity cannot be established; do not infer that it stopped. |
| `not_running` / `stopped` | The recorded frontend is no longer running. |

`listening=true` is local listener evidence only. The helper always leaves
`end_to_end_verified=false`: it cannot infer a real phone round trip from node
status. On a failed node connection it does not silently switch transports.
Serve mode prepares its certificate before opening the listener, with a
45-second bound. During preparation it remains `authenticated`; the private
status includes `certificate_preparing`. A timeout or invalid certificate fails
startup instead of leaving the first phone request to perform slow issuance.
If the first authenticated snapshot lacks account or node metadata, startup can
wait up to 15 seconds for those fields, with private status `initializing_identity`.
It opens no listener during that wait. A populated account or DNS mismatch fails
immediately; once serving, missing or mismatched identity still fails immediately.

In the local owner dashboard, open **Phone access**, choose a phone label and account,
then grant **View** and **Send messages** separately for the intended Agents. Select
**Create a one-time pairing link** and scan the locally generated QR code with your phone's
camera. A private copy-link fallback is also available; no QR service receives
the token. With Android Tailscale connected, open the link in its browser and
retain the ordinary mobile page as a bookmark.
Do not transfer the desktop owner link. Selected grants remain bound to the
selected memberships. Only an explicitly chosen enrolled-roster policy follows
new Agents, independently for viewing and sending. The first-connection assistant
and daily remembered-login instructions are in [MOBILE-QUICKSTART.md](MOBILE-QUICKSTART.md).

## Upgrade an existing private frontend

When updating OFFICE, update the managed frontend binary as well if its route
contract changes. Remembered-phone login requires the exact POST routes
`/api/mobile/renew` and `/api/mobile/remember`; an older binary rejects those
routes even when the Python gateway has been updated. Do not broaden the proxy
to every API route to work around this.

Use this installation's existing configuration and a verified supported Go
compiler with `phone_tsnet.py ... build --go ...`. Build first while the current
frontend remains running. Keep the previous binary manifest for rollback, then
stop only the recorded frontend run ID and start serve with the same hostname,
origin, account and approved phone node IDs. Preserve its private node state;
do not start a second frontend, re-enroll a node, change VPN routes, or widen
network policy for an upgrade. Confirm current status and test from the phone.
The exact lifecycle commands and human approval boundaries above still apply.

After the upgrade, refresh an already-open phone page once to load the matching
browser modules. A successful build or `ready` status does not establish a
phone login, renewal, or completed Agent message.

## Acceptance record: local checks and live proof are separate

The mobile foundation has local authorization, revocation and HTTP tests. Its
phone-sized browser checks do not constitute physical Android testing. Tests of
this optional frontend use controlled request identities and simulated lifecycle
states; a green unit test does not contact or certify a tailnet.

The pinned Go frontend passed 25 top-level offline tests (87 including subtests)
with `ts_omit_peerapiserver` enabled. These cover configuration and state rejection,
request routing and identity checks, header handling, bounded resources, and
bounded identity and certificate preparation before listener readiness. They
do not enroll a node, obtain a real certificate or open a live tailnet listener.
Lifecycle test results are recorded separately in the package verification record.

For each deployment, record these live results before calling mobile access
verified:

1. Essential VPN remains connected; host default routes and DNS are unchanged.
   Record before/after observations without copying credentials or account dumps.
2. Android on 4G/5G, with Wi-Fi off, reaches only the approved mobile application.
   A different device, even under the same account, is rejected. Test IPv4 and
   IPv6 as applicable, and confirm owner/native/core ports are unreachable.
3. The phone sees only permitted Agents, public conversation and delivery history.
   A uniquely marked phone command reaches the intended connected Agent and its
   matching reply/delivery returns. Repeat after idle time; preserve approval rules.
4. Invalid/replayed pairing, wrong identity, missing CSRF, forbidden Agent access
   and cross-origin writes fail. Revoke the phone and verify its next requests
   fail and undispatched work does not execute. A started task is not claimed
   cancelled merely because the phone was revoked.
5. Desktop 3D, rendering settings, parent/child Agents and desktop message paths
   retain their established behavior. A sleeping/offline host still makes its
   local OFFICE unavailable; this transport does not move Agent execution to cloud.

Until recorded otherwise, real Android cellular access, NordVPN coexistence,
effective tailnet policy and a phone-origin Agent round trip remain unverified.
Keep deployment evidence and private endpoint details outside public packages.

## Stop, revoke or troubleshoot without disturbing the host VPN

Use the exact frontend run ID from `status`:

```sh
python3 phone_tsnet.py --config /path/to/office.json stop \
  --run-id '<current frontend run_id>'
```

The helper verifies the recorded process identity before sending a signal and
refuses an ambiguous target. It does not use process-name killing or stop the
host's Tailscale/NordVPN applications. `stop_requested` is not a completed stop;
check status again. Stopping preserves node state and does not revoke phone
cookies by itself.

To withdraw phone access, revoke the phone in the owner dashboard and remove its
network grant/device authorization as appropriate. Disable the mobile gateway
through `mobile-configure` without `--enabled` and perform the normal OFFICE
restart if retiring mobile access. Never restore a shared owner/feed credential.

For connection failure, inspect only the sanitized state/code and bounded local
run records. Keep private login URLs, node state, cookies, prompts and peer lists
out of support issues. Do not delete state to manufacture a new node, retry login
indefinitely, loosen the allowed-node list or disconnect an essential VPN. Report
the blocked stage and retain the existing local OFFICE.
