#!/usr/bin/env python3
"""Redeem an owner-created local invitation and report a bounded join check."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from client import load_credential,send
from connect import auto_connect


def receiver_result(conversation, native=None):
    """A saved route is durable configuration, not proof of a live receiver."""
    capability=conversation.get('capability',{})
    result=dict(native or {})
    if capability.get('persistent'):
        ready=capability.get('native_ready') is True
        state='ready' if ready else 'configured_unavailable'
        result.update(connected=ready,persistent=True,native_ready=ready,
                      reason=None if ready else capability.get('reason') or 'native_runtime_unavailable')
    else:
        state='unsupported' if result.get('reason')=='cursor_local_background_unavailable' else 'setup_required'
        result.update(connected=False,persistent=False,native_ready=False)
    if capability.get('reason')=='dispatch_disabled':state='disabled'
    if result.get('requires_rejoin'):
        state='rejoin_required'
        result.update(connected=False,persistent=False,native_ready=False)
    result.update(receiver_status=state,execution_verified=False,conversation=conversation)
    return result


def normalized_reporting(conversation, agent_id=None):
    identity=agent_id or conversation.get('agent_id','')
    reporting=conversation.get('reporting')
    grok=isinstance(identity,str) and identity.startswith('grok_bot:')
    if not grok:return reporting if isinstance(reporting,dict) else None
    valid=isinstance(reporting,dict) and reporting.get('method')=='cooperative' and reporting.get('agent_id')==identity
    states={'setup_required','agent_confirmed','stale','revoked','unsupported'}
    if (not valid or reporting.get('state') not in states or
            reporting.get('state')=='agent_confirmed' and not all(isinstance(reporting.get(k),str) and reporting[k]
                for k in ('manifest_path','guide_path','policy_version','policy_hash','setup_id'))):
        return {'method':'cooperative','agent_id':identity,'state':'setup_required','reason':'reporting_metadata_missing_or_invalid'}
    return reporting


def reporting_result(conversation, receiver_status, agent_id=None):
    """A receiver and a remembered public-reporting policy are independent."""
    reporting=normalized_reporting(conversation,agent_id)
    required=isinstance(reporting,dict) and reporting.get('method')=='cooperative'
    state=reporting.get('state','setup_required') if required else 'not_required'
    if required and receiver_status=='rejoin_required':state='revoked'
    complete=receiver_status in {'ready','configured_unavailable'} and (not required or state=='agent_confirmed')
    return {'reporting_status':state,'reporting_setup_required':required and state!='agent_confirmed',
            'full_onboarding_complete':bool(complete),'reporting_execution_verified':False}


def normalized_observation(conversation, agent_id=None):
    identity=agent_id or conversation.get('agent_id','')
    observation=conversation.get('observation')
    capability=conversation.get('capability',{})
    local_cursor=(isinstance(identity,str) and identity.startswith('cursor:')
        and capability.get('mode')!='cursor_cloud')
    if not local_cursor:return None
    states={'setup_required','binding_required','configured','stale','revoked','unsupported'}
    if (not isinstance(observation,dict) or observation.get('method')!='native_hook' or
            observation.get('agent_id')!=identity or observation.get('state') not in states or
            observation.get('state')=='configured' and observation.get('configured') is not True):
        return {'agent_id':identity,'method':'native_hook','state':'setup_required',
            'configured':False,'reason':'observation_metadata_missing_or_invalid'}
    return observation


def observation_result(conversation, receiver_status, agent_id=None):
    observation=normalized_observation(conversation,agent_id)
    required=observation is not None
    state=observation.get('state','setup_required') if required else 'not_required'
    if required and receiver_status=='rejoin_required':state='revoked'
    complete=required and state=='configured'
    return {'observation_status':state,'observation_setup_required':required and not complete,
        'public_sync_onboarding_complete':bool(complete),
        'observation_execution_verified':False}


def receiver_next_step(state, reporting=None, agent_id=None, observation=None):
    if (isinstance(observation,dict) and observation.get('method')=='native_hook'
            and state!='rejoin_required'):
        sync=observation.get('state')
        if sync=='configured':
            return ('所選 Cursor 原聊天的公開同步綁定已由伺服器確認；沿用目前設定，下一項 App 工作自動同步。'
                '這不代替新工作實測。'+('原聊天背景收訊未支援，OFFICE 只能保存留言；沒有送達或執行證據前不能稱已收到。'
                    if state=='unsupported' else '持續收訊狀態='+state+'，與公開同步分開回報。'))
        if sync in {'revoked','unsupported'} or not observation.get('guide_path') or not observation.get('setup_command'):
            return ('入場已回報；所選 Cursor 原聊天公開同步未完成，狀態='+str(sync)+'。'
                '目前沒有可安全執行的原公開事件設定；回報 observation.reason，停止使用舊或不明憑證。'
                '不要混用 inbox 憑證、恢復已登出檔案、改接其他 session 或啟動等待工具。')
        return ('入場已回報；Cursor 公開同步尚未完成，不能因 receiver_status='+state+' 就此結束。'
            '現在讀取 conversation.observation.guide_path，依 setup_command 完成共用 Hook。'
            '如回傳 activation_marker，在本次原聊天公開最終回覆原樣獨立包含該行一次，讓原生 Hook 核對綁定；'
            '不要等待人類再貼第二份 Prompt。一次性批准仍按原平台規則，未批准時停在該步。'
            'hook_installed=true 及標記本身都不是已完成綁定或背景收訊的證據。')
    reporting=normalized_reporting({'reporting':reporting},agent_id)
    if (isinstance(reporting,dict) and reporting.get('method')=='cooperative' and
            reporting.get('state')!='agent_confirmed' and state not in {'rejoin_required','disabled'}):
        if not reporting.get('guide_path') or not reporting.get('manifest_path'):
            return ('入場已回報；收訊結果='+state+'。公開同步設定未完成（'+reporting.get('state','setup_required')+'）。'
                '目前沒有可安全使用的原公開事件指引；保留既有收訊入口，回報 reporting.reason。'
                '不要猜測路徑、混用 inbox 憑證、恢復已登出憑證或改接其他 Bot；由擁有人提供有效的新接入指引後再繼續。')
        return ('入場已回報；收訊結果='+state+'。公開同步設定尚未完成，不能就此結束。'
            '現在讀取 conversation.reporting.guide_path 及 manifest_path，為目前 Bot 實際保存長期回報規則、'
            '讀回核對，再按同一份指引提交設定確認。不要混用 inbox 憑證；毋須人類再貼第二份 Prompt。'
            '平台不能保存時如實回報未完成；設定確認不代替之後原 App 工作的實際同步驗證。')
    return {
        'ready':'原生持續收訊已設定，正常完成目前工作即可；不用重新待命或啟動等待工具。尚未驗證新的實際指令。',
        'configured_unavailable':'持續收訊入口已保存，但原平台目前不可用。OFFICE 會保留指令；平台恢復後沿用同一入口，不要重綁或啟動等待工具。',
        'unsupported':'已入場；目前平台沒有可用的原聊天背景入口。可保存留言及同步公開工作，但未能自動喚醒。不要用等待工具或替代 session 冒充持續收訊。',
        'disabled':'已入場，但擁有人已停用派工。不要自行重啟、改接其他入口或啟動等待工具。',
        'rejoin_required':'首次回報已接收，但目前身份已失效。請擁有人產生新邀請，再完成接入；不要重用舊指引、已登出憑證或改接其他平台。',
        'setup_required':'已入場；請讀取 conversation.guide_path，完成該平台的一次性原生接駁。未接通前不能自動喚醒；不要用等待工具冒充持續收訊。',
    }[state]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--invite',required=True);args=parser.parse_args()
    path=Path(args.invite).expanduser().resolve();invite=json.loads(path.read_text())
    url=urllib.parse.urlsplit(invite['endpoint'])
    if url.scheme!='http' or url.hostname not in {'127.0.0.1','localhost'} or url.path!='/api/agents/join' or url.username or url.password or url.query or url.fragment:
        raise ValueError('Invitation must use the local Office endpoint')
    claim_path=path.with_name('claim.json')
    try:
        fd=os.open(claim_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump({'claim_id':uuid.uuid4().hex,'workspace_path':str(Path.cwd())},f)
    except FileExistsError:pass
    claim=json.loads(claim_path.read_text())
    request=urllib.request.Request(invite['endpoint'],data=json.dumps(claim).encode(),method='POST',headers={
        'Content-Type':'application/json','X-Invitation-Id':invite['invitation_id'],'Authorization':'Bearer '+invite['token']})
    with urllib.request.urlopen(request,timeout=5) as response:joined=json.load(response)
    if joined.get('agent_id')!=invite['agent_id'] or joined.get('credential_path')!=invite['credential_path']:
        raise ValueError('Unexpected invitation response')
    credential=load_credential(joined['credential_path'])
    if credential['agent_id']!=invite['agent_id']:raise ValueError('Unexpected agent credential')
    run_id='join-'+invite['invitation_id'];events=[]
    for kind,text in [('turn_started','執行用戶指定的 OFFICE 接入驗證。'),('turn_completed','OFFICE 已接收接入驗證；等待用戶下一項工作。')]:
        event={'source_id':credential['source_id'],'event_id':run_id+':'+kind,'run_id':run_id,'type':kind,'text':text}
        if kind=='turn_started' and claim.get('workspace_path'):event['workspace_path']=claim['workspace_path']
        receipt=send(credential,event)
        if receipt.get('accepted') is not True:raise ValueError('Office did not accept the report')
        events.append({'type':kind,'accepted':True,'duplicate':receipt.get('duplicate',False)})
    conversation=joined.get('conversation') or {}
    if conversation.get('capability',{}).get('persistent'):
        native=receiver_result(conversation)
    else:
        try:native=auto_connect(credential)
        except Exception:native={'connected':False,'reason':'native_connection_unconfirmed'}
        conversation=native.get('conversation') or conversation
        native=receiver_result(conversation,native)
    normalized=normalized_reporting(conversation,credential['agent_id'])
    if normalized is not None:
        conversation=dict(conversation,reporting=normalized)
        native['conversation']=conversation
    reporting=reporting_result(conversation,native['receiver_status'],credential['agent_id'])
    observation=normalized_observation(conversation,credential['agent_id'])
    if observation is not None:
        conversation=dict(conversation,observation=observation)
        native['conversation']=conversation
    observed=observation_result(conversation,native['receiver_status'],credential['agent_id'])
    if observed['observation_setup_required']:
        reporting['full_onboarding_complete']=False
    print(json.dumps({'joined':True,'agent_id':credential['agent_id'],'run_id':run_id,'events':events,'guide_path':joined['guide_path'],
        'native_connection':native,'conversation':conversation,'receiver_status':native['receiver_status'],
        **reporting,**observed,'next_step':receiver_next_step(native['receiver_status'],conversation.get('reporting'),credential['agent_id'],observation)},ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except urllib.error.HTTPError as error:
        try:code=json.load(error).get('error','request_failed')
        except Exception:code='request_failed'
        print('OFFICE 接入未完成：HTTP '+str(error.code)+' '+str(code),file=sys.stderr);sys.exit(1)
    except Exception as error:
        print('OFFICE 接入未完成：'+type(error).__name__,file=sys.stderr);sys.exit(1)
