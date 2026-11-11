// Office mascots are scene characters, not altered platform logos. The original
// platform artwork is displayed separately on flat, square identification badges.
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

const PLATFORMS = new Set(['codex', 'cursor', 'hermes']);
const iconTextures = new Map(); // Three local textures, shared for the page lifetime.

function platformIcon(platform) {
  if (!iconTextures.has(platform)) {
    const texture = new THREE.TextureLoader().load(new URL(`../platform-icons/${platform}.svg`, import.meta.url).href);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = 2;
    iconTextures.set(platform, texture);
  }
  return iconTextures.get(platform);
}

/** Fresh model, facing +Z with its base at y=0. Only its own resources are freed.
 *  icons:false supports geometry checks without an image/DOM environment.
 *  A palette changes body/accessory colors only; original badge artwork stays untinted. */
export function createPlatformModel(platform, { icons = true, palette } = {}) {
  if (!PLATFORMS.has(platform)) return null;
  const model = new THREE.Group();
  model.name = `office-platform-${platform}`;
  model.userData.platformVisual = platform;
  model.userData.paletteKey = typeof palette?.key === 'string' ? palette.key : 'default';
  const paint = (field, fallback) => typeof palette?.[field] === 'string' && /^#[0-9a-f]{6}$/i.test(palette[field]) ? palette[field] : fallback;
  const geometries = new Set(), materials = new Set();
  const ownGeo = geometry => (geometries.add(geometry), geometry);
  const ownMat = material => (materials.add(material), material);
  const material = (color, options = {}) => ownMat(new THREE.MeshStandardMaterial({ color, roughness: .48, metalness: .08, ...options }));
  const add = (geometry, paint, x, y, z, name = '') => {
    const mesh = new THREE.Mesh(ownGeo(geometry), paint);
    mesh.position.set(x, y, z); mesh.name = name;
    mesh.castShadow = mesh.receiveShadow = true;
    model.add(mesh); return mesh;
  };
  const round = (w, h, d, radius, paint, x, y, z, name = '') => add(new RoundedBoxGeometry(w, h, d, 2, radius), paint, x, y, z, name);
  const ball = (radius, paint, x, y, z, name = '') => add(new THREE.SphereGeometry(radius, 20, 14), paint, x, y, z, name);
  const eye = (paint, x, y, z, side, width = .027, length = .045) => add(new THREE.CapsuleGeometry(width, length, 3, 8), paint, x, y, z, `eye_${side}`);
  const badge = (y, frontZ, backZ, size = .19) => {
    const white = ownMat(new THREE.MeshBasicMaterial({ color: '#ffffff' }));
    const ink = ownMat(new THREE.MeshBasicMaterial({ color: '#ffffff', map: icons ? platformIcon(platform) : null, transparent: true, toneMapped: false }));
    for (const [z, rotation] of [[frontZ, 0], [backZ, Math.PI]]) {
      const plate = round(size + .035, size + .035, .015, .025, white, 0, y, z, 'platform-badge-plate');
      plate.castShadow = plate.receiveShadow = false;
      const image = add(new THREE.PlaneGeometry(size, size), ink, 0, y, z + (rotation ? -.011 : .011), 'platform-badge');
      image.rotation.y = rotation; image.castShadow = image.receiveShadow = false;
    }
  };

  if (platform === 'codex') {
    const ceramic = material(paint('shell', '#f4f5f1'), { roughness: .32, metalness: .13 });
    const graphite = material(paint('dark', '#1f2b34'), { roughness: .3 });
    const sky = material(paint('accent', '#83c1ed'), { roughness: .34, metalness: .2 });
    const light = material('#eaf8ff', { emissive: '#9cd7ff', emissiveIntensity: .32 });
    // A terminal-headed coding companion, with a ceramic shell and blue ear pods.
    round(.64, .44, .43, .095, ceramic, 0, .65, 0, 'terminal-shell');
    round(.49, .225, .045, .05, graphite, 0, .66, .227, 'terminal-visor');
    for (const sign of [-1, 1]) {
      round(.09, .19, .28, .045, sky, sign * .357, .66, 0, 'terminal-ear');
      round(.105, .20, .15, .045, ceramic, sign * .29, .295, .015, 'terminal-hand');
      round(.19, .095, .235, .035, graphite, sign * .125, .0475, .045, 'terminal-foot');
    }
    round(.40, .29, .34, .065, ceramic, 0, .275, -.01, 'terminal-body');
    round(.29, .045, .34, .015, sky, 0, .125, -.01, 'terminal-belt');
    eye(light, -.095, .68, .258, 'L'); eye(light, .095, .68, .258, 'R');
    round(.075, .014, .012, .004, sky, .09, .592, .255, 'terminal-prompt');
    badge(.285, .175, -.195, .16);
    model.userData.silhouette = 'rounded-terminal-robot';
  } else if (platform === 'cursor') {
    const white = material(paint('shell', '#f2f2ec'), { roughness: .34, metalness: .18 });
    const silver = material(paint('metal', '#b4bbc5'), { roughness: .35, metalness: .42 });
    const charcoal = material(paint('dark', '#303239'), { roughness: .37, metalness: .2 });
    const eyeInk = material(paint('dark', '#202229'), { roughness: .28 });
    const violet = material(paint('accent', '#bab5d4'), { roughness: .35, metalness: .28 });
    // A faceted cube character, echoing Cursor's cube instead of its older arrow.
    const facets = [silver, white, white, charcoal, white, silver];
    const cube = add(new THREE.BoxGeometry(.53, .46, .49), facets, 0, .665, 0, 'cursor-cube');
    cube.rotation.y = 0;
    round(.38, .245, .32, .04, charcoal, 0, .285, -.015, 'cursor-core');
    for (const sign of [-1, 1]) {
      const arm = add(new THREE.OctahedronGeometry(.12), silver, sign * .318, .325, 0, 'cursor-faceted-hand');
      arm.scale.set(.7, 1.2, .75);
      const foot = round(.18, .105, .23, .022, charcoal, sign * .12, .0525, .04, 'cursor-foot');
      foot.rotation.y = sign * -.10;
    }
    eye(eyeInk, -.095, .665, .253, 'L', .025, .045); eye(eyeInk, .095, .665, .253, 'R', .025, .045);
    round(.13, .022, .014, .006, violet, 0, .558, .253, 'cube-indicator');
    badge(.294, .16, -.19, .17);
    model.userData.silhouette = 'faceted-cube-robot';
  } else {
    const cobalt = material(paint('shell', '#2449c5'), { roughness: .36, metalness: .15 });
    const cream = material(paint('secondary', '#e8e6d8'), { roughness: .57 });
    const feather = material(paint('wing', '#f8f8ef'), { roughness: .37, metalness: .1 });
    const brass = material(paint('metal', '#bc9a58'), { roughness: .42, metalness: .52 });
    const visorInk = material(paint('dark', '#22336c'), { roughness: .3 });
    const light = material('#ffffff', { emissive: '#c8dcff', emissiveIntensity: .22 });
    const accent = material(paint('accent', '#2449c5'), { roughness: .36, metalness: .15 });
    // A winged messenger helmet and short robe distinguish Hermes from the robots.
    ball(.265, cobalt, 0, .625, -.01, 'messenger-helmet').scale.set(1, 1.02, .88);
    round(.365, .265, .12, .075, visorInk, 0, .617, .209, 'messenger-visor');
    add(new THREE.CylinderGeometry(.155, .245, .31, 12), cream, 0, .28, -.015, 'messenger-robe');
    add(new THREE.CylinderGeometry(.18, .19, .08, 12), accent, 0, .405, -.015, 'messenger-collar');
    for (const sign of [-1, 1]) {
      round(.16, .09, .21, .035, accent, sign * .105, .045, .025, 'messenger-boot');
      const hand = round(.09, .18, .12, .04, cream, sign * .245, .315, .005, 'messenger-hand');
      hand.rotation.z = sign * .14;
      const wing = new THREE.Shape();
      wing.moveTo(0, 0); wing.lineTo(.105, .035); wing.lineTo(.275, .275);
      wing.quadraticCurveTo(.295, .125, .20, .055); wing.lineTo(.235, .065);
      wing.quadraticCurveTo(.23, -.015, .13, -.055); wing.lineTo(.15, -.055);
      wing.quadraticCurveTo(.11, -.12, 0, -.085); wing.closePath();
      const mesh = add(new THREE.ExtrudeGeometry(wing, { depth: .035, bevelEnabled: true, bevelSegments: 1, steps: 1, bevelSize: .008, bevelThickness: .006, curveSegments: 5 }), feather, sign * .222, .715, -.015, 'messenger-wing');
      mesh.scale.x = sign;
    }
    const strap = round(.035, .26, .018, .006, brass, .105, .295, .166, 'messenger-strap');
    strap.rotation.z = -.35;
    round(.115, .125, .09, .025, brass, .21, .215, .13, 'messenger-satchel');
    eye(light, -.075, .635, .278, 'L', .022, .04); eye(light, .075, .635, .278, 'R', .022, .04);
    badge(.285, .19, -.22, .15);
    model.userData.silhouette = 'winged-messenger';
  }

  model.userData.ownedResourceCounts = { geometries: geometries.size, materials: materials.size };
  let disposed = false;
  model.userData.disposePlatformModel = () => {
    if (disposed) return;
    disposed = true;
    for (const geometry of geometries) geometry.dispose();
    for (const paint of materials) paint.dispose();
    geometries.clear(); materials.clear();
  };
  return model;
}
