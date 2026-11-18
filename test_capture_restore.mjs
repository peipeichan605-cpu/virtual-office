import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
import {dirname,resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {forEachCaptureBox} from './public/office/scene/mesh-bounds.js';

const root=dirname(fileURLToPath(import.meta.url));
let threeRoot=null;
for(const base of [process.env.OFFICE_THREE_TEST_ROOT,root,process.cwd()].filter(Boolean)){
  try{const require=createRequire(resolve(base,'package.json'));threeRoot=resolve(dirname(require.resolve('three')),'..');break;}catch{}
}

// Execute the actual capture, resize and framing functions with deterministic
// browser/renderer boundaries. No Office service, invitation or Agent is used.
test('capture restores the actual live render state even when resize cache matches',{skip:!threeRoot},async t=>{
  const THREE=await import(pathToFileURL(resolve(threeRoot,'build/three.module.js')).href);
  const source=await readFile(resolve(root,'public/office/scene/scene.js'),'utf8');
  const section=(start,end)=>{
    const at=source.indexOf(start),stop=source.indexOf(end,at);
    assert.ok(at>=0&&stop>at,'actual scene function boundary must exist');
    return source.slice(at,stop);
  };
  const limitsSource=section('export function graphicsRenderLimits(', '\n// The CSS viewport').replace('export function','function');
  const resizeSource=section('function resize(', '\nconst pxPerMeter');
  const captureSource=section('export function captureScene(', '\n// The camera at its home spot').replace('export function','function');
  const frameSource=section('const shotBox = new THREE.Box3();', '\n// ---------- loop ----------');
  const factory=new Function('env',`
    const {THREE,renderer,post,camera,container,labels,building,actors,retiring,effects,document,wakeScene,forEachCaptureBox}=env;
    const clamp=(v,a,b)=>Math.min(b,Math.max(a,v));
    const isGround=id=>id==='ground';
    const WALLPAPER=false,VIEW_SPAN={w:35,h:22},PHONE_SPAN_W=29.5,UI_LEFT=230,UI_RIGHT=360;
    const HOME={x:-.4,y:-.3,z:.3},ISO_DIR=new THREE.Vector3(1,1,1).normalize();
    const focusV=new THREE.Vector3();
    let sizeSignature='';
    ${limitsSource}
    ${resizeSource}
    ${captureSource}
    ${frameSource}
    return {resize,captureScene,getSignature:()=>sizeSignature};
  `);

  function fixture({width=1280,height=720,ratio=1.5,failure=null}={}){
    const camera=new THREE.OrthographicCamera(-1,1,1,-1,.1,240);
    camera.position.set(26,34,28);camera.lookAt(0,7,0);camera.zoom=1.6;
    const mesh=new THREE.Mesh(new THREE.BoxGeometry(22,3,17),new THREE.MeshBasicMaterial());
    mesh.position.set(2,7,-3);mesh.rotation.y=.23;
    const group=new THREE.Group();group.add(mesh);group.updateMatrixWorld(true);
    const live={floorName:'Example workspace',bots:[{name:'Example Agent',task:'Example task',step:'Example step'}]};
    const walls={data:live,screen:{},board:{},screenData:live,boardData:live,
      drawScreen(data){this.screenData=data;},drawBoard(data){this.boardData=data;}};
    const inst={walls};
    const building={activeId:'ground',activeIndex:1,activeY:7,cabinY:{x:7},overview:{layout:{width:60,depth:40}},group,activeOpen:()=>inst};
    const visible=on=>{const object=new THREE.Group();object.visible=on;return object;};
    const ring=visible(true),hiddenRing=visible(false),retired=visible(true),hiddenRetired=visible(false),effect=visible(true),hiddenEffect=visible(false);
    const actors=new Map([['a',{ring}],['b',{ring:hiddenRing}]]),retiring=new Set([{root:retired},{root:hiddenRetired}]);
    const effects=[{group:effect},{group:hiddenEffect}];
    const wakes=[],sizes=[],renders=[],copies=[];
    const renderer={ratio,width,height,domElement:{width:Math.floor(width*ratio),height:Math.floor(height*ratio)},
      capabilities:{maxTextureSize:8192,maxRenderbufferSize:8192},
      getPixelRatio(){return this.ratio;},
      setPixelRatio(value){this.ratio=value;this.setSize(this.width,this.height,false);},
      setSize(w,h){this.width=w;this.height=h;this.domElement.width=Math.floor(w*this.ratio);this.domElement.height=Math.floor(h*this.ratio);sizes.push([w,h,this.ratio]);}};
    const labels={width:0,height:0,setSize(w,h){this.width=w;this.height=h;}};
    const post={width:0,height:0,
      setSize(w,h){this.width=w;this.height=h;},
      render(focus,dt,intensity){
        camera.updateMatrixWorld(true);
        renders.push({focus,dt,intensity,width:this.width,height:this.height,ratio:renderer.ratio,
          buffer:[renderer.domElement.width,renderer.domElement.height],
          frustum:[camera.left,camera.right,camera.top,camera.bottom],zoom:camera.zoom,
          view:camera.view?{...camera.view}:null,visible:[ring.visible,hiddenRing.visible,retired.visible,hiddenRetired.visible,effect.visible,hiddenEffect.visible],
          screenData:walls.screenData,boardData:walls.boardData});
        if(failure==='render'&&renders.length===1)throw Error('injected capture render');
        if(failure==='restore-render'&&renders.length===2)throw Error('injected live render');
      }};
    const drawing={imageSmoothingQuality:'low',drawImage(...args){copies.push(args);if(failure==='copy')throw Error('injected image copy');}};
    const document={createElement(kind){
      assert.equal(kind,'canvas');if(failure==='create')throw Error('injected canvas creation');
      return {width:0,height:0,getContext(type){assert.equal(type,'2d');return failure==='context'?null:drawing;}};
    }};
    const env={THREE,renderer,post,camera,container:{clientWidth:width,clientHeight:height},labels,building,actors,retiring,effects,document,wakeScene:reason=>wakes.push(reason),forEachCaptureBox};
    const api=factory(env);api.resize();camera.updateMatrixWorld(true);
    const original={position:camera.position.clone(),zoom:camera.zoom,projection:camera.projectionMatrix.clone(),
      frustum:[camera.left,camera.right,camera.top,camera.bottom],view:camera.view?{...camera.view}:null,
      ratio,width,height,signature:api.getSignature()};
    const before=sizes.length;api.resize();assert.equal(sizes.length,before,'normal repeated resize must exercise a real signature-cache hit');
    api.resize([{target:env.container}]);assert.equal(sizes.length,before,'ResizeObserver entries must not accidentally force a resize or wake');
    return {...env,api,original,wakes,sizes,renders,copies,drawing,live,
      visibility:()=>[ring.visible,hiddenRing.visible,retired.visible,hiddenRetired.visible,effect.visible,hiddenEffect.visible],
      dispose(){mesh.geometry.dispose();mesh.material.dispose();}};
  }

  function restored(f){
    const {renderer,post,camera,labels,original}=f;
    assert.equal(renderer.ratio,original.ratio,'live pixel ratio');
    assert.deepEqual([renderer.width,renderer.height],[original.width,original.height],'live canvas CSS size');
    assert.deepEqual([renderer.domElement.width,renderer.domElement.height],[Math.floor(original.width*original.ratio),Math.floor(original.height*original.ratio)],'live drawing-buffer size');
    assert.deepEqual([post.width,post.height],[original.width,original.height],'post targets return to live size');
    assert.deepEqual([labels.width,labels.height],[original.width,original.height],'label viewport');
    assert.deepEqual([camera.left,camera.right,camera.top,camera.bottom],original.frustum,'live frustum');
    assert.deepEqual(camera.view?{...camera.view}:null,original.view,'panel view offset');
    assert.equal(camera.zoom,original.zoom);assert.ok(camera.position.equals(original.position));
    assert.ok(camera.projectionMatrix.equals(original.projection),'live projection matrix');
    assert.deepEqual(f.visibility(),[true,false,true,false,true,false],'restore only originally visible capture decorations');
    assert.equal(f.building.activeOpen().walls.screenData,f.live);assert.equal(f.building.activeOpen().walls.boardData,f.live);
    assert.ok(f.wakes.includes('capture'),'a capture wakes an idle scene');
    assert.ok(f.renders.length>=2,'finally redraws the live scene');
    const redraw=f.renders.at(-1);
    assert.deepEqual([redraw.width,redraw.height],[original.width,original.height]);
    assert.deepEqual(redraw.frustum,original.frustum);assert.equal(redraw.zoom,original.zoom);
    assert.equal(redraw.screenData,f.live);assert.equal(redraw.boardData,f.live);
  }

  await t.test('successful share render restores live size, camera and decorations after a cached resize',()=>{
    const f=fixture();
    try{
      const out=f.api.captureScene({width:1600,height:900,scale:2});
      assert.deepEqual([out.width,out.height],[1600,900]);assert.equal(f.copies.length,1);assert.equal(f.drawing.imageSmoothingQuality,'high');
      const shot=f.renders[0];assert.deepEqual([shot.width,shot.height,shot.ratio],[1600,900,2]);assert.deepEqual(shot.buffer,[3200,1800]);
      assert.equal(shot.zoom,1);assert.equal(shot.view?.enabled,false);assert.deepEqual(shot.visible,[false,false,false,false,false,false]);
      for(const data of [shot.screenData,shot.boardData]){
        assert.equal(data.floorName,'Floor 2');assert.deepEqual(data.bots,[{name:'',task:'',step:''}]);assert.notEqual(data,f.live);
      }
      restored(f);
    }finally{f.dispose();}
  });

  for(const [failure,pattern]of [['render',/capture render/],['copy',/image copy/],['create',/canvas creation/],['context',/null/],['restore-render',/live render/]]){
    await t.test(`${failure} failure still restores live targets, frustum, privacy and visibility`,()=>{
      const f=fixture({failure});
      try{assert.throws(()=>f.api.captureScene({width:1600,height:900,scale:2}),pattern);restored(f);}
      finally{f.dispose();}
    });
  }

  await t.test('repeated captures with different aspect and hardware-capped scale return to a narrow live viewport',()=>{
    const f=fixture({width:840,height:900,ratio:1.25});
    try{
      f.renderer.capabilities.maxTextureSize=4096;f.renderer.capabilities.maxRenderbufferSize=2048;
      for(const [width,height]of [[1600,900],[900,1600],[720,720]]){
        const first=f.renders.length;f.api.captureScene({width,height,scale:4});restored(f);
        const shot=f.renders[first];assert.ok(Math.max(...shot.buffer)<=2048);assert.equal(shot.ratio,Math.min(4,2048/width,2048/height));
      }
      assert.equal(f.copies.length,3);assert.equal(f.original.view,null);
    }finally{f.dispose();}
  });

  await t.test('capture respects the real GL renderbuffer limit even when it needs sub-one rendering scale',()=>{
    const f=fixture({width:840,height:900,ratio:1.25});
    try{
      f.renderer.capabilities.maxTextureSize=16384;delete f.renderer.capabilities.maxRenderbufferSize;
      f.renderer.getContext=()=>({MAX_RENDERBUFFER_SIZE:123,getParameter:key=>{assert.equal(key,123);return 4096;}});
      f.api.captureScene({width:6000,height:3000,scale:4});const shot=f.renders[0];
      assert.equal(shot.ratio,4096/6000);assert.ok(Math.max(...shot.buffer)<=4096);restored(f);
    }finally{f.dispose();}
  });

  await t.test('capture while the office is not ready returns null without changing render dimensions',()=>{
    const f=fixture();
    try{f.building.activeOpen=()=>null;assert.equal(f.api.captureScene(),null);assert.equal(f.renders.length,0);assert.equal(f.copies.length,0);assert.equal(f.renderer.width,f.original.width);}
    finally{f.dispose();}
  });
});
