const pairingPattern=/^[A-Za-z0-9_-]{20,256}$/;
export function parsePairingInput(value,origin){
  if(typeof value!=='string')return '';
  const input=value.trim();
  if(pairingPattern.test(input))return input;
  if(input.length>2048||/[\\\u0000-\u0020\u007f]/.test(input)||!/^https:\/\/[^/?#]+\/mobile\/pair#pair=[A-Za-z0-9_-]{20,256}$/.test(input))return '';
  try{
    const url=new URL(input),expected=new URL(origin);
    if(expected.protocol!=='https:'||url.origin!==expected.origin||url.username||url.password||url.pathname!=='/mobile/pair'||url.search)return '';
    return /^#pair=([A-Za-z0-9_-]{20,256})$/.exec(url.hash)?.[1]||'';
  }catch{return '';}
}
export function takePairingToken(location, history) {
  const token=/^#pair=([A-Za-z0-9_-]{20,256})$/.exec(location.hash||'')?.[1]||'';
  if (location.hash) history.replaceState({officePairingForm:true},'',location.pathname+location.search);
  return token;
}

export function initMobilePair(doc = document, fetcher = fetch) {
  const form=doc.getElementById('mobile-pair-form'),input=doc.getElementById('pair-token'),notice=doc.getElementById('pair-notice'),button=form.querySelector('button');
  // A saved pairing page may be opened with an existing browser login.
  // Only the normal office page renews that login. Pairing remains explicit.
  let pairingIntent=history.state?.officePairingForm===true;
  notice.textContent='';
  function readFragment(){
    if(!location.hash)return;
    pairingIntent=true;
    input.value=takePairingToken(location,history);
    notice.textContent=input.value?'':'配對連結格式未能辨認。請掃描電腦 OFFICE 的新配對碼。';
  }
  readFragment();
  doc.defaultView.addEventListener('hashchange',readFragment);
  form.addEventListener('input',()=>{pairingIntent=true;notice.textContent='';});
  if(!pairingIntent)void resumeExistingLogin();

  async function resumeExistingLogin(){
    try{
      const response=await fetcher('/api/mobile/session',{
        credentials:'same-origin',cache:'no-store',redirect:'error',signal:AbortSignal.timeout(5000),
      });
      const data=await response.json();
      const active=response.ok&&data?.authenticated===true&&typeof data.csrf_token==='string'&&data.csrf_token.length>0;
      const renewable=response.status===401&&data?.error==='mobile_session_renewal_required';
      if((active||renewable)&&!pairingIntent&&!input.value&&!location.hash&&location.pathname==='/mobile/pair'){
        location.replace('/mobile/');
      }
    }catch{/* A new code can still be entered if the check is unavailable. */}
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();pairingIntent=true;if(button.disabled)return;
    const token=parsePairingInput(input.value,location.origin);
    if(!token){notice.textContent='請貼上這個 OFFICE 提供的完整私人配對連結或一次性配對碼。';return;}
    button.disabled=true;notice.textContent='正在配對…';
    try {
      const response=await fetcher('/api/mobile/pair',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,remember_device:doc.getElementById('remember-device')?.checked===true}),signal:AbortSignal.timeout(10000)});
      let data;try{data=await response.json();}catch{data={};}
      if(!response.ok)throw Object.assign(new Error(data.error||'pair_failed'),{status:response.status});
      // A scan can replace the fragment without loading a new document.
      // A response for the previous submission must not hide the new form.
      const stillSelected=parsePairingInput(input.value,location.origin)===token;
      // Old retries must not cross a freshly paired device/session boundary.
      try { for(let i=sessionStorage.length-1;i>=0;i--){const key=sessionStorage.key(i);if(key?.startsWith('office-command:'))sessionStorage.removeItem(key);} } catch {}
      if(stillSelected){input.value='';location.replace('/mobile/');}
    } catch(error) {
      if(parsePairingInput(input.value,location.origin)!==token)return;
      notice.textContent=error.status===429?'配對嘗試太頻繁，請稍後再試。':error.status===403?'請確認 Tailscale 使用指定帳戶，並向電腦 OFFICE 取得新配對碼。':error.status===400||error.status===401||error.status===410?'此配對碼已失效或已使用。已配對的手機可按「已配對？返回 OFFICE」使用原有登入。要重新配對，請向電腦 OFFICE 取得新配對碼。':'暫時未能配對。請確認 Tailscale 私人連線，再重試。';
    } finally {button.disabled=false;}
  });
  button.disabled=false;
}
if(typeof document!=='undefined'&&document.getElementById('mobile-pair-form'))initMobilePair();
