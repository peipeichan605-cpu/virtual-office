// Visit authored geometry boxes with their world transforms. Projecting each
// instance separately preserves the original silhouette framing; projecting an
// aggregate axis-aligned instance box includes empty space between furniture.
// The callback must consume the matrix immediately (the instance scratch matrix
// is reused). Callers retain responsibility for ancestor visibility.
export function forEachCaptureBox(object, callback) {
  if (!object?.isMesh || !object.geometry || object.visible === false) return 0;
  const geometry = object.geometry;
  if (!geometry.boundingBox) geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  if (!box || box.isEmpty()) return 0;
  if (!object.isInstancedMesh) {
    callback(box, object.matrixWorld);
    return 1;
  }

  const instance = object.matrixWorld.clone(), world = object.matrixWorld.clone();
  const metadata = object.userData?.officeInstanceMetadata;
  const count = Math.min(object.count, object.instanceMatrix.count);
  let visited = 0;
  for (let index = 0; index < count; index++) {
    if (metadata?.[index]?.visible === false) continue;
    object.getMatrixAt(index, instance);
    world.multiplyMatrices(object.matrixWorld, instance);
    callback(box, world, index);
    visited++;
  }
  return visited;
}

// Aggregate local bounds remain available for containment/culling callers.
// Share-image framing uses forEachCaptureBox to avoid padding its silhouette.
export function captureMeshBounds(object) {
  if (!object?.isMesh || !object.geometry) return null;
  let bounds;
  if (object.isInstancedMesh) {
    if (object.count < 1) return null;
    object.computeBoundingBox();
    bounds = object.boundingBox;
  } else {
    if (!object.geometry.boundingBox) object.geometry.computeBoundingBox();
    bounds = object.geometry.boundingBox;
  }
  return bounds && !bounds.isEmpty() ? bounds : null;
}
