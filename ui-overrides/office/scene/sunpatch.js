// Sun on the baked surfaces. The floor (with the rugs and patches floorstyle.js lays on it) and the left wall are unlit
// bakes, rendered once under one fixed sun, so their shadows would point the same way all day. This module relights
// them in their own shader, every frame, from the live sun (daylight.js):
//  1. it takes the bake's own sun out: on a floor every texel brighter than the bake's shade level is pulled down to
//     that level (sky light only, in the surface's own colour); darker texels (contacts, footprints) keep their occlusion;
//  2. it puts the live sun back where the sun's shadow map (furniture, bots, the window frame, the left wall) says it
//     reaches, with a soft edge that widens with the distance to the caster (PCSS-lite: a short blocker search, then a
//     Poisson filter; fewer taps on touch screens).
//  3. at night it adds the desk pendants' warm pools, the same cones as daylight.js's spot lights on the furniture,
//     with the desk tops shading the floor below them.
// The same shadow map shades the lit furniture and the bots (three's own shadows), so every shadow in the room agrees.
// The code is chained after building.js's night patch (onBeforeCompile, one extra compile per program) and weighted
// by the day share of the lightmap crossfade, so the night lightmaps stay exactly as they are.
import * as THREE from 'three';

const LEVEL = 0.32;                                       // shade / sunlit luminance in the floor bake (0.26 vs 0.83)
const COARSE = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
const SEARCH = COARSE ? 3 : 5;                            // blocker search taps
const TAPS = COARSE ? 5 : 10;                             // filter taps (4 texels each)

const PARS = /* glsl */`
#include <packing>
#define BQ_LEVEL ${LEVEL.toFixed(3)}
#define BQ_SEARCH ${SEARCH}
#define BQ_TAPS ${TAPS}
uniform vec3 bqSun;                                       // linear sun on a lit floor, relative to its shade (0 = none)
uniform vec3 bqDir;                                       // unit, toward the sun (world)
uniform float bqDay;                                      // 1 - the night lightmap share
uniform vec3 bqAlbedo;                                    // this surface's sunlit colour in the bake
uniform sampler2D bqShadowMap;
uniform mat4 bqShadowMatrix;
uniform vec4 bqShadowInfo;                                // metres per uv (x, y), depth range (m), 1 = the map is ready
uniform vec3 bqLampPos[ 6 ];                              // the desk pendants (world)
uniform vec3 bqLamp;                                      // their light (linear colour x strength), 0 by day
uniform vec2 bqLampCone;                                  // cos of the outer and inner cone angle
uniform vec4 bqTable[ 4 ];                                // desk tops that shade the floor from the lamps: x0, z0, x1, z1
uniform vec4 bqTableY;                                    // their heights (world), < -50 = none
varying vec3 vBqWorld;
varying vec3 vBqNormal;
const vec3 BQ_LUMA = vec3( 0.2126, 0.7152, 0.0722 );
const vec2 BQ_DISK[ 12 ] = vec2[](
  vec2( -0.326, -0.406 ), vec2( 0.840, -0.074 ), vec2( -0.696, 0.457 ), vec2( 0.203, 0.621 ),
  vec2( 0.962, -0.195 ), vec2( -0.185, -0.893 ), vec2( 0.473, -0.480 ), vec2( 0.519, 0.767 ),
  vec2( -0.321, -0.933 ), vec2( -0.792, -0.598 ), vec2( 0.507, 0.064 ), vec2( 0.896, 0.412 ) );

float bqDepth( vec2 uv ) { return unpackRGBAToDepth( texture2D( bqShadowMap, uv ) ); }

// Bilinear depth compare (what hardware PCF does): the four nearest texels, weighted, so the filter has no grain.
float bqCompare( vec2 uv, float z ) {
  vec2 size = vec2( textureSize( bqShadowMap, 0 ) ), t = uv * size - 0.5, f = fract( t );
  vec2 base = ( floor( t ) + 0.5 ) / size, dx = vec2( 1.0 / size.x, 0.0 ), dy = vec2( 0.0, 1.0 / size.y );
  return mix( mix( step( z, bqDepth( base ) ), step( z, bqDepth( base + dx ) ), f.x ),
    mix( step( z, bqDepth( base + dy ) ), step( z, bqDepth( base + dx + dy ) ), f.x ), f.y );
}

// 1 = in the sun. The edge is soft in proportion to how far the shadow has travelled from its caster.
float bqVisible( vec3 p ) {
  vec4 c = bqShadowMatrix * vec4( p, 1.0 );
  vec3 s = c.xyz / c.w;
  if ( bqShadowInfo.w < 0.5 || s.x < 0.0 || s.x > 1.0 || s.y < 0.0 || s.y > 1.0 || s.z > 1.0 ) return 1.0;
  float z = s.z - 0.0006;
  vec2 search = 0.16 / bqShadowInfo.xy;
  float sum = 0.0, n = 0.0;
  for ( int i = 0; i < BQ_SEARCH; i ++ ) {
    float d = bqDepth( s.xy + BQ_DISK[ i ] * search );
    if ( d < z ) { sum += d; n += 1.0; }
  }
  if ( n == 0.0 && bqDepth( s.xy ) >= z ) return 1.0;
  float dist = n > 0.0 ? ( z - sum / n ) * bqShadowInfo.z : 0.0;
  vec2 r = ( 0.008 + 0.012 * dist ) / bqShadowInfo.xy;
  float lit = 0.0;
  for ( int i = 0; i < BQ_TAPS; i ++ ) lit += bqCompare( s.xy + BQ_DISK[ i ] * r, z );
  return lit / float( BQ_TAPS );
}

vec3 bqRelight( vec3 day ) {
  float floorish = step( 0.5, vBqNormal.y );              // walls were in the bake's shade: nothing to take out
  float level = mix( 1.0, BQ_LEVEL, floorish );
  // below the shade level only the darkest texels are occlusion (footprints, contacts); the rest are the cores of the
  // bake's own sun shadows (and floorstyle.js's, drawn the same way), which point one way all day: lifted to the shade
  float ao = dot( day, BQ_LUMA ) / max( level * dot( bqAlbedo, BQ_LUMA ), 1e-4 );
  vec3 shade = mix( day, bqAlbedo * level, floorish * smoothstep( 0.3, 0.75, ao ) );
  float facing = clamp( dot( vBqNormal, bqDir ) / max( bqDir.y, 0.25 ), 0.0, 1.5 );
  if ( facing <= 0.0 || bqSun.r + bqSun.g + bqSun.b <= 0.0 ) return shade;
  return shade * ( 1.0 + bqSun * ( facing * bqVisible( vBqWorld + vBqNormal * 0.025 ) ) );
}

// The pendants' warm pools: each lamp a soft downward cone (the same as the spot lights on the lit furniture), dimmed
// by the distance and the angle of incidence, and shaded where a desk top stands between the lamp and the floor.
vec3 bqLampPools() {
  vec3 sum = vec3( 0.0 );
  for ( int i = 0; i < 6; i ++ ) {
    vec3 d = bqLampPos[ i ] - vBqWorld;
    float len = length( d );
    if ( bqLampPos[ i ].y < -50.0 || len < 0.05 ) continue;
    vec3 l = d / len;
    float k = smoothstep( bqLampCone.x, bqLampCone.y, l.y ) * max( dot( vBqNormal, l ), 0.0 ) / ( len * len );
    for ( int j = 0; j < 4; j ++ ) {
      float y = bqTableY[ j ];
      if ( y < -50.0 || vBqWorld.y > y || bqLampPos[ i ].y < y ) continue;
      vec2 o = mix( vBqWorld.xz, bqLampPos[ i ].xz, ( y - vBqWorld.y ) / ( bqLampPos[ i ].y - vBqWorld.y ) );
      vec4 r = bqTable[ j ];
      k *= 1.0 - ( smoothstep( r.x - 0.06, r.x + 0.06, o.x ) - smoothstep( r.z - 0.06, r.z + 0.06, o.x ) )
        * ( smoothstep( r.y - 0.06, r.y + 0.06, o.y ) - smoothstep( r.w - 0.06, r.w + 0.06, o.y ) );
    }
    sum += k;
  }
  return bqAlbedo * bqLamp * sum;
}`;

const MAIN = /* glsl */`
#ifdef USE_MAP
  {
    vec3 bqD = texture2D( map, vMapUv ).rgb;
    diffuseColor.rgb += diffuse * ( bqRelight( bqD ) - bqD ) * bqDay;
    if ( bqLamp.r + bqLamp.g + bqLamp.b > 0.0 ) diffuseColor.rgb += bqLampPools();
  }
#endif`;

// Keep the bake's authored world-normal transform identical for ordinary and batched meshes.
// Three's project_vertex already applies instanceMatrix to gl_Position, but `transformed` stays local.
const WORLD = /* glsl */`
  mat4 bqModelMatrix = modelMatrix;
  #ifdef USE_INSTANCING
    bqModelMatrix = modelMatrix * instanceMatrix;
  #endif
  vBqWorld = ( bqModelMatrix * vec4( transformed, 1.0 ) ).xyz;
  vBqNormal = normalize( mat3( bqModelMatrix ) * normal );`;

const lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };

// The sunlit colour of a bake: the mean of its brightest texels (88th..98th percentile), linear.
const albedos = new WeakMap();
function albedoOf(tex) {
  if (albedos.has(tex)) return albedos.get(tex);
  const out = new THREE.Vector3(0.83, 0.82, 0.78);
  try {
    const img = tex.image, w = 96, h = Math.max(1, Math.round((96 * img.height) / img.width));
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const g = c.getContext('2d', { willReadFrequently: true });
    g.drawImage(img, 0, 0, w, h);
    const d = g.getImageData(0, 0, w, h).data, px = [];
    for (let i = 0; i < d.length; i += 4) {
      const r = lin(d[i]), gr = lin(d[i + 1]), b = lin(d[i + 2]);
      px.push([0.2126 * r + 0.7152 * gr + 0.0722 * b, r, gr, b]);
    }
    px.sort((a, b) => a[0] - b[0]);
    const lo = Math.floor(px.length * 0.88), hi = Math.max(lo + 1, Math.floor(px.length * 0.98));
    out.set(0, 0, 0);
    for (let i = lo; i < hi; i++) out.x += px[i][1], out.y += px[i][2], out.z += px[i][3];
    out.divideScalar(hi - lo);
  } catch { /* not readable yet: the default floor colour */ }
  albedos.set(tex, out);
  return out;
}

export function createSunPatch(sun) {
  const uniforms = {
    bqSun: { value: new THREE.Color(0, 0, 0) },
    bqDir: { value: new THREE.Vector3(0, 1, 0) },
    bqDay: { value: 1 },
    bqShadowMap: { value: null },
    bqShadowMatrix: { value: sun.shadow.matrix },
    bqShadowInfo: { value: new THREE.Vector4(1, 1, 1, 0) },
    bqLampPos: { value: Array.from({ length: 6 }, () => new THREE.Vector3(0, -99, 0)) },
    bqLamp: { value: new THREE.Color(0, 0, 0) },
    bqLampCone: { value: new THREE.Vector2(0.7, 0.9) },
    bqTable: { value: Array.from({ length: 4 }, () => new THREE.Vector4()) },
    bqTableY: { value: new THREE.Vector4(-99, -99, -99, -99) },
  };

  return {
    // Chain the relight into one baked material (once; new materials are found by daylight.js's scan).
    relight(mat) {
      if (mat.userData.sunRelit || !mat.map) return;
      mat.userData.sunRelit = true;
      const albedo = { value: albedoOf(mat.map) };
      const prev = mat.onBeforeCompile, prevKey = mat.customProgramCacheKey;
      mat.onBeforeCompile = (shader, renderer) => {
        prev.call(mat, shader, renderer);
        Object.assign(shader.uniforms, uniforms, { bqAlbedo: albedo });
        shader.vertexShader = shader.vertexShader
          .replace('#include <common>', '#include <common>\nvarying vec3 vBqWorld;\nvarying vec3 vBqNormal;')
          .replace('#include <project_vertex>', `#include <project_vertex>\n${WORLD}`);
        shader.fragmentShader = shader.fragmentShader
          .replace('#include <common>', `#include <common>\n${PARS}`)
          .replace('#include <color_fragment>', `${MAIN}\n#include <color_fragment>`);
      };
      mat.customProgramCacheKey = () => `${prevKey.call(mat)}|sun`;
      mat.needsUpdate = true;
    },
    // dir = unit vector toward the sun; colour = sRGB numbers; strength = 1 reproduces the bake's own sunlit floor;
    // day = 1 - the night lightmap share.
    update(dir, colour, strength, day) {
      uniforms.bqDir.value.copy(dir);
      uniforms.bqSun.value.setRGB(...colour, THREE.SRGBColorSpace).multiplyScalar(Math.max(0, strength) * (1 / LEVEL - 1));
      uniforms.bqDay.value = day;
      const map = sun.shadow.map, cam = sun.shadow.camera;
      uniforms.bqShadowMap.value = map ? map.texture : null;
      uniforms.bqShadowInfo.value.set(cam.right - cam.left, cam.top - cam.bottom, cam.far - cam.near, map ? 1 : 0);
    },
    // The pendants (world positions, up to 6, or none), their light (linear colour x strength) and cone (the spot
    // lights' angle and penumbra, so floor and furniture pools match); tables = [{ x0, z0, x1, z1, y }] in world space.
    lamps(positions, light, angle, penumbra, tables) {
      uniforms.bqLampPos.value.forEach((v, i) => { if (positions?.[i]) v.copy(positions[i]); else v.set(0, -99, 0); });
      uniforms.bqLamp.value.copy(light);
      uniforms.bqLampCone.value.set(Math.cos(angle), Math.cos(angle * (1 - penumbra)));
      uniforms.bqTable.value.forEach((v, i) => { const t = tables[i]; if (t) v.set(t.x0, t.z0, t.x1, t.z1); });
      uniforms.bqTableY.value.set(...[0, 1, 2, 3].map((i) => (tables[i] ? tables[i].y : -99)));
    },
  };
}
