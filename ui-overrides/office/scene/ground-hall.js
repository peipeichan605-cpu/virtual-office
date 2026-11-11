import * as THREE from 'three';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { WallDisplays } from './walls.js';
import { groundLayout } from '../overview.js';
import {NATIVE_LAMP,lobbyLightLayout} from './lighting-layout.js';

// Reuse the same native office.glb furniture as the workspace floors. Materials
// belong to this instance; original template geometries/textures stay shared.
export class GroundHall {
  constructor(assets){
    this.assets=assets;this.native=assets?.templates?.floor_open;
    this.root=new THREE.Group();this.root.name='ground-overview';
    this.obj=new THREE.Group();this.root.add(this.obj);this.alpha=0;
    this.closed=new THREE.Group();this.closed.name='ground-podium';
    this.ownedGeometries=new Set();this.ownedMaterials=new Set();this.ownedTextures=new Set();this.labels=[];
    this.lobbyLights=[];this.lobbyLightLevel=0;
    this.laptopTemplate=this.makeLaptopTemplate();
    this.configure(0);this.setShown(false);
  }
  // Native laptop bases share a combined GLB mesh. Extract its first laptop's
  // triangles once, then reuse that template without remodelling the furniture.
  makeLaptopTemplate(){
    const bases=this.native?.getObjectByName('laptops'),screen=this.native?.getObjectByName('laptop_screen_01');
    if(!bases||!screen)return null;
    const group=new THREE.Group();group.name='native-single-laptop';
    bases.traverse(mesh=>{
      if(!mesh.isMesh)return;
      const source=mesh.geometry,positions=source.getAttribute('position'),indices=source.index;
      const count=indices?.count||positions.count,kept=[];
      for(let index=0;index<count;index+=3){
        const triangle=[0,1,2].map(offset=>indices?indices.getX(index+offset):index+offset);
        if(triangle.every(vertex=>positions.getX(vertex)>-5.94&&positions.getX(vertex)<-5.46&&positions.getZ(vertex)>-3.23&&positions.getZ(vertex)<-2.67))kept.push(...triangle);
      }
      if(!kept.length)return;
      const selected=source.clone();selected.setIndex(kept);selected.clearGroups();
      const geometry=selected.toNonIndexed();selected.dispose();geometry.computeBoundingBox();geometry.computeBoundingSphere();
      const part=new THREE.Mesh(geometry,mesh.material);part.position.set(5.7,0,2.9);group.add(part);
    });
    if(!group.children.length)return null;
    const display=screen.clone(true);display.position.add(new THREE.Vector3(5.7,0,2.9));group.add(display);
    return group;
  }
  material(color,options={}){const material=new THREE.MeshStandardMaterial({color,roughness:.83,...options});this.ownedMaterials.add(material);return material;}
  box(root,material,w,h,d,x,y,z,name=''){
    const geometry=new THREE.BoxGeometry(w,h,d);this.ownedGeometries.add(geometry);
    const mesh=new THREE.Mesh(geometry,material);mesh.position.set(x,y,z);mesh.name=name;mesh.castShadow=mesh.receiveShadow=true;root.add(mesh);return mesh;
  }
  addNative(name,root,x,y,z,{size,rotation=0,straighten=false,template,palette={},glass=false}={}){
    const source=template||this.native?.getObjectByName(name);if(!source)return null;
    const wrapper=new THREE.Group(),object=source.clone(true);wrapper.name='native-'+name;
    wrapper.userData.nativeModule=name;wrapper.add(object);
    if(straighten)object.rotation.set(0,0,0);
    object.traverse(mesh=>{
      if(!mesh.isMesh)return;
      const materials=(Array.isArray(mesh.material)?mesh.material:[mesh.material]).map(original=>{
        let owned;
        if(glass)owned=new THREE.MeshStandardMaterial({name:'ground-glass',color:'#9eaaa0',transparent:true,opacity:.19,roughness:.38,metalness:.12,depthWrite:false,side:THREE.DoubleSide});
        else if(original.isMeshBasicMaterial&&name!=='wall_screen')owned=new THREE.MeshStandardMaterial({name:original.name,map:original.map,color:original.color,roughness:.86,side:original.side});
        else owned=original.clone();
        if(palette[original.name])owned.color.set(palette[original.name]);
        this.ownedMaterials.add(owned);return owned;
      });mesh.material=Array.isArray(mesh.material)?materials:materials[0];mesh.castShadow=mesh.receiveShadow=!glass;
    });
    object.updateMatrixWorld(true);
    const bounds=new THREE.Box3().setFromObject(object),dimensions=bounds.getSize(new THREE.Vector3());
    const center=bounds.getCenter(new THREE.Vector3());
    object.position.x-=center.x;object.position.y-=bounds.min.y;object.position.z-=center.z;
    // Resize in the wrapper's axes, after the native object's authored rotation.
    // wall_screen is authored at 90 degrees: scaling its local Z would stretch
    // the thin normal instead of making the television wider.
    if(size)for(const key of ['x','y','z'])if(size[key]!=null&&dimensions[key]>1e-6)wrapper.scale[key]=size[key]/dimensions[key];
    wrapper.position.set(x,y,z);wrapper.rotation.y=rotation;root.add(wrapper);this.nativeModules.add(name);
    if(this.shadowMaterial&&['table_1','sofa','armchair_1','coffee_table','tree_2','coffee_bar','bar_stools'].includes(name)){
      const shadow=new THREE.Mesh(this.shadowGeometry,this.shadowMaterial);shadow.name='ground-contact-shadow';shadow.userData.contactShadow=true;
      const width=dimensions.x*wrapper.scale.x,depth=dimensions.z*wrapper.scale.z;
      shadow.scale.set(width*1.12,1,depth*1.15);shadow.position.set(x,y+.008,z);shadow.rotation.y=rotation;shadow.renderOrder=1;root.add(shadow);
    }
    return wrapper;
  }
  label(text,x,z){
    const element=document.createElement('div');element.className='ground-office-label';element.textContent=text;
    Object.assign(element.style,{font:'600 11px/1.3 system-ui',color:'#65513c',background:'rgba(255,251,242,.91)',border:'1px solid #dac9af',borderRadius:'12px',padding:'5px 9px',whiteSpace:'nowrap',pointerEvents:'none',boxShadow:'0 2px 9px #55371912'});
    const label=new CSS2DObject(element);label.position.set(x,.15,z);this.obj.add(label);this.labels.push(label);return label;
  }
  woodTexture(){
    const canvas=document.createElement('canvas');canvas.width=canvas.height=512;const context=canvas.getContext('2d');
    const colors=['#b09b7e','#aa9477','#b6a184','#b29b7d','#b8a58a','#a99175','#b5a082','#af987b'];
    for(let plank=0;plank<8;plank++){
      const x=plank*64;context.fillStyle=colors[plank];context.fillRect(x,0,64,512);
      context.strokeStyle='rgba(91,55,25,.18)';context.lineWidth=1;context.beginPath();context.moveTo(x+.5,0);context.lineTo(x+.5,512);context.stroke();
      for(let line=0;line<7;line++){context.strokeStyle='rgba(255,226,174,.12)';context.beginPath();for(let y=0;y<=512;y+=16){const xx=x+7+line*8+Math.sin(y*.027+plank*2+line)*1.9;if(y===0)context.moveTo(xx,y);else context.lineTo(xx,y);}context.stroke();}
      const end=(plank%3)*128+96;context.strokeStyle='rgba(91,55,25,.15)';context.beginPath();context.moveTo(x,end);context.lineTo(x+64,end);context.stroke();
    }
    const texture=new THREE.CanvasTexture(canvas);texture.colorSpace=THREE.SRGBColorSpace;texture.wrapS=texture.wrapT=THREE.RepeatWrapping;texture.repeat.set(1.4,1.1);this.ownedTextures.add(texture);return texture;
  }
  contactTexture(){
    const canvas=document.createElement('canvas');canvas.width=canvas.height=64;const context=canvas.getContext('2d');
    const gradient=context.createRadialGradient(32,32,3,32,32,31);
    if(gradient?.addColorStop){gradient.addColorStop(0,'rgba(42,30,18,.31)');gradient.addColorStop(.45,'rgba(42,30,18,.16)');gradient.addColorStop(1,'rgba(42,30,18,0)');context.fillStyle=gradient;context.fillRect(0,0,64,64);}
    const texture=new THREE.CanvasTexture(canvas);texture.generateMipmaps=false;texture.minFilter=THREE.LinearFilter;this.ownedTextures.add(texture);return texture;
  }
  desktopTexture(){
    const canvas=document.createElement('canvas');canvas.width=1024;canvas.height=640;const context=canvas.getContext('2d');
    context.fillStyle='#32415b';context.fillRect(0,0,1024,640);
    context.fillStyle='#59557c';context.beginPath();context.ellipse(730,510,650,380,-.3,0,Math.PI*2);context.fill();
    context.fillStyle='#b09b89';context.beginPath();context.ellipse(80,650,600,330,.2,0,Math.PI*2);context.fill();
    context.fillStyle='rgba(248,248,252,.92)';context.fillRect(180,110,664,390);
    context.fillStyle='#dfdfe7';context.fillRect(180,110,664,35);
    for(const [i,color] of ['#e47b6f','#e3bd67','#89b682'].entries()){context.fillStyle=color;context.beginPath();context.arc(204+i*25,128,7,0,Math.PI*2);context.fill();}
    context.fillStyle='#354459';context.font='600 48px system-ui';context.fillText('CEO OFFICE',250,245);
    context.font='26px system-ui';context.fillText('Private workspace',250,295);
    context.fillStyle='#a18b6c';context.fillRect(250,350,390,12);context.fillRect(250,385,270,12);
    context.fillStyle='rgba(255,255,255,.75)';context.fillRect(280,570,464,52);
    const texture=new THREE.CanvasTexture(canvas);texture.colorSpace=THREE.SRGBColorSpace;this.ownedTextures.add(texture);return texture;
  }
  silverDesktopTexture(){
    const canvas=document.createElement('canvas');canvas.width=512;canvas.height=320;const context=canvas.getContext('2d');
    context.fillStyle='#c7cdce';context.fillRect(0,0,512,320);context.fillStyle='#515653';
    // The silver back and small apple mark make the desktop recognizable from
    // the office camera. Furniture geometry still comes only from native assets.
    context.beginPath();context.moveTo(257,135);context.bezierCurveTo(218,113,198,147,207,181);context.bezierCurveTo(216,214,231,227,250,219);context.bezierCurveTo(257,216,264,216,272,220);context.bezierCurveTo(289,228,303,211,310,193);context.bezierCurveTo(291,185,285,161,307,149);context.bezierCurveTo(296,130,275,123,257,135);context.fill();
    context.beginPath();context.moveTo(256,126);context.bezierCurveTo(253,108,266,96,281,94);context.bezierCurveTo(281,109,270,124,256,126);context.fill();
    const texture=new THREE.CanvasTexture(canvas);texture.colorSpace=THREE.SRGBColorSpace;this.ownedTextures.add(texture);return texture;
  }
  executiveComputer(station){
    const silver={wall_white:'#c6cbcd'};
    const computer=new THREE.Group();computer.name='apple-style-desktop';computer.userData.computerStyle='Apple-inspired silver desktop';station.add(computer);
    this.addNative('wall_left_core',computer,0,.75,-.57,{size:{x:.43,y:.025,z:.29},palette:silver});
    this.addNative('wall_left_core',computer,0,.775,-.60,{size:{x:.085,y:.20,z:.12},palette:silver});
    const back=this.addNative('wall_left_core',computer,0,.94,-.59,{size:{x:.035,y:.76,z:1.17},rotation:Math.PI/2,palette:silver});
    const chassisMaterial=this.material('#ffffff',{map:this.silverDesktopTexture(),metalness:.28,roughness:.4});
    back?.traverse(mesh=>{if(mesh.isMesh){this.fullScreenUV(mesh);mesh.material=chassisMaterial;}});
    const display=this.addNative('wall_screen',computer,0,1.01,-.565,{size:{y:.63,z:1.08},rotation:-Math.PI/2});
    const screen=display?.getObjectByName('wall_screen');
    const material=this.material('#ffffff',{map:this.desktopTexture(),emissive:'#b8c7e2',emissiveIntensity:.12,roughness:.35});
    if(screen){this.fullScreenUV(screen);screen.material=material;screen.name='executive-desktop-screen';}
    if(this.laptopTemplate){
      const keyboard=new THREE.Group();for(const part of this.laptopTemplate.children)if(part.name!=='laptop_screen_01')keyboard.add(part.clone(true));
      this.addNative('native-keyboard',computer,0,.755,.02,{template:keyboard,size:{x:.56,y:.035,z:.25},rotation:Math.PI,palette:{black:'#c3c8cb'}});
    }
    return material;
  }
  fullScreenUV(screen){
    if(!screen?.geometry)return;
    const geometry=screen.geometry.clone();geometry.computeBoundingBox();const bounds=geometry.boundingBox,size=bounds.getSize(new THREE.Vector3());
    const positions=geometry.getAttribute('position'),uv=new Float32Array(positions.count*2),axis=size.x>=size.z?'x':'z';
    for(let index=0;index<positions.count;index++){
      uv[index*2]=((axis==='x'?positions.getX(index):positions.getZ(index))-bounds.min[axis])/Math.max(size[axis],1e-6);
      uv[index*2+1]=(positions.getY(index)-bounds.min.y)/Math.max(size.y,1e-6);
    }
    geometry.setAttribute('uv',new THREE.BufferAttribute(uv,2));screen.geometry=geometry;this.ownedGeometries.add(geometry);
  }
  tagSeat(object,ids){
    if(!object)return;object.userData.officeSeatIds=ids;
    if(ids.length===1)object.userData.officeDeskId=ids[0];
    object.traverse(mesh=>{if(mesh.isMesh){mesh.userData.officeSeatIds=ids;if(ids.length===1)mesh.userData.officeDeskId=ids[0];this.deskMeshes.push(mesh);}});
  }
  clear(){
    for(const light of this.lobbyLights){light.removeFromParent();light.target.removeFromParent();light.dispose?.();}
    this.lobbyLights=[];
    this.walls?.screen?.tex.dispose();this.walls?.board?.tex.dispose();
    for(const label of this.labels)label.element.remove();
    for(const geometry of this.ownedGeometries)geometry.dispose();for(const material of this.ownedMaterials)material.dispose();for(const texture of this.ownedTextures)texture.dispose();
    this.ownedGeometries.clear();this.ownedMaterials.clear();this.ownedTextures.clear();this.labels=[];this.obj.clear();this.closed.clear();this.walls=null;
  }
  configure(count){
    const layout=groundLayout(count),key=String(layout.mainCount);if(key===this.key)return false;
    this.clear();this.key=key;this.layout=layout;this.screens=[];this.spots=[];this.deskMeshes=[];this.nativeModules=new Set();
    const ivory=this.material('#eee7d9'),cream=this.material('#e6ddca'),brass=this.material('#a77c42',{metalness:.45,roughness:.48}),dark=this.material('#34362d'),sage=this.material('#8b9b82');
    this.shadowGeometry=new THREE.PlaneGeometry(1,1);this.shadowGeometry.rotateX(-Math.PI/2);this.ownedGeometries.add(this.shadowGeometry);
    this.shadowMaterial=new THREE.MeshBasicMaterial({map:this.contactTexture(),transparent:true,opacity:.63,depthWrite:false,toneMapped:false});this.ownedMaterials.add(this.shadowMaterial);
    const {width:w,depth:d,minX,maxX,minZ,maxZ,center}=layout;
    const slab=this.addNative('slab_open',this.obj,center.x,-.36,center.z,{size:{x:w,y:.36,z:d}});if(!slab)this.box(this.obj,ivory,w,.36,d,center.x,-.18,center.z,'slab');
    if(!this.addNative('slab_open',this.closed,center.x,-.36,center.z,{size:{x:w,y:.36,z:d}}))this.box(this.closed,ivory,w,.36,d,center.x,-.18,center.z,'slab');
    const floor=this.box(this.obj,ivory,w-.2,.026,d-.2,center.x,.012,center.z,'ground-lounge-floor');floor.userData.shadowReceiver=true;floor.castShadow=false;
    const wall=(x,z,length,h=1.3,horizontal=false)=>{
      const native=this.addNative('wall_left_core',this.obj,x,0,z,{size:{x:.18,y:h,z:length},rotation:horizontal?Math.PI/2:0,palette:{wall_white:'#e6dfd0'}});
      if(!native)this.box(this.obj,cream,horizontal?length:.18,h,horizontal?.18:length,x,h/2,z);
    };
    const glass=(x,z,length,h=1.05,vertical=false)=>{
      if(!this.addNative('glass_back',this.obj,x,.32,z,{size:{x:length,y:h,z:.025},rotation:vertical?Math.PI/2:0,glass:true})){
        const material=this.material('#a9bab0',{transparent:true,opacity:.18,depthWrite:false});this.box(this.obj,material,vertical?.025:length,h,vertical?length:.025,x,.32+h/2,z);
      }
      // A quiet brass sill replaces the former opaque blue barrier.
      this.box(this.obj,brass,vertical?.045:length,.04,vertical?length:.045,x,.34,z,'glass-brass-sill');
    };
    wall(minX,center.z,d,1.4);wall(center.x,minZ,w,1.45,true);
    // The native curtain wall is the actual opaque window mullion geometry.
    // Reuse it at the existing rear perimeter, at its authored height, so the
    // same moving sun can cast window stripes without changing room layouts.
    const frameCount=Math.ceil(w/16),frameWidth=w/frameCount;
    for(let index=0;index<frameCount;index++){
      const frame=this.addNative('curtain_wall',this.obj,minX+frameWidth*(index+.5),0,minZ+.055,{size:{x:frameWidth,y:2.7,z:.12},palette:{black:'#48483e'}});
      if(frame)frame.userData.nativeSunFrame=true;
    }
    this.box(this.obj,ivory,w,.25,.16,center.x,.125,maxZ);this.box(this.obj,ivory,.16,.25,d,maxX,.125,center.z);
    for(const aisle of layout.aisles){const path=this.box(this.obj,cream,aisle.width,.012,aisle.depth,aisle.x,.03,aisle.z,'central-aisle');path.userData.shadowReceiver=true;path.castShadow=false;}
    const wood=this.woodTexture();
    for(const [index,room] of layout.rooms.entries()){
      const section=new THREE.Group();section.name=room.id;section.userData.officeRoomId=room.id;this.obj.add(section);
      const oak=this.material(['#e4dece','#ded5c4','#e7dfcf','#e0d6c7','#e5dccd'][index],{map:wood,roughness:.75,metalness:.025});
      const border=this.box(section,brass,room.width-.2,.026,room.depth-.2,room.x,.027,room.z,'office-floor-border');border.castShadow=false;border.userData.shadowReceiver=true;
      const inlay=this.box(section,oak,room.width-.34,.028,room.depth-.34,room.x,.043,room.z,'office-floor-inlay');inlay.castShadow=false;inlay.userData.shadowReceiver=true;
      wall(room.minX,room.z,room.depth,1.4);wall(room.x,room.minZ,room.width,1.4,true);
      if(room.entry==='south'){
        wall(room.maxX,room.z,room.depth,.7);
        for(const [start,end] of [[room.minX,room.entrance.x-room.entrance.width/2],[room.entrance.x+room.entrance.width/2,room.maxX]])if(end>start)glass((start+end)/2,room.maxZ,end-start);
      }else{
        wall(room.x,room.maxZ,room.width,.7,true);
        for(const [start,end] of [[room.minZ,room.entrance.z-room.entrance.width/2],[room.entrance.z+room.entrance.width/2,room.maxZ]])if(end>start)glass(room.maxX,(start+end)/2,end-start,1.05,true);
      }
      // Rear corner planting keeps the chair/actor envelopes and front doors clear.
      this.addNative('tree_2',this.obj,room.minX+.65,.06,room.minZ+.65,{size:{y:1.85}});
      this.addNative('tree_2',this.obj,room.maxX-.65,.06,room.minZ+.65,{size:{y:1.55}});
      this.label(room.label,room.entrance.x+(room.entry==='east'?.9:0),room.entrance.z+(room.entry==='south'?.8:0));
    }
    // Independent executive suite: full-height native glazing, walnut and brass.
    const ceo=layout.ceo,executive=new THREE.Group();executive.name='ceo-suite';executive.userData.officeRoomId=ceo.id;this.obj.add(executive);
    const ceoBorder=this.box(executive,brass,ceo.width-.12,.028,ceo.depth-.12,ceo.x,.04,ceo.z,'ceo-floor-border');ceoBorder.castShadow=false;ceoBorder.userData.shadowReceiver=true;
    const walnut=this.material('#b4a087',{map:wood,roughness:.69,metalness:.035});
    const ceoFloor=this.box(executive,walnut,ceo.width-.28,.03,ceo.depth-.28,ceo.x,.055,ceo.z,'ceo-floor-inlay');ceoFloor.castShadow=false;ceoFloor.userData.shadowReceiver=true;
    for(const side of [-1,1]){
      const x=side<0?ceo.minX:ceo.maxX;wall(x,ceo.z,ceo.depth,.24);glass(x,ceo.z,ceo.depth,2.65,true);
      this.box(executive,brass,.045,.045,ceo.depth,x,2.99,ceo.z,'ceo-glass-crown');
    }
    wall(ceo.x,ceo.minZ,ceo.width,.24,true);glass(ceo.x,ceo.minZ,ceo.width,2.65);
    this.box(executive,brass,ceo.width,.045,.045,ceo.x,2.99,ceo.minZ,'ceo-glass-crown');
    for(const [start,end] of [[ceo.minX,ceo.entrance.x-ceo.entrance.width/2],[ceo.entrance.x+ceo.entrance.width/2,ceo.maxX]]){
      wall((start+end)/2,ceo.maxZ,end-start,.24,true);glass((start+end)/2,ceo.maxZ,end-start,2.65);
    }
    this.box(executive,brass,ceo.width,.045,.045,ceo.x,2.99,ceo.maxZ,'ceo-glass-crown');
    for(const [x,z] of [[ceo.minX,ceo.minZ],[ceo.maxX,ceo.minZ],[ceo.minX,ceo.maxZ],[ceo.maxX,ceo.maxZ],[ceo.entrance.x-1.1,ceo.maxZ],[ceo.entrance.x+1.1,ceo.maxZ]])this.box(executive,brass,.055,3.02,.055,x,1.51,z,'ceo-glass-post');
    const ceoRug=this.box(executive,this.material('#ded1bc',{roughness:1}),6.7,.018,4.5,-22.2,.08,-6.3,'ceo-guest-rug');ceoRug.castShadow=false;ceoRug.userData.shadowReceiver=true;
    this.addNative('sofa',executive,-24.3,.095,-6.1,{rotation:Math.PI/2,palette:{fabric_warm:'#b29b7e',fabric_light:'#ece2d0'}});
    this.addNative('coffee_table',executive,-22.1,.095,-6.1,{palette:{oak:'#a78d68'}});
    for(const [x,z,yaw] of [[-22.7,-8.4,Math.PI],[-20.3,-8.4,Math.PI],[-19.7,-5.8,3*Math.PI/2]])this.addNative('armchair_1',executive,x,.095,z,{straighten:true,rotation:yaw,palette:{graphite:'#b6a78e',black:'#47453e'}});
    this.addNative('coffee_bar',executive,-24.3,.075,-12.3,{rotation:Math.PI/2,palette:{oak:'#9d815b'}});
    for(const [x,z] of [[-25.1,-9.5],[-17.9,-12.9],[-18,-4.2]])this.addNative('tree_2',executive,x,.075,z,{size:{y:1.9}});
    this.addNative('wall_left_core',executive,ceo.x,.82,-15.02,{size:{x:.13,y:2.68,z:6.8},rotation:Math.PI/2,palette:{wall_white:'#302b25'}});
    const ceoDisplay=this.addNative('wall_screen',executive,ceo.x,.94,-14.94,{size:{y:2.45,z:6.55},rotation:-Math.PI/2});
    const ceoTV=ceoDisplay?.getObjectByName('wall_screen');this.fullScreenUV(ceoTV);if(ceoTV)ceoTV.userData.ceoTelevision=true;
    this.label('CEO ROOM · 行政辦公室',ceo.x,ceo.maxZ+.7);
    const spotMap=new Map(),screenMap=new Map();
    // Work tables, laptops and task chairs exist only inside the five offices.
    for(const seat of layout.seats.filter(seat=>seat.kind==='desk')){
      const station=new THREE.Group();station.name='office-workstation';station.position.set(seat.x,.065,seat.z);station.rotation.y=seat.rotation;station.userData.officeRoomId=seat.room_id;this.obj.add(station);
      const table=this.addNative('table_1',station,0,0,-.55,{size:{x:seat.desk_width},palette:{oak:seat.executive?'#a98d64':'#d1ac76'}})||this.box(station,cream,seat.desk_width,.08,1.4,0,.71,-.55,'worktable');
      this.addNative('armchair_1',station,0,0,.7,{straighten:true,rotation:Math.PI,palette:{graphite:seat.executive?'#4d514a':'#6d7568'}});
      const laptop=seat.executive?null:this.laptopTemplate&&this.addNative('single-laptop',station,.4,.75,-.55,{template:this.laptopTemplate,rotation:Math.PI});
      let screenMaterial=seat.executive?this.executiveComputer(station):null;
      laptop?.traverse(mesh=>{if(mesh.isMesh&&mesh.name==='laptop_screen_01')screenMaterial=Array.isArray(mesh.material)?mesh.material[0]:mesh.material;});
      if(!screenMaterial){screenMaterial=this.material('#334b45');screenMaterial.emissive.set('#9fd0ff');this.box(station,screenMaterial,.7,.45,.035,.4,1.04,-.65);}
      this.tagSeat(station,[seat.id]);
      screenMap.set(seat.id,{mat:screenMaterial,glow:0,target:0});
      const offset=new THREE.Vector3(0,.42,.7).applyAxisAngle(new THREE.Vector3(0,1,0),seat.rotation);
      spotMap.set(seat.id,{...seat,pos:new THREE.Vector3(seat.x,.065,seat.z).add(offset),yaw:seat.yaw});
    }
    // Planned square, parallel and L-shaped seating follows the building axes.
    // Shared sofas have
    // three distinct persisted seat IDs; native armchairs provide the other seats.
    for(const [index,cluster] of layout.lounges.entries()){
      const rugMaterial=this.material(index%2?'#d5c5aa':'#bec8b1',{roughness:1});
      const rug=this.box(this.obj,rugMaterial,7.1,.02,6.0,cluster.x,.04,cluster.z+.15,'lounge-rug');rug.rotation.y=cluster.rotation;rug.userData.shadowReceiver=true;rug.castShadow=false;
      const members=layout.seats.filter(seat=>seat.lounge_id===cluster.id),sofaSeats=members.filter(seat=>seat.furniture==='sofa');
      for(const couch of cluster.sofas||[cluster.sofa]){
        const sofa=this.addNative('sofa',this.obj,couch.x,.06,couch.z,{rotation:couch.rotation,palette:{fabric_warm:index%2?'#b69b7d':'#a9b399',fabric_light:'#e7d9c2'}});
        this.tagSeat(sofa,couch.seat_ids||sofaSeats.map(seat=>seat.id));
      }
      this.addNative('coffee_table',this.obj,cluster.x,.06,cluster.z-(cluster.cinema?.25:0));
      for(const seat of members){
        if(seat.furniture==='armchair'){
          const chair=this.addNative('armchair_1',this.obj,seat.x,.06,seat.z,{straighten:true,rotation:seat.yaw,palette:{graphite:index%2?'#a89274':'#83937b',black:'#35392e'}});
          this.tagSeat(chair,[seat.id]);
        }
        spotMap.set(seat.id,{...seat,pos:new THREE.Vector3(seat.x,.48,seat.z),yaw:seat.yaw,lounge:true});
      }
      const plantX=cluster.cinema?cluster.x-3.4:cluster.x-3.2,plantZ=cluster.cinema?cluster.z+3.65:cluster.z-2.45;
      this.addNative('tree_2',this.obj,plantX,.04,plantZ,{size:{y:index%2?1.8:2.1}});
    }
    // Pantry: an L-shaped native coffee counter with its native three-stool bank.
    const pantry=layout.pantry;
    const pantryFloor=this.box(this.obj,this.material('#d5c7ad',{roughness:.95}),pantry.width,.02,pantry.depth,pantry.x,.04,pantry.z,'pantry-floor');pantryFloor.castShadow=false;pantryFloor.userData.shadowReceiver=true;
    const pantryGroup=new THREE.Group();pantryGroup.name='pantry';pantryGroup.userData.zone='pantry';this.obj.add(pantryGroup);
    this.addNative('coffee_bar',pantryGroup,-20.5,.06,19.8);
    this.addNative('coffee_bar',pantryGroup,-22.68,.06,21.12,{rotation:Math.PI/2});
    this.addNative('bar_stools',pantryGroup,-20.4,.06,21.2);
    this.addNative('tree_2',pantryGroup,-17,.06,22.8,{size:{y:1.75}});
    // Native coffee bars also provide the television console and a reading counter.
    this.addNative('coffee_bar',this.obj,14.8,.04,21.2,{rotation:Math.PI});
    this.addNative('coffee_bar',this.obj,5.5,.04,-1.2,{size:{x:6.9,y:.68,z:.9},rotation:Math.PI});
    this.addNative('wall_left_core',this.obj,5.5,.65,-1.28,{size:{x:.16,y:3.7,z:6.88},rotation:Math.PI/2,palette:{wall_white:'#342f29'}});
    const display=this.addNative('wall_screen',this.obj,5.5,.82,-1.15,{size:{y:3.35,z:6.6},rotation:-Math.PI/2});
    let screen=display?.getObjectByName('wall_screen');
    if(!screen){const geometry=new THREE.PlaneGeometry(6.6,3.35);this.ownedGeometries.add(geometry);screen=new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({color:'#172a24'}));this.ownedMaterials.add(screen.material);screen.position.set(5.5,2.5,-1.15);this.obj.add(screen);}
    this.fullScreenUV(screen);
    this.walls=new WallDisplays(screen,null);this.walls.setAccent('#b9864e');for(const material of this.walls.materials)this.ownedMaterials.add(material);
    if(ceoTV)ceoTV.material=this.walls.screen.mat;
    for(const [x,z] of [[16.5,22],[16.5,-15.2],[-24.4,21.1],[-11.1,8],[16,9.2],[.3,16.0]])this.addNative('tree_2',this.obj,x,.03,z,{size:{y:2.0}});
    this.label('休息大堂',.1,7.2);this.label('影音區',5.5,-2.1);this.label(pantry.label,-20.1,23.25);
    this.lampPositions=layout.rooms.map(room=>new THREE.Vector3(room.x,4.3,room.z));this.lampPositions.push(new THREE.Vector3(ceo.x,4.3,ceo.z));
    this.lobbyLightPlans=lobbyLightLayout(layout);
    this.lobbyLights=this.lobbyLightPlans.map(plan=>{
      const light=new THREE.SpotLight(NATIVE_LAMP.color,0,0,plan.angle,NATIVE_LAMP.penumbra,NATIVE_LAMP.decay);
      light.name='ground-local-lamp-'+plan.id;light.userData.lobbyZone=plan.id;light.castShadow=false;
      light.position.set(plan.x,plan.y,plan.z);light.target.position.set(plan.x,0,plan.z);
      this.obj.add(light,light.target);return light;
    });
    this.setLobbyLightLevel(this.lobbyLightLevel);
    this.spots=layout.seats.map(seat=>spotMap.get(seat.id));this.screens=layout.seats.map(seat=>screenMap.get(seat.id)||null);
    this.obj.userData.nativeModules=[...this.nativeModules];this.obj.userData.workstationCount=layout.seats.filter(seat=>seat.kind==='desk').length;this.obj.userData.loungeSeats=layout.mainCount;
    return true;
  }
  setShown(value){this.alpha=value?1:0;this.root.visible=value;}
  setLobbyLightLevel(value){
    const level=Number.isFinite(value)?Math.max(0,Math.min(1,value)):0;
    this.lobbyLightLevel=level;
    for(const [index,light] of this.lobbyLights.entries())light.intensity=this.lobbyLightPlans[index].intensity*level;
  }
}
