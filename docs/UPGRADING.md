# Upgrade an existing OFFICE

An in-place upgrade keeps the same installation, private configuration, agents, conversations and phone permissions. Read the [release notes](../RELEASE_NOTES.md) and identify the config used by the running service before changing files.

## 1. Finish current work and stop this installation

Let active agents finish and review commands with an unconfirmed outcome. In the original installation folder, run the following commands. Replace the example config with the original path throughout, including installations using `OFFICE_CONFIG`.

```sh
python3 office.py --config .office/config.json status
python3 office.py --config .office/config.json stop
python3 office.py --config .office/config.json status
```

The stop command targets the OFFICE instance managed by that config. Work already executing on a provider may continue. If this installation uses a separate tsnet phone frontend, record its run ID, stop that exact frontend, and verify its outcome. [Private frontend lifecycle](MOBILE-TSNET.md#stop-revoke-or-troubleshoot-without-disturbing-the-host-vpn)

## 2. Back up your private state and application

Once writers have stopped, copy the entire `.office/` folder to an owner-only backup outside the public repository. Include any external config or private data directory. The backup contains account and device identities. Keep it on storage you control. Record the current version and Git commit, or save a copy of the old program files. Use the existing config after upgrading. Running `init` again would create a different installation.

## 3. Update the program files

**Git on a tracking branch.** Run `git status --short` and save any local changes. Confirm the remote and current branch. For a clean checkout on the default tracking branch, run `git pull --ff-only`. If the histories have diverged, review both changes before proceeding.

**Git installed from a release tag.** In a clean checkout with the backup in place, fetch the tags and switch to the version the owner selected.

```sh
git fetch origin --tags
git switch --detach v0.2.2-preview.1
```

**ZIP installation.** Verify the new release ZIP against its matching SHA-256, extract it into a separate folder, and compare it with the installed copy. Update the program files at the original location. Preserve your private state, local changes and old program copy.

## 4. Rebuild the UI

The generated `public/` directory may contain a different build or custom assets. Move it to your backup outside the repository before rebuilding. With its old location clear, run the installer in the original installation folder.

```sh
python3 setup_ui.py
```

The installer fetches pinned upstream files, verifies their hashes and applies the bundled UI changes. If a check fails, resolve the reported error before starting the service. Keep the old UI available for recovery.

## 5. Prepare runtimes and verify the result

On Windows, use your working Python launcher or interpreter path. Keep the original config on every command.

```sh
python3 office.py --config .office/config.json setup-platforms
python3 office.py --config .office/config.json setup-platforms --check
python3 office.py --config .office/config.json start
python3 office.py --config .office/config.json status
python3 office.py --config .office/config.json url
```

The setup prepares the pinned Cursor SDK for this OFFICE and checks the Codex and Grok runtimes. Supply any missing official executables and repeat the check until `runtimes_ready=true`. Existing platform accounts and agent bindings belong to this installation. [Installation prerequisites](QUICKSTART.md#install-on-a-new-computer)

Open the private dashboard. Check the original roster, floors and conversations, then send a short message to a connected agent and confirm its reply. If OFFICE reports a migration or compatibility error, follow that error using the original state.

## Optional phone frontend

For an existing tsnet installation, rebuild and start its private binary with the original config, node identity, hostname and approved phone. [Upgrade the private frontend](MOBILE-TSNET.md#upgrade-an-existing-private-frontend)

For Tailscale Serve, keep your existing account and permissions. Refresh the phone once and check its login and history. Pair again if the login has expired or was revoked. [Mobile access](MOBILE-ACCESS.md)

## Roll back carefully

Stop this installation, preserve the newest data, and check whether the earlier program can read the current configuration and database. Restoring an old backup over active state can discard new conversations, permissions and revocations. Keep both copies until the recovery has been verified.
