"""Public observation scope is independent of Office's outbound command route.

These descriptors identify the implemented producer; they are not proof that a
new manual App turn was observed. Cooperative reporting is never an automatic
native subscription, and an observer for one Office-created run does not cover
unrelated work in the original platform.
"""


def descriptor(mode, scope, state, configured=False, automatic=False,
               manual_app_automatic=False, reason=None):
    return {'mode': mode, 'scope': scope, 'state': state,
            'configured': configured, 'automatic': automatic,
            'manual_app_automatic': manual_app_automatic,
            'public_only': True, 'reason': reason}


def capability(config, agent_id, binding=None, observer=None):
    """Describe only the selected identity; do not probe Apps or credentials.

    ``observer(agent_id) is True`` must mean the host has checked an active,
    non-pending native Cursor observer in the current membership generation.
    A truthy record, pending installation, or callback failure is not enough.
    Disabling outbound work leaves observation scope unchanged.
    """
    definition = next((a for a in config.get('agents', []) if a.get('id') == agent_id), None)
    if not definition:
        return descriptor('unsupported', 'none', 'unsupported', reason='agent_not_selected')
    binding = binding or {}
    adapter = binding.get('adapter')
    driver = definition.get('driver')
    if driver == 'codex' or (definition.get('provider') == 'codex'
                             and adapter == 'codex_app' and binding.get('session_id')):
        return descriptor('native_observer', 'selected_conversation', 'configured',
                          configured=True, automatic=True, manual_app_automatic=True)
    if definition.get('provider') == 'cursor' and driver == 'push':
        active = False
        if callable(observer):
            try:
                active = observer(agent_id) is True
            except Exception:
                active = False
        return descriptor('native_hook', 'selected_conversation',
                          'configured' if active else 'setup_required',
                          configured=active, automatic=active, manual_app_automatic=active,
                          reason=None if active else 'native_observer_not_bound')
    if adapter in {'hermes_acp', 'cursor_cloud'}:
        return descriptor('native_run_observer', 'office_dispatched_runs', 'limited',
                          configured=True, automatic=True, reason='office_runs_only')
    if driver == 'crouton':
        return descriptor('native_run_observer', 'selected_runtime_runs', 'limited',
                          configured=bool(config.get('crouton_root')),
                          automatic=bool(config.get('crouton_root')), reason='selected_runtime_only')
    return descriptor('cooperative', 'reported_runs', 'cooperative', configured=True,
                      reason='agent_reports_required')
