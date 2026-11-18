"""Explicit, read-only native proof for a legacy Cloud receipt lost after POST.

Only an owner-specified run is read. The exact original marker and keyed prompt
fingerprint must match before an existing uncertain command can gain a receipt.
This module never sends a prompt, retries delivery, or fabricates completion.
"""
import datetime as dt
import hashlib
import hmac
import http.client
import json
import sqlite3
import uuid

from history import timestamp
from platform_adapters import CURSOR_RUN, MARKER, RemoteError, cursor_agent_id, sse_events


class RecoveryError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _time(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, AttributeError, TypeError):
        raise RecoveryError('cursor_recovery_invalid_timestamp') from None


def _membership(commands, agent):
    definition = next((row for row in commands.config.get('agents', []) if row['id'] == agent), None)
    binding = commands.binding(agent)
    if not definition or not binding or binding.get('adapter') != 'cursor_cloud':
        raise RecoveryError('cursor_recovery_agent_not_selected')
    try:
        sid = cursor_agent_id(binding['session_id'])
    except (ValueError, KeyError):
        raise RecoveryError('cursor_recovery_agent_not_selected') from None
    if agent in commands.config.get('disabled_command_agents', []):
        raise RecoveryError('dispatch_disabled')
    event = None
    with commands.history.lock:
        if commands.history.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='office_membership_events'").fetchone():
            row = commands.history.db.execute('SELECT event_id FROM office_membership_events WHERE agent_id=? ORDER BY rowid DESC LIMIT 1', (agent,)).fetchone()
            event = row['event_id'] if row else None
    settings = commands.config.get('command_adapters', {}).get('cursor_cloud', {}).get('sessions', {}).get(sid)
    stamp = json.dumps({'definition': definition, 'binding': binding, 'settings': settings, 'event': event}, sort_keys=True, separators=(',', ':'))
    return sid, hashlib.sha256(stamp.encode()).hexdigest()


def _saved(commands, agent, native):
    if not commands.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursor_cloud_recoveries'").fetchone():
        return None
    row = commands.db.execute('''SELECT r.*,c.native_message_id,c.state FROM cursor_cloud_recoveries r
        JOIN commands c ON c.id=r.command_id WHERE r.agent_id=? AND r.native_run_id=?
        AND c.id NOT IN (SELECT command_id FROM command_detachments)''', (agent, native)).fetchone()
    return dict(row) if row and row['native_message_id'] == native else None


def _result(agent, native, command, proof, *, applied=False, duplicate=False, state='delivery_unknown'):
    return {'verified': True, 'applied': applied, 'duplicate': duplicate, 'agent_id': agent,
            'native_run_id': native, 'command_id': command, 'state': state,
            'proof_sha256': proof, 'provenance': 'cursor_cloud_legacy_marker',
            'ready_for_observation': applied or duplicate}


def recover_legacy_run(commands, agent_id, native_run_id, *, apply=False, transport=None):
    """Verify one legacy marked native run; optionally attach its lost receipt.

    Call on the running Bridge's Commands object, under the existing owner API.
    Default is a dry-run. A successful apply still needs normal exact-run
    observation before the command can become completed, failed, or cancelled.
    """
    if (not isinstance(agent_id, str) or not isinstance(native_run_id, str)
            or not CURSOR_RUN.fullmatch(native_run_id) or type(apply) is not bool):
        raise RecoveryError('invalid_cursor_recovery', 400)
    with commands.lock:
        sid, membership = _membership(commands, agent_id)
        adapter = commands.adapters.get('cursor_cloud')
        if not adapter or not adapter.available(sid):
            raise RecoveryError('cursor_recovery_adapter_unavailable')
        saved = _saved(commands, agent_id, native_run_id)
        if saved:
            if saved['membership_sha256'] != membership:
                raise RecoveryError('cursor_recovery_membership_changed')
            return _result(agent_id, native_run_id, saved['command_id'], saved['proof_sha256'], duplicate=True, state=saved['state'])
        candidates = {row['id']: dict(row) for row in commands.db.execute('''SELECT * FROM commands
            WHERE agent_id=? AND session_id=? AND adapter='cursor_cloud' AND state='delivery_unknown'
            AND native_message_id IS NULL AND run_id IS NULL
            AND id NOT IN (SELECT command_id FROM command_detachments)''', (agent_id, sid))}
        if not candidates:
            raise RecoveryError('cursor_recovery_no_uncertain_command')
    remote = transport or adapter.transport
    url = adapter.base + sid + '/runs/' + native_run_id
    connection = None
    try:
        native = remote.json('GET', url, adapter.headers(sid))
        if (native.get('id') != native_run_id or native.get('agentId') != sid or
                native.get('status') not in {'CREATING', 'QUEUED', 'RUNNING', 'WAITING_FOR_BACKGROUND_WORK', 'FINISHED', 'ERROR', 'CANCELLED', 'EXPIRED'}):
            raise RecoveryError('cursor_recovery_run_mismatch')
        created = _time(native.get('createdAt'))
        connection, response = remote.open('GET', url + '/stream', {**adapter.headers(sid), 'Accept': 'text/event-stream'})
        matched = None
        for count, (kind, value) in enumerate(sse_events(response, adapter.stop, duration=20), 1):
            if count > 512:
                raise RecoveryError('cursor_recovery_scan_limit')
            if value.get('runId', native_run_id) != native_run_id or value.get('agentId', sid) != sid:
                raise RecoveryError('cursor_recovery_run_mismatch')
            if kind != 'interaction_update' or value.get('type') != 'user-message-appended':
                continue
            message = value.get('userMessage')
            if not isinstance(message, dict) or message.get('type') != 'user_message' or message.get('session_id') != native_run_id:
                raise RecoveryError('cursor_recovery_prompt_mismatch')
            text = message.get('text')
            if not isinstance(text, str) or len(text.encode()) > 24500:
                raise RecoveryError('cursor_recovery_prompt_mismatch')
            marker = MARKER.match(text)
            # The first native user message must be the original marked POST,
            # not a later pasted quote or steer inside an unrelated run.
            if not marker or marker[1] not in candidates:
                raise RecoveryError('cursor_recovery_prompt_mismatch')
            command = candidates[marker[1]]
            try:
                if str(uuid.UUID(marker[1])) != marker[1]:
                    raise ValueError()
            except ValueError:
                raise RecoveryError('cursor_recovery_prompt_mismatch') from None
            body = text[marker.end():]
            if not commands.matches_fingerprint(agent_id, body, command['fingerprint']):
                raise RecoveryError('cursor_recovery_prompt_mismatch')
            # A lost HTTP receipt is recorded shortly after dispatch. Time is
            # only a rejection fence; marker + exact HMAC are the identity proof.
            dispatched = _time(command['updated_at'])
            skew = dt.timedelta(minutes=5)
            if abs(created - dispatched) > skew or created < _time(command['created_at']) - skew:
                raise RecoveryError('cursor_recovery_outside_dispatch_window')
            evidence = {'agent_id': agent_id, 'native_run_id': native_run_id, 'command_id': command['id'],
                        'native_created_at': native['createdAt'], 'prompt_fingerprint': command['fingerprint'],
                        'membership_sha256': membership, 'source': 'interaction_update/user-message-appended'}
            proof = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            matched = command, proof
            break
        if matched is None:
            raise RecoveryError('cursor_recovery_no_matching_prompt')
    except RemoteError as error:
        raise RecoveryError(error.code) from None
    except (OSError, http.client.HTTPException, KeyError, TypeError):
        raise RecoveryError('cursor_recovery_read_failed') from None
    finally:
        if connection:
            connection.close()
    command, proof = matched
    with commands.lock:
        if _membership(commands, agent_id) != (sid, membership) or commands.adapters.get('cursor_cloud') is not adapter or adapter.stop.is_set():
            raise RecoveryError('cursor_recovery_membership_changed')
        current = commands.db.execute('SELECT * FROM commands WHERE id=?', (command['id'],)).fetchone()
        if not current or dict(current) != command or commands.detached(command['id']):
            raise RecoveryError('cursor_recovery_command_changed')
        if commands.db.execute('SELECT 1 FROM commands WHERE agent_id=? AND native_message_id=?', (agent_id, native_run_id)).fetchone():
            raise RecoveryError('cursor_recovery_receipt_already_owned')
        # A previous observer may already have presented this native run with a
        # different local identity. Do not create duplicate deliveries by
        # relabelling it implicitly; a separate explicit history repair would
        # need to reconcile those records first. Projection also takes the
        # command lock, so it cannot race this check and receipt attachment.
        with commands.history.lock:
            projected = commands.history.db.execute('''SELECT 1 FROM runs WHERE agent_id=?
                AND source_ref=? AND run_id!=? LIMIT 1''', (agent_id, native_run_id, command['id'])).fetchone()
            if projected:
                raise RecoveryError('cursor_recovery_requires_history_reconciliation')
        if apply:
            with commands.db:
                commands.db.execute('''CREATE TABLE IF NOT EXISTS cursor_cloud_recoveries (
                    command_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, native_run_id TEXT NOT NULL,
                    proof_sha256 TEXT NOT NULL, membership_sha256 TEXT NOT NULL, recovered_at TEXT NOT NULL,
                    UNIQUE(agent_id,native_run_id))''')
                commands.db.execute('INSERT INTO cursor_cloud_recoveries VALUES (?,?,?,?,?,?)',
                                    (command['id'], agent_id, native_run_id, proof, membership, timestamp()))
                commands.db.execute('''UPDATE commands SET native_message_id=?,detail_code='legacy_receipt_recovered'
                    WHERE id=? AND state='delivery_unknown' AND native_message_id IS NULL AND run_id IS NULL''', (native_run_id, command['id']))
        return _result(agent_id, native_run_id, command['id'], proof, applied=apply)
