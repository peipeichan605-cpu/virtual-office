// Screen-space head labels stay small; only an explicitly opened panel gets space.
export class SingleHeadPanel {
  current = null;
  claim(owner) {
    if (this.current === owner) return;
    const previous = this.current;
    this.current = owner;
    previous?.closePanels();
  }
  release(owner) { if (this.current === owner) this.current = null; }
}

const clamp = (n, min, max) => Math.min(Math.max(n, min), Math.max(min, max));

export function headPanelPlacement(anchor, size, viewport) {
  const padding = 12, top = viewport.top ?? Math.min(76, viewport.height * .12);
  const minX = viewport.left ?? padding, maxX = viewport.right ?? viewport.width-padding;
  const bottom = viewport.bottom ?? viewport.height-padding;
  const width = Math.min(size.width, Math.max(80, maxX-minX));
  const height = Math.min(size.height, Math.max(40, bottom-top));
  const above = anchor.y - top, below = bottom-anchor.y;
  const useBelow = above < height + 10 && below > above;
  const left = clamp(anchor.x - width / 2, minX, maxX-width);
  const y = clamp(useBelow ? anchor.y + 9 : anchor.y - height - 9, top, bottom-height);
  return { left, top: y, right: left + width, bottom: y + height, width, height,
    below: useBelow, dx: left + width / 2 - anchor.x,
    dy: (useBelow ? y : y + height) - anchor.y };
}

const intersects = (a,b,gap=7) => a.left < b.right + gap && a.right + gap > b.left &&
  a.top < b.bottom + gap && a.bottom + gap > b.top;

export function layoutHeadLabels(items, viewport) {
  const placed = new Map(), occupied = [...(viewport.obstacles || [])];
  const score = item => (item.expanded ? 10000 : 0) + (item.focused ? 5000 : 0) +
    (item.selected ? 3000 : 0) + (item.hovered ? 2000 : 0) + (item.child ? 0 : 1000);
  const ordered = [...items].sort((a,b) => score(b)-score(a) || String(a.id).localeCompare(String(b.id)));
  for (const item of ordered) {
    const offscreen = item.x < -20 || item.x > viewport.width+20 || item.y < -20 || item.y > viewport.height+20;
    const rect = headPanelPlacement(item, item, item.expanded ? viewport : {...viewport,left:12,right:viewport.width-12});
    // Never steal the current reader or keyboard focus because a sibling arrived.
    const visible = item.expanded || item.focused || (!offscreen && !occupied.some(other => intersects(rect,other)));
    placed.set(item.id, {...rect, visible});
    if (visible) occupied.push(rect);
  }
  return placed;
}

export const headStatus = status => ({working:'工作中',idle:'休息',waiting_user:'等待',error:'錯誤',stuck:'待確認',done:'完成'}[status] || '待確認');

export function headOpenAction(bot, hasActor) {
  if(hasActor)return {mode:'head'};
  // Existing local history remains a safe fallback if no 3D standing slot fits.
  return typeof bot?.chat_url==='string'&&bot.chat_url.startsWith('/office/agent.html?')?
    {mode:'history',url:bot.chat_url}:{mode:'unavailable'};
}
