// The ground floor is a view of the selected roster, never a second enrollment.
export const GROUND_ID='office-ground';
export const OFFICE_CAPACITIES=Object.freeze([8,6,4,6,8]);
export const isGround=(id)=>id===GROUND_ID;
const unassigned=(id)=>id==='workspace-unassigned'||id?.startsWith('workspace-unassigned-');
export function withGroundFloor(state){
  const bots=Object.fromEntries(Object.entries(state.bots).map(([id,bot])=>[id,unassigned(bot.floor)?{...bot,floor:GROUND_ID}:bot]));
  const ids=Object.keys(bots).sort();
  const counts={working:0,idle:0,waiting_user:0,error:0,stuck:0,done:0};
  for(const bot of Object.values(bots)){if(bot.is_subagent)continue;const status=bot.effective_status||bot.status;if(status in counts)counts[status]++;}
  const floors=state.floors.filter(f=>!isGround(f.id)&&!unassigned(f.id)).map((floor,i)=>({...floor,order:i+1}));
  return {...state,bots,floors:[{id:GROUND_ID,name:'全員工作總覽',kind:'overview',order:0,bots:ids,counts,free_desks:0},...floors]};
}
export function selectionFloor(state,current,id){return isGround(current)?current:state.bots[id]?.floor||current;}
// All architectural furniture follows the native building axes. Saved main IDs
// identify lounge seats; each office keeps its original IDs and appends capacity.
export function groundLayout(count=0){
  const mainCount=Math.min(400,Math.max(36,Math.ceil(Math.max(0,Number(count)||0)/6)*6));
  const groupCount=Math.ceil(mainCount/6),quarter=Math.PI/2;
  const base=[[-6,2.2],[5.5,2.2],[-6.8,10],[6.5,10.8],[-4.8,18.2],[8.7,18]];
  const names=['花園方庭','影音休閒','雙排會客','閱讀 L 座','茶聚長廊','共享 L 座'];
  // Sofa anchor/yaw, followed by three individual armchair anchors/yaws.
  const plans=[
    {style:'square',sofa:[0,-1.8,0],chairs:[[-2.3,.2,quarter],[2.3,.2,3*quarter],[0,2.1,Math.PI]]},
    {style:'L',sofa:[0,1.8,Math.PI],chairs:[[-2.4,-1.45,quarter],[-2.4,.05,quarter],[-2.4,1.55,quarter]]},
    {style:'parallel',sofa:[-1.8,0,quarter],chairs:[[1.8,-1.55,3*quarter],[1.8,0,3*quarter],[1.8,1.55,3*quarter]]},
    {style:'L',sofa:[0,-1.8,0],chairs:[[-2.2,-.6,quarter],[-2.2,.95,quarter],[-2.2,2.5,quarter]]},
    {style:'parallel',sofa:[0,1.8,Math.PI],chairs:[[-1.6,-1.6,0],[0,-1.6,0],[1.6,-1.6,0]]},
    {style:'L',sofa:[1.8,0,3*quarter],chairs:[[-1.8,-1.8,0],[-.2,-1.8,0],[1.4,-1.8,0]]},
  ];
  const seats=[],lounges=[];
  const rotate=(x,z,angle)=>({x:x*Math.cos(angle)+z*Math.sin(angle),z:-x*Math.sin(angle)+z*Math.cos(angle)});
  for(let group=0;group<groupCount;group++){
    const extra=group-6,[x,z]=base[group]||[[-6.5,3.1,12.7][extra%3],27.4+Math.floor(extra/3)*7.8];
    const plan=plans[group%plans.length],cinema=group===1,sofaRow=group===4;
    const cluster={id:'lounge-'+(group+1),label:names[group]||'休閒角落 '+(group+1),x,z,rotation:0,cinema,style:sofaRow?'sofa-row':plan.style,seat_ids:[]};
    const [sofaX,sofaZ,sofaYaw]=plan.sofa;cluster.sofa={x:x+sofaX,z:z+sofaZ,rotation:sofaYaw};
    if(sofaRow)cluster.sofas=[-1.64,1.64].map(dx=>({x:x+dx,z:z+1.8,rotation:Math.PI,seat_ids:[]}));
    for(let within=0;within<6&&group*6+within<mainCount;within++){
      const id=`gf-main-${String(group*6+within+1).padStart(3,'0')}`;
      const sofaSeat=sofaRow||within<3,anchor=sofaRow?cluster.sofas[Math.floor(within/3)]:cluster.sofa;
      const at=sofaSeat?rotate((within%3-1)*1.03,.05,anchor.rotation):null;
      const [chairX,chairZ,chairYaw]=sofaSeat?[0,0,0]:plan.chairs[within-3];
      seats.push({id,room_id:'lounge',lounge_id:cluster.id,kind:'lounge',lounge:true,
        furniture:sofaSeat?'sofa':'armchair',within,label:`${cluster.label} · ${within+1} 號休息位`,
        x:sofaSeat?anchor.x+at.x:x+chairX,z:sofaSeat?anchor.z+at.z:z+chairZ,y:.42,yaw:sofaSeat?anchor.rotation:chairYaw});cluster.seat_ids.push(id);
      if(sofaRow)anchor.seat_ids.push(id);
    }
    lounges.push(cluster);
  }
  const positions=[{x:-11.6,z:-10.4,entry:'south'},{x:0,z:-10.4,entry:'south'},{x:11.6,z:-10.4,entry:'south'},{x:-20,z:2.6,entry:'east'},{x:-20,z:13,entry:'east'}];
  const rooms=positions.map((position,index)=>{
    const number=index+1,capacity=OFFICE_CAPACITIES[index];
    const room={...position,id:`office-${number}`,label:`辦公室 ${number} · ${capacity} 位`,width:10.2,depth:8.6,capacity,desk_width:capacity===8?1.9:capacity===6?2.35:2.7,layout:capacity===4?'L':'bench'};
    room.minX=room.x-room.width/2;room.maxX=room.x+room.width/2;room.minZ=room.z-room.depth/2;room.maxZ=room.z+room.depth/2;
    room.entrance={side:room.entry,width:2.2,x:room.entry==='east'?room.maxX:room.x,z:room.entry==='south'?room.maxZ:room.z+(index===4?2.8:0)};room.desk_ids=[];
    const pattern=capacity===4?[[-1.6,-2.6,0],[1.6,-2.6,0],[-3.6,-.4,quarter],[-3.6,2.4,quarter]]:
      [-1.25,1.25].flatMap((dz,row)=>(capacity===8?[-3.45,-1.55,1.55,3.45]:[-2.35,0,2.35]).map(dx=>[dx,dz,row===0?Math.PI:0]));
    pattern.forEach(([dx,dz,rotation],desk)=>{
      const id=`gf-office-${number}-${String(desk+1).padStart(3,'0')}`;room.desk_ids.push(id);
      seats.push({id,room_id:room.id,kind:'desk',label:`${room.label} · ${desk+1} 號工作台`,x:room.x+dx,z:room.z+dz,desk_width:room.desk_width,rotation,yaw:(Math.PI+rotation)%(2*Math.PI)});
    });return room;
  });
  const ceo={id:'ceo-office',label:'CEO ROOM',x:-21.5,z:-9.3,width:8.8,depth:11.8,minX:-25.9,maxX:-17.1,minZ:-15.2,maxZ:-3.4,capacity:1,
    entrance:{side:'south',width:2.2,x:-21.5,z:-3.4},desk_ids:['gf-ceo-001']};
  seats.push({id:'gf-ceo-001',room_id:ceo.id,kind:'desk',executive:true,label:'CEO ROOM · 行政工作台',x:-21.5,z:-11,desk_width:3.6,rotation:Math.PI,yaw:0});
  const minX=-26.5,maxX=19,minZ=-17,maxZ=Math.max(24,...lounges.map(group=>group.z+4.7));
  return {width:maxX-minX,depth:maxZ-minZ,minX,maxX,minZ,maxZ,center:{x:(minX+maxX)/2,z:(minZ+maxZ)/2},
    seats,rooms,ceo,lounges,pantry:{x:-20.4,z:21.1,width:8.2,depth:4.8,label:'Pantry · 茶水間'},columns:6,rows:Math.ceil(mainCount/6),mainCount,
    aisles:[{x:0,z:-4.2,width:33,depth:2.7},{x:-13.2,z:7.5,width:3.4,depth:24}],
  };
}
