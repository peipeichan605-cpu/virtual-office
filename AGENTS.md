# Install Virtual Office on the user's computer

When the user asks to install or start Virtual Office, carry out the following steps on the computer that will host it. This file is the installation guide for any local agent with a terminal. The repository link is sufficient to find it. Only browsing the project requires no changes to the computer.

## Choose the installation

- Start in the user's chosen workspace or an existing Virtual Office checkout. For a fresh install, choose a writable, dedicated folder. From its parent directory run the following command if the source is not already present. Use the version the user requested, or the release linked at the top of the README. Record the version from `VERSION` and the full Git commit.

  ```sh
  git clone --branch v0.2.2-preview --depth 1 https://github.com/peipeichan605-cpu/virtual-office.git virtual-office
  ```

- Without Git, download the named release ZIP and its matching `.sha256` file. Compare the entire SHA-256 before extracting to a new folder. Use `Get-FileHash -Algorithm SHA256` on Windows, `shasum -a 256` on macOS, or `sha256sum` on Linux. Open the folder containing `office.py`.
- If this is already an installation (including `.office/`, an explicitly configured data directory, or a running instance), use its original config to check its status. A healthy installation of the requested version can be reused. For an upgrade, follow the backup, in-place update and verification procedure. Preserve active work, private settings, conversations and device permissions. A checkout cloned from a release tag is detached. To upgrade it, fetch tags and check out the chosen release tag in the clean working tree after the backup. [Upgrade an existing OFFICE](docs/UPGRADING.md)

## Install and start a fresh OFFICE

On **Windows**, use a healthy x64 Python 3.10+ interpreter, such as `py -3` or the full path to `python.exe`. On **macOS and Linux**, use `python3` 3.10+. Full setup also needs Node.js 22.13+ and a compatible local Codex executable. The standard setup prepares the pinned Cursor SDK inside this OFFICE, checks the bundled Grok SDK and reports which dependencies need attention. When a prerequisite is missing, use the platform's official installation instructions and repeat the check. [Platform prerequisites and troubleshooting](docs/QUICKSTART.md)

From the folder containing `office.py`, run each command after the previous one succeeds. Replace `python3` with the interpreter identified above. Use 4318 if it is free or pass another available loopback port to `init`. Keep the same explicit config path throughout.

```sh
python3 setup_ui.py
python3 office.py --config .office/config.json init --port 4318
python3 office.py --config .office/config.json setup-platforms
python3 office.py --config .office/config.json setup-platforms --check
python3 office.py --config .office/config.json start
python3 office.py --config .office/config.json status
python3 office.py --config .office/config.json url
```

After a partial installation, resume from the first unfinished step using the existing config. If a Windows terminal reports that its parent will terminate background processes, use the supplied `Start Office.cmd` from Explorer for the initialized default installation, or the same config in a normal PowerShell terminal. Then check `status` again.

## Verify and hand over

1. Check that `setup-platforms --check` returns `runtimes_ready=true`. Check the same config's `status` for `state=running` and `local_ok=true`. A fresh installation has `selected=0`.
2. Open the private URL from `url` in the user's local browser and verify that the 3D lobby loads. If you only have terminal access, check HTTP on that port at `/office/` and report which visual check is still pending. Keep the private URL and all contents of `.office/` on the owner's device.
3. Give the user the version, install location and how to reopen or stop this instance. The precise stop command is in the start receipt. Agent selection and account sign-in happen later in **＋ 邀請 Agent**. OpenRouter uses the Python core and takes the user's key in the local UI when selected. A paired phone is optional. [Phone setup](docs/MOBILE-QUICKSTART.md)

For additional steps covering platform approvals, recovery and existing installations, read the detailed guide included in this checkout. [Detailed installation procedure in Traditional Chinese](INSTALL-PROMPT.txt)
