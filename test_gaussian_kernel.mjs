import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile} from 'node:fs/promises';
import {dirname,resolve} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {createGaussianKernel,gaussianPairingAligned,GaussianKernelCache,MAX_GAUSSIAN_PAIRS} from './public/office/scene/gaussian-kernel.js';

const clamp = (n,lo,hi) => Math.max(lo,Math.min(hi,n));
const linear = (data,at) => {
  at = clamp(at,0,data.length-1);
  const index = Math.floor(at), fraction = at-index;
  return data[index]*(1-fraction)+data[Math.min(index+1,data.length-1)]*fraction;
};
// Independent original-shader oracle: full, unpaired normalized taps. Float32
// uniform/support arithmetic matches the GPU radius at n/3 boundaries.
function legacy(data,at,inputSigma,direction=1) {
  const sigma=Math.fround(inputSigma);
  if(sigma<Math.fround(.01))return linear(data,at);
  const radius=Math.min(Math.ceil(Math.fround(sigma*3)),40);
  let value=0,total=0;
  for(let i=-radius;i<=radius;i++){
    const weight=Math.exp(-i*i/(2*sigma*sigma));
    value+=linear(data,at+i*direction)*weight;total+=weight;
  }
  return value/total;
}
function paired(data,at,sigma,direction=1) {
  const kernel=createGaussianKernel(sigma);
  let value=linear(data,at)*kernel.center;
  for(const tap of kernel.pairs)value+=(linear(data,at+tap.offset*direction)+linear(data,at-tap.offset*direction))*tap.weight;
  return value;
}

test('finite support follows shader Float32 radius/copy boundaries and remains normalized',()=>{
  for(const sigma of [-1,0,.009,.0099999999,.01,.011,.1,.33333334,1/3,2/3,1,1.7,3.75,12.9999999,13,13.5,100,Infinity]){
    const shaderSigma=Math.fround(sigma),kernel=createGaussianKernel(sigma);
    assert.equal(kernel.radius,shaderSigma<Math.fround(.01)?0:Math.min(40,Math.ceil(Math.fround(shaderSigma*3))));
    assert.equal(kernel.copy,shaderSigma<Math.fround(.01));
    assert.ok(kernel.fetches<=41);assert.ok(kernel.pairs.length<=MAX_GAUSSIAN_PAIRS);
    assert.ok(Number.isFinite(kernel.center));
    const total=kernel.center+2*kernel.pairs.reduce((sum,tap)=>sum+tap.weight,0);
    assert.ok(Math.abs(total-1)<1e-14);
    assert.ok(kernel.pairs.every(tap=>Number.isFinite(tap.offset)&&Number.isFinite(tap.weight)));
  }
  assert.equal(createGaussianKernel(.33333334).radius,1,'the CPU-double product would incorrectly ceil to2');
  assert.equal(createGaussianKernel(13).pairs.at(-1).offset,39,'odd support retains its final unpaired tap');
  assert.equal(createGaussianKernel(13.5).radius,40);assert.equal(createGaussianKernel(13.5).fetches,41);
  assert.throws(()=>createGaussianKernel(NaN),/sigma/);
});

test('paired convolution equals all original taps including clamped boundaries and tiny textures',()=>{
  let seed=84629;
  const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/2**32;};
  const sigmas=[0,.009,.01,.08,.33333334,.7,1.1,2.8,5.3,13,13.5,100];
  // Include each live studio layer across ordinary viewport sizes and zooms.
  for(const height of [320,836,1080,1620])for(const zoom of [.75,1,1.5,3]){
    const unit=height/1080*Math.pow(zoom,.55);
    sigmas.push(...[4,30,120].map((sigma,index)=>Math.min(13,sigma*unit/[4,8,32][index])));
  }
  let cases=0;
  for(const width of [2,3,7,31,83]){
    const patterns=[Array(width).fill(0),Array(width).fill(.37),Array(width).fill(1),
      Array.from({length:width},(_,i)=>Number(i===0)),Array.from({length:width},(_,i)=>Number(i===width-1)),
      Array.from({length:width},(_,i)=>i%2),Array.from({length:width},random)];
    for(const sigma of sigmas)for(const data of patterns)for(const direction of [-1,1])for(let index=0;index<width;index++){
      const expected=legacy(data,index,sigma,direction),actual=paired(data,index,sigma,direction);
      assert.ok(Math.abs(expected-actual)<5e-13,`width=${width},sigma=${sigma},index=${index},delta=${actual-expected}`);cases++;
    }
  }
  assert.ok(cases>100000);
});

test('both separable axes preserve the same clamped two-dimensional convolution',()=>{
  const width=7,height=5,grid=Array.from({length:height},(_,y)=>Array.from({length:width},(_,x)=>(x*13+y*7)%17/16));
  for(const sigma of [.7,2.3,13]){
    const xLegacy=grid.map(row=>row.map((_,x)=>legacy(row,x,sigma)));
    const xPaired=grid.map(row=>row.map((_,x)=>paired(row,x,sigma)));
    for(let x=0;x<width;x++)for(let y=0;y<height;y++){
      const a=legacy(xLegacy.map(row=>row[x]),y,sigma),b=paired(xPaired.map(row=>row[x]),y,sigma);
      assert.ok(Math.abs(a-b)<5e-13);
    }
  }
});

test('pairing requires matching grids and one-texel axis directions',()=>{
  for(const [dx,dy] of [[1,0],[-1,0],[0,1],[0,-1]])assert.equal(gaussianPairingAligned(12,9,12,9,dx,dy),true);
  for(const args of [[12,9,6,9,1,0],[12,9,12,6,1,0],[12,9,12,9,2,0],[12,9,12,9,1,1],[12,9,12,9,0,0],[0,9,0,9,1,0],[12.5,9,12.5,9,1,0]])assert.equal(gaussianPairingAligned(...args),false);
  const data=[0,1,0,1,0,0,1];
  assert.ok(Math.abs(legacy(data,.2,1.1)-paired(data,.2,1.1))>.001,'off-grid pairing is materially different and must not be used');
});

test('kernel cache shares actual shader sigmas and stays bounded across camera zooms',()=>{
  const cache=new GaussianKernelCache(12),one=cache.get(1);
  assert.equal(cache.get(1+1e-10),one);assert.equal(cache.get(1),one);
  for(let i=0;i<1000;i++)cache.get(.2+i/100);
  assert.equal(cache.entries.size,12);
  const last=cache.get(10.19);assert.equal(cache.get(10.19),last);
  cache.clear();assert.equal(cache.entries.size,0);
  assert.throws(()=>new GaussianKernelCache(0),/limit/);
});

let threeRoot=null;
const testRoot=dirname(fileURLToPath(import.meta.url));
for(const base of [process.env.OFFICE_THREE_TEST_ROOT,testRoot,process.cwd()].filter(Boolean)){
  try{const require=createRequire(resolve(base,'package.json'));threeRoot=resolve(dirname(require.resolve('three')),'..');break;}catch{}
}
test('StudioPass dispatch keeps exact legacy fallback, filter guards and material lifecycle',{skip:!threeRoot},async()=>{
  const THREE=await import(pathToFileURL(resolve(threeRoot,'build/three.module.js')).href);
  const imports={
    three:resolve(threeRoot,'build/three.module.js'),
    'three/addons/postprocessing/Pass.js':resolve(threeRoot,'examples/jsm/postprocessing/Pass.js'),
    './aura-cache.js':resolve(testRoot,'public/office/scene/aura-cache.js'),
    './gaussian-kernel.js':resolve(testRoot,'public/office/scene/gaussian-kernel.js'),
  };
  let source=await readFile(resolve(testRoot,'public/office/scene/studio.js'),'utf8');
  for(const [name,path]of Object.entries(imports))source=source.replace(`from '${name}';`,`from '${pathToFileURL(path).href}';`);
  const {StudioPass}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  const pass=new StudioPass(new THREE.Scene(),new THREE.OrthographicCamera());pass.setSize(400,300);
  const renderer={setRenderTarget(target){this.target=target;},render(quad){this.material=quad.material;}};
  pass.blur(renderer,pass.t.mask,pass.t.cB,1,0,2.3);
  assert.equal(renderer.material,pass.blurMat);assert.doesNotMatch(pass.blurMat.fragmentShader,/\bexp\s*\(/);
  assert.match(pass.legacyBlurMat.fragmentShader,/\bexp\s*\(/);
  assert.equal(pass.blurMat.uniforms.kernelCount.value,4);assert.equal(pass.blurMat.uniforms.sigma.value,2.3);
  const kernel=pass.currentKernel;
  pass.blur(renderer,pass.t.cB,pass.t.cA,0,1,2.3);assert.equal(pass.currentKernel,kernel);
  pass.blur(renderer,pass.t.mask,pass.t.mB,1,0,2.3);assert.equal(renderer.material,pass.legacyBlurMat,'different grids use unpaired original samples');
  pass.blur(renderer,pass.t.mask,pass.t.cB,1,1,2.3);assert.equal(renderer.material,pass.legacyBlurMat,'diagonal bilinear interpolation is not a pair of diagonal taps');
  pass.t.mask.texture.minFilter=THREE.NearestFilter;
  pass.blur(renderer,pass.t.mask,pass.t.cB,1,0,2.3);assert.equal(renderer.material,pass.legacyBlurMat);
  pass.t.mask.texture.minFilter=THREE.LinearFilter;pass.t.mask.texture.wrapS=THREE.RepeatWrapping;
  pass.blur(renderer,pass.t.mask,pass.t.cB,1,0,2.3);assert.equal(renderer.material,pass.legacyBlurMat);
  pass.t.mask.texture.wrapS=THREE.ClampToEdgeWrapping;
  pass.blur(renderer,pass.t.mask,pass.t.mB,0,0,0);assert.equal(renderer.material,pass.legacyBlurMat);assert.equal(renderer.material.uniforms.sigma.value,0);
  pass.blur(renderer,pass.t.mask,pass.t.cB,1,0,.009);assert.equal(renderer.material,pass.blurMat);assert.equal(pass.blurMat.uniforms.kernelCount.value,0);
  let disposed=0;pass.blurMat.addEventListener('dispose',()=>disposed++);pass.legacyBlurMat.addEventListener('dispose',()=>disposed++);pass.dispose();assert.equal(disposed,2);assert.equal(pass.gaussianKernels.entries.size,0);
  const old=new StudioPass(new THREE.Scene(),new THREE.OrthographicCamera(),{kernel:'legacy'});old.setSize(400,300);
  old.blur(renderer,old.t.mask,old.t.cB,1,0,2.3);assert.equal(renderer.material,old.blurMat);assert.equal(old.blurMat,old.legacyBlurMat);assert.match(old.blurMat.fragmentShader,/\bexp\s*\(/);
  let once=0;old.blurMat.addEventListener('dispose',()=>once++);old.dispose();assert.equal(once,1);
});
