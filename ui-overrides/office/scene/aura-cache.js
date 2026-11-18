// The aura mask replaces every visible material with solid white. Its static
// pixels depend on shape, camera and visibility, not lighting or texture colour.
class Signature {
  constructor() { this.values = []; }
  begin() { this.at = 0; this.changed = false; }
  add(value) {
    if (!Object.is(this.values[this.at], value)) { this.values[this.at] = value; this.changed = true; }
    this.at++;
  }
  array(values) { this.add(values?.length); if (values) for (const value of values) this.add(value); }
  end() {
    if (this.values.length !== this.at) { this.values.length = this.at; this.changed = true; }
    return this.changed;
  }
}

const renderable = object => object.isMesh || object.isLine || object.isPoints || object.isSprite;
const dynamic = object => object.userData?.auraDynamic || object.isSkinnedMesh || object.isBatchedMesh ||
  object.morphTargetInfluences?.length || object.morphTexture ||
  (Object.prototype.hasOwnProperty.call(object, 'onBeforeRender') && object.userData?.auraStatic !== true);

function attribute(signature, value) {
  signature.add(value);
  signature.add(value?.version);
  signature.add(value?.count);
  signature.add(value?.data);
  signature.add(value?.data?.version); // InterleavedBufferAttribute updates its backing buffer.
}

function shape(signature, object) {
  signature.begin();
  signature.add(object.parent);
  signature.add(object.layers?.mask);
  signature.add(object.frustumCulled);
  signature.add(object.renderOrder);
  signature.array(object.matrixWorld?.elements);
  signature.add(object.center?.x); signature.add(object.center?.y);
  const geometry = object.geometry;
  signature.add(geometry);
  signature.add(geometry?.drawRange?.start); signature.add(geometry?.drawRange?.count);
  attribute(signature, geometry?.index);
  for (const name in geometry?.attributes || {}) { signature.add(name); attribute(signature, geometry.attributes[name]); }
  signature.add(geometry?.groups?.length);
  for (const group of geometry?.groups || []) { signature.add(group.start); signature.add(group.count); signature.add(group.materialIndex); }
  const sphere = geometry?.boundingSphere;
  signature.add(sphere?.center.x); signature.add(sphere?.center.y); signature.add(sphere?.center.z); signature.add(sphere?.radius);
  const objectSphere = object.boundingSphere; // InstancedMesh owns its aggregate culling sphere.
  signature.add(objectSphere?.center.x); signature.add(objectSphere?.center.y); signature.add(objectSphere?.center.z); signature.add(objectSphere?.radius);
  if (Array.isArray(object.material)) {
    signature.add(object.material.length);
    for (const material of object.material) { signature.add(material); signature.add(material?.visible); }
  } else { signature.add(1); signature.add(object.material); signature.add(object.material?.visible); }
  signature.add(object.count);
  attribute(signature, object.instanceMatrix);
  attribute(signature, object.instanceColor);
  return signature.end();
}

export class AuraCache {
  constructor() {
    this.camera = new Signature();
    this.records = new Map();
    this.serial = 0;
    this.valid = false;
  }
  invalidate() { this.valid = false; }
  commit() { this.valid = true; }
  clear() { this.invalidate(); this.records.clear(); this.camera = new Signature(); }

  inspect(scene, camera) {
    try { return this.inspectScene(scene, camera); }
    catch (error) { this.invalidate(); throw error; }
  }

  inspectScene(scene, camera) {
    // StudioPass normally follows RenderPass. Also update here so a direct
    // capture/render after a transform change cannot reuse yesterday's pixels.
    if (scene.matrixWorldAutoUpdate !== false) scene.updateMatrixWorld?.();
    if (camera.matrixWorldAutoUpdate !== false) camera.updateWorldMatrix?.(true, false);
    const view = this.camera;
    view.begin(); view.add(camera); view.add(camera.layers?.mask);
    view.array(camera.projectionMatrix?.elements);
    view.array(camera.matrixWorld?.elements);
    view.array(camera.matrixWorldInverse?.elements);
    const viewChanged = view.end();
    let changed = !this.valid || viewChanged;
    const serial = ++this.serial, staticObjects = [], dynamicRoots = [], keepAncestors = new Set();
    const visit = (object, ancestors) => {
      if (object.visible === false) return;
      if (object !== scene && dynamic(object)) {
        dynamicRoots.push(object);
        for (const ancestor of ancestors) keepAncestors.add(ancestor);
        return;
      }
      const draws = renderable(object);
      if (draws) {
        staticObjects.push(object);
        let record = this.records.get(object);
        if (!record) { record = {signature:new Signature()}; this.records.set(object, record); changed = true; }
        record.seen = serial;
        if (shape(record.signature, object)) changed = true;
      }
      const next = draws && object.children?.length ? [...ancestors, object] : ancestors;
      for (const child of object.children || []) visit(child, next);
    };
    visit(scene, []);
    for (const [object, record] of this.records) if (record.seen !== serial) { this.records.delete(object); changed = true; }
    return {
      changed, dynamicRoots, staticObjects,
      // A mesh may itself be a parent of a dynamic subtree. Drawing that static
      // mesh again is harmless with the white union mask; hiding it is not.
      hideForDynamic: staticObjects.filter(object => !keepAncestors.has(object)),
    };
  }
}

export function withHidden(objects, action) {
  const hidden = [];
  try {
    for (const object of objects) if (object.visible !== false) { hidden.push(object); object.visible = false; }
    return action();
  } finally { for (const object of hidden) object.visible = true; }
}
