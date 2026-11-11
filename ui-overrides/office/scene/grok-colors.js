// Recolor an already-cloned authored Grok model. Geometry, textures and all
// non-body materials stay shared with the unchanged cached source GLB.
const ownedModels = new WeakMap();
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

function isBodyMesh(mesh, model) {
  if (!mesh.isMesh) return false;
  let body = false;
  for (let node = mesh; node && node !== model.parent; node = node.parent) {
    const name = String(node.name || '').toLowerCase();
    // Explicit features keep their original paint even in a body group.
    if (/^(eye(?:_[lr])?|eyes|extra|stem)$/.test(name)) return false;
    if (name === 'body') body = true;
  }
  return body;
}

export function colorOriginalGrok(model, color) {
  if (!model?.traverse || typeof color !== 'string' || !HEX_COLOR.test(color)) return model;
  const normalized = color.toUpperCase();
  let state = ownedModels.get(model);
  const recolor = original => {
    if (!original?.clone || !original.color?.set) return original;
    if (!state) {
      state = { copies: new Map(), owned: new Set(), disposed: false };
      ownedModels.set(model, state);
    }
    let copy = state.owned.has(original) ? original : state.copies.get(original);
    if (!copy) {
      copy = original.clone();
      state.copies.set(original, copy);
      state.owned.add(copy);
    }
    copy.color.set(normalized);
    return copy;
  };

  model.traverse(mesh => {
    if (!isBodyMesh(mesh, model)) return;
    if (Array.isArray(mesh.material)) {
      const copies = mesh.material.map(recolor);
      if (copies.some((copy, index) => copy !== mesh.material[index])) mesh.material = copies;
    } else mesh.material = recolor(mesh.material);
  });
  if (!state?.owned.size) return model;

  model.userData ||= {};
  model.userData.grokBodyColor = normalized;
  const resources = state;
  model.userData.disposePlatformModel = () => {
    if (resources.disposed) return;
    resources.disposed = true;
    for (const material of resources.owned) material.dispose();
    resources.owned.clear();
    resources.copies.clear();
    if (ownedModels.get(model) === resources) ownedModels.delete(model);
  };
  return model;
}
