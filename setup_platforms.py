"""Prepare installation-owned runtimes without logging in or creating Agents.

The check path never installs dependencies or starts a service. Native probes
only inspect runtime versions/help; they do not query accounts or models.
"""
import time


def _result(ready, reason, next_step, **metadata):
    return {'runtime_status': 'ready' if ready else 'missing', 'ready': bool(ready),
            'reason': reason, 'next_step': next_step,
            'login': {'status': 'not_checked', 'next_step': 'user_action_required'},
            **metadata}


def check_cursor(config):
    # Inspect the environment Office start/serve would use, not this CLI's Python.
    from cursor_sdk_setup import SDK_VERSION, _owned_python, probe_interpreter
    candidate = _owned_python(config)
    if candidate is None:
        return _result(False, 'cursor_sdk_required', 'run_setup_platforms', sdk_version=None)
    if not probe_interpreter(candidate, sdk=True):
        return _result(False, 'cursor_sdk_environment_unavailable',
                       'repair_managed_cursor_environment', sdk_version=None)
    return _result(True, None, 'review_login_in_office', sdk_version=SDK_VERSION)


def check_grok(config):
    try:
        # Keep SDK imports out of CLI startup and Cursor's installer path.
        from grok_sdk import runtime_binary, _vendor_valid, WORKER, SDK_VERSION
        settings = config.get('command_adapters', {}).get('grok_sdk', {})
        runtime_binary(settings)
        if not WORKER.is_file() or not _vendor_valid():
            return _result(False, 'grok_sdk_package_incomplete',
                           'restore_official_office_package', minimum_node_version='22.13.0')
        return _result(True, None, 'review_login_in_office',
                       minimum_node_version='22.13.0', sdk_version=SDK_VERSION)
    except Exception as error:
        if getattr(error, 'code', None) == 'grok_sdk_runtime_required':
            return _result(False, 'grok_sdk_runtime_required',
                           'install_official_node_22_13_or_newer', minimum_node_version='22.13.0')
        return _result(False, 'grok_runtime_check_failed',
                       'check_python_and_grok_runtime', minimum_node_version='22.13.0')


def check_codex(config):
    try:
        from codex_rpc import (resolve_binary, runtime_info,
                               _desktop_server_available, PROBE_TIMEOUT)
        deadline = time.monotonic() + PROBE_TIMEOUT
        settings = config.get('command_adapters', {}).get('codex_background', {})
        binary = resolve_binary(settings, deadline=deadline)
        # resolve_binary also accepts explicitly selected/PATH CLI binaries.
        # Verify those support the same stdio interface before declaring ready.
        if not _desktop_server_available(binary, deadline=deadline):
            return _result(False, 'codex_app_server_unavailable',
                           'install_or_update_official_codex_runtime')
        source = runtime_info(binary).get('source')
        return _result(True, None, 'review_login_in_office',
                       runtime_source=source if source in {'desktop_app', 'cli'} else None)
    except Exception as error:
        reason = getattr(error, 'code', None)
        if reason not in {'codex_binary_unavailable', 'codex_rpc_timeout',
                          'codex_native_executable_required', 'codex_process_unavailable'}:
            reason = 'codex_runtime_check_failed'
        return _result(False, reason, 'install_or_update_official_codex_runtime')


def _cursor_setup_failure(error):
    # The existing installer raises bounded local exceptions, some before it
    # can create a log. Classify known failures without returning raw messages.
    text = str(error)
    if isinstance(error, ValueError):
        if text.startswith('A healthy Python 3.10+ is needed'):
            return 'cursor_python_required', 'select_existing_python_3_10_or_newer_windows_x64'
        if text.startswith('Install Python 3.10+ or uv,'):
            return 'cursor_python_download_tool_required', 'install_official_python_or_uv_then_retry'
        if text.startswith('Cursor SDK runtime metadata is not owned by this installer;'):
            return 'cursor_sdk_runtime_metadata_conflict', 'inspect_existing_runtime_metadata_without_deleting_it'
    if isinstance(error, OSError):
        return 'cursor_sdk_setup_io_failed', 'check_installation_write_access_and_local_setup_log'
    return 'cursor_sdk_setup_failed', 'inspect_local_cursor_setup_log_and_retry'


def prepare_platforms(config, *, check=False, python=None, download_python=False,
                      on_started=None):
    """Default full runtime preparation; --check is a read-only verification.

    Node/Codex installation and all account login remain explicit installer/user
    actions. Existing configuration, Agent membership and services are untouched.
    """
    if check and (python is not None or download_python):
        raise ValueError('--check cannot be combined with installation options')
    setup_failure = None
    if not check:
        from cursor_sdk_setup import setup_environment
        try:
            setup_environment(config, python, download_python, on_started=on_started)
        except (OSError, ValueError, RuntimeError) as error:
            setup_failure = _cursor_setup_failure(error)
    cursor = check_cursor(config)
    if setup_failure:
        cursor = _result(False, *setup_failure, sdk_version=None)
    platforms = {'cursor': cursor, 'grok': check_grok(config), 'codex': check_codex(config)}
    return {'command': 'setup-platforms', 'mode': 'check' if check else 'setup',
            'runtimes_ready': all(item['ready'] for item in platforms.values()),
            'platforms': platforms, 'accounts_checked': False,
            'native_conversations_created': False, 'service_restarted': False}
