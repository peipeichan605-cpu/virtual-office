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
  if (location.hash) history.replaceState(null,'',location.pathname+location.search);
  return token;
}

export function initMobilePair(doc = document, fetcher = fetch) {
  const form=doc.getElementById('mobile-pair-form'),input=doc.getElementById('pair-token'),notice=doc.getElementById('pair-notice'),button=form.querySelector('button');
  input.value=takePairingToken(location,history);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(button.disabled)return;
    const token=parsePairingInput(input.value,location.origin);
    if(!token){notice.textContent='請貼上這個 OFFICE 提供的完整私人配對連結或一次性配對碼。';return;}
    button.disabled=true;notice.textContent='正在配對…';
    try {
      const response=await fetcher('/api/mobile/pair',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,remember_device:doc.getElementById('remember-device')?.checked===true}),signal:AbortSignal.timeout(10000)});
      let data;try{data=await response.json();}catch{data={};}
      if(!response.ok)throw Object.assign(new Error(data.error||'pair_failed'),{status:response.status});
      input.value='';
      // Old retries must not cross a freshly paired device/session boundary.
      try { for(let i=sessionStorage.length-1;i>=0;i--){const key=sessionStorage.key(i);if(key?.startsWith('office-command:'))sessionStorage.removeItem(key);} } catch {}
      location.replace('/mobile/');
    } catch(error) {
      notice.textContent=error.status===429?'配對嘗試太頻繁，請稍後再試。':error.status===403?'請確認 Tailscale 使用指定帳戶，並向電腦 OFFICE 取得新配對碼。':error.status===400||error.status===401||error.status===410?'配對碼已失效或已使用，請在電腦 OFFICE 產生新配對碼。':'暫時未能配對。請確認 Tailscale 私人連線，再重試。';
    } finally {button.disabled=false;}
  });
}
if(typeof document!=='undefined'&&document.getElementById('mobile-pair-form'))initMobilePair();
