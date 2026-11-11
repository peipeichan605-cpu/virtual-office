"""Owner-selected, local, single-use invitations; no anonymous agent enrollment."""
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
import time
import uuid
from contextlib import nullcontext
from configuration import ROOT, PROVIDERS, agent_name, load_config, save_config
from workspaces import Workspaces, workspace_path
from reporting_policy import grok_public_reporting_policy
from grok_onboarding import grok_connection_handoff, grok_connection_hint
from public_reporting import ReportingError

class InvitationError(ValueError):
    def __init__(self,code,status=400):self.code=code;self.status=status;super().__init__(code)

def private_file(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as output:output.write(value)


ONBOARDING_FLOW=(
    '在同一個原 session 完成以下流程，毋須人類再貼第二段 Prompt：\n'
    '1. 執行入場及首次回報，確認 joined=true、兩個 accepted=true，保留實際 run_id；已成功的步驟沿用結果，不重做已完成工作。\n'
    '2. 先檢查 receiver_status；若為 rejoin_required 或 requires_rejoin=true，停止使用舊指引，請擁有人產生新邀請。'
    '其他情況讀取回傳的 conversation.guide_path，並按本平台指引完成身份綁定及公開同步。\n'
    '3. 接續設定本平台的持續收訊入口，保留目前 session 身份，不能完成入場後就停下來等待另一段 Prompt。'
    'setup_required 時，按照同一份指引完成平台支援的一次性原生接駁，再確認最新能力；'
    'ready 表示入口目前可用，不用重新待命或執行等待工具；'
    'configured_unavailable 表示入口已保存、等待原平台恢復；disabled 遵從擁有人的停用設定；'
    'unsupported 時保留入場及公開同步，如實回報只能保存留言、未能背景喚醒。'
    'capability.persistent=false 不代表入場失敗；persistent=true 亦不能單獨證明平台目前可用。\n'
    '3b. 如 conversation.reporting.method=cooperative，必須接續讀取其 guide_path 與 manifest_path，'
    '為目前已入選 Bot 實際保存長期公開回報規則、讀回核對，再依指引提交設定確認。'
    '即使 receiver_status=ready，亦不能略過這一步或等待人類再貼另一份 Prompt。'
    'reporting_status=setup_required 或 stale 時完成設定；agent_confirmed 只代表合作設定已確認，'
    '不能代替之後在原 App 直接工作的實際同步證據；revoked 時停止並要求新邀請。\n'
    '3c. 如 conversation.observation.method=native_hook，必須在同一份入場流程完成所選 Cursor 原聊天的公開同步：'
    '讀取 observation.guide_path，依其 setup_command 安裝本 OFFICE 共用 Hook，再按回傳結果處理。'
    'setup_required／stale 時不能略過；binding_required 時在本次原聊天公開最終回覆原樣包含 activation_marker 一次，'
    '由原生 Hook 核對並綁定身份，不要求人類再貼第二段 Prompt。configured 才表示伺服器已確認目前綁定，'
    '安裝成功、activation_marker 或入場成功本身都不等於已同步新工作。'
    '即使 receiver_status=unsupported，仍須完成這項公開同步；兩項能力分開回報，不改接 Cloud、CLI 或其他聊天。\n'
    '4. 最後一次回覆 run_id、首次回報結果、公開同步結果及持續收訊結果。'
    '分開說明 OFFICE 指示收發與原 App 直接工作的同步範圍；收發成功不能代替 App 對話同步證據。'
    '說明入口是否保留同一 session；若只綁定同一 Bot，清楚說明未保證原聊天。'
    '入口已設定不代表已驗證新的實際指令；未有相配的接收、執行及交付證據時，不宣稱端到端完成。\n'
    '保留原 session、專案、模型及批准規則。若平台要求一次性批准，只指出確切待批步驟並等人類批准，'
    '若 Grok 的連接資料只向人類顯示，請提示擁有人回到同一個 OFFICE 邀請視窗，使用「Grok 連接助手」逐步完成；'
    '不要要求人類研究技術設定，不索取第二段 Prompt，也不把秘密貼進聊天。'
    '批准後繼續同一份指引；不要繞過批准、轉接其他聊天／平台、另起服務或用等待工具冒充持續收訊。\n'
)


class Invitations:
    def __init__(self,config,history,activate,workspaces=None,conversation=None,capability=None,lifecycle_lock=None,reporting_setup=None):
        self.config=config;self.history=history;self.activate=activate
        self.conversation=conversation
        self.capability=capability
        self.reporting_setup=reporting_setup
        self.lifecycle_lock=lifecycle_lock or nullcontext()
        self.workspaces=workspaces or Workspaces(history)
        self.root=Path(config['data_dir'])/'invitations'
        with history.lock,history.db:
            history.db.execute('''CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                provider TEXT NOT NULL, source_id TEXT NOT NULL, token_hash TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                claim_id TEXT, credential_hash TEXT NOT NULL)''')
            if 'workspace_id' not in {r[1] for r in history.db.execute('PRAGMA table_info(invitations)')}:
                history.db.execute('ALTER TABLE invitations ADD COLUMN workspace_id TEXT')

    def row(self,ident):
        row=self.history.db.execute('SELECT * FROM invitations WHERE id=?',(ident,)).fetchone()
        if not row:raise InvitationError('invitation_not_found',404)
        return dict(row)

    def paths(self,ident):
        if not re.fullmatch(r'[a-f0-9]{32}',ident):raise InvitationError('invitation_not_found',404)
        folder=self.root/ident
        return folder/'invite.json',folder/'agent-guide.txt',folder/'credential.json'

    def view(self,row):
        invite,guide,_=self.paths(row['id'])
        state=row['state'];received=False
        if state=='pending' and time.time()>=row['expires']:state='expired'
        reporting=None
        if state=='claimed':
            current=load_config(self.config['config_path'])
            selected=next((a for a in current['agents'] if a['id']==row['provider']+':'+row['source_id']),None)
            if (not selected or selected.get('token_sha256')!=row['credential_hash'] or
                    row['id'] in current.get('revoked_invitation_ids',[])):
                state='revoked'
            elif row['provider']=='grok_bot' and self.reporting_setup:
                try:reporting=self.reporting_setup(selected['id'])
                except ReportingError as error:
                    reporting={'agent_id':selected['id'],'method':'cooperative',
                        'state':'unsupported' if error.code in {'reporting_platform_unsupported','reporting_original_credential_unknown'} else
                        'revoked' if error.code=='agent_not_selected' else 'stale','reason':error.code}
                self._upgrade_reporting_guide(guide,reporting)
        if state=='claimed':
            received=self.history.db.execute('SELECT 1 FROM event_receipts WHERE agent_id=? LIMIT 1',
                (row['provider']+':'+row['source_id'],)).fetchone() is not None
            if received:state='connected'
        prompt=('你已被我邀請加入我的 Virtual Office。這份 Prompt 同時包含入場及持續收訊設定。'
                '請使用這部電腦的本機執行工具，讀取並依照以下接入指引操作：\n\n'
                +str(guide)+'\n\n'+ONBOARDING_FLOW+
                '\n若你無法存取這部電腦，請回報接入未完成及原因。不要只回答「已加入」。')
        requires_new = state in {'revoked', 'expired'}
        if row['provider']=='grok_bot' and not requires_new:
            prompt+=grok_connection_handoff(row['name'],row['provider']+':'+row['source_id'])
        if requires_new:
            prompt=('這份 OFFICE 邀請已撤銷或過期，不能再次登入。請由擁有人在 OFFICE 產生新邀請，再使用新接入指引。'
                    '不要執行舊指引、恢復已登出憑證、嘗試其他平台入口或啟動等待工具。')
        return {'id':row['id'],'name':row['name'],'provider':row['provider'],'state':state,
                'agent_id':row['provider']+':'+row['source_id'],
                'expires_at':row['expires'],'first_report_received':received,'prompt':prompt,
                'requires_new_invitation':requires_new,
                'workspace_id':row.get('workspace_id'),
                'reporting':reporting,
                **({'grok_setup':grok_connection_hint(row['name'],row['provider']+':'+row['source_id'])} if row['provider']=='grok_bot' and not requires_new else {}),
                'conversation':self.capability(row['provider']+':'+row['source_id']) if self.capability and state in {'claimed','connected'} else None}

    def _upgrade_reporting_guide(self,guide,reporting):
        """Bounded append only: never replace a user's concurrent guide edit."""
        if not reporting or reporting.get('state') in {'unsupported','revoked'} or not reporting.get('guide_path'):
            return
        version=str(reporting.get('policy_version',''))
        marker='OFFICE_PUBLIC_REPORTING_UPGRADE:'+hashlib.sha256((version+'\0'+reporting['guide_path']).encode()).hexdigest()
        end_marker='END_'+marker
        block=('\n\n'+marker+'\n'
            '目前公開同步設定版本：'+version+'。以下補接沿用原身份及公開事件憑證，不重新入場或建立 routine。\n'
            '立即讀取這份已選定 Bot 的公開同步指引：\n'+reporting['guide_path']+'\n'
            '依該指引讀取 manifest、實際保存本 Bot 長期規則並讀回核對，然後提交設定確認。'
            '即使收訊入口 ready，也必須完成；毋須人類再貼第二份 Prompt。'
            '只記住身份、規則版本和 manifest 路徑，不記錄秘密內容，不修改其他 Bot 或共享設定。'
            'agent_confirmed 是合作設定回條，之後仍須用原 App 的實際工作驗證同步。\n'+end_marker+'\n')
        fd=None
        try:
            info=guide.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>256*1024 or
                    hasattr(os,'getuid') and info.st_uid!=os.getuid()):
                raise InvitationError('invitation_guide_unavailable',409)
            fd=os.open(guide,os.O_RDWR|os.O_APPEND|getattr(os,'O_NOFOLLOW',0))
            opened=os.fstat(fd)
            revision=lambda item:(item.st_dev,item.st_ino,item.st_size,item.st_mtime_ns,item.st_ctime_ns)
            if revision(opened)!=revision(info):raise InvitationError('invitation_guide_changed',409)
            original=os.read(fd,256*1024+1)
            if len(original)>256*1024 or len(original)!=opened.st_size:
                raise InvitationError('invitation_guide_unavailable',409)
            text=original.decode('utf-8')
            if end_marker in text:return
            if revision(os.fstat(fd))!=revision(opened) or revision(guide.lstat())!=revision(opened):
                raise InvitationError('invitation_guide_changed',409)
            encoded=block.encode('utf-8')
            if os.write(fd,encoded)!=len(encoded):raise InvitationError('invitation_guide_upgrade_incomplete',409)
            os.fsync(fd)
            current=guide.lstat()
            if (current.st_dev,current.st_ino)!=(opened.st_dev,opened.st_ino):
                raise InvitationError('invitation_guide_changed',409)
        except (OSError,UnicodeError):
            raise InvitationError('invitation_guide_unavailable',409)
        finally:
            if fd is not None:os.close(fd)

    def get(self,ident):
        with self.lifecycle_lock,self.history.lock:return self.view(self.row(ident))

    def create(self,data):
        if not isinstance(data,dict) or not {'name','provider','request_id'}<=set(data) or set(data)-{'name','provider','request_id','workspace_id'}:raise InvitationError('invalid_invitation')
        try:name=agent_name(data['name'])
        except (ValueError,UnicodeError):raise InvitationError('invalid_name')
        provider=data['provider'];request_id=data['request_id']
        if provider not in PROVIDERS or not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',request_id):
            raise InvitationError('invalid_invitation')
        with self.lifecycle_lock,self.history.lock,self.history.db:
            workspace_id=data.get('workspace_id')
            if workspace_id is not None:self.workspaces.require(workspace_id)
            previous=self.history.db.execute('SELECT * FROM invitations WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if previous['name']!=name or previous['provider']!=provider or previous['workspace_id']!=workspace_id:raise InvitationError('request_conflict',409)
                return self.view(dict(previous))
            current=load_config(self.config['config_path'])
            if any(a['name']==name for a in current['agents']):raise InvitationError('name_in_use',409)
            pending=self.history.db.execute("SELECT * FROM invitations WHERE name=? AND state='pending' AND expires>?",(name,time.time())).fetchone()
            if pending:
                if pending['provider']!=provider or pending['workspace_id']!=workspace_id:raise InvitationError('name_in_use',409)
                return self.view(dict(pending))
            ident=uuid.uuid4().hex;source='inv-'+ident;token=secrets.token_urlsafe(32);agent_token=secrets.token_urlsafe(32)
            invite,guide,credential=self.paths(ident);base='http://127.0.0.1:'+str(current['port'])
            private_file(credential,json.dumps({'endpoint':base+'/api/agents/events','agent_id':provider+':'+source,
                'source_id':source,'token':agent_token},indent=2))
            private_file(invite,json.dumps({'endpoint':base+'/api/agents/join','invitation_id':ident,'token':token,
                'agent_id':provider+':'+source,'credential_path':str(credential),'guide_path':str(guide)},indent=2))
            command=shlex.join([sys.executable,str(ROOT/'adapters/join.py'),'--invite',str(invite)])
            reporter=str(ROOT/'adapters/emit.py')
            guide_text=(f'Virtual Office 接入：{name}\n\n'
                '只使用 OFFICE 所在電腦的本機執行能力。保留目前專案工作目錄，不要切換到 OFFICE 安裝目錄。'
                '接入程式會回報目前工作目錄，讓同一 Workspace 的 Agent 位於同一層；如擁有人已指定樓層則遵從其設定。'
                '先向用戶表示你正在接入，再執行以下命令：\n\n'
                +command+'\n\n'
                '這會兌換一次性邀請、登記你的身份，並送出接入開始與完成兩個事件。'
                '必須看到 joined=true 以及兩個 accepted=true 才能回覆接入測試成功；列出實際 run_id。'
                '這只證明服務接收，不代表已目視驗證 3D 畫面。失敗時回報實際障礙，不自動重試、改網絡或其他成員。\n\n'
                +ONBOARDING_FLOW+'\n'
                '在本聊天之後處理任務時，使用以下現有上報程式：\n'
                +json.dumps({'python':sys.executable,'reporter':reporter,'credential':str(credential)},ensure_ascii=False)+'\n\n'
                '每項任務產生新 UUID 作 run_id，每則事件產生獨立 event_id。用 subprocess.run 的 argv 陣列執行：\n'
                '[python, reporter, "--credential", credential, TYPE, "--run", run_id, "--event-id", event_id, "--text-stdin"]\n'
                '用 input 傳入文字，不將任務文字拼接成 shell 指令。開始用 turn_started；'
                '對用戶可見的進度用 message，額外加 ["--phase", "commentary"]；'
                '真正完成用 turn_completed 並附交付；失敗或中止用 turn_failed 或 turn_cancelled。\n'
                'turn_started 預設回報目前工作目錄；如在專案子資料夾執行，加入 ["--workspace", 專案根目錄的絕對路徑]。\n'
                '憑證只由程式讀取，不要列印、複製或貼入聊天。不讀取 OFFICE 擁有人設定，不上報內部推理、工具原文或其他聊天內容。'
                '只上報已向用戶發出的公開任務、進度及交付，先移除已知私人資料。\n'
                '接入命令同時建立你的私有收件箱，並自動辨認本機 Codex 原 session。完成首次回報後，依上方合併流程繼續，毋須第二份收訊 Prompt。'
                '原生持續收訊已設定後，不需要等待工具；Codex/Hermes/Cursor 原生觀察器會同步公開回覆，毋須重複上報。'
                '平台有可用原生背景入口時才按指引設定；沒有入口時如實回報背景收訊未接通，保留已完成的入場及公開同步。'
                '不要另起模型、服務或排程，也不要用等待工具冒充持續收訊。'
                '同步失敗時告知用戶，仍可繼續原本已授權工作。這沒有自動擷取平台的全部對話。\n')
            if provider=='grok_bot':
                guide_text+=grok_public_reporting_policy()
                child_reporter=str(ROOT/'adapters/children.py')
                guide_text+=('\nGrok Bot 原生 Task 子 Agent（合作回報模式）：\n'
                    'OFFICE 不會獨立訂閱 Grok Bot 雲端子 Agent。你使用原生 Task／CheckSubagent 後，'
                    '可把真正子 Agent 的原生 ID、公開進度及交付經本機 Python 上報；畫面明確標示 Agent 合作回報。'
                    '這不是建立其他長駐 Bot，也不要把普通回覆假扮子 Agent。\n'
                    '先按上述流程送出目前父任務的 turn_started，保留 parent_run_id。Task 真正建立 child 後，使用 argv：\n'
                    +json.dumps([sys.executable,child_reporter,'--credential',str(credential),'start','--child-id','NATIVE_CHILD_ID','--parent-run','PARENT_RUN_ID','--run','CHILD_RUN_ID','--event-id','CHILD_START_EVENT_ID','--name','子 Agent 名稱','--text-stdin'],ensure_ascii=False)+'\n'
                    '任務文字只經 stdin 傳入。公開進度用 message；真正完成用 complete 並傳最終交付；失敗／中止用 fail／cancel。'
                    '同一 child 保留原生 ID 及 child_run_id，每則事件使用固定唯一 event_id，重送同一事件保留全部內容。'
                    '巢狀子 Agent 加 --parent-id 直接父 child 的原生 ID。\n'
                    '僅上報對使用者公開的文字；不要輸出整份 ReadTranscript、thinking、工具參數或工具結果。'
                    '只有原生完成結果或相配狀態證明完成才送 complete；來源不明時告知用戶，不能推測完成。'
                    '如沒有原生 Task 或本機執行能力，清楚回報子 Agent 同步未接通，不建立替代 Bot、不開公網。\n')
                guide_text+=('\nOFFICE 背景指示接駁（Grok Bot 官方 Webhook routine）：\n'
                    '要完成持續收訊，先完成本機回報測試。確定你能從 routine 使用同一部電腦的本機執行工具，才建立你自己擁有的 '
                    'OFFICE 收訊 routine，名稱依下方「OFFICE 連接卡」指引；使用 Webhook 觸發，不加排程、不輪詢、不轉交其他 Bot。\n'
                    'routine 內固定保留上述 reporter / credential 路徑及以下規則：只處理 office.version=1、'
                    'office.agent_id='+provider+':'+source+' 的 JSON。用 office.command_id 作 run_id，'
                    '把 prompt.text 原文（包括 OFFICE 標記）經 stdin 送 turn_started；收到 accepted=true 才執行指示。'
                    '每次重複 Webhook 先使用固定 event_id=office.command_id+":start" 送相同開始事件；'
                    '如 duplicate=true 或回報失敗，不重做任務，回報結果未確認。'
                    '公開進度用 message，最終回覆同時交回這個 Bot 的聊天及 turn_completed；失敗用 turn_failed。'
                    '沿用既有平台權限與批准要求，不擴大權限、不輸出內部推理。\n'
                    '建立後按 conversation.guide_path，用 connect.py grok --details-stdin 將這個 routine 的 POST to 和 key 直接交回 OFFICE。'
                    '如平台只准人類讀取連接資料，清楚回報此步未完成，並按下方「OFFICE 連接卡」列出確切流程及可取得的收訊地址。'
                    '由擁有人跟 OFFICE 連接助手完成；不要要求擁有人把金鑰貼進聊天或執行技術指令。'
                    '不要在聊天貼出 key，不要讀取 OFFICE 擁有人設定或其他 Bot 憑證。Webhook HTTP 200 只代表接收；'
                    '必須由真實回報確認開始及完成。雲端 localhost 不等於擁有人電腦，無本機執行能力時，'
                    '清楚告知只能上報／尚未完成背景接駁，不開公網隧道或偽造完成。'
                    '即使已保存入口，仍只表示設定完成；由擁有人在 OFFICE 助手按「測試連接」，'
                    '服務核對同一 Bot 的測試開始、相配回覆及正常完成後，才顯示「已連接」。\n')
                guide_text+=grok_connection_handoff(name,provider+':'+source)
            if provider=='cursor':
                child_setup=shlex.join([sys.executable,str(ROOT/'adapters/connect.py'),'--credential',str(credential),
                    'cursor-children','--hooks-file',str(Path.home()/'.cursor/hooks.json')])
                guide_text+=('\nCursor 本機父子 Agent 原生 Hooks：\n'
                    '完成上面首次接入後，必須於同一份入場流程完成公開同步。優先讀取 conversation.observation.guide_path；'
                    '原聊天背景喚醒未支援，不得因此略過這項設定。於 OFFICE 所在電腦執行這個一次性設定命令：\n\n'+child_setup+'\n\n'
                    '它只追加本 OFFICE 的觀察 Hook，保留原有 Hook／批准規則，並以專用觀察憑證限定你自己的原生聊天。'
                    '不要修改原本安全 Hook、不要切換模型、不要另開 CLI Agent。\n'
                    '若回傳 activation_marker，在目前這個原聊天的公開最終回覆原樣包含該行一次。'
                    '原生 afterAgentResponse Hook 會用這個一次性標記驗證目前 conversation 身份；未綁定前不宣稱子 Agent 已同步。'
                    '後續由 Python Hook 同步原生父子身份及狀態，共用 Python 讀取器同步該子回合自己的公開文字及交付；不需要每次叫 Agent 待命或手動上報。'
                    'Hook 只觀察入選聊天，未選聊天、thinking、工具參數及工具原文不保存。\n'
                    '子 Agent 觀察與背景收訊是兩項能力；這個 Hook 不會把本機聊天變成 Cloud Agent，亦不會自動取得背景派工入口。'
                    '本機一般聊天的 capability.persistent=false 並不妨礙入場及公開同步；不要為此嘗試 Codex、Hermes、Grok 或 Cloud 入口。'
                    '如已登出或邀請被撤銷，先停止接駁，請擁有人產生新邀請；不可改名或重用 credential.logged-out 檔案。'
                    '若本機 Cursor 不提供這些 Hook 或無法執行，回報實際限制，不使用模擬子 Agent 代替。\n')
            private_file(guide,guide_text)
            now=time.time()
            self.history.db.execute('''INSERT INTO invitations
                (id,request_id,name,provider,source_id,token_hash,state,created,expires,claim_id,credential_hash,workspace_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ident,request_id,name,provider,source,hashlib.sha256(token.encode()).hexdigest(),'pending',now,now+86400,None,hashlib.sha256(agent_token.encode()).hexdigest(),workspace_id))
            return self.view(self.row(ident))

    def revoke(self,ident):
        with self.lifecycle_lock,self.history.lock,self.history.db:
            row=self.row(ident)
            if row['state']=='claimed':raise InvitationError('already_claimed',409)
            self.history.db.execute("UPDATE invitations SET state='revoked' WHERE id=?",(ident,))
            return self.view(self.row(ident))

    def claim(self,ident,token,data):
        if not isinstance(data,dict) or 'claim_id' not in data or set(data)-{'claim_id','workspace_path'} or not isinstance(data['claim_id'],str) or not re.fullmatch(r'[a-f0-9]{32}',data['claim_id']):
            raise InvitationError('invalid_claim')
        if 'workspace_path' in data:workspace_path(data['workspace_path'])
        with self.lifecycle_lock,self.history.lock,self.history.db:
            row=self.row(ident)
            if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),row['token_hash']):raise InvitationError('invalid_invitation',403)
            if row['state']=='revoked':raise InvitationError('invitation_revoked',410)
            if row['state']=='claimed' and row['claim_id']!=data['claim_id']:raise InvitationError('invitation_used',409)
            if row['state']=='pending' and time.time()>=row['expires']:raise InvitationError('invitation_expired',410)
            config=load_config(self.config['config_path']);agent={'id':row['provider']+':'+row['source_id'],'provider':row['provider'],
                'source_id':row['source_id'],'name':row['name'],'driver':'push','token_sha256':row['credential_hash']}
            existing=next((a for a in config['agents'] if a['id']==agent['id']),None)
            if ident in config.get('revoked_invitation_ids',[]) or (row['state']=='claimed' and not existing):
                raise InvitationError('invitation_revoked',410)
            if existing:
                if any(existing.get(k)!=v for k,v in agent.items()):raise InvitationError('agent_changed',409)
            else:
                if any(a['name']==agent['name'] for a in config['agents']):raise InvitationError('name_in_use',409)
                config['agents'].append(agent);save_config(self.config['config_path'],config)
            if row['state']=='pending':
                if data.get('workspace_path'):self.workspaces._observe(agent['id'],data['workspace_path'],'join_directory')
                if row['workspace_id']:self.workspaces._assign(agent['id'],row['workspace_id'])
            self.history.db.execute("UPDATE invitations SET state='claimed',claim_id=? WHERE id=?",(data['claim_id'],ident))
            self.activate(agent)
            _,guide,credential=self.paths(ident)
            result={'registered':True,'agent_id':agent['id'],'credential_path':str(credential),'guide_path':str(guide)}
        # Avoid history -> command lock inversion while preparing the channel.
        if self.conversation:result['conversation']=self.conversation(agent['id'])
        return result
