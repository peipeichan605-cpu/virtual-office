import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {captureMeshBounds, forEachCaptureBox} from './public/office/scene/mesh-bounds.js';

const require = createRequire(process.env.OFFICE_THREE_TEST_ROOT ? resolve(process.env.OFFICE_THREE_TEST_ROOT, 'package.json') : import.meta.url);
let THREE;
try { THREE = require('three'); } catch {}
const options = {skip: !THREE && 'Set OFFICE_THREE_TEST_ROOT to an existing Three.js test installation'};

function projectedExtent(objects, camera) {
  const extent = [Infinity, -Infinity, Infinity, -Infinity];
  const corner = new THREE.Vector3();
  for (const object of objects) forEachCaptureBox(object, (box, world) => {
    for (let index = 0; index < 8; index++) {
      corner.set(index & 1 ? box.max.x : box.min.x, index & 2 ? box.max.y : box.min.y, index & 4 ? box.max.z : box.min.z)
        .applyMatrix4(world).applyMatrix4(camera.matrixWorldInverse);
      extent[0] = Math.min(extent[0], corner.x); extent[1] = Math.max(extent[1], corner.x);
      extent[2] = Math.min(extent[2], corner.y); extent[3] = Math.max(extent[3], corner.y);
    }
  });
  return extent;
}

test('per-instance shot framing equals authored primitives under rotated, scaled and moving floors', options, () => {
  const geometry = new THREE.BoxGeometry(2, 1, 3), material = new THREE.MeshBasicMaterial();
  const floor = new THREE.Group(), instanced = new THREE.InstancedMesh(geometry, material, 3);
  floor.position.set(12, 8, -4); floor.rotation.set(.12, .6, -.1); floor.scale.set(1.3, .8, 1.1);
  floor.add(instanced);
  const authored = [];
  const transforms = [
    [[-7, 1, 2], [.3, .8, -.2], [2, 1, .5]],
    [[9, 3, -5], [-.1, .4, .3], [.5, 3, 2]],
    [[3, -1, 11], [.4, -.6, .2], [1.4, .3, .7]],
  ];
  transforms.forEach(([position, rotation, scale], index) => {
    const primitive = new THREE.Mesh(geometry, material);
    primitive.position.fromArray(position); primitive.rotation.set(...rotation); primitive.scale.fromArray(scale);
    primitive.updateMatrix(); instanced.setMatrixAt(index, primitive.matrix);
    // InstancedMesh stores matrices as float32. Compare precisely the same
    // authored transform so the test measures framing, not storage rounding.
    instanced.getMatrixAt(index, primitive.matrix); primitive.matrixAutoUpdate = false;
    floor.add(primitive); authored.push(primitive);
  });
  const camera = new THREE.OrthographicCamera(-30, 30, 30, -30, .1, 1000);
  camera.position.set(40, 60, 70); camera.lookAt(0, 0, 0); camera.updateMatrixWorld(true);
  for (const height of [8, 26, -3]) {
    floor.position.y = height; floor.updateMatrixWorld(true);
    const expected = projectedExtent(authored, camera), actual = projectedExtent([instanced], camera);
    actual.forEach((value, index) => assert.ok(Math.abs(value - expected[index]) < 1e-10, `extent ${index} changed at floor y=${height}`));
  }
  geometry.dispose(); material.dispose(); instanced.dispose();
});

test('shot framing skips hidden instance metadata and honors the current instance count', options, () => {
  const geometry = new THREE.BoxGeometry(), material = new THREE.MeshBasicMaterial();
  const mesh = new THREE.InstancedMesh(geometry, material, 3);
  [0, 100, 200].forEach((x, index) => mesh.setMatrixAt(index, new THREE.Matrix4().makeTranslation(x, 0, 0)));
  mesh.userData.officeInstanceMetadata = [{visible: true}, {visible: false}, {}];
  const visited = [];
  assert.equal(forEachCaptureBox(mesh, (box, world, index) => visited.push([index, world.elements[12]])), 2);
  assert.deepEqual(visited, [[0, 0], [2, 200]]);
  mesh.count = 2; visited.length = 0;
  assert.equal(forEachCaptureBox(mesh, (box, world, index) => visited.push(index)), 1);
  assert.deepEqual(visited, [0]);
  mesh.userData.officeInstanceMetadata[0].visible = false;
  assert.equal(forEachCaptureBox(mesh, () => assert.fail('hidden furniture must not frame the shot')), 0);
  geometry.dispose(); material.dispose(); mesh.dispose();
});

test('ordinary shot meshes keep their authored box and world transform without mutating borrowed bounds', options, () => {
  const geometry = new THREE.BoxGeometry(2, 4, 6), material = new THREE.MeshBasicMaterial();
  const plain = new THREE.Mesh(geometry, material); plain.position.set(40, 20, 10); plain.updateMatrixWorld(true);
  geometry.computeBoundingBox(); const original = geometry.boundingBox.clone();
  assert.equal(forEachCaptureBox(plain, (box, world) => {
    assert.equal(box, geometry.boundingBox); assert.equal(world, plain.matrixWorld);
  }), 1);
  assert.ok(geometry.boundingBox.equals(original));
  plain.visible = false;
  assert.equal(forEachCaptureBox(plain, () => assert.fail('hidden mesh')), 0);
  assert.equal(forEachCaptureBox(new THREE.Group(), () => assert.fail('non-mesh')), 0);
  geometry.dispose(); material.dispose();
});

test('share framing contains all rotated and scaled instances under a moving floor', options, () => {
  const geometry = new THREE.BoxGeometry(2, 1, 3), material = new THREE.MeshBasicMaterial();
  const mesh = new THREE.InstancedMesh(geometry, material, 2), floor = new THREE.Group();
  floor.position.set(12, 8, -4); floor.rotation.y = .6; floor.add(mesh);
  const matrices = [
    new THREE.Matrix4().compose(new THREE.Vector3(-7, 1, 2), new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), .8), new THREE.Vector3(2, 1, .5)),
    new THREE.Matrix4().compose(new THREE.Vector3(9, 3, -5), new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), .3), new THREE.Vector3(.5, 3, 2)),
  ];
  matrices.forEach((matrix, index) => mesh.setMatrixAt(index, matrix));
  floor.updateMatrixWorld(true);
  const world = captureMeshBounds(mesh).clone().applyMatrix4(mesh.matrixWorld).expandByScalar(1e-5);
  for (const matrix of matrices) for (let corner = 0; corner < 8; corner++) {
    const point = new THREE.Vector3(corner & 1 ? 1 : -1, corner & 2 ? .5 : -.5, corner & 4 ? 1.5 : -1.5)
      .applyMatrix4(matrix).applyMatrix4(mesh.matrixWorld);
    assert.ok(world.containsPoint(point), 'the exported shot must not crop an instance');
  }
  geometry.dispose(); material.dispose(); mesh.dispose();
});

test('a later instance move is included even when old aggregate bounds exist', options, () => {
  const geometry = new THREE.BoxGeometry(), material = new THREE.MeshBasicMaterial();
  const mesh = new THREE.InstancedMesh(geometry, material, 1);
  mesh.setMatrixAt(0, new THREE.Matrix4().makeTranslation(1, 0, 0));
  assert.ok(captureMeshBounds(mesh).max.x < 2);
  mesh.setMatrixAt(0, new THREE.Matrix4().makeTranslation(100, 0, 0));
  assert.ok(captureMeshBounds(mesh).min.x > 99);
  geometry.dispose(); material.dispose(); mesh.dispose();
});

test('instance framing never bakes transforms into shared geometry bounds', options, () => {
  const geometry = new THREE.BoxGeometry(2, 4, 6), material = new THREE.MeshBasicMaterial();
  geometry.computeBoundingBox(); const original = geometry.boundingBox.clone();
  const mesh = new THREE.InstancedMesh(geometry, material, 1);
  mesh.setMatrixAt(0, new THREE.Matrix4().makeTranslation(40, 20, 10));
  captureMeshBounds(mesh);
  assert.ok(geometry.boundingBox.equals(original));
  const plain = new THREE.Mesh(geometry, material);
  assert.ok(captureMeshBounds(plain).equals(original));
  geometry.dispose(); material.dispose(); mesh.dispose();
});

test('empty instance sets and non-mesh labels do not poison shot dimensions', options, () => {
  const geometry = new THREE.BoxGeometry(), material = new THREE.MeshBasicMaterial();
  const mesh = new THREE.InstancedMesh(geometry, material, 1); mesh.count = 0;
  assert.equal(captureMeshBounds(mesh), null);
  assert.equal(captureMeshBounds(new THREE.Group()), null);
  geometry.dispose(); material.dispose(); mesh.dispose();
});
