// Post chain: the scene (MSAA, half-float, alpha kept) -> tilt-shift "miniature" blur (sharp band around the active
// floor, soft toward the top and bottom edges; `?tiltshift=0` leaves it out) -> OutputPass (tone mapping + sRGB) ->
// the studio pass (studio.js: white backdrop + the building's soft shadow aura), which is what reaches the screen.
import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { StudioPass } from './studio.js';

const TiltShiftShader = {
  name: 'BotHQTiltShift',
  uniforms: {
    tDiffuse: { value: null },
    dir: { value: new THREE.Vector2(1, 0) },     // texel step (1/w, 0) or (0, 1/h)
    focus: { value: 0.5 },                      // uv.y of the sharp line
    band: { value: 0.24 },                       // half height of the fully sharp band (uv)
    falloff: { value: 0.5 },                   // uv distance over which the blur ramps up
    strength: { value: 1.5 },                   // blur radius in px at full effect
  },
  vertexShader: /* glsl */`
    varying vec2 vUv;
    void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 ); }`,
  fragmentShader: /* glsl */`
    uniform sampler2D tDiffuse;
    uniform vec2 dir;
    uniform float focus, band, falloff, strength;
    varying vec2 vUv;
    void main() {
      float d = max( 0.0, abs( vUv.y - focus ) - band );
      float amt = smoothstep( 0.0, falloff, d );
      vec2 s = dir * strength * amt;
      vec4 c = texture2D( tDiffuse, vUv ) * 0.2270270270;
      c += texture2D( tDiffuse, vUv + s * 1.3846153846 ) * 0.3162162162;
      c += texture2D( tDiffuse, vUv - s * 1.3846153846 ) * 0.3162162162;
      c += texture2D( tDiffuse, vUv + s * 3.2307692308 ) * 0.0702702703;
      c += texture2D( tDiffuse, vUv - s * 3.2307692308 ) * 0.0702702703;
      gl_FragColor = c;
    }`,
};

export function tiltShiftEnabled() {
  return new URLSearchParams(location.search).get('tiltshift') !== '0';
}

// Colour and depth must support the same sample count. A requested count is a
// GL minimum (some drivers round 2 up to 4), so select only exact format counts
// no larger than the user's budget and expose the effective choice to the UI.
export function antialiasSupport(renderer) {
  const limit = Number.isFinite(renderer.capabilities?.maxSamples) ? Math.max(0, renderer.capabilities.maxSamples) : 4;
  const gl = renderer.getContext?.();
  let samples = [2, 4].filter(value => value <= limit);
  if (gl?.getInternalformatParameter) {
    try {
      renderer.extensions?.get?.('EXT_color_buffer_float');
      const colour = Array.from(gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA16F, gl.SAMPLES) || []);
      const depth = Array.from(gl.getInternalformatParameter(gl.RENDERBUFFER, gl.DEPTH_COMPONENT24, gl.SAMPLES) || []);
      samples = samples.filter(value => colour.includes(value) && depth.includes(value));
    } catch { samples = []; } // Context restoration will negotiate again.
  }
  return {maxSamples:limit, availableAntialias:[1, ...samples]};
}

function resolveAntialias(renderer, requested) {
  if (![1, 2, 4].includes(requested)) throw new RangeError('antialias must be 1, 2 or 4');
  const support = antialiasSupport(renderer);
  const effectiveAntialias = support.availableAntialias.filter(value => value <= requested).at(-1) || 1;
  return {...support, requestedAntialias:requested, effectiveAntialias};
}

export function createPost(renderer, scene, camera, {antialias = 4} = {}) {
  const size = renderer.getSize(new THREE.Vector2());
  const pr = renderer.getPixelRatio();
  const legacy = new URLSearchParams(location.search).get('post_msaa') === 'legacy';
  let antialiasStatus = resolveAntialias(renderer, antialias);
  const initialSamples = antialiasStatus.effectiveAntialias === 1 ? 0 : antialiasStatus.effectiveAntialias;
  // The reference path deliberately keeps the previous ping-pong layout for
  // controlled GPU/pixel comparisons. Normal post targets never need MSAA or
  // depth: only the native 3D scene is rasterized into the dedicated 4x target.
  const rt = legacy
    ? new THREE.WebGLRenderTarget(size.x * pr, size.y * pr, { type: THREE.HalfFloatType, samples: initialSamples })
    : new THREE.WebGLRenderTarget(size.x, size.y, { type: THREE.HalfFloatType, depthBuffer: false });
  const composer = new EffectComposer(renderer, rt);
  composer.setPixelRatio(pr);
  if (legacy) {
    composer.renderTarget2.dispose();
    composer.renderTarget2 = new THREE.WebGLRenderTarget(size.x * pr, size.y * pr, { type: THREE.HalfFloatType });
    composer.writeBuffer = composer.renderTarget2;
    composer.readBuffer = composer.renderTarget1;
  }
  const scenePass = new RenderPass(scene, camera);
  composer.addPass(scenePass);
  const h = new ShaderPass(TiltShiftShader);
  const v = new ShaderPass(TiltShiftShader);      // ShaderPass clones the uniforms, so each pass owns its own
  h.enabled = v.enabled = tiltShiftEnabled();
  composer.addPass(h);
  composer.addPass(v);
  const output = new OutputPass();
  composer.addPass(output);
  const studio = new StudioPass(scene, camera);
  composer.addPass(studio);

  let sceneTarget = null;
  if (!legacy) {
    sceneTarget = new THREE.WebGLRenderTarget(size.x * pr, size.y * pr, { type: THREE.HalfFloatType, samples: initialSamples });
    sceneTarget.texture.name = 'Office.sceneMSAA';
    // RenderPass has needsSwap=false. Its first enabled consumer can sample the
    // resolved scene target directly; no extra fullscreen copy is necessary,
    // including when tilt-shift is disabled. Later consumers use the composer's
    // ordinary single-sample buffers and retain the original effect order.
    let sceneInputReady = false;
    const renderScene = scenePass.render.bind(scenePass);
    scenePass.render = (activeRenderer, writeBuffer, readBuffer, ...args) => {
      renderScene(activeRenderer, writeBuffer, sceneTarget, ...args);
      sceneInputReady = !scenePass.renderToScreen;
    };
    for (const pass of [h, v, output, studio]) {
      const draw = pass.render.bind(pass);
      pass.render = (activeRenderer, writeBuffer, readBuffer, ...args) => {
        const input = sceneInputReady ? sceneTarget : readBuffer;
        sceneInputReady = false;
        return draw(activeRenderer, writeBuffer, input, ...args);
      };
    }
    const render = composer.render.bind(composer);
    composer.render = (...args) => {
      const target = renderer.getRenderTarget(), autoClear = renderer.autoClear;
      sceneInputReady = false;
      try { return render(...args); }
      finally {
        sceneInputReady = false;
        renderer.autoClear = autoClear;
        renderer.setRenderTarget(target);
      }
    };
    const dispose = composer.dispose.bind(composer);
    composer.dispose = () => { sceneTarget.dispose(); dispose(); };
  }

  const resizeComposer = composer.setSize.bind(composer);
  composer.setSize = (width, height) => {
    resizeComposer(width, height);
    // WebGLRenderer floors its canvas drawing-buffer dimensions. Match that
    // integer grid for fractional DPR/scale instead of sampling an imaginary
    // fractional texel in the post shaders or scalar aura kernels.
    const pxW = Math.max(1, Math.floor(composer.renderTarget1.width));
    const pxH = Math.max(1, Math.floor(composer.renderTarget1.height));
    const rounded = composer.renderTarget1.width !== pxW || composer.renderTarget1.height !== pxH;
    composer.renderTarget1.setSize(pxW, pxH);
    composer.renderTarget2.setSize(pxW, pxH);
    if (rounded) for (const pass of composer.passes) pass.setSize(pxW, pxH);
    sceneTarget?.setSize(pxW, pxH);
  };

  const api = {
    composer,
    getAntialiasStatus() { return {...antialiasStatus, availableAntialias:[...antialiasStatus.availableAntialias]}; },
    setAntialias(requested) {
      antialiasStatus = resolveAntialias(renderer, requested);
      const target = sceneTarget || composer.renderTarget1;
      const samples = antialiasStatus.effectiveAntialias === 1 ? 0 : antialiasStatus.effectiveAntialias;
      if (target.samples !== samples) {
        // Keep target/texture identity for the pass inputs, but release its old
        // framebuffer and renderbuffers before lazily allocating the new mode.
        target.dispose();
        target.samples = samples;
      }
      return this.getAntialiasStatus();
    },
    setSize(w, hgt) {
      composer.setPixelRatio(renderer.getPixelRatio());
      composer.setSize(w, hgt);
      const pxW = composer.renderTarget1.width, pxH = composer.renderTarget1.height;
      h.uniforms.dir.value.set(1 / pxW, 0);
      v.uniforms.dir.value.set(0, 1 / pxH);
      this.base = 1.05 * renderer.getPixelRatio();
      h.uniforms.strength.value = v.uniforms.strength.value = this.base;
    },
    render(focusV, dt, intensity = 1) {
      h.uniforms.focus.value = v.uniforms.focus.value = focusV;
      h.uniforms.strength.value = v.uniforms.strength.value = this.base * intensity;
      composer.render(dt);
    },
  };
  api.setSize(size.x, size.y);
  return api;
}
