import {ensureMobileSession,rememberMobileDevice,logoutMobileDevice} from './mobile-access.js';
import {mountHeaderAction} from './header-actions.js';

export function mobileLoginSummary(device) {
  if(!device?.remembered)return '目前只保留短期登入。離開太久或登入到期後，需要重新掃碼配對。';
  const expiry=Number(device.renewal_expires_at);
  const until=Number.isFinite(expiry)?new Date(expiry*1000).toLocaleDateString():'指定期限';
  return `已保留此手機登入，最遲至 ${until}；連續 7 日未使用會到期。Tailscale 保持已連線時，日常重開 OFFICE 會自動續期。`;
}

/** Explicit controls for a paired browser; merely opening never opts it in. */
export function mountMobileSessionControls({doc=document,initial,
  read=()=>ensureMobileSession({refresh:true}),remember=rememberMobileDevice,
  logout=logoutMobileDevice,mount=mountHeaderAction,events=globalThis}={}) {
  if(doc.getElementById('office-mobile-login'))return ()=>{};
  const make=(tag,text,className)=>{const node=doc.createElement(tag);if(text)node.textContent=text;if(className)node.className=className;return node;};
  const dialog=make('dialog',null,'mobile-session-dialog');
  // A lightweight DOM fixture may lack native dialogs; never block 3D startup.
  if(typeof dialog.showModal!=='function')return ()=>{};
  const launcher=make('button','手機登入','office-mobile-login');launcher.id='office-mobile-login';launcher.type='button';
  launcher.setAttribute('aria-haspopup','dialog');launcher.setAttribute('aria-label','手機登入設定');
  const title=make('h2','手機登入');title.id='mobile-session-title';dialog.setAttribute('aria-labelledby',title.id);
  const summary=make('p');
  const explanation=make('p','只在你自己的手機使用「保留登入」。最長 30 日，連續 7 日未使用會失效；擁有人可以隨時撤銷。這不會增加可查看或發訊息的 Agents。');
  const daily=make('p','日常使用：打開 Tailscale，確認 Connected／已連線，再打開已收藏的 OFFICE。不要收藏一次性配對連結；到期或清除瀏覽器資料後，才需重新配對。');
  const notice=make('p',null,'mobile-session-notice');notice.setAttribute('role','status');notice.setAttribute('aria-live','polite');
  const change=make('button'),signout=make('button','登出並撤銷此手機'),close=make('button','關閉');
  for(const button of [change,signout,close])button.type='button';
  change.className='mobile-session-primary';signout.className='mobile-session-revoke';
  const actions=make('div',null,'mobile-session-actions');actions.append(change,signout,close);
  dialog.append(title,summary,explanation,daily,notice,actions);doc.body.append(dialog);
  const unmount=mount(launcher,{simple:true,doc});let current=initial,busy=false,epoch=0;
  const render=()=>{summary.textContent=mobileLoginSummary(current?.device);change.textContent=current?.device?.remembered?'關閉自動續期':'在此手機保留登入';change.disabled=busy||!current;signout.disabled=busy||!current;};
  const errorText=error=>error?.status===401?'登入已失效，請重新配對。':error?.status===403?'未能核對登入，請關閉後重開此視窗。':'暫時未能完成，請確認 Tailscale 已連線，再重試。';
  launcher.addEventListener('click',async()=>{const turn=++epoch;notice.textContent='正在核對登入…';busy=true;render();if(!dialog.open)dialog.showModal();
    try{const value=await read();if(turn===epoch){current=value;notice.textContent='';}}
    catch(error){if(turn===epoch)notice.textContent=errorText(error);}finally{if(turn===epoch){busy=false;render();}}
  });
  change.addEventListener('click',async()=>{if(busy||!current)return;const turn=epoch,enabled=!current.device?.remembered;busy=true;notice.textContent='正在儲存…';render();
    try{const value=await remember(enabled);if(turn===epoch){current=value;notice.textContent=enabled?'已保留此手機登入。之後直接重開 OFFICE 即可。':'已關閉自動續期；目前短期登入仍然有效。';}}
    catch(error){if(turn===epoch)notice.textContent=errorText(error);}finally{if(turn===epoch){busy=false;render();}}
  });
  signout.addEventListener('click',async()=>{if(busy||!current)return;busy=true;notice.textContent='正在登出…';render();
    try{await logout();}catch(error){busy=false;notice.textContent=errorText(error);render();}
  });
  close.addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{epoch++;busy=false;});
  const dispose=()=>{epoch++;unmount();dialog.remove();events.removeEventListener?.('office:mobile-locked',dispose);};
  events.addEventListener?.('office:mobile-locked',dispose);render();return dispose;
}
