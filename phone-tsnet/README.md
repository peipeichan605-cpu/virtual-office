# Optional private phone frontend

This small process embeds `tailscale.com/tsnet` **v1.104.7**. It creates an independent, private Tailscale node in its own state directory. It does not use the system Tailscale daemon, a TUN device, exit nodes, subnet routes, OS DNS settings, port forwarding or Funnel. It leaves existing VPN connections in place. Actual coexistence with a particular VPN must still be tested on that installation.

The Python mobile gateway remains the authorization boundary for phone sessions, pairing, CSRF, per-Agent scopes and revocation. This frontend adds authenticated Tailscale user **and selected device** checks. It cannot forward to arbitrary destinations or owner/native APIs. It forwards only selected mobile routes/static prefixes to `127.0.0.1:<backend_port>`; Python's exact static manifest and route validation remain authoritative.

Build with Go 1.27.4 or a compatible later release:

```sh
GOTOOLCHAIN=local go build -mod=readonly -tags=ts_omit_peerapiserver -o office-phone-tsnet .
GOTOOLCHAIN=local go test -mod=readonly -tags=ts_omit_peerapiserver ./...
```

The Peer API omission tag is required. An incorrectly built binary refuses to start a node. Tests use fake identity responses and ephemeral loopback HTTP servers; they do not authenticate or start tsnet.

Use `phone_tsnet.py` from the parent directory for installation, exact-PID lifecycle records and private configuration. The binary interface is:

```text
office-phone-tsnet --config /absolute/private/run/config.json
```

Configuration and existing status files must be regular, owned `0600` files, with owned `0700` immediate parent directories. The state directory must be an existing owned `0700` directory; symlinks are refused at those boundaries. The helper currently supports Unix ownership validation (macOS/Linux/BSD); other platforms fail closed pending equivalent private-ACL support. Do not share the state directory with another Tailscale application or run two helpers against it.

Configuration schema (placeholder values only):

```json
{
  "mode": "login",
  "state_dir": "/absolute/private/installation/state",
  "status_path": "/absolute/private/run/status.json",
  "hostname": "office-phone-neutral-name",
  "backend_port": 4319,
  "core_port": 4318,
  "expected_origin": "",
  "expected_identity": "",
  "allowed_node_ids": []
}
```

`login` starts the isolated node solely for browser authentication and private device discovery. It **never calls ListenTLS or exposes OFFICE**. Browser approval remains a human action. Auth keys, OAuth secrets, custom control servers and automatic phone enrollment are not supported. An untagged node belonging to the expected user is required.

`serve` additionally requires:

- `expected_origin`: exact lowercase `https://<node>.<tailnet>.ts.net`, no explicit port or path.
- `expected_identity`: the exact verified Tailscale `LoginName` for the owner.
- `allowed_node_ids`: 1–16 explicitly selected stable device IDs, not node keys, IPs or hostnames. Only the selected phone(s) may reach the mobile gateway, even if another device belongs to the same user.
- Tailscale MagicDNS and HTTPS certificates enabled by the tailnet administrator, and tailnet policy allowing the selected device to reach this node's TCP 443. HTTPS certificate issuance can put the neutral node DNS name in public certificate-transparency records; the OFFICE content and listener remain private.
- A separately configured Python mobile gateway on its loopback port, with the matching origin and identity. `backend_port` defaults to 4319 and must be 1024–65535 and different from `core_port` (default 4318).

The only OFFICE listener is private tsnet TLS port 443. No generic forwarding/fallback handler is registered. Persisted Serve/Funnel configuration is hidden before startup (upstream `Up` alone would clear it too late). WebClient is off, built-in Peer API is omitted, inherited `TS_*`/`TSNET_*` settings are cleared, router port mapping is disabled for this process, and logtail upload is disabled. The helper does not change the tailnet's access policy or OS services. Tailscale's account/control-plane audit records are separate from the disabled client logs.

Every request uses the embedded node's `WhoIs(RemoteAddr)` response. Tagged nodes, mismatched users, inconsistent user IDs and unselected stable device IDs are denied. Claimed Tailscale/forwarded identity headers are stripped before setting the verified login. The fixed transport ignores proxy environment variables. Requests, connections, headers, bodies and response waits are bounded; request contents and authentication URLs are never logged.

Status is written atomically as a private `0600` JSON file with `version`, `phase`, `pid`, `updated_at`, and an optional constant error `code`:

| Phase | Meaning |
| --- | --- |
| `starting` | Node startup or coordination pending; no ready claim |
| `login_required` | Human authentication needed; private `auth_url` is included only for exact HTTPS `login.tailscale.com` |
| `device_approval_required` | Tailnet device approval is pending |
| `authenticated` | Login completed; login mode still has no OFFICE listener |
| `ready` | Serve mode opened its private HTTPS listener; this does not prove phone pairing, Agent delivery or a successful round trip |
| `stopped` | Helper exited normally |
| `error` | Fail-closed error; see non-sensitive `code` |

Authenticated status may contain private `own: {node_id, identity, dns_name}` and `peers: [{node_id, hostname, os, online}]` for eligible same-owner, untagged devices. These are local setup data; do not print or publish the full status file or login URL. Device selection is explicit and separate from authentication. Stopping/revoking a phone session, stopping this exact helper and removing the isolated tailnet node are separate operations.

Source review references: [tsnet implementation](https://github.com/tailscale/tailscale/blob/v1.104.7/tsnet/tsnet.go), [node status identity types](https://github.com/tailscale/tailscale/blob/v1.104.7/ipn/ipnstate/ipnstate.go), [WhoIs response](https://github.com/tailscale/tailscale/blob/v1.104.7/client/tailscale/apitype/apitype.go), [embedded listener dispatch](https://github.com/tailscale/tailscale/blob/v1.104.7/ipn/ipnlocal/netstack.go).
