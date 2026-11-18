// Render at a bounded rate while animation clocks keep real elapsed time.
// A vsync grid avoids slowing 30 fps down to 24/28 fps on faster displays.
const EPSILON_MS = 0.001; // tolerate floating-point error, not an extra vsync

export function createFramePacer(fps = 30) {
  if (!Number.isFinite(fps) || fps <= 0) throw new RangeError('fps must be positive');
  let interval = 1000 / fps;
  let nextDraw = null;

  const shouldDraw = function(timestamp, hidden = false) {
    if (hidden) { nextDraw = null; return false; }
    if (nextDraw === null || timestamp + EPSILON_MS >= nextDraw + interval) {
      // First frame, resumed tab or a hitch: start here, without replaying frames.
      nextDraw = timestamp + interval;
      return true;
    }
    if (timestamp + EPSILON_MS < nextDraw) return false;
    nextDraw += interval;
    return true;
  };
  shouldDraw.setFps = value => {
    if (!Number.isFinite(value) || value <= 0) throw new RangeError('fps must be positive');
    if (value !== fps) { fps = value; interval = 1000 / fps; nextDraw = null; }
    return fps;
  };
  shouldDraw.getFps = () => fps;
  return shouldDraw;
}
