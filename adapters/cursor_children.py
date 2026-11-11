#!/usr/bin/env python3
"""Native Cursor parent/child hooks. No model API or desktop control.

Only the observer's selected native root and verified descendants are accepted.
Permission hooks always allow the original work, even if observation fails.
"""
from contextlib import contextmanager
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from history import redact

EVENTS = {'beforeSubmitPrompt', 'afterAgentResponse', 'stop', 'subagentStart',
          'subagentStop', 'preToolUse', 'postToolUse', 'postToolUseFailure'}
APPROVAL = {'subagentStart', 'preToolUse'}
MAX_INPUT = 1024 * 1024
MAX_STATE = 16 * 1024 * 1024
MAX_PENDING = 128
MAX_SEEN = 2048
MAX_INVOCATIONS = 128
MAX_TERMINAL_HISTORY = 128
MAX_ROOT_HISTORY = 32
MAX_OBSERVERS = 128
ACTIVE_INDEX = '.cursor-active.json'
ID = re.compile(r'[A-Za-z0-9_.:@/-]{1,200}\Z')
RUN = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
HOOK_ID_PREFIX = 'cursor:v1:'
MAX_OPAQUE_ID_BYTES = 172  # Prefix + unpadded base64url fits backend's 240 chars.


class HookError(ValueError):
    pass


def native_id(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def hook_id(value):
    """Lossless native callback identity; credential identities stay strict.

    Cursor can emit Task IDs containing LF. Do not strip, hash, normalize or
    mistake them for missing IDs. Reserve the encoded namespace even for raw
    strict IDs, so a native ID equal to an encoded value cannot collide.
    """
    if not isinstance(value, str) or not value or '\0' in value or len(value) > 200:
        return None
    if native_id(value) and not value.startswith(HOOK_ID_PREFIX):
        return value
    try:
        raw = value.encode('utf-8', errors='strict')
    except UnicodeEncodeError:
        return None
    if len(raw) > MAX_OPAQUE_ID_BYTES:
        return None
    return HOOK_ID_PREFIX + base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def run_id(value):
    return value if isinstance(value, str) and RUN.fullmatch(value) else None


def response_for(kind):
    return {'permission': 'allow'} if kind in APPROVAL else {'continue': True} if kind == 'beforeSubmitPrompt' else {}


def private_open(path, flags):
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise HookError('not_regular_file')
    if os.name != 'nt':
        os.fchmod(fd, 0o600)
    return fd


def endpoint(value, expected):
    try:
        url = urllib.parse.urlsplit(value)
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
                or url.username or url.password or url.path != expected
                or url.query or url.fragment):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HookError('invalid_observer_endpoint')
    return value


def load_observer(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise HookError('absolute_observer_required')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 32768 or os.name != 'nt' and info.st_mode & 0o077:
        raise HookError('observer_permissions')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd) as source:
        value = json.load(source)
    if not isinstance(value, dict) or value.get('provider') != 'cursor' or value.get('evidence') != 'native_hook':
        raise HookError('cursor_observer_required')
    if value.get('source_kind') not in {'cursor_hook', 'cursor_hooks'}:
        raise HookError('native_cursor_observer_required')
    for key in ('observer_id', 'agent_id', 'native_session_id', 'generation'):
        if not native_id(value.get(key)):
            raise HookError('invalid_observer_identity')
    token = value.get('token')
    if not isinstance(token, str) or not 16 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise HookError('invalid_observer_token')
    endpoint(value.get('endpoint'), '/api/agents/children/events')
    endpoint(value.get('root_event_endpoint'), '/api/agents/observers/root-events')
    a, b = urllib.parse.urlsplit(value['endpoint']), urllib.parse.urlsplit(value['root_event_endpoint'])
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        raise HookError('observer_origin_mismatch')
    return value


def fingerprint(observer):
    return hashlib.sha256(json.dumps([observer[key] for key in
        ('observer_id', 'agent_id', 'native_session_id', 'generation', 'token')]).encode()).hexdigest()


def prune_history(state):
    """Bound delivered history, while preserving every pending event and parent.

    The invocation limit concerns concurrent work, not membership lifetime.
    Small event receipts remain after history eviction so duplicate old Starts
    cannot recreate an active local child.
    """
    invocations = state['invocations']
    protected = {item['body'].get('child_run_id') for item in state['outbox']}
    protected.update(key for key, item in invocations.items() if not item['terminal'])
    pending_roots = {item['body'].get('run_id') for item in state['outbox'] if item['route'] == 'root'}
    todo = list(protected)
    while todo:
        item = invocations.get(todo.pop(), {})
        parent = item.get('parent_invocation')
        if parent and parent not in protected:
            protected.add(parent)
            todo.append(parent)
    terminals = [key for key, item in invocations.items() if item['terminal'] and key not in protected]
    keep = protected | set(terminals[-MAX_TERMINAL_HISTORY:])
    for key in list(invocations):
        if key not in keep:
            del invocations[key]
    for name in ('calls', 'child_generations', 'current'):
        state[name] = {key: value for key, value in state[name].items() if value in invocations}
    known = {item['sid'] for item in invocations.values()}
    state['children'] = {key: value for key, value in state['children'].items() if key in known}
    state['conversations'] = {key: value for key, value in state['conversations'].items() if value in known}
    roots = {item['parent_run'] for item in invocations.values()} | pending_roots
    roots.update(list(state['roots'])[-MAX_ROOT_HISTORY:])
    state['roots'] = {key: value for key, value in state['roots'].items() if key in roots}


class Journal:
    """Short file transactions; HTTP runs outside the state lock."""
    def __init__(self, directory, observer, deadline=None):
        self.directory = Path(directory)
        self.scope = fingerprint(observer)
        self.root = observer['native_session_id']
        self.deadline = deadline
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            os.chmod(self.directory, 0o700)
        self.path = self.directory / 'state.json'

    @contextmanager
    def lock(self, name, timeout=.8):
        if fcntl is None:
            raise HookError('platform_lock_unavailable')
        fd = private_open(self.directory / name, os.O_RDWR | os.O_CREAT)
        deadline = min(time.monotonic() + timeout, self.deadline or float('inf'))
        try:
            while True:
                if self.deadline and time.monotonic() > self.deadline:
                    raise HookError('observer_busy')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise HookError('observer_busy')
                    time.sleep(.01)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def transaction(self):
        with self.lock('state.lock'):
            if self.path.exists():
                if self.path.lstat().st_size > MAX_STATE:
                    raise HookError('observer_state_too_large')
                with os.fdopen(private_open(self.path, os.O_RDONLY)) as source:
                    state = json.load(source)
                if state.get('scope') != self.scope:
                    raise HookError('observer_generation_changed')
            else:
                state = {'version': 1, 'scope': self.scope, 'roots': {}, 'children': {},
                         'invocations': {}, 'calls': {}, 'conversations': {}, 'child_generations': {},
                         'current': {}, 'seen': {}, 'outbox': [], 'dead': [], 'blocked': False}
            yield state
            prune_history(state)
            while len(state['seen']) > MAX_SEEN:
                key = next((k for k, v in state['seen'].items() if v.get('delivered')), None)
                if key is None:
                    raise HookError('observer_journal_full')
                del state['seen'][key]
            raw = json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode()
            if len(raw) > MAX_STATE:
                raise HookError('observer_state_too_large')
            temporary = self.path.with_name('state.' + uuid.uuid4().hex + '.tmp')
            with os.fdopen(private_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as out:
                out.write(raw)
            os.replace(temporary, self.path)
            routing = {'version': 1, 'scope': self.scope,
                'state_mtime_ns': self.path.stat().st_mtime_ns,
                'natives': [self.root, *state['children'], *state['conversations']],
                'calls': list(state['calls'])}
            temporary = self.path.with_name('routing.' + uuid.uuid4().hex + '.tmp')
            with os.fdopen(private_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'w') as out:
                json.dump(routing, out, separators=(',', ':'))
            os.replace(temporary, self.directory / 'routing.json')


def event_id(key):
    return 'cursor-hook:' + hashlib.sha256(key.encode()).hexdigest()


def enqueue(state, route, body, key):
    ident = event_id(key)
    raw = dict(body, event_id=ident)
    signature = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = state['seen'].get(ident)
    if previous:
        if previous['fingerprint'] != signature:
            raise HookError('native_event_conflict')
        return
    if len(state['outbox']) >= MAX_PENDING:
        raise HookError('observer_journal_full')
    at = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    raw['at'] = at
    state['seen'][ident] = {'fingerprint': signature, 'at': at, 'delivered': False}
    state['outbox'].append({'route': route, 'body': raw})


class CursorMapper:
    def __init__(self, observer):
        self.observer = observer
        self.root = observer['native_session_id']

    def text(self, value):
        return redact(value, secrets=(self.observer['token'],), limit=23500)[:24000] if isinstance(value, str) else ''

    def child_for_native(self, state, native):
        if native in state['children']:
            return native
        return state['conversations'].get(native)

    def invocation(self, state, data):
        """Identity from native Task metadata, never names or task text."""
        conversation = hook_id(data.get('conversation_id'))
        generation = run_id(data.get('generation_id'))
        call = hook_id(data.get('parent_tool_call_id'))
        key = state['calls'].get(call)
        if key:
            item = state['invocations'][key]
            if conversation and conversation != self.root:
                previous = state['conversations'].get(conversation)
                if previous not in (None, item['sid']):
                    raise HookError('native_alias_conflict')
                state['conversations'][conversation] = item['sid']
                if generation:
                    native_generation = conversation + '|' + generation
                    old = state['child_generations'].get(native_generation)
                    if old not in (None, key):
                        raise HookError('native_generation_conflict')
                    state['child_generations'][native_generation] = key
                return key, item, {'kind': 'conversation', 'id': conversation, 'parent_tool_call_id': call}
        if conversation and generation:
            key = state['child_generations'].get(conversation + '|' + generation)
            if key:
                return key, state['invocations'][key], None
        # Exact native ID may identify a child; a known parent generation still
        # fences its run. No assumptions about different conversation IDs.
        sid = self.child_for_native(state, conversation)
        if sid and generation:
            matches = [(k, x) for k, x in state['invocations'].items()
                       if x['sid'] == sid and x['parent_run'] == generation]
            if len(matches) == 1:
                return matches[0][0], matches[0][1], None
        return None, None, None

    @staticmethod
    def child_body(item, kind, **fields):
        return {'native_child_id': item['sid'], 'native_parent_id': item['parent'],
                'parent_run_id': item['parent_run'], 'child_run_id': item['run'],
                'type': kind, **fields}

    def translate(self, state, data, event):
        if state.get('blocked'):
            return False
        conversation = hook_id(data.get('conversation_id'))
        generation = run_id(data.get('generation_id'))
        if not conversation or not generation or data.get('hook_event_name') != event:
            return False
        if event == 'beforeSubmitPrompt':
            if conversation != self.root:
                return False
            if generation not in state['roots'] and event_id('root:start:' + generation) in state['seen']:
                return False
            state['roots'].setdefault(generation, {'last_text': '', 'terminal': False})
            body = {'source_id': self.root, 'run_id': generation,
                    'type': 'turn_started', 'text': self.text(data.get('prompt', ''))}
            workspaces = data.get('workspace_roots')
            if (isinstance(workspaces, list) and len(workspaces) == 1
                    and isinstance(workspaces[0], str) and len(workspaces[0]) <= 4096
                    and not any(ord(c) < 32 for c in workspaces[0]) and Path(workspaces[0]).is_absolute()):
                body['workspace_path'] = workspaces[0]
            enqueue(state, 'root', body, 'root:start:' + generation)
            return True
        if event == 'subagentStart':
            parent = hook_id(data.get('parent_conversation_id'))
            parent_key, parent_alias = None, None
            if parent == self.root and conversation == self.root and generation in state['roots']:
                if state['roots'][generation]['terminal']:
                    return False
                parent_run = generation
            else:
                parent_key, parent_invocation, parent_alias = self.invocation(state, data)
                parent_sid = self.child_for_native(state, parent)
                if not parent_invocation or parent_invocation['terminal'] or parent_sid != parent_invocation['sid']:
                    return False
                parent_run = parent_invocation['parent_run']
                parent = parent_sid
            if parent_run not in state['roots']:
                return False
            sid, call = hook_id(data.get('subagent_id')), hook_id(data.get('tool_call_id'))
            if not sid or not call or sid == self.root or sid == parent:
                return False
            ownership = state['children'].get(sid)
            if ownership and ownership['parent'] != parent:
                raise HookError('native_parent_conflict')
            child_run = 'cursor-' + hashlib.sha256(json.dumps([parent_run, call, sid]).encode()).hexdigest()[:40]
            old_call = state['calls'].get(call)
            if old_call not in (None, child_run):
                raise HookError('native_tool_conflict')
            if child_run not in state['invocations']:
                if event_id('child:start:' + child_run) in state['seen']:
                    return False
                if sum(not item['terminal'] for item in state['invocations'].values()) >= MAX_INVOCATIONS:
                    raise HookError('observer_invocation_limit')
                active = state['invocations'].get(state['current'].get(sid), {})
                if active and not active['terminal']:
                    raise HookError('native_child_already_working')
                state['invocations'][child_run] = {'sid': sid, 'parent': parent,
                    'parent_run': parent_run, 'tool': call, 'run': child_run,
                    'parent_invocation': parent_key, 'terminal': False, 'last_text': ''}
            item = state['invocations'][child_run]
            state['children'][sid] = {'parent': parent}
            state['current'][sid] = child_run
            state['calls'][call] = child_run
            if parent_alias:
                enqueue(state, 'child', self.child_body(parent_invocation, 'message', role='assistant',
                    phase='commentary', text='', native_alias=parent_alias),
                    'child:alias:' + parent_key + ':' + parent_alias['id'])
            name = self.text(data.get('subagent_type'))[:80] or 'Cursor 子 Agent'
            enqueue(state, 'child', self.child_body(item, 'started', name=name,
                text=self.text(data.get('task')), native_alias={'kind': 'tool_call', 'id': call}),
                'child:start:' + child_run)
            return True

        key, child, alias = (None, None, None) if event == 'subagentStop' else self.invocation(state, data)
        if event in {'preToolUse', 'postToolUse', 'postToolUseFailure'}:
            if not child:
                # Root tool metadata is not dialogue and never finishes a run.
                return conversation == self.root and generation in state['roots']
            if alias and not child['terminal']:
                enqueue(state, 'child', self.child_body(child, 'message', role='assistant',
                    phase='commentary', text='', native_alias=alias),
                    'child:alias:' + key + ':' + alias['id'])
            return True
        if event == 'afterAgentResponse':
            text = self.text(data.get('text'))
            if not text:
                return False
            if child:
                child['last_text'] = text
                fields = {'role': 'assistant', 'phase': 'commentary', 'text': text}
                if alias:
                    fields['native_alias'] = alias
                enqueue(state, 'child', self.child_body(child, 'message', **fields),
                        'child:public:' + key + ':' + hashlib.sha256(text.encode()).hexdigest())
                return True
            if conversation == self.root and generation in state['roots']:
                root = state['roots'][generation]
                if root['terminal'] and root['last_text'] == text:
                    # stop already carried this body as the final delivery.
                    # A repeated native callback must not reuse its previous
                    # commentary event ID with a different final phase.
                    return True
                root['last_text'] = text
                enqueue(state, 'root', {'source_id': self.root, 'run_id': generation, 'type': 'message',
                    'role': 'assistant', 'phase': 'final' if root['terminal'] else 'commentary', 'text': text},
                    'root:public:' + generation + ':' + hashlib.sha256(text.encode()).hexdigest()
                    + (':final' if root['terminal'] else ''))
                return True
            return False
        if event == 'subagentStop':
            stop_conversation = hook_id(data.get('child_conversation_id'))
            stop_sid = hook_id(data.get('subagent_id'))
            stop_call = hook_id(data.get('tool_call_id'))
            # Quiet children may have no child-tool or public-response hook.
            # An explicit native SID with one matching parent generation, or
            # the exact Start Task ID, is enough. Child conversation alone is
            # never matched by task text, type, or a most-recent-child guess.
            context_key, context, _ = self.invocation(state, data)
            def same_context(candidate_key, candidate):
                return (conversation == self.root and candidate['parent_run'] == generation
                        or context_key and (candidate_key == context_key
                            or candidate.get('parent_invocation') == context_key))
            candidates = list(state['invocations'].items())
            if stop_call:
                candidates = [(k, item) for k, item in candidates if state['calls'].get(stop_call) == k]
            if stop_sid:
                candidates = [(k, item) for k, item in candidates if item['sid'] == stop_sid]
            if not stop_sid and not stop_call:
                owner = self.child_for_native(state, stop_conversation)
                if not owner:
                    return False
                candidates = [(k, item) for k, item in candidates if item['sid'] == owner]
            candidates = [(k, item) for k, item in candidates if same_context(k, item)]
            if len(candidates) != 1:
                return False
            key, child = candidates[0]
            if stop_sid and child['sid'] != stop_sid:
                return False
            if stop_conversation:
                owner = self.child_for_native(state, stop_conversation)
                if stop_conversation == self.root or owner not in (None, child['sid']):
                    return False
                if not owner and not (stop_sid or stop_call):
                    return False
                state['conversations'][stop_conversation] = child['sid']
                alias = {'kind': 'conversation', 'id': stop_conversation,
                         'parent_tool_call_id': child['tool']}
            status = {'completed': 'completed', 'error': 'failed', 'aborted': 'cancelled'}.get(data.get('status'))
            if not status:
                return False
            text = self.text(data.get('summary')) or child['last_text']
            fields = {'text': text}
            if alias:
                fields['native_alias'] = alias
            enqueue(state, 'child', self.child_body(child, status, **fields), 'child:stop:' + key)
            child['terminal'] = True
            return True
        if event == 'stop' and conversation == self.root and generation in state['roots']:
            status = {'completed': 'turn_completed', 'error': 'turn_failed', 'aborted': 'turn_cancelled'}.get(data.get('status'))
            if not status:
                return False
            enqueue(state, 'root', {'source_id': self.root, 'run_id': generation,
                'type': status, 'text': state['roots'][generation]['last_text']}, 'root:stop:' + generation)
            state['roots'][generation]['terminal'] = True
            return True
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post(observer, item, timeout=.6):
    target = observer['root_event_endpoint' if item['route'] == 'root' else 'endpoint']
    req = urllib.request.Request(target, data=json.dumps(item['body'], ensure_ascii=False).encode(),
        method='POST', headers={'Content-Type': 'application/json',
            'X-Observer-Id': observer['observer_id'], 'Authorization': 'Bearer ' + observer['token']})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=timeout) as response:
            value = json.loads(response.read(65537))
        if not isinstance(value, dict) or value.get('accepted') is not True:
            return 'pending', 'receipt_unconfirmed'
        return 'delivered', None
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 404}:
            return 'blocked', 'observer_not_active'
        if error.code in {400, 409}:
            return 'rejected', 'native_event_rejected'
        return 'pending', 'office_unavailable'
    except (ValueError, OSError, urllib.error.URLError):
        return 'pending', 'office_unavailable'


def drain(journal, observer, transport=post):
    try:
        with journal.lock('delivery.lock', timeout=0):
            deadline = min(time.monotonic() + 1.2, journal.deadline or float('inf'))
            for _ in range(8):
                with journal.transaction() as state:
                    if state['blocked'] or not state['outbox']:
                        return
                    item = dict(state['outbox'][0])
                if time.monotonic() >= deadline:
                    return
                outcome, reason = transport(observer, item, timeout=min(.6, max(.05, deadline - time.monotonic())))
                with journal.transaction() as state:
                    if not state['outbox'] or state['outbox'][0]['body']['event_id'] != item['body']['event_id']:
                        raise HookError('observer_journal_changed')
                    if outcome == 'delivered':
                        state['outbox'].pop(0)
                        state['seen'][item['body']['event_id']]['delivered'] = True
                    elif outcome == 'rejected':
                        state['outbox'].pop(0)
                        state['seen'][item['body']['event_id']]['delivered'] = True
                        state['dead'] = [*state['dead'], {'event_id': item['body']['event_id'], 'reason': reason}][-32:]
                    elif outcome == 'blocked':
                        state['blocked'] = True
                    state['last_error'] = reason
                if outcome not in {'delivered', 'rejected'}:
                    return
    except HookError:
        return


def handle(observer, directory, data, event, transport=post, deadline=None):
    journal = Journal(directory, observer, deadline)
    mapper = CursorMapper(observer)
    try:
        with journal.transaction() as state:
            known = mapper.translate(state, data, event)
    except HookError as error:
        if str(error) != 'observer_journal_full':
            raise
        # Try delivery before admitting this same callback again. This retries
        # only a local observation, never the agent's task or a model request.
        drain(journal, observer, transport)
        try:
            with journal.transaction() as state:
                known = mapper.translate(state, data, event)
        except HookError:
            with journal.transaction() as state:
                state['last_error'] = 'observer_journal_full'
                state['dropped_callbacks'] = state.get('dropped_callbacks', 0) + 1
            return
    if known:
        drain(journal, observer, transport)


def bind_pending(path, observer, data, event, deadline=None):
    """One native response echoes the invitation's single-use binding marker.

    The server rotates token/generation in the same local file. Onboarding's
    native turn is not synthesized as a working root or child run.
    """
    marker, text = observer.get('activation_marker'), data.get('text')
    native = native_id(data.get('conversation_id'))
    if (event != 'afterAgentResponse' or data.get('hook_event_name') != event
            or not isinstance(marker, str) or not marker or len(marker) > 128
            or not isinstance(text, str) or not native or native.startswith('pending:')
            or not re.search(r'(?<![A-Za-z0-9_])' + re.escape(marker) + r'(?![A-Za-z0-9_])', text)):
        return False
    origin = urllib.parse.urlsplit(observer['endpoint'])
    url = urllib.parse.urlunsplit((origin.scheme, origin.netloc, '/api/agents/observers/bind', '', ''))
    request = urllib.request.Request(url, data=json.dumps({'native_session_id': native,
        'activation_marker': marker}).encode(), method='POST', headers={
            'Content-Type': 'application/json', 'X-Observer-Id': observer['observer_id'],
            'Authorization': 'Bearer ' + observer['token']})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        remaining = min(.6, (deadline or float('inf')) - time.monotonic())
        if remaining <= .02:
            return False
        with opener.open(request, timeout=remaining) as response:
            value = json.loads(response.read(65537))
        if not isinstance(value, dict) or value.get('pending') is not False:
            return False
    except (ValueError, OSError, urllib.error.URLError):
        # A timed-out request may still have committed; only the atomically
        # rotated local credential can establish success here.
        pass
    try:
        current = load_observer(path)
        return current['observer_id'] == observer['observer_id'] and current['native_session_id'] == native and current['generation'] != observer['generation']
    except (ValueError, OSError):
        return False


def process_observer(path, data, event, state_directory=None, transport=post, deadline=None):
    observer = load_observer(path)
    if observer['native_session_id'].startswith('pending:'):
        return bind_pending(path, observer, data, event, deadline)
    directory = Path(state_directory) if state_directory else Path(path).parent / (observer['observer_id'] + '-cursor-state')
    conversation = hook_id(data.get('conversation_id'))
    if event in {'beforeSubmitPrompt', 'stop'} and conversation != observer['native_session_id']:
        return False
    # Do not create state or send anything for unrelated native conversations.
    if (conversation != observer['native_session_id']
            and hook_id(data.get('parent_conversation_id')) != observer['native_session_id']
            and not (directory / 'state.json').exists()):
        return False
    routing = directory / 'routing.json'
    if conversation != observer['native_session_id'] and routing.exists():
        if routing.lstat().st_size > 262144:
            return False
        with os.fdopen(private_open(routing, os.O_RDONLY)) as source:
            scope = json.load(source)
        if scope.get('scope') != fingerprint(observer):
            return False
        natives, calls = set(scope.get('natives', [])), set(scope.get('calls', []))
        if (scope.get('state_mtime_ns') == (directory / 'state.json').stat().st_mtime_ns
                and not any(hook_id(data.get(key)) in natives for key in ('conversation_id', 'parent_conversation_id',
                    'child_conversation_id', 'subagent_id'))
                and not any(hook_id(data.get(key)) in calls for key in ('tool_call_id', 'parent_tool_call_id'))):
            return False
    handle(observer, directory, data, event, transport, deadline)
    return True


def observer_paths(directory):
    """Use the server's private active list; never scan outside this directory."""
    directory = Path(directory).expanduser()
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise HookError('absolute_observer_directory_required')
    index = directory / ACTIVE_INDEX
    if index.exists() or index.is_symlink():
        info = index.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 32768
                or os.name != 'nt' and info.st_mode & 0o077):
            raise HookError('observer_index_permissions')
        fd = os.open(index, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd) as source:
            value = json.load(source)
        if (not isinstance(value, dict) or value.get('version') != 1
                or not isinstance(value.get('credentials'), list)
                or len(value['credentials']) > MAX_OBSERVERS):
            raise HookError('invalid_observer_index')
        names = value['credentials']
        if any(not isinstance(name, str) or not re.fullmatch(r'[a-f0-9]{32}\.json', name) for name in names) or len(set(names)) != len(names):
            raise HookError('invalid_observer_index')
        return [directory / name for name in names]
    # Compatibility for installations created before the active index existed.
    # Retired credentials cannot take precedence over the newest join.
    paths = [entry for entry in directory.iterdir() if not entry.name.startswith('.')
             and entry.suffix == '.json' and not entry.is_symlink() and entry.is_file()]
    return sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True)[:MAX_OBSERVERS]


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--observer', type=Path)
    source.add_argument('--observer-dir', type=Path)
    parser.add_argument('--event', required=True, choices=sorted(EVENTS))
    parser.add_argument('--state-dir', type=Path)
    args = parser.parse_args()
    deadline = time.monotonic() + 2.4
    try:
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) <= MAX_INPUT:
            data = json.loads(raw)
            if isinstance(data, dict):
                if args.observer:
                    process_observer(args.observer, data, args.event, args.state_dir, deadline=deadline)
                else:
                    for path in observer_paths(args.observer_dir):
                        if time.monotonic() >= deadline:
                            break
                        try:
                            process_observer(path, data, args.event, deadline=deadline)
                        except Exception:
                            pass
    except Exception:
        # Hook observation never blocks original work or prints private inputs.
        pass
    print(json.dumps(response_for(args.event)), flush=True)


if __name__ == '__main__':
    main()
