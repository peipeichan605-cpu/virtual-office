// The studio blur is the same finite Gaussian as its original fragment shader.
// Pair adjacent positive/negative taps so linear filtering performs their
// weighted interpolation. This changes sampling work, not radius or quality.
export const MAX_GAUSSIAN_RADIUS = 40;
export const MAX_GAUSSIAN_PAIRS = MAX_GAUSSIAN_RADIUS / 2;

export function createGaussianKernel(sigma) {
  if (typeof sigma !== 'number' || Number.isNaN(sigma)) throw new RangeError('sigma must be a number');
  // Uniforms and the shader's multiplication use single precision. Preserve
  // that support at n/3 boundaries rather than adding an extra CPU-only tap.
  sigma = Math.fround(sigma);
  if (sigma < Math.fround(0.01)) return {sigma, copy:true, radius:0, center:1, weights:[1], pairs:[], fetches:1};
  const radius = Math.min(Math.ceil(Math.fround(sigma * 3)), MAX_GAUSSIAN_RADIUS);
  const weights = Array.from({length:radius + 1}, (_, index) => Math.exp(-index * index / (2 * sigma * sigma)));
  const total = weights[0] + 2 * weights.slice(1).reduce((sum, value) => sum + value, 0);
  for (let index = 0; index < weights.length; index++) weights[index] /= total;
  const pairs = [];
  for (let index = 1; index <= radius; index += 2) {
    const first = weights[index], second = weights[index + 1] || 0;
    const weight = first + second;
    // Very narrow kernels can underflow both taps to zero. A zero-weight pair
    // contributes nothing; omitting it also avoids an undefined 0/0 offset.
    if (weight > 0) pairs.push({weight, offset:index + second / weight});
  }
  return {sigma, copy:false, radius, center:weights[0], weights, pairs, fetches:1 + pairs.length * 2};
}

export function gaussianPairingAligned(srcWidth, srcHeight, dstWidth, dstHeight, dx, dy) {
  return srcWidth === dstWidth && srcHeight === dstHeight && Number.isInteger(srcWidth) && Number.isInteger(srcHeight) && srcWidth > 0 && srcHeight > 0 &&
    ((Math.abs(dx) === 1 && dy === 0) || (dx === 0 && Math.abs(dy) === 1));
}

// Camera zoom changes sigma continuously. Keep only a small working set,
// rather than retaining every historical camera position for the page's life.
export class GaussianKernelCache {
  constructor(limit = 12) {
    if (!Number.isInteger(limit) || limit < 1) throw new RangeError('kernel cache limit must be positive');
    this.limit = limit;
    this.entries = new Map();
  }
  get(sigma) {
    sigma = Math.fround(sigma);
    let kernel = this.entries.get(sigma);
    if (kernel) this.entries.delete(sigma);
    else kernel = createGaussianKernel(sigma);
    this.entries.set(sigma, kernel);
    if (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value);
    return kernel;
  }
  clear() { this.entries.clear(); }
}
