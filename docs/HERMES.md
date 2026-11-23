# Connect a native Hermes session

OFFICE uses the installed Hermes **ACP stdio** interface. Hermes runs the work and retains the native conversation. Python queues commands and displays public results. The chosen Hermes environment supplies its model, provider, key and approval settings.

## Create an ACP session

Set up Hermes on the host and verify `hermes acp --check`. From the OFFICE installation folder, run the following command.

```sh
python3 office.py connect-hermes --name "My Hermes" --workspace /absolute/path/to/project
```

The native API creates an ACP session and the command prints its actual ID. Registration sends no model prompt. Use `--binary /absolute/path/to/hermes` if the executable is outside PATH. Use `--hermes-home /absolute/path/to/profile` to select an existing configured profile.

If OFFICE is already running, finish active work, stop and restart that installation, then refresh the page. Click the avatar's name to send an instruction. Its workspace determines both its floor and Hermes's actual working directory.

## Bind an existing ACP session

```sh
python3 office.py connect-hermes --name "My existing Hermes" \
  --workspace /absolute/path/to/project --session ORIGINAL_ACP_SESSION_ID
```

Choose a session owned by the selected profile and currently unused by another client. Hermes must implement `session/load`. OFFICE locks that session within the profile against concurrent OFFICE use. Hermes v0.14.0 in the recorded test could load `source=acp` sessions. Interactive CLI, tmux or temporary helper sessions can have different identities.

## Execution, approvals and recovery

- OFFICE sends prompts sequentially on the native session ID and resumes context after restart through `session/load`. It keeps terminal and App windows in the background.
- A queue receipt records submission. Native updates or a correlated reply establish execution. The native turn response supplies the outcome.
- Public chunks update one message. A completed turn retains its delivery. Internal reasoning and raw tool arguments or output are excluded.
- A native approval request offers **Allow once** or **Deny** in the chat. It cannot grant permanent approval. A 45-second timeout, disconnect or denial does not authorize the operation.
- Each request ID dispatches once. A disconnect or unexpected OFFICE exit during execution leaves the outcome unconfirmed and blocks later sends for that session. Inspect the native session before reconciling the record.

The private outbox holds prompt text until dispatch. The Hermes profile retains its original history and OFFICE saves redacted public content. Local tools still have the profile's native authority. This adapter uses POSIX processes and file locks. Native macOS was tested. Native Windows execution is unsupported for this route. Install Hermes's ACP dependencies in its own environment.

## Use an existing launcher

Supply `--launch-file /absolute/path/to/argv.json` for an owner-configured local launcher. The JSON argument array must begin with the absolute path to an executable that provides Hermes ACP on stdin/stdout. Select its profile or model proxy locally. Web prompts cannot supply shell commands, models or credentials to this configuration.
