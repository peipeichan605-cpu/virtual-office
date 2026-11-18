// Geometry-only preferences: never persist an Agent identity, draft, or transcript.
const FRAME_KEY='office-dialogue-frame-v1';
const finite=(value,fallback)=>Number.isFinite(value)?value:fallback;
const clamp=(value,min,max)=>Math.min(Math.max(value,min),Math.max(min,max));

function bounds(viewport){
  const width=Math.max(1,finite(viewport.width,1)),height=Math.max(1,finite(viewport.height,1));
  const padX=Math.min(12,width/4),padY=Math.min(12,height/4);
  const left=finite(viewport.left,0)+padX,top=finite(viewport.top,0)+padY;
  return {left,top,right:left+width-padX*2,bottom:top+height-padY*2,
    width:width-padX*2,height:height-padY*2};
}

export function clampDialogueFrame(frame={},viewport){
  const b=bounds(viewport);
  const width=clamp(finite(frame.width,440),Math.min(320,b.width),b.width);
  const height=clamp(finite(frame.height,400),Math.min(260,b.height),b.height);
  return {left:clamp(finite(frame.left,b.right-width),b.left,b.right-width),
    top:clamp(finite(frame.top,b.top+60),b.top,b.bottom-height),width,height};
}

export function changeDialogueFrame(frame,edge,dx,dy,viewport){
  const start=clampDialogueFrame(frame,viewport),b=bounds(viewport);
  dx=finite(dx,0);dy=finite(dy,0);
  if(edge==='move')return clampDialogueFrame({...start,left:start.left+dx,top:start.top+dy},viewport);
  let left=start.left,top=start.top,right=left+start.width,bottom=top+start.height;
  const minWidth=Math.min(320,b.width),minHeight=Math.min(260,b.height);
  if(edge.includes('w'))left=clamp(left+dx,b.left,right-minWidth);
  if(edge.includes('e'))right=clamp(right+dx,left+minWidth,b.right);
  if(edge.includes('n'))top=clamp(top+dy,b.top,bottom-minHeight);
  if(edge.includes('s'))bottom=clamp(bottom+dy,top+minHeight,b.bottom);
  return {left,top,width:right-left,height:bottom-top};
}

/** Move the existing reader, preserving its composer, requests, draft and message nodes. */
export class DialogueWindow {
  constructor({element,handle,home,list,onChange=()=>{},onDock=()=>{}}){
    this.element=element;this.handle=handle;this.home=home;this.list=list;
    this.onChange=onChange;this.onDock=onDock;this.next=element.nextSibling;
    this.floating=false;this.opened=false;this.disposed=false;this.gesture=null;
    this.cleanups=[];this.resizeHandles=[];this.frame=null;this.originalStyle=null;
    this.listen=(target,type,fn,options)=>{target.addEventListener(type,fn,options);this.cleanups.push(()=>target.removeEventListener(type,fn,options));};
    this.listen(handle,'pointerdown',event=>this.start(event,'move',handle));
    this.listen(handle,'keydown',event=>this.key(event,'move'));
    this.listen(handle,'dblclick',event=>{event.preventDefault();event.stopPropagation();if(this.floating)this.dock();});
    for(const edge of ['w','e','n','s','se']){
      const node=document.createElement('button');node.type='button';
      node.className='speech-window-resize is-'+({w:'left',e:'right',n:'top',s:'bottom',se:'se'})[edge];node.dataset.edge=edge;
      node.setAttribute('aria-label',({w:'調整對話框左邊',e:'調整對話框右邊',n:'調整對話框上邊',s:'調整對話框下邊',se:'調整對話框大小'})[edge]);
      node.title='拖拉調整大小；方向鍵微調，Shift 加快';
      this.listen(node,'pointerdown',event=>this.start(event,edge,node));
      this.listen(node,'keydown',event=>this.key(event,edge));
      element.append(node);this.resizeHandles.push(node);
    }
    // A portalled reader must not pan the scene or trigger seat movement.
    for(const type of ['pointerdown','pointerup','pointermove','click','dblclick','wheel','touchstart','touchmove'])
      this.listen(element,type,event=>event.stopPropagation());
    this.onResize=()=>{if(this.gesture)this.finish(true);if(this.floating)this.apply(this.frame);};
    this.onBlur=()=>this.finish(true);
    this.onVisibility=()=>{if(document.hidden)this.finish(true);};
    this.escape=event=>{if(event.key==='Escape'&&this.gesture){event.preventDefault();event.stopPropagation();this.finish(true);}};
    this.label('Agent');
  }
  viewport(){
    const visual=window.visualViewport;
    return {width:visual?.width||window.innerWidth,height:visual?.height||window.innerHeight,
      left:visual?.offsetLeft||0,top:visual?.offsetTop||0};
  }
  read(){
    try{
      const saved=JSON.parse(window.localStorage.getItem(FRAME_KEY)||'null');
      if(saved&&typeof saved.floating==='boolean'&&saved.frame&&
        ['left','top','width','height'].every(key=>Number.isFinite(saved.frame[key])))
        return {floating:saved.floating,frame:clampDialogueFrame(saved.frame,this.viewport())};
    }catch{/* Private browsers may refuse storage. */}
    return null;
  }
  save(floating=this.floating){
    if(!this.frame)return;
    try{window.localStorage.setItem(FRAME_KEY,JSON.stringify({floating,frame:{...this.frame}}));}catch{/* Optional layout preference only. */}
  }
  label(name){
    this.handle.setAttribute('aria-label','移動 '+String(name||'Agent')+' 的對話框');
    this.handle.title='拖曳移動對話框；方向鍵微調，Shift 加快；雙擊回到頭頂';
    this.element.setAttribute('aria-label',String(name||'Agent')+' 的公開對話');
  }
  open(){
    if(this.disposed||this.opened)return;
    this.opened=true;
    window.addEventListener('resize',this.onResize);window.addEventListener('blur',this.onBlur);
    window.visualViewport?.addEventListener('resize',this.onResize);
    window.visualViewport?.addEventListener('scroll',this.onResize);
    document.addEventListener('visibilitychange',this.onVisibility);
    const saved=this.read();
    if(saved)this.frame=saved.frame;
    if(saved?.floating)this.float({persist:false});
  }
  preserveMove(target,before=null){
    const focus=this.element.contains(document.activeElement)?document.activeElement:null;
    const top=this.list?.scrollTop,outerTop=this.element.scrollTop;
    target.insertBefore(this.element,before?.parentNode===target?before:null);
    focus?.focus({preventScroll:true});
    if(this.list&&Number.isFinite(top))this.list.scrollTop=top;
    this.element.scrollTop=outerTop;
  }
  float({fromAnchor=false,persist=true}={}){
    if(this.disposed||!this.opened||this.floating)return;
    const reading=this.readingPosition();
    const anchor=this.element.getBoundingClientRect();
    this.originalStyle={};
    for(const key of ['position','left','top','right','bottom','width','height','maxHeight','maxWidth','transform','translate'])
      this.originalStyle[key]=this.element.style[key];
    this.originalListMaxHeight=this.list?.style.maxHeight;
    const saved=this.read();
    const initial={left:anchor.left,top:anchor.top,
      width:Math.max(anchor.width,fromAnchor?320:440),height:Math.max(anchor.height,fromAnchor?360:420)};
    this.frame=clampDialogueFrame(fromAnchor?initial:this.frame||saved?.frame||initial,this.viewport());
    this.floating=true;this.element.classList.add('office-dialogue-window');
    this.element.style.position='fixed';this.element.style.right='auto';this.element.style.bottom='auto';
    this.element.style.maxHeight='none';this.element.style.maxWidth='none';
    this.element.style.transform='none';this.element.style.translate='none';
    if(this.list)this.list.style.maxHeight='none';
    this.preserveMove(document.body);this.apply(this.frame,reading);this.onChange();
    if(persist)this.save(true);
  }
  readingPosition(){
    const top=this.list?.scrollTop;
    return Number.isFinite(top)?{top,bottom:this.list.scrollHeight-top-this.list.clientHeight<48}:null;
  }
  restoreReadingPosition(reading){
    if(this.list&&reading)this.list.scrollTop=reading.bottom?this.list.scrollHeight:reading.top;
  }
  apply(frame,reading=null){
    this.frame=clampDialogueFrame(frame,this.viewport());
    const resizing=this.frame.width!==parseFloat(this.element.style.width)||this.frame.height!==parseFloat(this.element.style.height);
    if(!reading&&resizing)reading=this.readingPosition();
    for(const key of ['left','top','width','height'])this.element.style[key]=this.frame[key]+'px';
    // Resize synchronously follows the user's current reading position. A
    // keyboard/viewport change keeps latest replies in view without scheduling
    // a later jump that could override a subsequent manual scroll.
    this.restoreReadingPosition(reading);
  }
  restore(){
    if(!this.floating)return;
    const reading=this.readingPosition();
    this.floating=false;this.element.classList.remove('office-dialogue-window');
    for(const [key,value] of Object.entries(this.originalStyle||{}))this.element.style[key]=value||'';
    if(this.list)this.list.style.maxHeight=this.originalListMaxHeight||'';
    this.preserveMove(this.home,this.next);this.restoreReadingPosition(reading);this.originalStyle=null;this.onChange();
  }
  dock({persist=true}={}){
    if(this.disposed)return;
    this.finish(true);this.restore();if(persist)this.save(false);this.onDock();
  }
  start(event,edge,target){
    if(this.disposed||!this.opened||this.gesture||event.button!==0||event.isPrimary===false)return;
    const interactive=event.target?.closest?.('button,a,input,textarea,select,[contenteditable]');
    if(interactive&&interactive!==target)return;
    event.preventDefault();event.stopPropagation();
    const previous=this.frame?{...this.frame}:null;
    const move=next=>{
      const g=this.gesture;if(!g||next.pointerId!==g.id)return;
      const dx=next.clientX-g.x,dy=next.clientY-g.y;
      if(!g.moved&&Math.abs(dx)+Math.abs(dy)<4)return;
      next.preventDefault();next.stopPropagation();
      if(!g.moved){
        this.float({fromAnchor:true,persist:false});g.start={...this.frame};g.moved=true;
        // Reparenting can release capture; acquire it again on the same handle.
        try{target.setPointerCapture(g.id);}catch{/* Pointer may already have ended. */}
      }
      this.apply(changeDialogueFrame(g.start,g.edge,dx,dy,this.viewport()));
    };
    const up=next=>{if(next.pointerId===this.gesture?.id){next.stopPropagation();this.finish(false);}};
    const cancel=next=>{
      if(next.pointerId!==this.gesture?.id)return;
      // A DOM move may queue a lost-capture event before capture is reacquired.
      if(next.type==='lostpointercapture'&&target.hasPointerCapture?.(next.pointerId))return;
      this.finish(true);
    };
    this.gesture={id:event.pointerId,x:event.clientX,y:event.clientY,edge,target,previous,
      wasFloating:this.floating,moved:false,move,up,cancel};
    target.addEventListener('pointermove',move);target.addEventListener('pointerup',up);
    target.addEventListener('pointercancel',cancel);target.addEventListener('lostpointercapture',cancel);
    document.addEventListener('keydown',this.escape,true);
    try{target.setPointerCapture(event.pointerId);}catch{/* Keyboard remains available. */}
  }
  finish(cancel=false){
    const g=this.gesture;if(!g)return;
    this.gesture=null;
    g.target.removeEventListener('pointermove',g.move);g.target.removeEventListener('pointerup',g.up);
    g.target.removeEventListener('pointercancel',g.cancel);g.target.removeEventListener('lostpointercapture',g.cancel);
    document.removeEventListener('keydown',this.escape,true);
    try{if(g.target.hasPointerCapture(g.id))g.target.releasePointerCapture(g.id);}catch{/* Already released on blur/removal. */}
    if(cancel&&g.moved){if(g.wasFloating)this.apply(g.previous);else{this.restore();this.frame=g.previous;}}
    else if(g.moved)this.save(true);
  }
  key(event,edge){
    if(!this.opened||this.disposed||event.altKey||event.ctrlKey||event.metaKey)return;
    const direction={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[event.key];
    if(!direction)return;
    event.preventDefault();event.stopPropagation();
    this.finish(true);this.float({fromAnchor:true,persist:false});
    const step=event.shiftKey?32:8;
    this.apply(changeDialogueFrame(this.frame,edge,direction[0]*step,direction[1]*step,this.viewport()));this.save(true);
  }
  close(){
    if(!this.opened)return;
    this.finish(true);this.opened=false;this.restore();
    window.removeEventListener('resize',this.onResize);window.removeEventListener('blur',this.onBlur);
    window.visualViewport?.removeEventListener('resize',this.onResize);
    window.visualViewport?.removeEventListener('scroll',this.onResize);
    document.removeEventListener('visibilitychange',this.onVisibility);
  }
  dispose(){
    if(this.disposed)return;
    this.close();this.disposed=true;
    for(const cleanup of this.cleanups)cleanup();this.cleanups=[];
    for(const node of this.resizeHandles)node.remove();this.resizeHandles=[];
  }
}
