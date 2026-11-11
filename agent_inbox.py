"""Provider-neutral pull channel. A pending message never implies a live agent.

Only the selected agent's scoped credential can receive or acknowledge its work.
An exposed delivery is never reassigned automatically, including after restart.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import sys
import threading
import time
import uuid
from configuration import ROOT
from history import timestamp
from reporting_policy import grok_public_reporting_policy
from grok_onboarding import grok_connection_handoff, grok_connection_hint
from public_reporting import ReportingError


class InboxError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9-]{36}', value):
        raise InboxError('invalid_inbox_request')
    try:
        if str(uuid.UUID(value)) != value:raise ValueError()
    except ValueError:raise InboxError('invalid_inbox_request')
    return value


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:output.write(value)
    tmp.replace(path)


class Inbox:
    def __init__(self, bridge):
        self.bridge, self.commands = bridge, bridge.commands
        self.root = Path(bridge.config['data_dir']) / 'channels'
        self.waiters = {}  # Only an actually blocked request indicates listening.
        self.waiters_lock = threading.RLock()
        with self.commands.lock, self.commands.db:
            self.commands.db.execute('CREATE TABLE IF NOT EXISTS inbox_credentials (agent_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL)')
            self.commands.db.execute('''CREATE TABLE IF NOT EXISTS inbox_deliveries (
                command_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, receiver_id TEXT NOT NULL,
                request_id TEXT NOT NULL, receipt_id TEXT NOT NULL, delivered_at TEXT NOT NULL,
                UNIQUE(agent_id,request_id))''')
            self.commands.db.execute('''CREATE TABLE IF NOT EXISTS inbox_events (
                agent_id TEXT NOT NULL,event_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                PRIMARY KEY(agent_id,event_id))''')

    def cursor_observation(self, agent, folder):
        """Describe the selected local chat independently of outbound delivery.

        The original invitation credential is the only setup authority. Merely
        having an inbox, a copied marker or an installed Hook is not a binding.
        """
        ident=agent['id']
        result={'agent_id':ident,'method':'native_hook','scope':'selected_conversation',
            'state':'setup_required','configured':False,'setup_required':True,
            'execution_verified':False,'background_delivery':False,
            'guide_path':None,'setup_command':None,'reason':'native_observer_not_bound'}
        source=agent.get('source_id','')
        if not re.fullmatch(r'inv-[a-f0-9]{32}',source):
            return dict(result,state='unsupported',reason='original_event_credential_unknown')
        original=Path(self.bridge.config['data_dir'])/'invitations'/source[4:]/'credential.json'
        try:
            with self.bridge.history.lock:
                row=self.bridge.history.db.execute('SELECT state,credential_hash FROM invitations WHERE id=?',
                    (source[4:],)).fetchone()
            if (not row or row['state']!='claimed' or
                    source[4:] in self.bridge.config.get('revoked_invitation_ids',[])):
                return dict(result,state='revoked',reason='invitation_not_current')
            fd=os.open(original,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            with os.fdopen(fd) as stream:
                info=os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size>16384 or info.st_mode&0o077 or
                        hasattr(os,'getuid') and info.st_uid!=os.getuid()):
                    raise ValueError()
                value=json.load(stream)
            if not isinstance(value,dict):raise ValueError()
            token=value.get('token')
            if (value.get('agent_id')!=ident or value.get('source_id')!=source or
                    value.get('endpoint')!='http://127.0.0.1:'+str(self.bridge.config['port'])+'/api/agents/events' or
                    not isinstance(token,str) or not token or
                    not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),agent.get('token_sha256','')) or
                    not hmac.compare_digest(row['credential_hash'],agent.get('token_sha256',''))):
                raise ValueError()
        except (OSError,ValueError,TypeError,KeyError):
            return dict(result,state='stale',reason='original_event_credential_unavailable')
        setup=[sys.executable,str(ROOT/'adapters/connect.py'),'--credential',str(original),
            'cursor-children','--hooks-file',str(Path.home()/'.cursor/hooks.json')]
        observer=getattr(self.bridge,'child_events',None)
        if observer is not None:
            with self.commands.lock,self.bridge.history.lock:
                row=self.bridge.history.db.execute('''SELECT o.* FROM child_observers o
                    JOIN observed_agent_roots r ON r.root_id=o.root_id AND r.generation=o.generation
                    WHERE o.root_id=? AND o.active=1 AND r.active=1 AND o.provider='cursor'
                    AND o.source_kind='cursor_hook' AND o.evidence='native_hook'
                    ORDER BY o.rowid DESC LIMIT 1''',(ident,)).fetchone()
                if row:
                    try:
                        if not observer._file_matches(row,observer._read_private(row['credential_path'])):
                            raise ValueError()
                        pending=row['native_session_id'].startswith('pending:')
                        result.update(state='binding_required' if pending else 'configured',
                            configured=not pending,setup_required=pending,
                            reason='native_activation_required' if pending else None)
                    except (OSError,ValueError,TypeError,KeyError):
                        result.update(state='stale',reason='native_observer_credential_unavailable')
        guide=folder/'cursor-public-sync-guide.txt'
        text=(f'Virtual Office：Cursor 原聊天公開同步（{agent["name"]}）\n\n'
            '這是同一份入場 Prompt 的必要步驟，與原聊天背景收訊分開。'
            'receiver_status=unsupported 不可令這一步提早結束；不用人類再貼第二段 Prompt。\n'
            '目前公開同步 state='+result['state']+'。configured 代表伺服器已核對目前世代的原生聊天綁定；'
            '仍需之後真正的 App 工作驗證狀態、公開對話及交付。\n'
            '若 configured，沿用目前綁定，不重裝或另開聊天。其他可設定狀態於目前原 session 執行：\n\n'
            +shlex.join(setup)+'\n\n'
            '此命令只使用原邀請的公開事件憑證，不使用收件箱 credential.json。'
            '它合併本 OFFICE 共用觀察 Hook、保留其他 Hook、模型、專案及批准規則。'
            '若平台要求一次性批准，只指出確切待批命令並等人類批准；不得改用其他執行入口繞過。\n'
            'hook_installed=true 只是安裝結果。若回傳 activation_marker，必須在本次原聊天的公開最終回覆'
            '原樣獨立包含該行一次；afterAgentResponse 原生 Hook 將實際 conversation 綁定到目前 OFFICE 身份。'
            '不要把標記當成新指示送回聊天，不用 emit.py 代替原生綁定，也不用人類重貼。'
            '標記送出前如實回報 binding_required；只有伺服器 observation.state=configured 才稱設定已確認。\n'
            '下一項普通 App 對話或工作由 Python Hook 自動同步公開 Prompt、工作狀態、公開回覆及交付；'
            '不需 Agent 每次記住回報，子 Agent 沿用同一綁定。'
            '不保存 thinking、工具參數／原文、未選聊天或其他身份。\n'
            '這個 Hook 不提供原聊天背景指令入口。OFFICE 留言不代表 Cursor 已收到或執行；'
            '不啟動等待工具、常駐模型、替代 CLI／Cloud 聊天、服務或排程。'
            '缺少原憑證、requires_rejoin=true 或身份撤銷時停止，請擁有人產生新邀請；'
            '不還原、改名或讀取 credential.logged-out 檔案。\n')
        private_write(guide,text)
        return dict(result,guide_path=str(guide),setup_command=setup)

    def ensure(self, agent_id):
        agent = self.bridge.definitions.get(agent_id)
        if not agent:raise InboxError('agent_not_selected', 404)
        folder = self.root / hashlib.sha256(agent_id.encode()).hexdigest()
        credential, guide = folder / 'credential.json', folder / 'receive-guide.txt'
        with self.commands.lock:
            agent=self.bridge.definitions.get(agent_id)
            if not agent:raise InboxError('agent_not_selected',404)
            old = self.commands.db.execute('SELECT token_hash FROM inbox_credentials WHERE agent_id=?', (agent_id,)).fetchone()
            if credential.exists():
                saved = json.loads(credential.read_text())
                if saved.get('agent_id') != agent_id:raise InboxError('inbox_credential_conflict', 409)
                token = saved['token']
            elif old:
                raise InboxError('inbox_credential_missing', 409)
            else:token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            if old and not hmac.compare_digest(old['token_hash'], digest):raise InboxError('inbox_credential_conflict', 409)
            private_write(credential, json.dumps({'version':1, 'agent_id':agent_id,
                'endpoint':'http://127.0.0.1:'+str(self.bridge.config['port'])+'/api/agents/inbox', 'token':token}, indent=2)+'\n')
            self.commands.db.execute('INSERT OR IGNORE INTO inbox_credentials VALUES (?,?)', (agent_id, digest))
            self.commands.db.commit()
            self.bridge.history.secrets = (*self.bridge.history.secrets, token)
        prefix = [sys.executable, str(ROOT/'adapters/inbox.py'), '--credential', str(credential)]
        capability=self.commands.capability(agent_id)
        binding=self.commands.binding(agent_id) or {}
        observation=(self.cursor_observation(agent,folder)
            if agent['provider']=='cursor' and agent.get('driver')=='push' and binding.get('adapter')!='cursor_cloud'
            else None)
        reporting=None
        reporting_text=''
        if agent['provider']=='grok_bot':
            reporting_service=self.bridge.get_public_reporting()
            try:reporting=reporting_service.ensure(agent_id)
            except ReportingError:reporting=reporting_service.status(agent_id)
            reporting_text=('\n原 App 公開同步設定（與收訊入口分開）：\n'
                'reporting_status='+reporting['state']+'\n'
                '必須完成同一份入場 Prompt 的長期合作回報設定；即使收訊入口 ready 也不能提早結束。'
                '只為目前 Bot 實際保存規則、讀回核對，再依指引提交設定回條；不要只口頭答應。'
                '收件箱憑證只供 inbox，不能用於 emit.py；原公開事件憑證由 reporting manifest 指定。\n')
            if reporting.get('guide_path'):
                reporting_text+='現在讀取：\n'+reporting['guide_path']+'\n'
            if reporting.get('manifest_path'):
                reporting_text+='本身份 reporting manifest：\n'+reporting['manifest_path']+'\n'
            reporting_text+=('setup_required／stale 時按該指引接續；agent_confirmed 時沿用已確認版本。'
                '設定回條只是 Agent 自述已保存，不代表新的 App 工作已同步；'
                'revoked／unsupported 時如實回報，不重用其他身份或憑證。毋須人類再貼第二份 Prompt。\n')
        if capability.get('persistent'):
            grok=agent['provider']=='grok_bot'
            verified=bool(capability.get('connection_check',{}).get('verified'))
            text=(f'Virtual Office 持續收訊：{agent["name"]}\n\n'
                '這個身份已綁定原生指令入口；不需要執行 inbox.py wait，也不受一小時等候限制。'
                'OFFICE 的 Python 服務會保存指令並交給已綁定的原生入口；沒有指令時不啟動模型。'
                '保留原 session、模型、權限及批准規則。正常完成目前工作及回覆即可。\n'
                'Codex App、Hermes ACP、Cursor Cloud 的公開回覆由原生觀察器同步；同一工作不要再用 emit.py 重複回報。'
                'Grok Webhook routine 仍需按接入指引回傳開始、公開進度及完成事件。\n'
                '持續收訊需要 OFFICE 服務及原平台可用；Codex App 未載入原聊天時，指令會排隊，待平台載入後執行。'
                '這不表示可以繞過平台批准、睡眠、關機或帳戶限制，也不代表已完成新的實際指令。\n')
            if grok:
                text+=grok_connection_handoff(agent['name'],agent_id)
                text+=grok_public_reporting_policy()
                text+=reporting_text
                text+=('Grok 收發測試已通過；沿用這個入口。\n' if verified else
                    'Grok 入口已保存，收發尚未確認。請擁有人回到 OFFICE 的「Grok 連接助手」按「測試連接」；'
                    '只有同一 Bot 回傳相配測試的開始、回覆及正常完成後，畫面才顯示「已連接」。\n')
            private_write(guide,text)
            return {'agent_id':agent_id,'credential_path':str(credential),'guide_path':str(guide),
                'prompt':('Grok 入口已設定，請讀取指引並確認 OFFICE 助手的實際收發測試結果：\n' if grok else
                    '原生持續收訊已設定，請讀取這份指引；不用重新待命：\n')+str(guide),'capability':capability,
                    **({'grok_setup':grok_connection_hint(agent['name'],agent_id)} if grok else {}),
                    **({'reporting':reporting} if reporting is not None else {}),
                    **({'observation':observation} if observation is not None else {})}
        if agent['provider']=='cursor' and agent.get('driver')=='push':
            text=(f'Virtual Office 入場、公開同步及收訊能力：{agent["name"]}\n\n'
                '保留目前 session、模型、權限及專案，只使用自己的有效接入身份。\n'
                '入場結果以 join.py 的 joined=true 及兩個 accepted=true 為準；capability.persistent=false 不代表入場失敗。'
                'Cursor 本機一般聊天可同步工作狀態及公開對話，但本版尚未接通原聊天的原生背景喚醒。\n')
            if observation and observation.get('guide_path'):
                text+='完成首次回報後，必須接續讀取這份原聊天公開同步指引：\n'+observation['guide_path']+'\n'
            elif observation:
                text+='公開同步 state='+observation['state']+'；reason='+str(observation.get('reason'))+'。停止使用失效或不明的觀察憑證。\n'
            text+=(
                '依 observation 的狀態完成觀察設定及 activation_marker 驗證，不能因收訊 unsupported 就停下來。'
                '這份收件箱的 credential.json 只供留言通道使用，不要拿來代替原邀請的上報憑證或安裝觀察 Hook。'
                '父聊天綁定後，子 Agent 自動沿用公開同步，毋須逐隻設定。\n'
                '不要為本機 Cursor 執行 Codex 自動辨認，也不要嘗試 Hermes、Grok Webhook 或 Cursor Cloud 代替原聊天。'
                '不啟動等待工具、常駐模型、服務或排程；不要宣稱持續待命。OFFICE 可保存留言，但不表示原聊天已收到或執行。\n'
                '如果指定憑證不存在、只剩 credential.logged-out 檔案，或伺服器回覆身份已撤銷，停止接駁並回報需要重新入場。'
                '請擁有人在 OFFICE 產生新邀請，再讀取新接入指引；不要恢復、改名、讀取或重用已登出憑證。\n')
            private_write(guide,text)
            return {'agent_id':agent_id,'credential_path':str(credential),'guide_path':str(guide),
                'prompt':'請確認目前 Cursor 本機聊天的入場及公開同步；背景收訊未接通不代表入場失敗。讀取：\n'+str(guide),
                'capability':capability,**({'observation':observation} if observation is not None else {})}
        connect=[sys.executable,str(ROOT/'adapters/connect.py'),'--credential',str(credential)]
        text = (f'Virtual Office 雙向對話：{agent["name"]}\n\n'
            '這是同一份邀請的持續收訊步驟；新入場 Agent 直接接續執行，毋須向人類索取第二段 Prompt，也不要重新入場。\n'
            '只讀取這份指引和你自己的憑證檔，保留目前 session、模型、權限及專案。不要轉交其他 Agent。\n'
            '目前尚未接通持續收訊。先在這個原 session 執行一次自動辨認：\n'
            +shlex.join(connect+['auto'])+'\n'
            '若 connected=true，讀取新回傳的 conversation.guide_path；不用執行等待工具。'
            'Codex App 可從本 session 環境自動綁定。Hermes、Cursor Cloud 請使用平台原生接駁；'
            '一般聊天沒有背景入口時，只能保存留言，不能假稱會自動喚醒。\n'
            +('Grok Bot 請依接入指引建立自己擁有的 Webhook routine（無排程），然後用 subprocess.run 的 argv 及 input，'
              '將該 routine 的 {"url":"POST to","key":"webhook key"} JSON 經 stdin 傳入：\n'
              +shlex.join(connect+['grok','--details-stdin'])+'\n不要把 key 寫入聊天、shell 參數或日誌；只可綁定你自己的 Bot。\n'
              +grok_connection_handoff(agent['name'],agent_id) if agent['provider']=='grok_bot' else '')+
            'OFFICE 擁有人從此 Agent 對話框傳來的文字，是給你的下一項指示。每項工作仍遵從原平台批准要求。\n'
            '以下只供用戶要求臨時收訊測試時使用，不是持續收訊方案。Python 在沒有訊息時只等待，不呼叫模型：\n\n'
            +shlex.join(prefix+['wait','--max-seconds','3600'])+'\n\n'
            '必須把這個命令作為目前 session 正在等待的工具呼叫，收到 JSON 才處理。不要把它放到背景後結束 session；'
            '獨立 Python 程序不能喚醒已結束的模型回合。如工具不支援長時間等待或需要人類批准，清楚回報限制，'
            '不要反覆發出空白模型回合。超時或中止後不宣稱仍在線；擁有人仍可在 OFFICE 留言。\n'
            '收到 state=received 時，先確認 agent_id 與本身份相符。然後以同一 command_id 執行 start：\n'
            +shlex.join(prefix+['start','--command','COMMAND_UUID'])+'\n'
            '只有 accepted=true 且 execute=true 才執行 text 中的指示。execute=false 代表已開始／已結束，絕不重做。'
            'start 失敗或回應不明時，保留結果未確認，不執行工作。不能因超時自行重新派送。\n'
            '公開進度及最終交付先正常回覆用戶，再用以下命令經 stdin 回報（用 argv 陣列和 input，不拼接 shell）：\n'
            +shlex.join(prefix+['message','--command','COMMAND_UUID','--event-id','NEW_EVENT_UUID','--text-stdin'])+'\n'
            +shlex.join(prefix+['complete','--command','COMMAND_UUID','--event-id','NEW_EVENT_UUID','--text-stdin'])+'\n'
            '工作失敗用 fail，中止用 cancel。每則公開訊息使用新的 event-id；網絡結果未確認時重試同一事件要沿用相同 ID 及文字。'
            '不要上報內部推理、工具原文、密鑰或其他聊天。此通道已自行記錄 OFFICE 工作，毋須再用 emit.py 重複回報同一工作。\n'
            '完成後如用戶仍要求待命，在同一 session 再呼叫 wait。若原平台已提供原生背景入口，工具會回覆 native_route，'
            '毋須建立第二個接收循環。不要自行新增服務、排程、付費模型或更改 App 安全設定。\n')
        if reporting is not None:text+=grok_public_reporting_policy()+reporting_text
        private_write(guide, text)
        return {'agent_id':agent_id, 'credential_path':str(credential), 'guide_path':str(guide),
                'prompt':'請在目前這個 session 完成 Virtual Office 持續收訊接駁，讀取並依照這份本機指引操作：\n'+str(guide)+
                '\n優先建立原生背景入口；成功後回覆實際接收結果。若平台沒有入口，回報只能保存留言，不要用等待工具冒充持續收訊。',
                'capability':capability,
                **({'grok_setup':grok_connection_hint(agent['name'],agent_id)} if agent['provider']=='grok_bot' else {}),
                **({'reporting':reporting} if reporting is not None else {})}

    def authenticated(self, agent_id, token):
        with self.commands.lock:
            if agent_id not in self.bridge.definitions or not token:return False
            row = self.commands.db.execute('SELECT token_hash FROM inbox_credentials WHERE agent_id=?', (agent_id,)).fetchone()
            return bool(row and hmac.compare_digest(row['token_hash'], hashlib.sha256(token.encode()).hexdigest()))

    def check_current(self,agent_id,token=None):
        if agent_id not in self.bridge.definitions:raise InboxError('agent_not_selected',403)
        if token is not None and not self.authenticated(agent_id,token):raise InboxError('invalid_agent_credential',403)

    def listening(self, agent_id):
        with self.waiters_lock:return any(item[0]==agent_id for item in self.waiters.values())

    def receive(self, agent_id, data, connected=lambda:True, authenticated_token=None):
        if set(data) != {'action','receiver_id','request_id','wait_seconds'}:raise InboxError('invalid_inbox_request')
        receiver, request = identifier(data['receiver_id']), identifier(data['request_id'])
        seconds = data['wait_seconds']
        if type(seconds) not in {int,float} or not 0<=seconds<=50:raise InboxError('invalid_inbox_request')
        with self.commands.lock:
            self.check_current(agent_id,authenticated_token)
            if not self.commands.allowed(agent_id):raise InboxError('dispatch_disabled',503)
            if self.commands.binding(agent_id)['adapter']!='agent_inbox':return {'state':'native_route'}
        handle = uuid.uuid4().hex
        with self.waiters_lock:
            if any(item[0]==agent_id and item[1]!=receiver for item in self.waiters.values()):raise InboxError('receiver_already_waiting', 409)
            if len(self.waiters)>=64:raise InboxError('too_many_receivers', 429)
            self.waiters[handle]=(agent_id,receiver)
        deadline = time.monotonic()+seconds
        try:
            while not self.bridge.stop.is_set():
                if not connected():return {'state':'receiver_disconnected'}
                with self.commands.lock:
                    self.check_current(agent_id,authenticated_token)
                    if not self.commands.allowed(agent_id):raise InboxError('dispatch_disabled',503)
                    if self.commands.binding(agent_id)['adapter']!='agent_inbox':return {'state':'native_route'}
                    previous=self.commands.db.execute('SELECT * FROM inbox_deliveries WHERE agent_id=? AND request_id=?',(agent_id,request)).fetchone()
                    if previous:
                        if previous['receiver_id']!=receiver:raise InboxError('request_id_conflict',409)
                        row=self.commands.db.execute('SELECT * FROM commands WHERE id=?',(previous['command_id'],)).fetchone()
                        if row['state']=='queued' and row['payload'] is not None:return self.delivery(row,previous)
                        return {'state':'already_started','command_id':row['id'],'command_state':row['state']}
                    active=self.commands.db.execute("SELECT 1 FROM commands WHERE agent_id=? AND state IN ('sending','queued','running','waiting_user','delivery_unknown') LIMIT 1",(agent_id,)).fetchone()
                    if active:return {'state':'unresolved_delivery'}
                    row=self.commands.db.execute("SELECT * FROM commands WHERE agent_id=? AND adapter='agent_inbox' AND state='pending' ORDER BY created_at,id LIMIT 1",(agent_id,)).fetchone()
                    if row:
                        receipt=str(uuid.uuid4())
                        with self.commands.db:
                            self.commands.db.execute('INSERT INTO inbox_deliveries VALUES (?,?,?,?,?,?)',(row['id'],agent_id,receiver,request,receipt,timestamp()))
                            self.commands.db.execute("UPDATE commands SET state='queued',native_message_id=?,updated_at=? WHERE id=?",(receipt,timestamp(),row['id']))
                        return self.delivery(row,{'receipt_id':receipt})
                remaining=deadline-time.monotonic()
                if remaining<=0:return {'state':'waiting','command':None}
                self.bridge.stop.wait(min(.2,remaining))
            return {'state':'office_stopping'}
        finally:
            with self.waiters_lock:self.waiters.pop(handle,None)

    @staticmethod
    def delivery(row, receipt):
        return {'state':'received','agent_id':row['agent_id'],'command_id':row['id'],
                'receipt_id':receipt['receipt_id'],'text':row['payload'],'execute':False}

    def event(self, agent_id, data, authenticated_token=None):
        action=data.get('action')
        if action not in {'start','message','complete','fail','cancel'}:raise InboxError('invalid_inbox_request')
        required={'action','command_id','receipt_id'} | (set() if action=='start' else {'event_id','text'})
        if set(data)!=required:raise InboxError('invalid_inbox_request')
        command, receipt=identifier(data['command_id']),identifier(data['receipt_id'])
        if action!='start':
            identifier(data['event_id'])
            if not isinstance(data['text'],str) or not data['text'].strip() or len(data['text'].encode())>96000:raise InboxError('invalid_inbox_request')
        with self.commands.lock, self.bridge.history.lock:
            self.check_current(agent_id,authenticated_token)
            row=self.commands.db.execute("SELECT * FROM commands WHERE id=? AND agent_id=? AND adapter='agent_inbox'",(command,agent_id)).fetchone()
            if not row or not hmac.compare_digest(row['native_message_id'] or '',receipt):raise InboxError('invalid_delivery',403)
            agent=self.bridge.definitions[agent_id];history=self.bridge.history
            if action=='start':
                if row['state']!='queued':return {'accepted':True,'execute':False,'duplicate':True,'state':row['state']}
                if not self.commands.allowed(agent_id):raise InboxError('dispatch_disabled',503)
                self.commands.db.execute("UPDATE commands SET state='running',run_id=id,payload=NULL,updated_at=? WHERE id=?",(timestamp(),command));self.commands.db.commit()
                history.record(agent_id,agent['provider'],agent['name'],command,'working',prompt=row['preview'],source_kind='agent_inbox')
                history.message(agent_id,command,'user','prompt',row['preview'],event_id='inbox:'+command+':prompt')
                self.bridge.report(agent['name'],{'status':'working','step':'Agent 已接收 OFFICE 指示','run_id':command},agent['provider'],agent_id=agent_id)
                return {'accepted':True,'execute':True,'duplicate':False,'state':'running'}
            fingerprint=self.commands.fingerprint(agent_id,json.dumps(data,sort_keys=True,ensure_ascii=False))
            previous=self.commands.db.execute('SELECT fingerprint FROM inbox_events WHERE agent_id=? AND event_id=?',(agent_id,data['event_id'])).fetchone()
            if previous:
                if not hmac.compare_digest(previous['fingerprint'],fingerprint):raise InboxError('event_conflict',409)
                return {'accepted':True,'duplicate':True,'state':row['state']}
            if row['state']!='running':raise InboxError('command_not_running',409)
            terminal={'complete':('completed','done'),'fail':('failed','error'),'cancel':('cancelled','cancelled')}.get(action)
            # Persist public evidence before acknowledging. Reconcile restores a
            # final history row if the process exits before command commit.
            history.message(agent_id,command,'assistant','final' if terminal else 'commentary',data['text'],event_id='inbox:'+data['event_id'])
            if terminal:
                history.record(agent_id,agent['provider'],agent['name'],command,terminal[1],delivery=data['text'],source_kind='agent_inbox')
                self.commands.db.execute('UPDATE commands SET state=?,updated_at=? WHERE id=?',(terminal[0],timestamp(),command))
            self.commands.db.execute('INSERT INTO inbox_events VALUES (?,?,?)',(agent_id,data['event_id'],fingerprint));self.commands.db.commit()
            self.bridge.report(agent['name'],{'status':terminal[1] if terminal else 'working',
                'step':'已完成 OFFICE 指示' if action=='complete' else 'Agent 正在回報' if not terminal else 'Agent 已結束本次工作','run_id':command},agent['provider'],agent_id=agent_id)
            return {'accepted':True,'duplicate':False,'state':terminal[0] if terminal else 'running'}
