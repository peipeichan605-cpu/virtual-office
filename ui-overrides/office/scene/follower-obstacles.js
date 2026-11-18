// Cached, floor-local furniture footprints. Read the authored components, not
// seat markers or a batch's aggregate box (which includes empty aisles).
const componentCache = new WeakMap();

function components(geometry) {
  if (componentCache.has(geometry)) return componentCache.get(geometry);
  const positions = geometry.getAttribute('position'), index = geometry.index;
  if (!positions) return [];
  const parent = new Int32Array(positions.count), vertices = new Map();
  const find = n => { while (parent[n] !== n) { parent[n] = parent[parent[n]]; n = parent[n]; } return n; };
  const join = (a,b) => { a=find(a); b=find(b); if(a!==b)parent[b]=a; };
  for(let n=0;n<positions.count;n++) {
    parent[n]=n;
    const key=[positions.getX(n),positions.getY(n),positions.getZ(n)].map(v=>Math.round(v*1e5)).join(',');
    if(vertices.has(key))join(n,vertices.get(key)); else vertices.set(key,n);
  }
  const count=index?.count ?? positions.count;
  for(let n=0;n+2<count;n+=3) {
    const a=index?index.getX(n):n,b=index?index.getX(n+1):n+1,c=index?index.getX(n+2):n+2;
    join(a,b);join(a,c);
  }
  const boxes=new Map();
  for(let n=0;n<count;n++) {
    const v=index?index.getX(n):n, id=find(v), x=positions.getX(v),y=positions.getY(v),z=positions.getZ(v);
    let b=boxes.get(id);
    if(!b)boxes.set(id,b={minX:x,maxX:x,minY:y,maxY:y,minZ:z,maxZ:z});
    else {b.minX=Math.min(b.minX,x);b.maxX=Math.max(b.maxX,x);b.minY=Math.min(b.minY,y);b.maxY=Math.max(b.maxY,y);b.minZ=Math.min(b.minZ,z);b.maxZ=Math.max(b.maxZ,z);}
  }
  const result=[...boxes.values()];componentCache.set(geometry,result);return result;
}

// Includes both unbatched and instanced furniture. Transform into the same
// frame as actor positions, excluding floor surfaces and overhead lamps.
export function collectFollowerObstacles(THREE, object, frame=object.parent) {
  frame.updateMatrixWorld(true);
  const inverse=frame.matrixWorld.clone().invert(),matrix=new THREE.Matrix4(),instance=new THREE.Matrix4();
  const box=new THREE.Box3(),point=new THREE.Vector3(),out=[];let floorBounds=null;
  const visit=node=>{
    if(node.visible===false)return;
    if(node.isMesh&&node.geometry&&!node.userData.contactShadow) {
      const count=node.isInstancedMesh?Math.min(node.count,node.instanceMatrix.count):1;
      for(let n=0;n<count;n++) {
        if(node.userData.officeInstanceMetadata?.[n]?.visible===false)continue;
        matrix.multiplyMatrices(inverse,node.matrixWorld);
        if(node.isInstancedMesh){node.getMatrixAt(n,instance);matrix.multiply(instance);}
        for(const b of components(node.geometry)) {
          box.makeEmpty();
          for(const x of [b.minX,b.maxX])for(const y of [b.minY,b.maxY])for(const z of [b.minZ,b.maxZ])box.expandByPoint(point.set(x,y,z).applyMatrix4(matrix));
          if(/^(slab|slab_open|baked_slab_top|ground-lounge-floor)$/.test(node.name)){
            floorBounds ||= {minX:Infinity,maxX:-Infinity,minZ:Infinity,maxZ:-Infinity};
            floorBounds.minX=Math.min(floorBounds.minX,box.min.x);floorBounds.maxX=Math.max(floorBounds.maxX,box.max.x);
            floorBounds.minZ=Math.min(floorBounds.minZ,box.min.z);floorBounds.maxZ=Math.max(floorBounds.maxZ,box.max.z);
          }
          // Child model is at most .72 m high; include low furniture surfaces
          // with a little animation clearance, but not the floor/ceiling.
          if(box.max.y<=.10||box.min.y>=1.05)continue;
          out.push({minX:box.min.x,maxX:box.max.x,minZ:box.min.z,maxZ:box.max.z});
        }
      }
    }
    for(const child of node.children)visit(child);
  };
  visit(object);const index=createFollowerObstacles(out);index.floorBounds=floorBounds;return index;
}

// Static broad-phase index. No Three.js geometry work occurs while following.
export function createFollowerObstacles(boxes, cellSize=2) {
  const cells=new Map();
  const key=(x,z)=>`${x},${z}`;
  for(const b of boxes)for(let x=Math.floor(b.minX/cellSize);x<=Math.floor(b.maxX/cellSize);x++)
    for(let z=Math.floor(b.minZ/cellSize);z<=Math.floor(b.maxZ/cellSize);z++) {
      const k=key(x,z);if(!cells.has(k))cells.set(k,[]);cells.get(k).push(b);
    }
  const nearby=(minX,maxX,minZ,maxZ,visit)=>{
    const seen=new Set();
    for(let x=Math.floor(minX/cellSize);x<=Math.floor(maxX/cellSize);x++)for(let z=Math.floor(minZ/cellSize);z<=Math.floor(maxZ/cellSize);z++)
      for(const b of cells.get(key(x,z))||[])if(!seen.has(b)){seen.add(b);if(visit(b))return true;}
    return false;
  };
  return {boxes,
    blocks(p,r=.4){return nearby(p.x-r,p.x+r,p.z-r,p.z+r,b=>p.x>=b.minX-r&&p.x<=b.maxX+r&&p.z>=b.minZ-r&&p.z<=b.maxZ+r);},
    crosses(a,b,r=.4){return nearby(Math.min(a.x,b.x)-r,Math.max(a.x,b.x)+r,Math.min(a.z,b.z)-r,Math.max(a.z,b.z)+r,box=>{
      let low=0,high=1;
      for(const axis of ['x','z']) {
        const d=b[axis]-a[axis],min=box[axis==='x'?'minX':'minZ']-r,max=box[axis==='x'?'maxX':'maxZ']+r;
        if(Math.abs(d)<1e-10){if(a[axis]<min||a[axis]>max)return false;}
        else {let l=(min-a[axis])/d,h=(max-a[axis])/d;if(l>h)[l,h]=[h,l];low=Math.max(low,l);high=Math.min(high,h);if(low>high)return false;}
      }
      return true;
    });},
  };
}

// Reused only while a child is travelling. Cache walkable cells for the floor;
// bound each search, then collision-check every shortcut and movement segment.
export function createFollowerNavigator(bounds, obstacles) {
  const floor=obstacles?.floorBounds;
  if(floor)bounds={minX:Math.max(bounds.minX,floor.minX),maxX:Math.min(bounds.maxX,floor.maxX),minZ:Math.max(bounds.minZ,floor.minZ),maxZ:Math.min(bounds.maxZ,floor.maxZ)};
  const spacing=.5,margin=.65,walkable=new Map(),edges=new Map();
  const nx=Math.floor((bounds.maxX-bounds.minX-2*margin)/spacing)+1,nz=Math.floor((bounds.maxZ-bounds.minZ-2*margin)/spacing)+1;
  const point=(x,z)=>({x:bounds.minX+margin+x*spacing,z:bounds.minZ+margin+z*spacing});
  const inside=p=>p.x>=bounds.minX+margin&&p.x<=bounds.maxX-margin&&p.z>=bounds.minZ+margin&&p.z<=bounds.maxZ-margin;
  const clear=p=>inside(p)&&!obstacles?.blocks(p);
  const segment=(a,b)=>inside(a)&&inside(b)&&!obstacles?.crosses(a,b);
  const available=(x,z)=>{
    if(x<0||x>=nx||z<0||z>=nz)return false;
    const id=z*nx+x;if(!walkable.has(id))walkable.set(id,clear(point(x,z)));return walkable.get(id);
  };
  const edge=(x,z,u,v)=>{
    const a=z*nx+x,b=v*nx+u,key=Math.min(a,b)*(nx*nz)+Math.max(a,b);
    if(!edges.has(key))edges.set(key,segment(point(x,z),point(u,v)));return edges.get(key);
  };
  const nearest=(p,availableCell=available,openSegment=segment)=>{
    const cx=Math.round((p.x-bounds.minX-margin)/spacing),cz=Math.round((p.z-bounds.minZ-margin)/spacing);
    for(let r=0;r<=4;r++)for(let x=cx-r;x<=cx+r;x++)for(let z=cz-r;z<=cz+r;z++)
      if((Math.abs(x-cx)===r||Math.abs(z-cz)===r)&&availableCell(x,z)&&openSegment(p,point(x,z)))return {x,z,id:z*nx+x};
    return null;
  };
  let connected=null;
  const reachable=p=>{
    if(!clear(p))return false;
    if(!connected){
      // Tiny gaps between a chair, plant and wall can hold a point but cannot
      // be entered by a child. Reserve the connected circulation area, not
      // these isolated pockets. Build/cache the static component once.
      connected=new Set();const visited=new Set();
      for(let z=0;z<nz;z++)for(let x=0;x<nx;x++)if(!visited.has(z*nx+x)&&available(x,z)){
        const region=new Set([z*nx+x]),queue=[z*nx+x];visited.add(queue[0]);
        for(let n=0;n<queue.length;n++){
          const id=queue[n],cx=id%nx,cz=Math.floor(id/nx);
          for(const [dx,dz] of [[1,0],[-1,0],[0,1],[0,-1]]){
            const u=cx+dx,v=cz+dz,next=v*nx+u;
            if(!region.has(next)&&available(u,v)&&edge(cx,cz,u,v)){region.add(next);visited.add(next);queue.push(next);}
          }
        }
        if(region.size>connected.size)connected=region;
      }
    }
    return !!nearest(p,(x,z)=>available(x,z)&&connected.has(z*nx+x));
  };
  if(obstacles)obstacles.canStand=reachable;
  return {clear,segment,reachable,
    path(from,to){
      if(segment(from,to))return [{...to}];
      const start=nearest(from),goal=nearest(to);if(!start||!goal)return [];
      const queue=[],cost=new Map([[start.id,0]]),parent=new Map(),closed=new Set();
      const push=item=>{let n=queue.length;queue.push(item);while(n){const p=(n-1)>>1;if(queue[p].score<=item.score)break;queue[n]=queue[p];n=p;}queue[n]=item;};
      const pop=()=>{const first=queue[0],last=queue.pop();if(queue.length){let n=0;while(n*2+1<queue.length){let next=n*2+1;if(next+1<queue.length&&queue[next+1].score<queue[next].score)next++;if(queue[next].score>=last.score)break;queue[n]=queue[next];n=next;}queue[n]=last;}return first;};
      push({...start,score:0});
      while(queue.length&&closed.size<5000){
        const cur=pop();if(closed.has(cur.id))continue;closed.add(cur.id);
        if(cur.id===goal.id){
          const route=[{...to}];let id=goal.id;
          while(id!==start.id){route.push(point(id%nx,Math.floor(id/nx)));id=parent.get(id);}
          route.push(point(start.x,start.z));route.reverse();
          // Remove grid stair-steps without cutting any furniture corners.
          const result=[];let at=from,index=0;
          while(index<route.length){let far=index;while(far+1<route.length&&segment(at,route[far+1]))far++;result.push(route[far]);at=route[far];index=far+1;}
          return result;
        }
        for(let dx=-1;dx<=1;dx++)for(let dz=-1;dz<=1;dz++)if(dx||dz){
          const x=cur.x+dx,z=cur.z+dz,id=z*nx+x;
          if(!available(x,z)||closed.has(id)||!edge(cur.x,cur.z,x,z))continue;
          const g=cost.get(cur.id)+Math.hypot(dx,dz);
          if(g>=(cost.get(id)??Infinity))continue;
          cost.set(id,g);parent.set(id,cur.id);push({x,z,id,score:g+Math.hypot(x-goal.x,z-goal.z)});
        }
      }
      return [];
    },
  };
}
