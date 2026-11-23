# Install Virtual Office on the user's computer

**For AI agents.** When a user gives you this repository and asks for an installation, use your local execution tools to install and start OFFICE on the computer they intend to use. All the instructions you need are in this repository. A user who only asks to read about the project is asking for information.

## Choose the installation

1. Check the target operating system, available Python and the installation folder. If you can only operate a remote sandbox, tell the user which machine you can reach. The host's private dashboard URL works on that host.
2. If you find an existing installation, `.office/`, a custom config or a running instance, identify its config and check its status. Reuse a healthy copy of the requested version. For an upgrade, follow [Upgrade an existing OFFICE](docs/UPGRADING.md) to back up private state, preserve active work and update in place.
3. For a fresh install, choose a dedicated writable folder. Use the release the user specified or the one linked at the top of the [README](README.md). For this release, run the following from its parent folder.

```sh
git clone --branch v0.2.2-preview.1 --depth 1 https://github.com/peipeichan605-cpu/virtual-office.git virtual-office
cd virtual-office
git rev-parse HEAD
```

If Git is unavailable, download the named ZIP and matching `.sha256` file from the [release page](https://github.com/peipeichan605-cpu/virtual-office/releases/tag/v0.2.2-preview.1). Compare the entire digest before extracting into a new directory. Use `Get-FileHash -Algorithm SHA256` on Windows, `shasum -a 256` on macOS or `sha256sum` on Linux. Work in the folder containing `office.py`. Record `VERSION` and the commit or ZIP digest.

## Install and start

Use Python 3.10 or later. On Windows, select a healthy x64 interpreter using `py -3` or a full path. Full setup also requires Node.js 22.13 or later and a compatible local Codex executable. The installer prepares the pinned Cursor SDK for this OFFICE, checks the packaged Grok SDK and reports missing dependencies. Supply missing prerequisites from their official sources. Let the user complete any required OS approval or account sign-in. [Platform prerequisites](docs/QUICKSTART.md)

Run each command after the previous one succeeds. On Windows replace `python3` with the chosen interpreter. Use 4318 if available, otherwise choose a free loopback port for `init`. Keep the same explicit config throughout.

```sh
python3 setup_ui.py
python3 office.py --config .office/config.json init --port 4318
python3 office.py --config .office/config.json setup-platforms
python3 office.py --config .office/config.json setup-platforms --check
python3 office.py --config .office/config.json start
python3 office.py --config .office/config.json status
python3 office.py --config .office/config.json url
```

After a partial installation, use the existing config and continue from the first unfinished step. If a Windows terminal reports that it will terminate background processes, use `Start Office.cmd` from Explorer for an initialized default installation, or start from a regular PowerShell with the same custom config. Check `status` again.

## Verify and hand over

- Confirm `ui_installed=true`, `runtimes_ready=true`, and then `state=running` and `local_ok=true`. A fresh installation has `selected=0`. Preserve the existing roster on upgrade.
- Open the private URL locally and verify that the 3D lobby renders. With terminal-only access, check HTTP on that port at `/office/` and tell the user that visual confirmation is pending.
- Report the version, installation folder, runtime checks, and how to reopen or stop the instance. The start receipt gives the run ID, log and exact stop command. Keep the private URL, `.office/` data, credentials and conversation contents on the owner's device.
- Let the user select agents from **Invite Agent**. OpenRouter uses the Python core and accepts a key in the local desktop form when selected. Verify a real prompt, public reply and delivery separately for each route being tested. Phone pairing is optional. [Phone setup](docs/MOBILE-QUICKSTART.md)

[Detailed installation checklist](INSTALL-PROMPT.txt) · [Upgrade an existing installation](docs/UPGRADING.md)
