"""Bounded, read-only Cursor public bubbles for verified native turns.

Cursor's flattened JSONL export combines text and thinking in some releases.
This reader deliberately uses only the structured public ``text`` field. It
never loads complete composer values, encrypted state, thinking, or tool bodies.
Native observer credentials and lifecycle remain the caller's responsibility.
"""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import urllib.parse

# Load the installation's exact codec. A third-party regular ``adapters``
# package can otherwise take precedence over our namespace-only directory.
_codec_spec = importlib.util.spec_from_file_location(
    '_office_cursor_public_codec', Path(__file__).resolve().parent / 'adapters/cursor_children.py')
_codec = importlib.util.module_from_spec(_codec_spec)
_codec_spec.loader.exec_module(_codec)
hook_id = _codec.hook_id

MAX_COMPOSER_BYTES = 8 * 1024 * 1024
MAX_BUBBLE_BYTES = 8 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
MAX_PUBLIC_CHARS = 65536
MAX_PUBLIC_TOTAL = 512 * 1024
MAX_CHILDREN = 256
MAX_HEADERS = 8192
MAX_CHUNKS = 32
NATIVE = re.compile(r'[A-Za-z0-9_.:@/-]{1,240}\Z')


def default_cursor_db_path(home=None, platform=None, environ=None):
    home = Path.home() if home is None else Path(home)
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    if platform == 'darwin':
        base = home / 'Library/Application Support/Cursor'
    elif platform == 'win32':
        roaming = environ.get('APPDATA')
        base = Path(roaming) / 'Cursor' if roaming else home / 'AppData/Roaming/Cursor'
    else:
        configured = environ.get('XDG_CONFIG_HOME')
        base = (Path(configured) if configured and Path(configured).is_absolute() else home / '.config') / 'Cursor'
    return base / 'User/globalStorage/state.vscdb'


def _native(value):
    return isinstance(value, str) and NATIVE.fullmatch(value) is not None


def _integer(value, low=0, high=MAX_HEADERS):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _bubble_id(value):
    if not isinstance(value, str) or not value or '\0' in value or len(value) > 512:
        return False
    try:
        return len(value.encode('utf-8', errors='strict')) <= 512
    except UnicodeEncodeError:
        return False


def _json(value, kind):
    try:
        decoded = json.loads(value) if isinstance(value, str) else None
        return decoded if isinstance(decoded, kind) else None
    except (TypeError, ValueError):
        return None


def _at(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def _safe_identity(path):
    """Reject symlink components and sidecars; do not follow arbitrary links."""
    if not path.is_absolute():
        return None
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            return None
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode)
            or hasattr(os, 'getuid') and info.st_uid != os.getuid()):
        return None
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink():
            return None
        if sidecar.exists():
            side_info = sidecar.stat()
            if (not stat.S_ISREG(side_info.st_mode)
                    or hasattr(os, 'getuid') and side_info.st_uid != os.getuid()):
                return None
    return info.st_dev, info.st_ino


_META = """SELECT
 json_extract(value,'$.composerId'),
 json_extract(value,'$.subagentInfo.parentComposerId'),
 json_extract(value,'$.subagentInfo.rootParentConversationId'),
 json_extract(value,'$.subagentInfo.rootParentRequestId'),
 json_extract(value,'$.subagentInfo.toolCallId'),
 json_extract(value,'$.subagentInfo.toolCallIdHistory'),
 json_extract(value,'$.subagentComposerIds'),
 json_extract(value,'$.headerLog.chunkSize'),
 json_extract(value,'$.headerLog.chunkCount'),
 json_extract(value,'$.headerLog.count'),
 json_extract(value,'$.headerLog.generation'),
 json_type(value,'$.headerLog'),
 json_type(value,'$.fullConversationHeadersOnly'),
 json_array_length(value,'$.fullConversationHeadersOnly')
 FROM cursorDiskKV WHERE key=? AND length(value)<=? AND json_valid(value)=1"""


class CursorPublicStore:
    def __init__(self, path=None):
        self.path = Path(path).expanduser().absolute() if path is not None else default_cursor_db_path()
        self._identity = None

    @staticmethod
    def _metadata(connection, conversation):
        row = connection.execute(_META, ('composerData:' + conversation, MAX_COMPOSER_BYTES)).fetchone()
        if row is None or row[0] != conversation:
            return None
        return dict(zip(('id', 'parent', 'root', 'root_run', 'call', 'call_history', 'children',
                         'chunk_size', 'chunk_count', 'count', 'generation', 'log_type',
                         'inline_type', 'inline_count'), row))

    @staticmethod
    def _lineage(meta, root_id, parent_id, parent_run_id, call):
        if (meta['parent'], meta['root'], meta['root_run']) != (parent_id, root_id, parent_run_id):
            return False
        if hook_id(meta['call']) == call:
            return True
        history = _json(meta['call_history'], list)
        return (history is not None and len(history) <= MAX_CHILDREN
                and any(hook_id(value) == call for value in history))

    @staticmethod
    def _header_rows(connection, key, expression, size_limit):
        # Only select header identity/type/public flags. A whole header can carry
        # unrelated summaries; do not return it from SQLite to the process.
        sql = """SELECT json_extract(j.value,'$.bubbleId'),
          json_extract(j.value,'$.type'),
          json_extract(j.value,'$.createdAt'),
          json_extract(j.value,'$.grouping.hasText'),
          json_extract(j.value,'$.grouping.isRenderable'),
          json_extract(j.value,'$.grouping.capabilityType'),
          json_extract(j.value,'$.grouping.hasThinking'),
          json_extract(j.value,'$.grouping.toolFormerTool'),
          json_extract(j.value,'$.grouping.hasServiceStatus'),
          json_extract(j.value,'$.grouping.hasEventNotification'),
          json_extract(j.value,'$.grouping.isSimulatedMsg')
          FROM cursorDiskKV k, json_each(EXPRESSION) j
          WHERE k.key=? AND length(k.value)<=? AND json_valid(k.value)=1
          AND json_type(j.value)='object' LIMIT ?""".replace('EXPRESSION', expression)
        rows = connection.execute(sql, (key, size_limit, MAX_HEADERS + 1)).fetchall()
        return [dict(zip(('id', 'type', 'at', 'has_text', 'renderable', 'capability',
                          'thinking', 'tool', 'service', 'event', 'simulated'), row)) for row in rows]

    def _headers(self, connection, meta):
        if meta['log_type'] is None:
            if meta['inline_type'] != 'array' or not _integer(meta['inline_count']):
                return None
            headers = self._header_rows(connection, 'composerData:' + meta['id'],
                                        "json_extract(k.value,'$.fullConversationHeadersOnly')", MAX_COMPOSER_BYTES)
            if len(headers) != meta['inline_count']:
                return None
        else:
            if (meta['log_type'] != 'object' or not _integer(meta['chunk_size'], 1, 1024)
                    or not _integer(meta['chunk_count'], 0, MAX_CHUNKS)
                    or not _integer(meta['count']) or not _native(meta['generation'])):
                return None
            headers = []
            for index in range(meta['chunk_count']):
                key = 'composerConvHeaders:' + meta['id'] + ':' + str(index).zfill(6)
                shape = connection.execute("SELECT json_type(value),json_array_length(value) FROM cursorDiskKV WHERE key=? AND length(value)<=? AND json_valid(value)=1", (key, MAX_CHUNK_BYTES)).fetchone()
                if shape is None or shape[0] != 'array' or not _integer(shape[1], 0, meta['chunk_size']):
                    return None
                chunk = self._header_rows(connection, key, 'k.value', MAX_CHUNK_BYTES)
                if len(chunk) != shape[1]:
                    return None
                headers.extend(chunk)
                if len(headers) > MAX_HEADERS:
                    return None
            if len(headers) != meta['count']:
                return None
        ids = [row['id'] for row in headers]
        if (len(headers) > MAX_HEADERS or any(not _bubble_id(value) for value in ids)
                or len(ids) != len(set(ids)) or any(row['type'] not in (1, 2) for row in headers)):
            return None
        return headers

    @staticmethod
    def _boundary(connection, conversation, header, call):
        if header['type'] != 1:
            return False
        row = connection.execute("""SELECT json_extract(value,'$.bubbleId'),
          json_extract(value,'$.type'),json_extract(value,'$.subagentSpawnTaskToolCallId')
          FROM cursorDiskKV WHERE key=? AND length(value)<=? AND json_valid(value)=1""",
          ('bubbleId:' + conversation + ':' + header['id'], MAX_BUBBLE_BYTES)).fetchone()
        return row is not None and row[0] == header['id'] and row[1] == 1 and hook_id(row[2]) == call

    @staticmethod
    def _root_boundary(connection, conversation, header, request):
        if header['type'] != 1:
            return False
        # Native Hook generation_id is the HUMAN bubble's requestId. The
        # unrelated headerLog.generation describes header storage, not a turn.
        # Project only identity fields; never load the HUMAN prompt or context.
        row = connection.execute("""SELECT json_extract(value,'$.bubbleId'),
          json_extract(value,'$.type'),json_extract(value,'$.requestId')
          FROM cursorDiskKV WHERE key=? AND length(value)<=? AND json_valid(value)=1
          AND json_type(value,'$.bubbleId')='text' AND json_type(value,'$.type')='integer'
          AND json_type(value,'$.requestId')='text'""",
          ('bubbleId:' + conversation + ':' + header['id'], MAX_BUBBLE_BYTES)).fetchone()
        return row is not None and row[0] == header['id'] and row[1] == 1 and row[2] == request

    @staticmethod
    def _public(connection, conversation, header):
        if (header['type'] != 2 or header['has_text'] != 1 or header['renderable'] != 1
                or header['capability'] not in (None, 0)
                or header['tool'] is not None
                or any(header[key] not in (None, 0) for key in ('thinking', 'service', 'event', 'simulated'))):
            return None
        # Predicates exclude private fields inside SQLite, before selecting text.
        sql = """SELECT json_extract(value,'$.bubbleId'),json_extract(value,'$.text'),
          json_extract(value,'$.createdAt') FROM cursorDiskKV
          WHERE key=? AND length(value)<=? AND json_valid(value)=1
          AND json_extract(value,'$.type')=2
          AND json_type(value,'$.text')='text'
          AND length(json_extract(value,'$.text'))<=?
          AND (json_type(value,'$.capabilityType') IS NULL OR
               (json_type(value,'$.capabilityType')='integer' AND json_extract(value,'$.capabilityType')=0))
          AND json_type(value,'$.thinking') IS NULL
          AND json_type(value,'$.toolFormerData') IS NULL
          AND json_type(value,'$.eventNotification') IS NULL
          AND json_type(value,'$.serviceStatusUpdate') IS NULL
          AND json_type(value,'$.simulatedMessageMetadata') IS NULL
          AND json_type(value,'$.errorDetails') IS NULL
          AND json_type(value,'$.conversationSummary') IS NULL
          AND COALESCE(json_extract(value,'$.isSimulatedMsg'),0)=0
          AND COALESCE(json_extract(value,'$.isPlaceholder'),0)=0"""
        row = connection.execute(sql, ('bubbleId:' + conversation + ':' + header['id'], MAX_BUBBLE_BYTES, MAX_PUBLIC_CHARS)).fetchone()
        if row is None or row[0] != header['id'] or not isinstance(row[1], str) or not row[1].strip():
            return None
        return {'id': row[0], 'text': row[1], 'at': _at(row[2]) or _at(header['at'])}

    def _snapshot(self, connection, root_id, parent_id, parent_run_id, call, child_hint, terminal):
        parent = self._metadata(connection, parent_id)
        if parent is None:
            return None
        if parent_id != root_id and (parent['root'], parent['root_run']) != (root_id, parent_run_id):
            return None
        listed = _json(parent['children'], list) or []
        candidates = [child_hint] if child_hint is not None else listed[-MAX_CHILDREN:]
        candidates = list(dict.fromkeys(value for value in candidates if _native(value)))
        matched = []
        lineage_matches = 0
        for conversation in candidates:
            if conversation in {root_id, parent_id}:
                continue
            meta = self._metadata(connection, conversation)
            if meta is None or not self._lineage(meta, root_id, parent_id, parent_run_id, call):
                continue
            lineage_matches += 1
            if lineage_matches > 1:
                return None  # A partial export must not resolve ambiguous IDs.
            headers = self._headers(connection, meta)
            if headers is None:
                continue
            boundaries = [index for index, row in enumerate(headers) if self._boundary(connection, conversation, row, call)]
            if len(boundaries) != 1:
                continue
            start = boundaries[0]
            end = next((index for index in range(start + 1, len(headers)) if headers[index]['type'] == 1), len(headers))
            span = headers[start + 1:end]
            messages = []
            total = 0
            for index, row in enumerate(span):
                message = self._public(connection, conversation, row)
                if message is None:
                    continue
                total += len(message['text'].encode('utf-8'))
                if total > MAX_PUBLIC_TOTAL:
                    return None
                message['closed'] = terminal is True or index < len(span) - 1
                if end < len(headers):
                    message['closed'] = True
                messages.append(message)
            final_id = None
            if terminal is True and span and messages and messages[-1]['id'] == span[-1]['id']:
                final_id = messages[-1]['id']
            matched.append({'conversation_id': conversation, 'boundary_id': headers[start]['id'],
                            'messages': messages, 'complete': True, 'final_id': final_id})
        return matched[0] if len(matched) == 1 else None

    def _root_snapshot(self, connection, conversation, request, terminal):
        meta = self._metadata(connection, conversation)
        if meta is None:
            return None
        headers = self._headers(connection, meta)
        if headers is None:
            return None
        boundaries = [index for index, row in enumerate(headers)
                      if self._root_boundary(connection, conversation, row, request)]
        if len(boundaries) != 1:
            return None
        start = boundaries[0]
        end = next((index for index in range(start + 1, len(headers))
                    if headers[index]['type'] == 1), len(headers))
        span = headers[start + 1:end]
        messages = []
        total = 0
        for index, row in enumerate(span):
            message = self._public(connection, conversation, row)
            if message is None:
                continue
            total += len(message['text'].encode('utf-8'))
            if total > MAX_PUBLIC_TOTAL:
                return None
            message['closed'] = terminal is True or index < len(span) - 1 or end < len(headers)
            messages.append(message)
        final_id = None
        if terminal is True and span and messages and messages[-1]['id'] == span[-1]['id']:
            final_id = messages[-1]['id']
        return {'conversation_id': conversation, 'boundary_id': headers[start]['id'],
                'messages': messages, 'complete': True, 'final_id': final_id}

    def snapshot(self, root_id, parent_id, parent_run_id, tool_call_id, child_hint=None, terminal=False):
        if (not all(_native(value) for value in (root_id, parent_id, parent_run_id, tool_call_id))
                or child_hint is not None and not _native(child_hint)):
            return None
        descriptor = None
        connection = None
        try:
            identity = _safe_identity(self.path)
            if identity is None:
                return None
            if self._identity is not None and self._identity != identity:
                self._identity = identity
                return None  # Re-establish exact native scope on the next poll.
            self._identity = identity
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != identity or not stat.S_ISREG(opened.st_mode):
                return None
            uri = 'file:' + urllib.parse.quote(str(self.path), safe='/:') + '?mode=ro'
            connection = sqlite3.connect(uri, uri=True, timeout=.1)
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            deadline = time.monotonic() + .35
            connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
            connection.execute('BEGIN')  # Consistent WAL snapshot; never immutable=1.
            schema = connection.execute("SELECT type FROM sqlite_master WHERE name='cursorDiskKV'").fetchone()
            if schema is None or schema[0] != 'table':
                return None
            result = self._snapshot(connection, root_id, parent_id, parent_run_id, tool_call_id, child_hint, terminal)
            if _safe_identity(self.path) != identity:
                return None
            return result
        except (OSError, ValueError, TypeError, sqlite3.Error, OverflowError):
            return None
        finally:
            if connection is not None:
                connection.close()
            if descriptor is not None:
                os.close(descriptor)

    def root_snapshot(self, conversation_id, request_id, terminal=False):
        """Public root bubbles fenced by one exact native HUMAN request.

        Caller supplies an already-bound observer conversation and an accepted
        native Hook generation. This method observes public text only; it never
        discovers enrollment, starts work, or declares a native turn complete.
        """
        if not all(_native(value) for value in (conversation_id, request_id)):
            return None
        descriptor = None
        connection = None
        try:
            identity = _safe_identity(self.path)
            if identity is None:
                return None
            if self._identity is not None and self._identity != identity:
                self._identity = identity
                return None
            self._identity = identity
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != identity or not stat.S_ISREG(opened.st_mode):
                return None
            uri = 'file:' + urllib.parse.quote(str(self.path), safe='/:') + '?mode=ro'
            connection = sqlite3.connect(uri, uri=True, timeout=.1)
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA trusted_schema=OFF')
            deadline = time.monotonic() + .35
            connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
            connection.execute('BEGIN')
            schema = connection.execute("SELECT type FROM sqlite_master WHERE name='cursorDiskKV'").fetchone()
            if schema is None or schema[0] != 'table':
                return None
            result = self._root_snapshot(connection, conversation_id, request_id, terminal)
            if _safe_identity(self.path) != identity:
                return None
            return result
        except (OSError, ValueError, TypeError, sqlite3.Error, OverflowError):
            return None
        finally:
            if connection is not None:
                connection.close()
            if descriptor is not None:
                os.close(descriptor)
