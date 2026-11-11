"""Selected-Agent cooperative reporting setup; no native-listener claims."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.parse
import uuid

from configuration import ROOT
from reporting_policy import grok_public_reporting_policy

POLICY_VERSION = 'grok-public-reporting-v2'
EVIDENCE = 'agent_reported_saved'
CONFIRM_FIELDS = {'setup_id', 'policy_version', 'policy_hash', 'readback_agent_id',
                  'readback_policy_version', 'evidence'}


class ReportingError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def private_json(path):
    """Read only a named private regular file; reject links and loose modes."""
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
            raise ReportingError('reporting_file_not_private', 409)
        if hasattr(os, 'getuid') and info.st_uid != os.getuid():
            raise ReportingError('reporting_file_not_private', 409)
        fd = os.open(path, flags)
        with os.fdopen(fd, encoding='utf-8') as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ReportingError('reporting_file_changed', 409)
            value = json.load(source)
        if not isinstance(value, dict):
            raise ReportingError('reporting_file_invalid', 409)
        return value
    except ReportingError:
        raise
    except (OSError, ValueError, TypeError):
        raise ReportingError('reporting_file_unavailable', 409)


def event_endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
                or parsed.path != '/api/agents/events' or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port is None
                or not 1 <= parsed.port <= 65535):
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ReportingError('reporting_endpoint_invalid', 409)


class PublicReporting:
    """No constructor/status mutation. Only explicit ensure stages local setup."""
    def __init__(self, bridge):
        self.bridge = bridge

    @property
    def db(self):
        return self.bridge.history.db

    def _schema(self):
        self.db.execute('''CREATE TABLE IF NOT EXISTS public_reporting_setups (
            agent_id TEXT PRIMARY KEY, generation TEXT NOT NULL, setup_id TEXT NOT NULL,
            policy_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, confirmed REAL,
            evidence TEXT)''')

    def _row(self, agent_id):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_reporting_setups'").fetchone():
            return None
        row = self.db.execute('SELECT * FROM public_reporting_setups WHERE agent_id=?', (agent_id,)).fetchone()
        return dict(row) if row else None

    def _context(self, agent_id):
        agent = self.bridge.definitions.get(agent_id)
        if not agent:
            raise ReportingError('agent_not_selected', 404)
        if agent.get('provider') != 'grok_bot' or agent.get('driver') != 'push':
            raise ReportingError('reporting_platform_unsupported', 409)
        source = agent.get('source_id', '')
        if not re.fullmatch(r'inv-[0-9a-f]{32}', source) or agent_id != 'grok_bot:' + source:
            raise ReportingError('reporting_original_credential_unknown', 409)
        directory = Path(self.bridge.config['data_dir']).resolve()
        invitation = directory / 'invitations' / source[4:]
        # Never follow a substituted invitation directory to another identity.
        if (directory / 'invitations').is_symlink() or invitation.is_symlink():
            raise ReportingError('reporting_credential_path_invalid', 409)
        credential_path = invitation / 'credential.json'
        credential = private_json(credential_path)
        token = credential.get('token')
        if (credential.get('agent_id') != agent_id or credential.get('source_id') != source
                or not isinstance(token, str) or not token
                or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), agent.get('token_sha256', ''))):
            raise ReportingError('reporting_credential_invalid', 409)
        endpoint = event_endpoint(credential.get('endpoint'))
        if endpoint.port != int(self.bridge.config['port']):
            raise ReportingError('reporting_endpoint_invalid', 409)
        policy = grok_public_reporting_policy()
        policy_hash = hashlib.sha256(json.dumps({'version': POLICY_VERSION, 'policy': policy},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        folder = directory / 'reporting' / hashlib.sha256(agent_id.encode()).hexdigest()[:32]
        if (directory / 'reporting').is_symlink() or folder.is_symlink():
            raise ReportingError('reporting_manifest_path_invalid', 409)
        generation = hashlib.sha256(json.dumps({'agent_id': agent_id, 'source_id': source,
            'token_sha256': agent['token_sha256'], 'credential_path': str(credential_path),
            'reporter': str(ROOT / 'adapters/emit.py'), 'python': sys.executable},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'agent_id': agent_id, 'source_id': source, 'credential_path': str(credential_path),
            'folder': folder, 'manifest_path': str(folder / 'reporting.json'),
            'guide_path': str(folder / 'public-sync-guide.txt'), 'policy': policy,
            'policy_version': POLICY_VERSION, 'policy_hash': policy_hash, 'generation': generation,
            'confirmation_endpoint': urllib.parse.urlunsplit(endpoint._replace(path='/api/agents/reporting/setup'))}

    @staticmethod
    def _current(row, context):
        return bool(row and all(row[key] == context[key] for key in ('generation', 'policy_version', 'policy_hash')))

    @staticmethod
    def _public(context, row=None, state=None, reason=None):
        return {'agent_id': context['agent_id'], 'method': 'cooperative',
            'state': state or (row['state'] if row else 'setup_required'),
            'manifest_path': context.get('manifest_path'), 'guide_path': context.get('guide_path'),
            'policy_version': context.get('policy_version', POLICY_VERSION), 'policy_hash': context.get('policy_hash'),
            'setup_id': row['setup_id'] if row else None,
            'evidence': row.get('evidence') if row and (state or row['state']) == 'agent_confirmed' else None,
            'confirmed_at': row.get('confirmed') if row and (state or row['state']) == 'agent_confirmed' else None,
            'reason': reason, 'native_observer': False, 'manual_work_verified': False}

    def status(self, agent_id):
        with self.bridge.commands.lock, self.bridge.history.lock:
            row = self._row(agent_id)
            try:
                context = self._context(agent_id)
            except ReportingError as error:
                state = 'revoked' if error.code == 'agent_not_selected' else 'unsupported' if error.code in {
                    'reporting_platform_unsupported', 'reporting_original_credential_unknown'} else 'stale'
                return self._public({'agent_id': agent_id}, state=state, reason=error.code)
            if row and not self._current(row, context):
                return self._public(context, row, state='stale', reason='reporting_binding_changed')
            if row:
                try:
                    manifest = private_json(context['manifest_path'])
                    expected, _ = self._files(context, row)
                    if manifest != expected:
                        raise ReportingError('reporting_manifest_changed', 409)
                except ReportingError as error:
                    return self._public(context, row, state='stale', reason=error.code)
            return self._public(context, row)

    @staticmethod
    def _write(path, text):
        path = Path(path)
        for directory in (path.parent.parent, path.parent):
            if directory.is_symlink():
                raise ReportingError('reporting_manifest_path_invalid', 409)
            directory.mkdir(exist_ok=True, mode=0o700)
            if directory.stat().st_mode & 0o077:
                raise ReportingError('reporting_directory_not_private', 409)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ReportingError('reporting_file_not_private', 409)
            if path.read_text() == text:
                return
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'w') as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _files(self, context, row):
        agent_id = context['agent_id']
        memory_rule = (
            '這是目前 Bot 自己的 Virtual Office 長期公開回報規則。'
            f'適用身份={agent_id}；規則版本={POLICY_VERSION}；policy_hash={context["policy_hash"]}；setup_id={row["setup_id"]}。'
            f'每次收到人類在 Grok App 的新訊息，包括普通對話、提問與工作指示，先讀取本機 manifest：{context["manifest_path"]}，'
            '沿用其 reporter 及自己的 event 憑證檔，先回報公開任務開始並確認 accepted=true，然後才開始處理工作；'
            '工作期間即時回報實際已向人類發出的公開進度，真正完成後才回報交付；不能事後批次補齊假裝即時。毋須人類再提醒 OFFICE。'
            '當次用戶訊息必須完整原文回報，只遮蓋秘密，不可摘要、改寫或刪去資料表；公開進度與最終回覆也保留完整公開原文。'
            'OFFICE 派工沿用 command_id，不能重複開新工作。只回報公開文字，不傳內部推理、工具原文或秘密。'
            '只適用目前已入選 Bot；登出或身份撤銷即停止，不能改接其他身份。保留其他記憶與批准規則。')
        manifest = {'version': 1, 'kind': 'office_public_reporting', 'agent_id': agent_id,
            'source_id': context['source_id'], 'provider': 'grok_bot', 'setup_id': row['setup_id'],
            'policy_version': POLICY_VERSION, 'policy_hash': context['policy_hash'],
            'python': sys.executable, 'reporter': str(ROOT / 'adapters/emit.py'),
            'credential_path': context['credential_path'], 'guide_path': context['guide_path'],
            'setup_helper': str(ROOT / 'adapters/reporting_setup.py'),
            'confirmation_endpoint': context['confirmation_endpoint'], 'memory_rule': memory_rule,
            'method': 'cooperative', 'native_observer': False}
        confirmation = [sys.executable, str(ROOT / 'adapters/reporting_setup.py'), '--manifest',
            context['manifest_path'], 'confirm', '--readback-version', POLICY_VERSION, '--readback-agent', agent_id]
        guide = ('Virtual Office：完成目前 Bot 的持續公開回報設定\n\n'
            '此步驟屬於同一份入場流程；不要重新入場、重建 Webhook 或向人類索取另一份 Prompt。\n'
            '1. 讀取下列 manifest，保留目前 Bot、聊天、專案、模型及批准規則：\n'
            + context['manifest_path'] + '\n\n'
            '2. 必須使用目前 Bot 平台的長期記憶功能，保存下面這一條規則；只更新這項 OFFICE 規則，不清除其他記憶，不套用到所有 Bot：\n'
            + memory_rule + '\n\n'
            '3. 保存後讀回目前 Bot 記住的內容，核對適用身份、規則版本、manifest 路徑及回報順序。'
            '不能只回覆「我記住了」。若平台未能保存或讀回，停在 setup_required 並清楚說明，不能執行下面確認命令。\n'
            '4. 真正完成保存與讀回後，使用原生工具以 argv 陣列執行以下命令；回傳 agent_confirmed 才表示這次合作設定已登記：\n'
            + json.dumps(confirmation, ensure_ascii=False) + '\n\n'
            '這份設定回條只屬 agent_reported_saved，不代表原生監聽器，也不證明後續 App 工作已同步。'
            '後續人類直接交代普通工作時，依這條長期規則執行，不能等人類再次提醒 OFFICE。\n\n'
            + context['policy'] + '\n公開回報工具綁定（憑證只由程式讀取，不可貼出內容）：\n'
            + json.dumps({'python': sys.executable, 'reporter': manifest['reporter'],
                          'credential': context['credential_path']}, ensure_ascii=False) + '\n'
            '每項 App 直接工作使用新的 UUID 作 run_id；開始用 turn_started，已公開進度用 message --phase commentary，'
            '完成用 turn_completed，失敗／取消用相應終態。使用 --text-stdin 及 argv/input 傳文字，不能拼接 shell。\n')
        return manifest, guide

    def ensure(self, agent_id):
        with self.bridge.commands.lock, self.bridge.history.lock:
            context = self._context(agent_id)
            with self.db:
                self._schema()
                row = self._row(agent_id)
                if not self._current(row, context):
                    row = {'agent_id': agent_id, 'generation': context['generation'], 'setup_id': uuid.uuid4().hex,
                        'policy_version': POLICY_VERSION, 'policy_hash': context['policy_hash'],
                        'state': 'setup_required', 'created': time.time(), 'confirmed': None, 'evidence': None}
                    self.db.execute('''INSERT INTO public_reporting_setups VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(agent_id) DO UPDATE SET generation=excluded.generation,setup_id=excluded.setup_id,
                        policy_version=excluded.policy_version,policy_hash=excluded.policy_hash,state=excluded.state,
                        created=excluded.created,confirmed=NULL,evidence=NULL''', tuple(row.values()))
                manifest, guide = self._files(context, row)
                self._write(context['manifest_path'], json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
                self._write(context['guide_path'], guide)
            return self._public(context, row)

    def confirm(self, agent_id, data):
        if not isinstance(data, dict) or set(data) != CONFIRM_FIELDS:
            raise ReportingError('invalid_reporting_confirmation')
        with self.bridge.commands.lock, self.bridge.history.lock:
            context = self._context(agent_id)
            row = self._row(agent_id)
            if not self._current(row, context):
                raise ReportingError('reporting_setup_stale', 409)
            expected = {'setup_id': row['setup_id'], 'policy_version': POLICY_VERSION,
                'policy_hash': context['policy_hash'], 'readback_agent_id': agent_id,
                'readback_policy_version': POLICY_VERSION, 'evidence': EVIDENCE}
            if any(not isinstance(data[key], str) or not hmac.compare_digest(data[key].encode(), value.encode())
                   for key, value in expected.items()):
                raise ReportingError('reporting_confirmation_mismatch', 409)
            # A missing or substituted manifest cannot be silently blessed.
            manifest = private_json(context['manifest_path'])
            expected_manifest, _ = self._files(context, row)
            if manifest != expected_manifest:
                raise ReportingError('reporting_manifest_changed', 409)
            if row['state'] != 'agent_confirmed':
                with self.db:
                    self.db.execute("UPDATE public_reporting_setups SET state='agent_confirmed',confirmed=?,evidence=? WHERE agent_id=? AND setup_id=?",
                        (time.time(), EVIDENCE, agent_id, row['setup_id']))
            return self._public(context, self._row(agent_id))
