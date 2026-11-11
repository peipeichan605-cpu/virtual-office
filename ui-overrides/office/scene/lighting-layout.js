// A fitted lighting volume for the large, asymmetric G/F; native floors retain
// their original camera envelope. No renderer state is needed to validate it.
export function shadowVolume(layout=null){
  if(!layout)return {x:8.3,z:6.3,y0:-.05,y1:3,cx:0,cz:0,distance:30};
  const x=layout.width/2+.3,z=layout.depth/2+.3,y0=-.05,y1=4.8;
  return {x,z,y0,y1,cx:layout.center.x,cz:layout.center.z,distance:Math.max(30,Math.hypot(x,z,(y1-y0)/2)+6)};
}

// The expanded hall needs more texels for the same small furniture shadows.
// Keep a single sun map and return to the native size on workspace floors.
export function shadowMapSize(overview=false,coarse=false,maxTextureSize=Infinity){
  const desired=(coarse?1024:2048)*(overview?2:1);
  const cap=Number.isFinite(maxTextureSize)?Math.max(1,maxTextureSize):desired;
  return 2**Math.floor(Math.log2(Math.min(desired,cap)));
}

// Three.js does not resize an already allocated shadow target from mapSize alone.
// Dispose only on a real floor/quality change; the renderer recreates it next pass.
export function resizeShadowMap(shadow,size){
  if(shadow.mapSize.x===size&&shadow.mapSize.y===size)return false;
  shadow.map?.dispose();shadow.map=null;
  shadow.mapPass?.dispose();shadow.mapPass=null;
  shadow.mapSize.setScalar(size);shadow.needsUpdate=true;
  return true;
}

// Same warm emitter as the native desk pendants. The large open lobby uses a
// wider, softer cone; keep this local instead of raising the room's ambient.
export const NATIVE_LAMP = Object.freeze({color:0xffbb74,intensity:22,angle:.78,penumbra:.75,decay:2});

export function lobbyLightLayout(layout){
  if(!layout?.lounges?.length||!layout.pantry)return [];
  const centre=(groups)=>({x:groups.reduce((n,g)=>n+g.x,0)/groups.length,z:groups.reduce((n,g)=>n+g.z,0)/groups.length});
  // These are the permanent central leisure zones; overflow remains lit by the
  // shared night sky rather than allocating unbounded extra GPU lights.
  const front=centre(layout.lounges.slice(0,4));
  const rear=centre(layout.lounges.slice(4,6).length?layout.lounges.slice(4,6):layout.lounges.slice(0,4));
  return [
    {id:'lounge-front',...front,y:5.7,angle:1.3,intensity:NATIVE_LAMP.intensity*4},
    {id:'lounge-rear',...rear,y:5.7,angle:1.3,intensity:NATIVE_LAMP.intensity*4},
    {id:'pantry',x:layout.pantry.x,z:layout.pantry.z,y:4.3,angle:.92,intensity:NATIVE_LAMP.intensity},
  ];
}
