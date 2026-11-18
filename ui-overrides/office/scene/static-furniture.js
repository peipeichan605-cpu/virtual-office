// Batch only explicitly opted-in opaque, static furniture. Original nodes are
// retained as records so their ancestor transforms, visibility and seat metadata
// remain authoritative. Borrowed native geometries/textures stay owned by the
// floor; each batch owns only its instance buffers and one material snapshot.
const MATRIX_EPSILON = 1e-9;
const MATERIAL_FIELDS = ['opacity','transparent','visible','alphaTest','depthTest','depthWrite','side','blending','premultipliedAlpha',
  'roughness','metalness','emissiveIntensity','map','alphaMap','normalMap','roughnessMap','metalnessMap','aoMap','emissiveMap','lightMap'];

function materialState(material) {
  return [...MATERIAL_FIELDS.map(key => material[key]),material.color?.r,material.color?.g,material.color?.b,
    material.emissive?.r,material.emissive?.g,material.emissive?.b];
}

function sameMaterial(entry) {
  const material = entry.source.material, values = entry.materialValues;
  if (material !== entry.material || !material.visible) return false;
  for (let index = 0; index < MATERIAL_FIELDS.length; index++) if (material[MATERIAL_FIELDS[index]] !== values[index]) return false;
  const at = MATERIAL_FIELDS.length;
  return material.color?.r === values[at] && material.color?.g === values[at+1] && material.color?.b === values[at+2] &&
    material.emissive?.r === values[at+3] && material.emissive?.g === values[at+4] && material.emissive?.b === values[at+5];
}

function orthogonal(matrix) {
  const e = matrix.elements;
  const lengths = [Math.hypot(e[0], e[1], e[2]), Math.hypot(e[4], e[5], e[6]), Math.hypot(e[8], e[9], e[10])];
  if (lengths.some(n => n < 1e-12) || matrix.determinant() <= 0) return false;
  for (const [a, b] of [[0, 1], [0, 2], [1, 2]]) {
    const dot = e[a * 4] * e[b * 4] + e[a * 4 + 1] * e[b * 4 + 1] + e[a * 4 + 2] * e[b * 4 + 2];
    if (Math.abs(dot / (lengths[a] * lengths[b])) > 1e-7) return false;
  }
  return true;
}

function zoneOf(object, root) {
  for (let parent = object.parent; parent && parent !== root; parent = parent.parent) {
    if (parent.userData.officeRoomId) return 'room:' + parent.userData.officeRoomId;
    if (parent.userData.zone) return 'zone:' + parent.userData.zone;
  }
  return 'floor';
}

function seatMetadata(object) {
  const ids = furnitureSeatIds(object) || [];
  let desk;
  for (let parent = object; parent; parent = parent.parent) {
    if (parent.userData.officeDeskId) { desk = parent.userData.officeDeskId; break; }
    if (parent.userData.officeSeatIds) break;
  }
  return {
    officeSeatIds: [...ids],
    ...(desk ? { officeDeskId: desk } : {}),
  };
}

export function furnitureSeatIds(object, instanceId) {
  const entries = object?.userData?.officeInstanceMetadata;
  if (entries) {
    if (!Number.isInteger(instanceId) || instanceId < 0 || instanceId >= entries.length) return null;
    const entry = entries[instanceId];
    if (!entry || entry.visible === false) return [];
    return entry.officeSeatIds || (entry.officeDeskId ? [entry.officeDeskId] : []);
  }
  for (let parent = object; parent; parent = parent.parent) {
    if (parent.userData?.officeSeatIds) return parent.userData.officeSeatIds;
    if (parent.userData?.officeDeskId) return [parent.userData.officeDeskId];
  }
  return null;
}

export function batchStaticFurniture(THREE, root, { minCount = 2 } = {}) {
  const sourceToBatch = new Map(), batches = [], records = [];
  const inverse = new THREE.Matrix4(), world = new THREE.Matrix4(), local = new THREE.Matrix4();
  const zero = new THREE.Matrix4().makeScale(0, 0, 0);
  const groups = new Map();
  root.updateMatrixWorld(true);
  inverse.copy(root.matrixWorld).invert();
  root.traverse(object => {
    const material = object.material;
    if (!object.isMesh || object.isInstancedMesh || object.isSkinnedMesh || object.children.length || !object.userData.officeStaticBatchKey ||
        !material || Array.isArray(material) || material.transparent || material.opacity !== 1 || !material.visible ||
        material.onBeforeCompile !== THREE.Material.prototype.onBeforeCompile ||
        material.customProgramCacheKey !== THREE.Material.prototype.customProgramCacheKey ||
        object.morphTargetInfluences?.length || object.customDepthMaterial || object.customDistanceMaterial ||
        object.onBeforeRender !== THREE.Object3D.prototype.onBeforeRender || object.onAfterRender !== THREE.Object3D.prototype.onAfterRender) return;
    local.multiplyMatrices(inverse, object.matrixWorld);
    // Mirrored/sheared instances need different winding/normal handling. Keep
    // those authored meshes intact rather than changing their lighting.
    if (!orthogonal(local)) return;
    const zone = zoneOf(object, root);
    const key = [object.geometry.uuid, object.userData.officeStaticBatchKey, zone, object.castShadow, object.receiveShadow,
      object.layers.mask, object.renderOrder, object.frustumCulled].join('|');
    const entry = { source: object, parent: object.parent, index: object.parent.children.indexOf(object), metadata: seatMetadata(object),
      geometry:object.geometry, material, materialValues:materialState(material), matrix: local.clone(), enabled: true };
    if (!groups.has(key)) groups.set(key, { zone, entries: [] });
    groups.get(key).entries.push(entry);
  });

  for (const { zone, entries } of groups.values()) {
    if (entries.length < minCount) continue;
    const source = entries[0].source;
    const batch = new THREE.InstancedMesh(source.geometry, source.material.clone(), entries.length);
    batch.name = 'static-furniture-batch';
    batch.castShadow = source.castShadow; batch.receiveShadow = source.receiveShadow;
    batch.layers.mask = source.layers.mask; batch.renderOrder = source.renderOrder;
    batch.frustumCulled = source.frustumCulled;
    batch.userData.staticFurnitureBatch = true;
    batch.userData.officeBatchZone = zone;
    batch.userData.officeInstanceMetadata = entries.map(entry => entry.metadata);
    // Scene-level lighting scans must patch detached source materials too, so
    // an authored fallback has the same shader on its very first frame. Keep
    // resource references out of JSON-serialized Object3D.userData.
    batch.officeSourceMaterials = [...new Set(entries.map(entry => entry.material))];
    const bounds = new THREE.Box3(), boundsMatrix = new THREE.Matrix4();
    batch.computeBoundingBox = function() {
      if (!this.geometry.boundingBox) this.geometry.computeBoundingBox();
      this.boundingBox ||= new THREE.Box3(); this.boundingBox.makeEmpty();
      for (const entry of entries) if (entry.enabled) {
        this.getMatrixAt(entry.instanceId, boundsMatrix);
        this.boundingBox.union(bounds.copy(this.geometry.boundingBox).applyMatrix4(boundsMatrix));
      }
    };
    batch.computeBoundingSphere = function() {
      this.computeBoundingBox(); this.boundingSphere ||= new THREE.Sphere(); this.boundingBox.getBoundingSphere(this.boundingSphere);
    };
    const raycast = batch.raycast;
    batch.raycast = function(raycaster, intersects) {
      const hits = []; raycast.call(this, raycaster, hits);
      for (const hit of hits) if (entries[hit.instanceId]?.enabled) intersects.push(hit);
    };
    const seats = [...new Set(entries.flatMap(entry => entry.metadata.officeSeatIds))];
    if (seats.length) batch.userData.officeSeatIds = seats;
    entries.forEach((entry, index) => {
      entry.batch = batch; entry.instanceId = index;
      sourceToBatch.set(entry.source, batch);
      entry.source.removeFromParent();
      batch.setMatrixAt(index, entry.matrix);
    });
    batch.instanceMatrix.needsUpdate = true;
    batch.computeBoundingBox(); batch.computeBoundingSphere();
    root.add(batch); batches.push(batch); records.push(...entries);
  }

  let disposed = false;
  const refresh = () => {
    if (disposed) return false;
    inverse.copy(root.matrixWorld).invert();
    const changed = new Set(), anyVisible = new Map(batches.map(batch => [batch, false]));
    for (const entry of records) {
      const source = entry.source;
      if (source.matrixAutoUpdate) source.updateMatrix();
      world.multiplyMatrices(entry.parent.matrixWorld, source.matrix);
      local.multiplyMatrices(inverse, world);
      let visible = source.visible;
      let parent = entry.parent;
      for (; parent && parent !== root; parent = parent.parent) visible &&= parent.visible;
      const valid = parent === root && source.geometry === entry.geometry && orthogonal(local) && sameMaterial(entry) && source.layers.mask === entry.batch.layers.mask &&
        source.renderOrder === entry.batch.renderOrder && source.castShadow === entry.batch.castShadow && source.receiveShadow === entry.batch.receiveShadow;
      // A later edit may introduce a mirror/shear. Render that authored mesh
      // normally until it is compatible again, rather than changing its normals.
      if (!valid && source.parent !== entry.parent) {
        entry.parent.add(source);
        // Root traversal has already passed this wrapper this frame. Restore
        // its current world transform now, before shadow/main rendering.
        source.matrixWorld.copy(world); source.matrixWorldNeedsUpdate = false;
      }
      if (valid && source.parent === entry.parent) source.removeFromParent();
      visible &&= valid;
      entry.metadata.visible = visible;
      anyVisible.set(entry.batch, anyVisible.get(entry.batch) || visible);
      const next = visible ? local : zero;
      if (entry.enabled !== visible || entry.matrix.elements.some((n, index) => Math.abs(n - next.elements[index]) > MATRIX_EPSILON)) {
        entry.matrix.copy(next); entry.enabled = visible;
        entry.batch.setMatrixAt(entry.instanceId, entry.matrix); changed.add(entry.batch);
      }
    }
    for (const batch of batches) {
      batch.visible = anyVisible.get(batch);
      if (changed.has(batch)) { batch.instanceMatrix.needsUpdate = true; batch.computeBoundingBox(); batch.computeBoundingSphere(); }
    }
    return changed.size > 0;
  };

  // Scene.updateMatrixWorld runs before frustum culling and shadow rendering.
  // Refreshing here keeps moved/hidden source wrappers correct even if a batch
  // used to be outside the camera, without disabling normal culling.
  const previousUpdate = root.updateMatrixWorld;
  const update = function(force) { previousUpdate.call(this, force); refresh(); };
  root.updateMatrixWorld = update;
  refresh();
  return {
    batches, sourceToBatch, refresh,
    get sourceCount() { return records.length; },
    dispose({ restore = true } = {}) {
      if (disposed) return;
      disposed = true;
      if (root.updateMatrixWorld === update) root.updateMatrixWorld = previousUpdate;
      for (const batch of batches) { batch.removeFromParent(); batch.dispose(); batch.material.dispose(); batch.officeSourceMaterials.length = 0; }
      if (restore) {
        for (const entry of [...records].sort((a, b) => a.index - b.index)) {
          entry.parent.add(entry.source);
          const children = entry.parent.children, at = children.indexOf(entry.source);
          children.splice(at, 1); children.splice(Math.min(entry.index, children.length), 0, entry.source);
        }
      }
      sourceToBatch.clear(); records.length = 0; batches.length = 0;
    },
  };
}
